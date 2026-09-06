#!/bin/bash
# ttyd-seat.sh — what ttyd runs for each browser terminal: attach to the pane named in the URL.
#   http://<host>/tty/?arg=<group>&arg=<tab>   ->   herdr agent attach <pane for that group's tab>
#   http://<host>/tty/?arg=<group>              ->   no tab: the group's first agent pane, else its first pane
#   http://<host>/tty/?arg=all                  ->   full herdr UI (every workspace, ctrl+b keybindings)
# Attach only (never creates workspaces, tabs or agents): a group is a herdr workspace, not something
# a browser tab should invent. Bad or missing names get a message instead of a shell.
set -u
HERDR="${HERDR:-$HOME/.local/bin/herdr}"
group="${1:-}"
tab="${2:-}"
if [[ -z "$group" ]]; then
  echo "no group given — open /terminal/?arg=<group> from a card on the hub home page"
  echo; echo "herdr workspaces:"
  "$HERDR" workspace list 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for w in d.get("result",{}).get("workspaces",[]):
    print(f"  {w["label"]:<14} {w["agent_status"]}")' || echo "  (herdr server not running)"
  sleep 20; exit 1
fi
if [[ ! "$group" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "invalid group name"; sleep 20; exit 1
fi
if [[ "$group" == "all" ]]; then
  exec "$HERDR"
fi
if [[ -n "$tab" && ! "$tab" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "invalid tab name"; sleep 20; exit 1
fi
# Resolve <group>[/<tab>] to a pane id in one python3 pass over herdr's JSON listings.
out=$(python3 - "$HERDR" "$group" "$tab" <<'PY'
import json, subprocess, sys

herdr, group, tab_arg = sys.argv[1], sys.argv[2], sys.argv[3]

def call(*args):
    try:
        out = subprocess.run([herdr, *args], capture_output=True, text=True, timeout=8).stdout
        return json.loads(out).get("result", {})
    except Exception:
        return {}

workspaces = call("workspace", "list").get("workspaces", [])
w = next((x for x in workspaces if x.get("label") == group), None)
if not w:
    names = ", ".join(sorted(x.get("label", "") for x in workspaces)) or "(none)"
    print(f"no herdr workspace named '{group}'")
    print(f"workspaces: {names}")
    sys.exit(1)
wid = w["workspace_id"]

tabs = [t for t in call("tab", "list").get("tabs", []) if t.get("workspace_id") == wid]
agents = call("agent", "list").get("agents", [])
panes = call("pane", "list").get("panes", [])

def agent_pane_for_tab(tid):
    a = next((a for a in agents if a.get("tab_id") == tid), None)
    return a["pane_id"] if a else None

def first_pane_for_tab(tid):
    p = next((p for p in panes if p.get("tab_id") == tid), None)
    return p["pane_id"] if p else None

def tab_list_str():
    return ", ".join(f"{t.get('label') or '(unnamed)'}#{t.get('number')}" for t in tabs) or "(none)"

pane_id = None
if tab_arg:
    t = None
    if tab_arg.isdigit():
        t = next((x for x in tabs if str(x.get("number")) == tab_arg), None)
    if t is None:
        t = next((x for x in tabs if (x.get("label") or "").lower() == tab_arg.lower()), None)
    if t is None:
        print(f"no tab '{tab_arg}' in group '{group}'")
        print(f"tabs: {tab_list_str()}")
        sys.exit(1)
    tid = t["tab_id"]
    pane_id = agent_pane_for_tab(tid) or first_pane_for_tab(tid)
    if not pane_id:
        print(f"tab '{tab_arg}' in group '{group}' has no panes")
        sys.exit(1)
else:
    tab_ids = {t["tab_id"] for t in tabs}
    a = next((a for a in agents if a.get("tab_id") in tab_ids), None)
    if a:
        pane_id = a["pane_id"]
    else:
        p = next((p for p in panes if p.get("tab_id") in tab_ids), None)
        pane_id = p["pane_id"] if p else None
    if not pane_id:
        print(f"group '{group}' has no panes")
        print(f"tabs: {tab_list_str()}")
        sys.exit(1)

print(f"PANE:{pane_id}")
PY
)
pane=$(printf '%s\n' "$out" | sed -n 's/^PANE://p' | head -1)
if [[ -n "$pane" ]]; then
  exec "$HERDR" agent attach "$pane"
fi
printf '%s\n' "$out"
sleep 20
exit 1
