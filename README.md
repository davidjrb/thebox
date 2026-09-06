# The box: a home server run by a team of AI agents

This is a write-up of a setup I have been building for two weeks, and it is only the beginning. The one-line version: a small Debian VM at home runs a handful of AI coding agents as **standing roles**, a tiny web page lets me drive them from my phone, and a set of house rules fell out of doing it for real. Everything below is a work in progress. There is a lot more coming, most of it about work productivity.

## Seats, not processes

The first thing I learned is that an agent session is not the unit that matters. Sessions crash, get resumed, get replaced by a cheaper model. What persists is the **role**: "the one who owns the web page", "the one who curates the wiki", "the chess coach". I call these seats.

A seat is two files in a git repo: a **charter** (`SEAT.md`, what the role is for, its standing orders, where its files live) and a **beacon** (`seat.json`, one line of status that the occupant updates when its state changes). The home page is built from that repo. If it is in the repo, it is on the home page; pushing is publishing.

A seat can be a whole work group: one workspace with several tabs, one agent per tab, each in its own directory. The infrastructure group has a coordinator, a web tab, a wiki tab and a publisher tab. The chess group has a coach and an architect. Each tab has a charter section, an agent name, and a job.

## herdr, the part that makes it work

I ran all of this in tmux for the first week, with a pile of `send-keys` helpers to talk to sessions. Then I trialled [herdr](https://herdr.dev), a terminal multiplexer written in Rust that is **agent-aware**: it detects the coding agent running in a pane and reports whether it is idle, working, blocked on a question, or done. After a few days I moved everything over and retired tmux.

The shape is: a herdr **workspace** is a work group, a **tab** is one agent, the agent's name is the tab's name. Everything else is scriptable through its socket:

```
herdr                              # attach; ctrl+b q detaches, everything keeps running
herdr --remote <host>              # thin client from the laptop, no ssh dance
herdr workspace list               # JSON: workspaces, each with agent_status and tab_count
herdr tab list --workspace <id>    # the tabs of one workspace
herdr agent list                   # every live agent with its pane, tab and state (the hub joins the three)
herdr agent prompt wiki "..."      # type a prompt into a named agent
herdr agent wait / pane read       # block on it, or read what is on its screen
herdr --skill                      # teaches an AI agent to drive herdr itself
```

A one-page `seat` script wraps this: `seat Chess` focuses that workspace, or creates it, starts the engine named in the beacon (Claude Code by default, resuming the last transcript; anything else if the beacon says so) and attaches. `seat rename old new` moves the home directory, inbox, transcripts, relabels the workspace, renames the record in git and restarts the agent resumed, in one command. Both scripts understand single-tab seats only so far; a multi-tab work group is still created and renamed by hand with `herdr workspace create`, `tab create` and `agent start`. A user systemd unit starts the herdr server at boot so the seats survive a reboot.

Things that bit me, so you can skip them:

- herdr forgets an agent's *name* whenever the session in its pane changes (a `/clear`, a resume). Key everything on the workspace label, and re-register the name every time you focus the seat.
- Keep the server and every remote client on the same herdr version. A mismatched remote attach can replace the server and take every pane with it.
- Do not nest herdr inside tmux or the other way round. Both want `ctrl+b`.
- Claude Code runs in the terminal's alternate screen and scrolls its own transcript, so the multiplexer keeps no history for it. Wheel events are the right primitive, which matters for the phone terminal below.

## The hub: one small web page

Behind Caddy sits a Flask app of about a thousand lines. It does only what static files cannot:

- **Home**: one card per work group, one row per tab with a live dot from herdr (green idle, blue working, red blocked), the beacon line, and per-tab send / inbox / terminal links. A workspace that exists in herdr but has no seat record shows up red until it is filed or closed. No ghosts.
- **Send**: a prompt plus attachments from any device lands as a markdown note in the group's inbox directory, and the agent gets a one-line nudge pointing at the file. *The file is the message; the nudge is only a doorbell.* If the nudge is lost, nothing is lost.
- **Inbox**, **board** (a markdown file with `## [ ]` items and notes attached to the row, editable on the page or by hand, committed either way), **search** (ripgrep over the site and the inboxes), a **wiki** rendered from numbered markdown pages, and a **paste bucket** for moving text between devices.
- **Terminal in the browser**, on the phone. ttyd attaches to one pane by URL; the hub wraps it with a viewport meta (without it the page is unreadably tiny), font zoom, and a key row: newline, arrows, page up/down, and Ctrl/Alt/Esc behind a "more" button. One-finger drags are turned into wheel events so an agent's transcript scrolls under your thumb. Do not CSS-transform the iframe and do not resize it when a key is pressed; Firefox on Android blanks the page if you do.
- **Deploy**: a webhook from the local Gitea pulls the site repo on push. Push is publish.

Everything the hub writes is a file. Nothing has a database.

## The house rules

These were not designed. Each one exists because something went wrong without it.

1. **One home per agent.** Never point two agents at one directory. Four sessions once shared the home directory and "resume the last conversation" became a lottery.
2. **Every agent runs without permission prompts.** An agent left in ask mode is gated by a classifier that blocks infrastructure work at random. The coordinator could not launch a sibling tab because of it.
3. **Separation of duties.** The coordinator wires seats and routes work; it does not do a specialist's job. When something in a domain breaks, it goes to the seat that owns it, not to whoever noticed. *Route it, don't absorb it.*
4. **Pipelines wake their owner.** A timer that a seat owns fails with `OnFailure=` pointing at a handler that files an inbox note **and** prompts that seat. A sync failed silently for five days before this rule existed.
5. **Done means documented.** Commit, push, update the beacon. Files are the truth, not what is on the screen.
6. **Economy.** The expensive model for judgement and synthesis; menial and bulk work goes to cheap models through [omp](https://github.com/can1357/oh-my-pi) on Synthetic, at a hundredth of the price. Each seat's beacon says which engine sits in it, and `seat` starts the right one.
7. **Credentials live in one file with mode 600**, mapped by name, never in a repo. A page on the hub can collect a token straight into that directory so it never passes through a note or a commit.
8. **Publishing has a gate.** A publisher seat stages anything meant for GitHub as a fresh tree, scans it, rewrites the README for a stranger, writes release notes on what was removed, and pushes only on an explicit per-release OK. This post went through it.

## What it is doing today

The infrastructure group keeps the box coherent. The chess group pulled every online game I have played into a database, ran an engine over them, and produced [a first coaching report](https://davidjrb.github.io/chess/6sep2026/). A couple of personal groups handle paperwork and experiments I will not go into.

The whole thing is one VM, a few hundred lines of Python and bash, a git repo of markdown, and herdr. That is the point. The agents do the work; the setup just makes sure there is always someone in the seat, that they can be reached from a phone, and that nothing that happens is lost.

More soon.

---

## What is in this repo

This is the setup itself, scrubbed of my hostnames, paths and personal workloads. It is a snapshot of a
work in progress, not a packaged product; expect to read it before you run it.

```
README.md                  this write-up
docs/PROTOCOL.md           the seats protocol: the README of the site repo that the hub serves
bin/seat                   seat X — focus/create a herdr workspace, start its engine, attach
bin/seat-rename            seat rename old new — move everything in one command
bin/seat-alert             OnFailure handler: file an inbox note + wake the owning tab
systemd/herdr-server.service     user unit so seats survive a reboot (pair with loginctl enable-linger)
systemd/seat-alert.service       seat-alert@.service template + example.onfailure.conf drop-in
hub/                       the Flask hub: app.py, templates, static, deploy/ (Caddyfile, units, ttyd wrapper)
index.html                 this write-up as a page (GitHub Pages)
```

Dependencies: Debian 13 (`python3-flask python3-markdown ripgrep caddy`), [herdr](https://herdr.dev)
0.8.x, [ttyd](https://github.com/tsl0922/ttyd) (upstream static binary), a Gitea or any git host with a
push webhook, and whichever agents you want in the seats (Claude Code, [omp](https://github.com/can1357/oh-my-pi), …).
The unit files use `USER`, `GROUP`, `TAB` and `HOSTNAME` as placeholders. `hub/README.md` documents the
hub's routes and the browser-terminal gotchas.

Licence: MIT.
