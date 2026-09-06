#!/usr/bin/env python3
"""hub — the home page of a box of seats.

One small Flask service behind Caddy that does the few things static pages cannot:

  /                 home: search, tools, seat cards (live herdr agent status), recent, all pages
  /<seat>/          seat page: SEAT.md, beacon, pages, inbox, peek at the pane
  /send             web inbox: prompt + attachments -> ~/inbox/<seat>/<msg>/ + herdr nudge
  /inbox/...        browse every seat's inbox (notes rendered, files served)
  /board            the board: items with their notes attached *to the row* (board.md in git)
  /search?q=        ripgrep over /srv/www + ~/inbox
  /api/...          send, sessions, capture, response, board, deploy (gitea webhook), status

Everything the hub writes is a file: inbox notes on disk, board.md committed to the site repo.
Static pages live in /srv/www (a checkout of the site repo) and are served by Caddy; the hub
serves them too, so it works standalone on :8090.

No auth: LAN + personal tailnet only. Never shell=True; seat names are validated
against the live herdr workspace list, never trusted from the client.
"""
import hashlib
import hmac
import html as htmlmod
import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import markdown as mdlib
from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, send_file, url_for)

log = logging.getLogger("hub")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512 MB uploads

HOME = Path.home()
WWW = Path(os.environ.get("HUB_WWW", "/srv/www"))
INBOX = Path(os.environ.get("HUB_INBOX", str(HOME / "inbox")))
GITEA_URL = os.environ.get("HUB_GITEA_URL", "http://localhost:3000")
WWW_REPO_URL = os.environ.get("HUB_WWW_REPO_URL", f"{GITEA_URL}/seats/www")
DEPLOY_SECRET_FILE = HOME / ".config" / "hub" / "hub-deploy-secret"
PULL_INTERVAL = int(os.environ.get("HUB_PULL_INTERVAL", "600"))
OWNER = os.environ.get("HUB_OWNER", "owner")  # how the person sending from the hub is named in notes and nudges
BOARD = WWW / "board.md"
PASTE = Path(os.environ.get("HUB_PASTE", str(HOME / "paste")))
WIKI = WWW / "wiki"

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\ \(\)\[\],@]+")
MSG_RE = re.compile(r"^\d{8}-\d{6}_[A-Za-z0-9_-]+$")
RESERVED = {"send", "inbox", "board", "search", "api", "static", "healthz", "paste", "wiki"}
HERDR_TIMEOUT = 8
GIT_LOCK = threading.Lock()

MD_EXT = ["tables", "fenced_code", "sane_lists", "toc"]


def render_md(text):
    return mdlib.markdown(text or "", extensions=MD_EXT)


def fmt_ts(epoch):
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def ago(epoch):
    try:
        d = int(time.time() - int(epoch))
    except Exception:
        return ""
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


app.jinja_env.filters["fmt_ts"] = fmt_ts
app.jinja_env.filters["ago"] = ago


# ----------------------------------------------------------------------------- herdr
# Seats live in herdr (since 2026-09-05): one workspace per seat, label = seat name,
# agent name = lowercase seat name. The CLI talks to ~/.config/herdr/herdr.sock as $HOME.

HERDR = str(HOME / ".local" / "bin" / "herdr")


def herdr_run(*args, check=False):
    """Run a herdr CLI command; JSON on stdout for most commands, errors as JSON on stderr."""
    try:
        r = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=HERDR_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        if check:
            raise RuntimeError(str(e)) from e
        return None
    if r.returncode != 0:
        if check:
            try:
                msg = json.loads(r.stderr)["error"]["message"]
            except Exception:  # noqa: BLE001
                msg = (r.stderr or r.stdout).strip() or f"herdr exit {r.returncode}"
            raise RuntimeError(msg)
        return None
    return r


def herdr_json(*args):
    r = herdr_run(*args)
    if r is None:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _tilde(p):
    """/home/<user>/X -> ~/X (what people type)."""
    if not p:
        return ""
    home = str(HOME)
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def herdr_seats():
    """Live herdr structure keyed by workspace label (= seat / work-group name):
       {label: {name, workspace_id, focused, status, tabs: [...],
                agent, kind, pane_id, session_id, cwd}}          # top level = the first agent (compat)
       tabs: [{tab_id, number, name, focused, status, agent, kind, pane_id, session_id, cwd}]
       — one entry per herdr tab, in tab order. status is herdr's agent lifecycle
       (idle | working | blocked | done | unknown); a tab whose pane hosts no agent has agent None."""
    ws = herdr_json("workspace", "list")
    if not ws:
        return {}
    tabs = (herdr_json("tab", "list") or {}).get("result", {}).get("tabs", [])
    agents = (herdr_json("agent", "list") or {}).get("result", {}).get("agents", [])
    panes = (herdr_json("pane", "list") or {}).get("result", {}).get("panes", [])
    by_tab_agent = {a["tab_id"]: a for a in agents}
    by_tab_pane = {}
    for p in panes:                       # first pane of each tab (the one ttyd attaches to)
        by_tab_pane.setdefault(p["tab_id"], p)
    out = {}
    for w in ws.get("result", {}).get("workspaces", []):
        rows = []
        for t in sorted((t for t in tabs if t["workspace_id"] == w["workspace_id"]), key=lambda t: t["number"]):
            a = by_tab_agent.get(t["tab_id"])
            p = by_tab_pane.get(t["tab_id"], {})
            sess = (a or {}).get("agent_session") or {}
            rows.append({
                "tab_id": t["tab_id"], "number": t["number"], "name": t.get("label") or str(t["number"]),
                "focused": bool(t.get("focused")), "status": t.get("agent_status") or "unknown",
                "agent": a.get("name") if a else None, "kind": a["agent"] if a else None,
                "pane_id": (a or p).get("pane_id"), "session_id": sess.get("value"),
                "cwd": (a or p).get("cwd") or "", "title": (a or p).get("terminal_title_stripped") or "",
            })
        first = next((r for r in rows if r["agent"]), rows[0] if rows else {})
        out[w["label"]] = {
            "name": w["label"], "workspace_id": w["workspace_id"],
            "focused": bool(w.get("focused")), "status": w.get("agent_status") or "unknown",
            "tabs": rows,
            "agent": first.get("agent"), "kind": first.get("kind"),
            "pane_id": first.get("pane_id"), "session_id": first.get("session_id"), "cwd": first.get("cwd", ""),
        }
    return out


def herdr_tab(live_seat, tab):
    """The tab row named `tab` in a live seat (label, case-insensitive; a bare number matches
       herdr's tab number). None if the seat has no such tab."""
    if not live_seat or not tab:
        return None
    for r in live_seat["tabs"]:
        if r["name"].lower() == tab.lower() or (tab.isdigit() and r["number"] == int(tab)):
            return r
    return None


def herdr_seat_pane(label, tab=None, live=None):
    """The pane to read/poke: the named tab's pane; else the seat's agent pane; else the first pane."""
    live = live if live is not None else herdr_seats()
    s = live.get(label)
    if not s:
        return None
    if tab:
        r = herdr_tab(s, tab)
        return r["pane_id"] if r else None
    if s["pane_id"]:
        return s["pane_id"]
    pl = herdr_json("pane", "list", "--workspace", s["workspace_id"])
    panes = (pl or {}).get("result", {}).get("panes", [])
    return panes[0]["pane_id"] if panes else None


def herdr_capture(label, lines=60, tab=None):
    pane = herdr_seat_pane(label, tab)
    if not pane:
        raise RuntimeError(f"no herdr pane for '{label}'" + (f" tab '{tab}'" if tab else ""))
    r = herdr_run("pane", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines), check=True)
    return r.stdout


# ----------------------------------------------------------------------------- www: seats & pages

def seat_dirs():
    if not WWW.is_dir():
        return []
    return sorted(d for d in WWW.iterdir()
                  if d.is_dir() and not d.name.startswith((".", "_")) and d.name not in RESERVED)


def load_seat(d):
    info = {"name": d.name, "title": "", "status": "", "kind": "", "updated": "",
            "remote_control": "", "has_record": False, "has_charter": (d / "SEAT.md").exists()}
    sj = d / "seat.json"
    if sj.exists():
        info["has_record"] = True
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                info.update({k: v for k, v in data.items() if k != "name"})
        except Exception as e:  # keep the card, show the problem
            info["error"] = f"seat.json: {e}"
    return info


def seats():
    return {d.name: load_seat(d) for d in seat_dirs()}


def seat_rows(info, live):
    """One row per tab of a work group. herdr is the source of live structure (tabs, agents,
       status, cwd); `tabs` in seat.json only adds standing metadata (cwd, kind/engine, cmd,
       resume id), matched by tab name. A single-tab seat is one row; if herdr's tab has no
       label (bare number) it takes the seat's name. Record-only tabs (in seat.json, not
       running) come last, grey."""
    meta = {}
    for t in (info.get("tabs") or []):
        if isinstance(t, dict) and t.get("name"):
            meta[t["name"].lower()] = t
    rows, seen = [], set()
    live_tabs = (live or {}).get("tabs") or []
    for r in live_tabs:
        name = r["name"]
        tab = r["name"]
        if name.isdigit() and len(live_tabs) == 1:
            name, tab = info["name"], ""             # single unlabelled tab = the seat itself; links need no &tab=
        m = meta.get(name.lower()) or (meta.get(info["name"].lower()) if len(live_tabs) == 1 else None) or {}
        seen.add((m.get("name") or name).lower())
        rows.append({**r, "name": name, "live": True, "tab": tab,
                     "engine": m.get("kind") or (info.get("kind") if len(live_tabs) == 1 else "") or r["kind"] or "",
                     "cmd": m.get("cmd", ""), "resume": m.get("resume", ""),
                     "cwd_short": _tilde(r["cwd"]), "meta_cwd": m.get("cwd", "")})
    for key, m in meta.items():
        if key in seen:
            continue
        rows.append({"name": m["name"], "tab": m["name"], "live": False, "status": "off", "agent": m.get("agent", ""),
                     "kind": "", "engine": m.get("kind") or info.get("kind") or "", "cmd": m.get("cmd", ""),
                     "resume": m.get("resume", ""), "pane_id": None, "cwd": "", "cwd_short": m.get("cwd", ""),
                     "meta_cwd": m.get("cwd", ""), "title": "", "focused": False})
    if not rows:                                       # no workspace, no tabs record: the seat itself
        rows.append({"name": info["name"], "tab": info["name"], "live": False, "status": "off",
                     "agent": info["name"].lower(), "kind": "", "engine": info.get("kind") or "",
                     "cmd": info.get("cmd", ""), "resume": "", "pane_id": None, "cwd": "",
                     "cwd_short": f"~/{info['name']}", "meta_cwd": "", "title": "", "focused": False})
    return rows


TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def html_title(p):
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return ""
    m = TITLE_RE.search(head)
    return htmlmod.unescape(m.group(1)).strip() if m else ""


def pages():
    """Every .html under /srv/www -> {seat, url, title, description, date, tags, filed, mtime}."""
    res = []
    if not WWW.is_dir():
        return res
    for f in WWW.rglob("*.html"):
        rel = f.relative_to(WWW)
        if any(part.startswith((".", "_")) for part in rel.parts):
            continue
        seat = rel.parts[0] if len(rel.parts) > 1 else ""
        if f.name == "index.html":
            url = "/" + "/".join(rel.parts[:-1]) + ("/" if len(rel.parts) > 1 else "")
            manifest = f.parent / "page.json"
        else:
            url = "/" + rel.as_posix()
            manifest = f.with_suffix(".json")
        meta = {}
        if manifest.exists():
            try:
                meta = json.loads(manifest.read_text(encoding="utf-8")) or {}
            except Exception as e:
                meta = {"error": f"{manifest.name}: {e}"}
        st = f.stat()
        res.append({
            "seat": seat, "url": url, "path": rel.as_posix(),
            "title": meta.get("title") or html_title(f) or rel.as_posix(),
            "description": meta.get("description", ""),
            "date": meta.get("date") or datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
            "tags": meta.get("tags", []), "filed": bool(meta) and "error" not in meta,
            "error": meta.get("error", ""), "mtime": int(st.st_mtime),
        })
    res.sort(key=lambda p: (p["date"], p["mtime"]), reverse=True)
    return res


# ----------------------------------------------------------------------------- git (www checkout)

def git(*args, timeout=90):
    return subprocess.run(["git", "-C", str(WWW), *args], capture_output=True, text=True, timeout=timeout)


def git_commit_push(paths, message):
    """Commit the given paths in /srv/www and push. Returns {ok, detail}. Never raises."""
    detail = []
    with GIT_LOCK:
        try:
            git("add", "--", *paths)
            r = git("-c", "user.name=hub", "-c", "user.email=hub@localhost", "commit", "-q", "-m", message)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
                return {"ok": False, "detail": (r.stderr or r.stdout).strip()}
            detail.append("committed")
            if git("remote").stdout.strip():
                p = git("pull", "--rebase", "--autostash", "-q")
                if p.returncode != 0:
                    detail.append("pull: " + (p.stderr or p.stdout).strip())
                q = git("push", "-q")
                if q.returncode != 0:
                    return {"ok": False, "detail": "; ".join(detail) + "; push: " + (q.stderr or q.stdout).strip()}
                detail.append("pushed")
            return {"ok": True, "detail": "; ".join(detail)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}


def git_pull():
    with GIT_LOCK:
        before = git("rev-parse", "--short", "HEAD").stdout.strip()
        r = git("pull", "--ff-only", "-q")
        after = git("rev-parse", "--short", "HEAD").stdout.strip()
        if r.returncode != 0:
            return {"ok": False, "detail": (r.stderr or r.stdout).strip()}
        return {"ok": True, "detail": f"updated {before} -> {after}" if before != after else f"already at {after}"}


def git_recent(n=15):
    r = git("log", f"-{n}", "--format=%h%x09%ct%x09%an%x09%s")
    out = []
    if r.returncode != 0:
        return out
    for line in r.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            h, ct, an, s = parts
            out.append({"kind": "commit", "time": int(ct), "who": an, "text": s,
                        "url": f"{WWW_REPO_URL}/commit/{h}"})
    return out


def puller():
    while True:
        time.sleep(PULL_INTERVAL)
        try:
            if WWW.joinpath(".git").exists() and git("remote").stdout.strip():
                git_pull()
        except Exception as e:  # noqa: BLE001
            log.warning("periodic pull failed: %s", e)


# ----------------------------------------------------------------------------- inbox

def sanitize_name(name):
    name = os.path.basename((name or "").strip())
    if name in ("", ".", ".."):
        return "upload"
    name = SAFE_NAME_RE.sub("_", name).replace("..", "_").strip(" .")
    return (name or "upload")[:180]


def slugify(text, fallback="message", n=40):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:n].rstrip("-") or fallback)


def unique_dir(parent, base):
    parent.mkdir(parents=True, exist_ok=True)
    p = parent / base
    if not p.exists():
        return p
    for i in range(2, 9999):
        c = parent / f"{base}-{i}"
        if not c.exists():
            return c
    return parent / f"{base}-{secrets.token_hex(3)}"


def inbox_message(seat, msgdir):
    """Describe one message directory."""
    note = msgdir / "note.md"
    files = sorted(p.name for p in msgdir.iterdir() if p.is_file() and p.name not in ("note.md", "READ"))
    ts = msgdir.name[:15]
    try:
        t = int(datetime.strptime(ts, "%Y%m%d-%H%M%S").timestamp())
    except ValueError:
        t = int(msgdir.stat().st_mtime)
    prompt = ""
    if note.exists():
        txt = note.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^## Prompt\s*\n+(.*?)(?:\n## |\Z)", txt, re.S | re.M)
        if m:
            prompt = m.group(1).strip()
    return {"seat": seat, "id": msgdir.name, "time": t, "slug": msgdir.name[16:],
            "read": (msgdir / "READ").exists(), "files": files, "prompt": prompt,
            "url": f"/inbox/{seat}/{msgdir.name}/", "path": str(msgdir)}


def inbox_messages(seat, limit=None):
    d = INBOX / seat
    if not d.is_dir():
        return []
    # a message is a dir matching the pattern AND carrying a note.md — stray artifact dirs
    # (e.g. migration pane captures) otherwise showed as phantom-unread and 404'd on click
    dirs = sorted((p for p in d.iterdir()
                   if p.is_dir() and MSG_RE.match(p.name) and (p / "note.md").exists()), reverse=True)
    if limit:
        dirs = dirs[:limit]
    return [inbox_message(seat, p) for p in dirs]


def inbox_seats():
    if not INBOX.is_dir():
        return []
    return sorted(p.name for p in INBOX.iterdir() if p.is_dir() and SESSION_RE.match(p.name))


def write_inbox_index(seat):
    """Regenerate ~/inbox/<seat>/INBOX.md from the message directories (READ marker = handled)."""
    msgs = inbox_messages(seat)
    L = [f"# Inbox — seat `{seat}`", "",
         "Newest first. A message is a directory with `note.md` (+ attachments). ",
         "When you have handled one, `touch READ` inside its directory; the hub regenerates this index.",
         "", f"Unread: **{sum(1 for m in msgs if not m['read'])}** / {len(msgs)}", "",
         "| when | status | message | prompt | files |", "|---|---|---|---|---|"]
    for m in msgs:
        p = (m["prompt"].splitlines() or [""])[0]
        p = (p[:90] + "…") if len(p) > 90 else p
        p = p.replace("|", "\\|")
        L.append(f"| {fmt_ts(m['time'])} | {'read' if m['read'] else '**unread**'} | "
                 f"[{m['id']}]({m['id']}/note.md) | {p} | {len(m['files'])} |")
    L.append("")
    (INBOX / seat).mkdir(parents=True, exist_ok=True)
    (INBOX / seat / "INBOX.md").write_text("\n".join(L), encoding="utf-8")


def write_note(msgdir, *, seat, prompt, files, mode, delivered, notice, error, tab=""):
    L = [f"# Message for seat `{seat}`" + (f" · tab `{tab}`" if tab else ""), "",
         f"- **from:** {OWNER} (hub)",
         *([f"- **for tab:** {tab}"] if tab else []),
         f"- **when:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
         f"- **mode:** {mode}",
         f"- **herdr nudge delivered:** {delivered}" + (f" ({error})" if error else ""),
         f"- **this directory:** `{msgdir}`",
         f"- **when handled:** `touch {msgdir}/READ`",
         "", "## Prompt", "",
         prompt.strip() if prompt and prompt.strip() else "_(no prompt text)_",
         "", "## Attachments", ""]
    if not files:
        L.append("_(none)_")
    else:
        L += ["| # | file | size | type |", "|---|---|---|---|"]
        for i, f in enumerate(files, 1):
            L.append(f"| {i} | `{f['path']}` | {f['size']} | {f['type'] or ''} |")
    L += ["", "## Notice typed into the pane", "", "```", notice, "```", ""]
    (msgdir / "note.md").write_text("\n".join(L), encoding="utf-8")


# ----------------------------------------------------------------------------- board

ITEM_RE = re.compile(r"^## \[( |x|X)\]\s*(.*?)\s*$")
TAG_RE = re.compile(r"(?<!\S)#([\w-]+)")
DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})\)\s*$")


def parse_board(text):
    preamble, items, cur = [], [], None
    for raw in (text or "").splitlines():
        m = ITEM_RE.match(raw)
        if m:
            rest = m.group(2)
            date = ""
            dm = DATE_RE.search(rest)
            if dm:
                date = dm.group(1)
                rest = rest[:dm.start()].rstrip()
            tags = TAG_RE.findall(rest)
            title = TAG_RE.sub("", rest).strip()
            title = re.sub(r"\s{2,}", " ", title)
            cur = {"idx": len(items), "done": m.group(1).lower() == "x", "title": title,
                   "tags": tags, "date": date, "notes": [], "body": []}
            items.append(cur)
            continue
        if cur is None:
            preamble.append(raw)
        elif raw.startswith("- "):
            cur["notes"].append(raw[2:].strip())
        elif raw.strip():
            cur["body"].append(raw.rstrip())
    while preamble and not preamble[-1].strip():
        preamble.pop()
    return preamble, items


def render_board(preamble, items):
    L = list(preamble) if preamble else ["# Board"]
    for it in items:
        L.append("")
        head = f"## [{'x' if it['done'] else ' '}] {it['title']}"
        if it["tags"]:
            head += "  " + " ".join("#" + t for t in it["tags"])
        if it["date"]:
            head += f"  ({it['date']})"
        L.append(head)
        for b in it["body"]:
            L.append(b)
        for n in it["notes"]:
            L.append(f"- {n}")
    return "\n".join(L) + "\n"


def load_board():
    if not BOARD.exists():
        return ["# Board", "", "Items with their notes attached. Add from /board or edit this file."], []
    return parse_board(BOARD.read_text(encoding="utf-8"))


def save_board(preamble, items, message):
    tmp = BOARD.with_suffix(".md.tmp")
    tmp.write_text(render_board(preamble, items), encoding="utf-8")
    os.replace(tmp, BOARD)
    return git_commit_push(["board.md"], message)


# ----------------------------------------------------------------------------- directory map

def git_origin(d):
    try:
        r = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def origin_link(url):
    if not url:
        return None
    u = url[:-4] if url.endswith(".git") else url
    m = re.search(r"[/:]([^/:]+/[^/]+)$", u)
    return {"label": m.group(1) if m else u, "href": u if u.startswith("http") else ""}


def dirmap(live=None, st=None):
    """Where things live on the box, from herdr's *real* pane cwds (not the ~/<seat> naming rule):
    each work group, then each tab's cwd with the repos in it; a record-only seat shows the
    cwd its seat.json promises. Then the fixed infrastructure."""
    live = live if live is not None else herdr_seats()
    st = st if st is not None else seats()
    rows = []

    def add(path, note="", href="", depth=0):
        rows.append({"path": path, "note": note, "href": href, "depth": depth})

    def repos_in(d):
        out = []
        try:
            kids = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return out
        for c in kids:
            if (c / ".git").exists():
                o = origin_link(git_origin(c))
                out.append((c.name + "/", f"repo ⎇ {o['label']}" if o else "repo", o["href"] if o else ""))
        return out

    names = list(st) + [n for n in live if n not in st]
    for name in names:
        info = st.get(name) or {"name": name, "kind": ""}
        s = live.get(name)
        add(f"{name}", ("work group · herdr " + s["status"]) if s else "seat record only — no herdr workspace",
            f"/{name}/", 0)
        for r in seat_rows(info, s):
            cwd = r["cwd"] or (os.path.expanduser(r["meta_cwd"]) if r["meta_cwd"] else "")
            if not cwd:
                add(f"{r['name']}", "tab · no cwd known", "", 1)
                continue
            d = Path(cwd)
            who = (r["agent"] or r["name"]) + (f" ({r['engine']})" if r["engine"] else "")
            state = ("herdr " + r["status"]) if r["live"] else "not running"
            missing = "" if d.is_dir() else " — MISSING"
            add(f"{_tilde(cwd)}/", f"{r['name']} · {who} · {state}{missing}", "", 1)
            if (d / ".git").exists():
                o = origin_link(git_origin(d))
                add("(this dir is a repo)", f"⎇ {o['label']}" if o else "repo", o["href"] if o else "", 2)
            for path, note, href in repos_in(d):
                add(path, note, href, 2)
    add(f"{WWW}/", "checkout of the site repo — everything on this site; push = publish", WWW_REPO_URL, 0)
    add("~/inbox/", "one dir per work group, one dir per message (note.md + files, READ marker)", "/inbox/", 0)
    add("~/.config/hub/", "credential map + hub webhook secret — never in git", "", 0)
    add("/etc/caddy/Caddyfile", "web front; source in hub/deploy/ (+ auth.caddy with the login hash)", "", 0)
    add("/etc/systemd/system/", "hub · ttyd units; sources in hub/deploy/", "", 0)
    add("~/bin/", "seat · seat-rename · seat-alert", "", 0)
    add("~/.claude/projects/<cwd>/", "Claude Code transcripts + memory, keyed on the tab's cwd", "", 0)
    return rows


# ----------------------------------------------------------------------------- deploy secret

def deploy_secret():
    try:
        s = DEPLOY_SECRET_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s
    except FileNotFoundError:
        pass
    DEPLOY_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    s = secrets.token_hex(24)
    DEPLOY_SECRET_FILE.write_text(s + "\n", encoding="utf-8")
    os.chmod(DEPLOY_SECRET_FILE, 0o600)
    return s


# ----------------------------------------------------------------------------- views

def nav_ctx():
    return {"gitea_url": GITEA_URL, "www_repo_url": WWW_REPO_URL, "host": request.host.split(":")[0]}


@app.context_processor
def inject():
    return nav_ctx()


@app.after_request
def no_stale_pages(resp):
    """Hub pages are dynamic and tiny; never let a phone browser show yesterday's version."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def home():
    live = herdr_seats()
    st = seats()
    all_pages = pages()
    cards = []
    for name, info in st.items():
        s = live.get(name)
        msgs = inbox_messages(name)
        cards.append({**info, "live": s, "rows": seat_rows(info, s),
                      "unread": sum(1 for m in msgs if not m["read"]),
                      "pages": [p for p in all_pages if p["seat"] == name and p["filed"]]})
    # live first (blocked, working, then idle), then records without a workspace
    rank = {"blocked": 0, "working": 1, "unknown": 2, "idle": 3, "done": 3}
    cards.sort(key=lambda c: (rank.get(c["live"]["status"], 2) if c["live"] else 9, c["name"].lower()))
    orphan_sessions = [s for n, s in live.items() if n not in st]
    unfiled = [p for p in all_pages if not p["filed"]]
    recent = git_recent(12)
    for seat in inbox_seats():
        for m in inbox_messages(seat, 5):
            recent.append({"kind": "inbox", "time": m["time"], "who": OWNER,
                           "text": f"→ {seat}: {(m['prompt'].splitlines() or [m['slug']])[0][:80]}",
                           "url": m["url"]})
    recent.sort(key=lambda r: r["time"], reverse=True)
    _, items = load_board()
    open_items = [i for i in items if not i["done"]]
    return render_template("home.html", cards=cards, orphan_sessions=orphan_sessions,
                           unfiled=unfiled, recent=recent[:8], pages=all_pages,
                           open_items=open_items[:8], open_count=len(open_items),
                           dirmap=dirmap(live, st), herdr_up=bool(live))


@app.get("/send")
def send_page():
    return render_template("send.html", preselect=request.args.get("seat", ""))


@app.get("/terminal/")
def terminal_page():
    """Mobile-correct wrapper around ttyd (which ships no viewport meta): frames
       /tty/?arg=<group>&arg=<tab> — ttyd passes each ?arg= as one argv to deploy/ttyd-seat.sh,
       which attaches herdr to that tab's pane (no tab: the group's agent pane; 'all': full herdr)."""
    seat = (request.args.get("arg") or "").strip()
    tab = (request.args.get("tab") or "").strip()
    if not SESSION_RE.match(seat) or (tab and not SESSION_RE.match(tab)):
        live = herdr_seats()
        items = []
        for s in live.values():
            items.append(f"<li><a href='/terminal/?arg={s['name']}'>{s['name']}</a> — {s['status']}<ul>"
                         + "".join(f"<li><a href='/terminal/?arg={s['name']}&tab={t['name']}'>{t['name']}</a> — {t['status']}</li>" for t in s["tabs"])
                         + "</ul></li>")
        return render_template("markdown.html", title="terminal",
                               body_html="<p>Open a terminal from a card: <code>/terminal/?arg=&lt;group&gt;&amp;tab=&lt;tab&gt;</code>"
                                         " or <a href='/terminal/?arg=all'>the full herdr UI</a></p><ul>" + "".join(items) + "</ul>",
                               raw_url=None, msg=None, path=None), 400
    return render_template("terminal.html", seat=seat, tab=tab)


@app.get("/board")
def board_page():
    preamble, items = load_board()
    return render_template("board.html", items=items,
                           preamble_html=render_md("\n".join(preamble)))


@app.get("/search")
def search_page():
    q = (request.args.get("q") or "").strip()
    results, err = [], ""
    if q:
        results, err = run_search(q)
    return render_template("search.html", q=q, results=results, err=err)


def run_search(q, limit=200):
    roots = [str(p) for p in (WWW, INBOX) if p.is_dir()]
    if not roots:
        return [], "nothing to search yet"
    cmd = ["rg", "--json", "-i", "--no-messages", "--max-count", "5", "--max-columns", "240",
           "--max-columns-preview", "-g", "!.git", "-g", "!*.{png,jpg,jpeg,gif,webp,pdf,zip,mp4,woff,woff2,ttf}",
           "-e", q, "--", *roots]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return [], "ripgrep (rg) is not installed"
    except subprocess.TimeoutExpired:
        return [], "search timed out"
    by_file = {}
    n = 0
    for line in r.stdout.splitlines():
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "match":
            continue
        d = o["data"]
        path = d["path"].get("text", "")
        text = d["lines"].get("text", "").strip()
        if path.endswith((".html", ".htm")):
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s{2,}", " ", text).strip()
        if not text:
            continue
        ent = by_file.setdefault(path, {"path": path, "url": path_to_url(path), "lines": []})
        if len(ent["lines"]) < 3:
            ent["lines"].append({"n": d.get("line_number"), "text": text[:240]})
        n += 1
        if n >= limit:
            break
    return list(by_file.values()), ""


def path_to_url(path):
    p = Path(path)
    try:
        rel = p.relative_to(WWW).as_posix()
        if rel.endswith("/index.html"):
            return "/" + rel[:-len("index.html")]
        return "/" + rel
    except ValueError:
        pass
    try:
        return "/inbox/" + p.relative_to(INBOX).as_posix()
    except ValueError:
        return ""


@app.get("/inbox/")
def inbox_home():
    rows = []
    for seat in inbox_seats():
        msgs = inbox_messages(seat)
        rows.append({"seat": seat, "count": len(msgs), "unread": sum(1 for m in msgs if not m["read"]),
                     "latest": msgs[0] if msgs else None})
    return render_template("inbox.html", rows=rows, seat=None, msgs=None)


@app.get("/inbox/<seat>/")
def inbox_seat(seat):
    if not SESSION_RE.match(seat) or not (INBOX / seat).is_dir():
        abort(404)
    return render_template("inbox.html", rows=None, seat=seat, msgs=inbox_messages(seat))


@app.get("/inbox/<seat>/<msg>/")
def inbox_msg(seat, msg):
    d = INBOX / seat / msg
    if not SESSION_RE.match(seat) or not MSG_RE.match(msg) or not d.is_dir():
        abort(404)
    return redirect(f"/inbox/{seat}/{msg}/note.md")


@app.get("/inbox/<seat>/<path:rest>")
def inbox_file(seat, rest):
    if not SESSION_RE.match(seat):
        abort(404)
    base = (INBOX / seat).resolve()
    target = (base / rest).resolve()
    if base not in target.parents and target != base:
        abort(404)
    if target.is_dir():
        return redirect(f"/inbox/{seat}/{rest.rstrip('/')}/")
    if not target.is_file():
        abort(404)
    if target.suffix.lower() == ".md" and request.args.get("raw") != "1":
        txt = target.read_text(encoding="utf-8", errors="replace")
        m = inbox_message(seat, target.parent) if MSG_RE.match(target.parent.name) else None
        return render_template("markdown.html", title=f"{seat} / {target.parent.name if m else target.name}",
                               body_html=render_md(txt), raw_url=f"/inbox/{seat}/{rest}?raw=1",
                               msg=m, path=str(target))
    return send_file(str(target), mimetype=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


@app.post("/api/inbox/<seat>/<msg>/read")
def api_inbox_read(seat, msg):
    d = INBOX / seat / msg
    if not SESSION_RE.match(seat) or not MSG_RE.match(msg) or not d.is_dir():
        abort(404)
    marker = d / "READ"
    if request.form.get("undo") or (request.is_json and (request.json or {}).get("undo")):
        marker.unlink(missing_ok=True)
    else:
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
    write_inbox_index(seat)
    if request.form.get("back"):
        return redirect(request.form["back"])
    return jsonify(ok=True, read=marker.exists())


# ----------------------------------------------------------------------------- paste bucket

def paste_items():
    if not PASTE.is_dir():
        return []
    out = []
    for f in PASTE.iterdir():
        if f.is_file() and f.suffix == ".txt":
            try:
                out.append({"name": f.name, "time": int(f.stat().st_mtime),
                            "text": f.read_text(encoding="utf-8", errors="replace")})
            except OSError:
                continue
    out.sort(key=lambda x: x["time"], reverse=True)
    return out


@app.get("/paste", strict_slashes=False)
def paste_page():
    return render_template("paste.html", items=paste_items())


@app.post("/paste")
def paste_add():
    text = (request.form.get("text") or "").rstrip()
    if text.strip():
        PASTE.mkdir(parents=True, exist_ok=True)
        base = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{slugify(text.strip().splitlines()[0], 'paste')}"
        f = PASTE / f"{base}.txt"
        if f.exists():
            f = PASTE / f"{base}-{secrets.token_hex(3)}.txt"
        f.write_text(text + "\n", encoding="utf-8")
    return redirect("/paste")


@app.post("/paste/delete")
def paste_delete():
    name = os.path.basename(request.form.get("name") or "")
    f = PASTE / name
    if name.endswith(".txt") and f.is_file():
        f.unlink()
    return redirect("/paste")


# ----------------------------------------------------------------------------- wiki

WIKI_H1_RE = re.compile(r"^#\s+(.+)$", re.M)


@app.get("/wiki", strict_slashes=False)
def wiki_page():
    sections = []
    if WIKI.is_dir():
        for f in sorted(WIKI.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = WIKI_H1_RE.search(text)
            sections.append({"slug": f.stem, "file": f.name,
                             "title": (m.group(1).strip() if m else f.stem),
                             "html": render_md(text)})
    return render_template("wiki.html", sections=sections)


@app.post("/paste/file")
def paste_file():
    """File every paste into the wiki's Unsorted section (committed), then empty the bucket."""
    items = paste_items()
    if items:
        WIKI.mkdir(parents=True, exist_ok=True)
        dest = WIKI / "90-unsorted.md"
        body = dest.read_text(encoding="utf-8") if dest.exists() else "# Unsorted (filed from the paste bucket)\n"
        for it in reversed(items):  # oldest first
            body += f"\n## {it['name'][:-4]}\n\n```\n{it['text'].rstrip()}\n```\n"
        dest.write_text(body, encoding="utf-8")
        res = git_commit_push([str(dest)], f"wiki: file {len(items)} paste(s) from the bucket")
        if res.get("ok"):
            for it in items:
                try:
                    (PASTE / it["name"]).unlink()
                except OSError:
                    pass
        else:
            log.error("paste file&clear: %s", res.get("detail"))
    return redirect("/wiki")


# ----------------------------------------------------------------------------- API: herdr + send

@app.get("/api/sessions")
def api_sessions():
    """Live herdr workspaces (name, status, agent, tabs[...]) + whether each has a seat record."""
    st = seats()
    return jsonify([{**s, "seat": s["name"] in st} for s in herdr_seats().values()])


@app.get("/api/status")
def api_status():
    live = herdr_seats()
    st = seats()
    return jsonify(
        seats={n: {**i, "live": live.get(n), "unread": sum(1 for m in inbox_messages(n) if not m["read"])}
               for n, i in st.items()},
        sessions_without_seat=[n for n in live if n not in st],
        pages=len(pages()), www=str(WWW), inbox=str(INBOX), time=int(time.time()))


@app.get("/api/capture")
def api_capture():
    session = (request.args.get("session") or "").strip()
    try:
        n = min(int(request.args.get("lines") or 100), 2000)
    except ValueError:
        n = 100
    tab = (request.args.get("tab") or "").strip()
    if not SESSION_RE.match(session) or (tab and not SESSION_RE.match(tab)):
        return jsonify(ok=False, error="invalid session/tab name"), 400
    if session not in herdr_seats():
        return jsonify(ok=False, error=f"no herdr workspace '{session}'"), 404
    try:
        out = herdr_capture(session, n, tab or None)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True, session=session, output=out, lines=out.count("\n"))


# herdr key names: modifiers join with "+", specials are lowercase words.
# (home/end/pgup/pgdn are not in herdr's key vocabulary — scroll is wheel events in the TUI.)
KEYS = {"newline": "ctrl+j", "esc": "esc", "tab": "tab", "enter": "enter", "up": "up", "down": "down",
        "left": "left", "right": "right", "backspace": "backspace", "space": "space",
        "ctrl-space": "ctrl+space", "ctrl-[": "ctrl+[", "ctrl-]": "ctrl+]", "ctrl-\\": "ctrl+\\",
        "ctrl-^": "ctrl+^", "ctrl-_": "ctrl+_",
        "alt-enter": "alt+enter", "alt-.": "alt+.", "alt-b": "alt+b", "alt-f": "alt+f", "alt-d": "alt+d"}
# every Ctrl-<letter> and Alt-<letter>: the phone key row offers a letter grid behind the Ctrl / Alt buttons
for _c in "abcdefghijklmnopqrstuvwxyz":
    KEYS.setdefault(f"ctrl-{_c}", f"ctrl+{_c}")
    KEYS.setdefault(f"alt-{_c}", f"alt+{_c}")
for _n in range(1, 13):
    KEYS[f"f{_n}"] = f"f{_n}"


@app.post("/api/keys")
def api_keys():
    """Press one special key in a seat's pane (for phone keyboards that lack Ctrl/Esc/arrows)."""
    data = request.get_json(silent=True) or request.form
    session = (data.get("session") or "").strip()
    key = (data.get("key") or "").strip()
    tab = (data.get("tab") or "").strip()
    if not SESSION_RE.match(session) or key not in KEYS or (tab and not SESSION_RE.match(tab)):
        return jsonify(ok=False, error="bad session, tab or key", keys=sorted(KEYS)), 400
    pane = herdr_seat_pane(session, tab or None)
    if not pane:
        return jsonify(ok=False, error=f"no herdr pane for '{session}'" + (f" tab '{tab}'" if tab else "")), 404
    try:
        herdr_run("pane", "send-keys", pane, KEYS[key], check=True)
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True, session=session, key=key)


def omp_session_file(session):
    """The OMP session jsonl behind a seat. Prefer what the herdr omp integration reports;
    fall back to OMP's per-tty breadcrumb (line 1 cwd, line 2 session jsonl)."""
    live = herdr_seats()
    s = live.get(session)
    if not s:
        return None
    sid = s.get("session_id")
    if sid and sid.endswith(".jsonl") and Path(sid).exists():
        return Path(sid).resolve()
    info = herdr_json("pane", "process-info", "--pane", s["pane_id"]) if s["pane_id"] else None
    procs = (info or {}).get("result", {}).get("process_info", {}).get("foreground_processes", [])
    for p in procs:
        try:
            tty = os.readlink(f"/proc/{p['pid']}/fd/0")
        except OSError:
            continue
        if not tty.startswith("/dev/"):
            continue
        crumb = HOME / ".omp" / "agent" / "terminal-sessions" / tty.removeprefix("/dev/").replace("/", "-")
        try:
            lines = crumb.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        if len(lines) >= 2 and lines[1]:
            return Path(lines[1]).resolve()
    return None


def last_assistant_text(path):
    try:
        data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in reversed(data.splitlines()):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "message" or e.get("message", {}).get("role") != "assistant":
            continue
        c = e["message"].get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
            return "".join(parts) if parts else None
    return None


@app.get("/api/response")
def api_response():
    session = (request.args.get("session") or "").strip()
    if not SESSION_RE.match(session):
        return jsonify(ok=False, error="invalid session name"), 400
    if session not in herdr_seats():
        return jsonify(ok=False, error=f"no herdr workspace '{session}'"), 404
    p = omp_session_file(session)
    if not p:
        return jsonify(ok=False, error="no OMP breadcrumb for this pane"), 404
    text = last_assistant_text(p)
    if text is None:
        return jsonify(ok=False, error="no assistant message found"), 404
    return jsonify(ok=True, session=session, source=str(p), response=text)


@app.post("/api/send")
def api_send():
    session = (request.form.get("session") or "").strip()
    prompt = request.form.get("prompt") or ""
    mode = request.form.get("mode") or "prompt"
    if mode not in ("prompt", "ping"):
        mode = "prompt"
    tab = (request.form.get("tab") or "").strip()
    if not SESSION_RE.match(session) or (tab and not SESSION_RE.match(tab)):
        return jsonify(ok=False, error="invalid session/tab name"), 400
    live = herdr_seats()
    if session not in live:
        return jsonify(ok=False, error=f"no herdr workspace '{session}'", sessions=sorted(live)), 400
    row = herdr_tab(live[session], tab) if tab else None
    if tab and not row:
        return jsonify(ok=False, error=f"no tab '{tab}' in workspace '{session}'",
                       tabs=[t["name"] for t in live[session]["tabs"]]), 400

    ts = time.strftime("%Y%m%d-%H%M%S")
    uploads = [f for f in request.files.getlist("files") if f and f.filename]
    slug = slugify(prompt.splitlines()[0] if prompt.strip() else (uploads[0].filename if uploads else ""))
    msgdir = unique_dir(INBOX / session, f"{ts}_{slug}")
    msgdir.mkdir(parents=True)

    saved = []
    for f in uploads:
        name = sanitize_name(f.filename)
        dest = msgdir / name
        i = 2
        while dest.exists():
            stem, ext = os.path.splitext(name)
            dest = msgdir / f"{stem}-{i}{ext}"
            i += 1
        f.save(str(dest))
        saved.append({"original": f.filename, "name": dest.name, "path": str(dest),
                      "size": dest.stat().st_size,
                      "type": f.mimetype or mimetypes.guess_type(f.filename)[0] or ""})

    n = len(saved)
    noun = "a prompt" if prompt.strip() else "a message"
    att = f" + {n} attachment{'' if n == 1 else 's'}" if n else ""
    note_path = msgdir / "note.md"
    notice = f"\U0001F4E5 {OWNER} sent {noun}{att} via hub — read {note_path} and act on it".replace("\n", " ")
    toast = f"\U0001F4E5 hub message from {OWNER}: {noun}{att}".replace("\n", " ")

    delivered, err = True, ""
    if mode == "prompt":
        # target = the tab's pane if a tab was named (pane ids survive herdr clearing agent names),
        # else the group's agent
        agent = (row["pane_id"] if row and row["agent"] else None) if tab else live[session]["agent"]
        if agent:
            try:
                herdr_run("agent", "prompt", agent, notice, check=True)
            except RuntimeError as e:  # e.g. agent_blocked: waiting at an approval dialog
                delivered, err = False, str(e)
        else:
            delivered, err = False, ("no live agent in tab '%s'" % tab) if tab else "no live agent in this workspace"
    herdr_run("notification", "show", toast)

    write_note(msgdir, seat=session, prompt=prompt, files=saved, mode=mode,
               delivered=delivered, notice=notice, error=err.strip(), tab=(row or {}).get("name", ""))
    write_inbox_index(session)
    return jsonify(ok=True, session=session, tab=(row or {}).get("name", ""), mode=mode, dir=str(msgdir), note=str(note_path),
                   url=f"/inbox/{session}/{msgdir.name}/", files=saved, delivered=delivered,
                   error=(err.strip() or None))


# ----------------------------------------------------------------------------- API: secrets

SECRET_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
SECRET_DIR = HOME / ".config" / "hub"


@app.post("/api/secret")
def api_secret():
    """Write a credential to ~/.config/hub/<name> (mode 600) so a page can collect a token without
       the token ever passing through an inbox note or git (house rule: credentials live only there).
       Body: JSON or form {name, value}. Never logs or echoes the value; reports only the path + size."""
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    value = (data.get("value") or "").strip()
    if not SECRET_RE.match(name):
        return jsonify(ok=False, error="bad secret name (lowercase, digits, dashes)"), 400
    if not value:
        return jsonify(ok=False, error="empty value"), 400
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    path = SECRET_DIR / name
    existed = path.exists()
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value + "\n")
    os.replace(str(tmp), str(path))
    os.chmod(path, 0o600)
    log.info("secret written: %s (%d chars, %s)", path, len(value), "replaced" if existed else "new")
    return jsonify(ok=True, path=str(path), chars=len(value), replaced=existed)


# ----------------------------------------------------------------------------- API: board

@app.post("/api/board")
def api_board():
    data = request.get_json(silent=True) or request.form.to_dict()
    action = (data.get("action") or "").strip()
    preamble, items = load_board()
    today = time.strftime("%Y-%m-%d")
    now = time.strftime("%Y-%m-%d %H:%M")

    def find():
        try:
            idx = int(data.get("idx"))
        except (TypeError, ValueError):
            return None
        if 0 <= idx < len(items) and items[idx]["title"] == (data.get("title") or ""):
            return items[idx]
        return None

    if action == "add":
        title = re.sub(r"\s+", " ", (data.get("title") or "")).strip()
        if not title:
            return jsonify(ok=False, error="title required"), 400
        tags = TAG_RE.findall(data.get("tags") or "") + TAG_RE.findall(title)
        title = TAG_RE.sub("", title).strip()
        item = {"idx": 0, "done": False, "title": title, "tags": tags, "date": today, "notes": [], "body": []}
        first = (data.get("note") or "").strip()
        if first:
            item["notes"].append(f"{now} — {first}")
        items.insert(0, item)
        res = save_board(preamble, items, f"board: add \"{title[:60]}\"")
    elif action in ("note", "toggle", "retitle", "delete"):
        it = find()
        if it is None:
            return jsonify(ok=False, error="item not found (board changed underneath you — reload)"), 409
        if action == "note":
            text = re.sub(r"\s+", " ", (data.get("text") or "")).strip()
            if not text:
                return jsonify(ok=False, error="empty note"), 400
            it["notes"].append(f"{now} — {text}")
            res = save_board(preamble, items, f"board: note on \"{it['title'][:50]}\"")
        elif action == "toggle":
            it["done"] = not it["done"]
            it["notes"].append(f"{now} — {'done' if it['done'] else 'reopened'}")
            res = save_board(preamble, items, f"board: {'done' if it['done'] else 'reopen'} \"{it['title'][:50]}\"")
        elif action == "retitle":
            new = re.sub(r"\s+", " ", (data.get("new_title") or "")).strip()
            if not new:
                return jsonify(ok=False, error="title required"), 400
            it["tags"] = list(dict.fromkeys(it["tags"] + TAG_RE.findall(new)))
            it["title"] = TAG_RE.sub("", new).strip()
            res = save_board(preamble, items, f"board: retitle -> \"{it['title'][:50]}\"")
        else:
            items.remove(it)
            res = save_board(preamble, items, f"board: delete \"{it['title'][:50]}\"")
    else:
        return jsonify(ok=False, error="unknown action"), 400
    return jsonify(ok=True, git=res)


# ----------------------------------------------------------------------------- API: deploy

@app.post("/api/deploy")
def api_deploy():
    secret = deploy_secret()
    body = request.get_data() or b""
    sig = request.headers.get("X-Gitea-Signature") or request.headers.get("X-Hub-Signature-256", "").removeprefix("sha256=")
    ok = False
    if sig:
        ok = hmac.compare_digest(sig, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
    if not ok and request.args.get("token"):
        ok = hmac.compare_digest(request.args["token"], secret)
    if not ok:
        return jsonify(ok=False, error="bad signature"), 403
    res = git_pull()
    log.info("deploy: %s", res)
    return jsonify(**res)


@app.get("/healthz")
def healthz():
    return "ok\n"


# ----------------------------------------------------------------------------- seat page + static fallback

def seat_page(name):
    d = WWW / name
    info = load_seat(d)
    live = herdr_seats().get(name)
    charter = (d / "SEAT.md").read_text(encoding="utf-8", errors="replace") if info["has_charter"] else ""
    seat_json = (d / "seat.json").read_text(encoding="utf-8", errors="replace") if info["has_record"] else ""
    return render_template("seat.html", seat=info, live=live, rows=seat_rows(info, live), charter_html=render_md(charter),
                           seat_json=seat_json, pages=[p for p in pages() if p["seat"] == name],
                           msgs=inbox_messages(name, 25))


@app.get("/<path:p>")
def static_fallback(p):
    """Serve /srv/www so the hub also works without Caddy; seat dirs without index.html -> seat page."""
    root = WWW.resolve()
    target = (root / p).resolve()
    if root != target and root not in target.parents:
        abort(404)
    if ".git" in target.parts:
        abort(404)
    if target.is_dir():
        if not p.endswith("/"):
            return redirect("/" + p + "/")
        idx = target / "index.html"
        if idx.is_file():
            return send_file(str(idx))
        if target.parent == root and target.name not in RESERVED:
            return seat_page(target.name)
        abort(404)
    if target.is_file():
        return send_file(str(target), mimetype=mimetypes.guess_type(target.name)[0] or "application/octet-stream")
    abort(404)


@app.errorhandler(404)
def not_found(_e):
    return render_template("markdown.html", title="Not found",
                           body_html=f"<p>No such page: <code>{htmlmod.escape(request.path)}</code></p>"
                                     "<p><a href='/'>Home</a></p>", raw_url=None, msg=None, path=None), 404


if __name__ == "__main__":
    INBOX.mkdir(parents=True, exist_ok=True)
    deploy_secret()
    threading.Thread(target=puller, daemon=True).start()
    app.run(host=os.environ.get("HUB_BIND", "127.0.0.1"), port=int(os.environ.get("HUB_PORT", "8090")), threaded=True)
