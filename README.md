# The box: a home server run by a team of AI agents

A small Debian VM at home runs a handful of AI coding agents as **standing roles**. A one-page web app drives them from my phone. A few house rules fell out of doing it for real. About two or three days of work spread over the last two weeks, and only the beginning: more is coming, most of it about work productivity.

## Seats, not processes

An agent session is not the unit that matters. Sessions crash, get resumed, get swapped for a cheaper model. What persists is the role: the one who owns the web page, the one who curates the wiki, the chess coach. I call these seats.

A seat is two files in a git repo: a **charter** (`SEAT.md`: what the role is for, its standing orders, where its files live) and a **beacon** (`seat.json`: one line of status the occupant updates when its state changes). The home page is built from that repo. If it is in the repo, it is on the home page; push is publish.

A seat can be a work group: one workspace, several tabs, one agent per tab in its own directory. The infrastructure group has a coordinator, a web tab, a wiki tab and a publisher. The chess group has a coach and an architect.

## herdr

I started in tmux with a pile of `send-keys` helpers. Then I tried [herdr](https://herdr.dev), a terminal multiplexer in Rust that is **agent-aware**: it recognises the coding agent in a pane and reports idle, working, blocked on a question, or done. After a few days I moved everything over and retired tmux.

A herdr **workspace** is a work group, a **tab** is one agent, and the agent is named after its tab. Everything is scriptable over its socket:

```
herdr                              # attach; ctrl+b q detaches, everything keeps running
herdr --remote <host>              # thin client from the laptop, no ssh dance
herdr workspace list               # JSON: workspaces, each with agent_status and tab_count
herdr tab list --workspace <id>    # the tabs of one workspace
herdr agent list                   # every live agent with its pane, tab and state
herdr agent prompt wiki "..."      # type a prompt into a named agent
herdr agent wait / pane read       # block on it, or read its screen
herdr --skill                      # teaches an AI agent to drive herdr itself
```

A short `seat` script wraps this: `seat Chess` focuses that workspace or creates it, starts the engine named in the beacon (Claude Code by default, resuming the last transcript), and attaches. `seat rename old new` moves home, inbox and transcripts, relabels the workspace, renames the record in git and restarts the agent resumed. Both understand single-tab seats only so far; multi-tab groups are still wired by hand with `herdr workspace create`, `tab create` and `agent start`. A user systemd unit starts the herdr server at boot.

Things that bit me:

- herdr forgets an agent's *name* whenever the session in its pane changes (a `/clear`, a resume). Key everything on the workspace label and re-register the name on every focus.
- Keep server and remote clients on the same herdr version. A mismatched attach can replace the server and take every pane with it.
- Don't nest herdr and tmux. Both want `ctrl+b`.
- Claude Code runs in the alternate screen and scrolls its own transcript, so the multiplexer keeps no history for it. Wheel events are the right primitive, which matters for the phone terminal.

## The hub

Behind Caddy, a Flask app of about a thousand lines. It does only what static files cannot:

- **Home**: a card per work group, a row per tab with a live dot from herdr, the beacon line, and send / inbox / terminal links. A workspace with no seat record shows red until it is filed or closed.
- **Send**: a prompt plus attachments from any device lands as a markdown note in the group's inbox, and the agent gets a one-line nudge pointing at it. The file is the message; the nudge is a doorbell.
- **Inbox**, a markdown **board** (items with notes attached to the row, editable on the page or by hand, committed either way), **search**, a **wiki** from numbered markdown pages, a **paste bucket**.
- **Terminal on the phone**: ttyd attaches to one pane by URL; the hub adds a viewport meta (without it the page is unreadably tiny), font zoom, a key row (newline, arrows, page up/down, Ctrl/Alt/Esc behind a "more" button), and one-finger drags turned into wheel events so the transcript scrolls under your thumb. Don't CSS-transform the iframe or resize it on a key press; Firefox on Android blanks the page.
- **Deploy**: a webhook from the local Gitea pulls the site repo on push.

Everything the hub writes is a file. There is no database.

## The house rules

Each one exists because something went wrong without it.

1. **One home per agent.** Four sessions once shared a home directory and "resume the last conversation" became a lottery.
2. **Every agent runs without permission prompts.** In ask mode a classifier blocks infrastructure work at random; the coordinator could not launch a sibling tab.
3. **Separation of duties.** The coordinator wires seats and routes work; it does not do a specialist's job. Route it, don't absorb it.
4. **Pipelines wake their owner.** `OnFailure=` points at a handler that files an inbox note and prompts the owning seat. A sync failed silently for five days before this rule.
5. **Done means documented.** Commit, push, update the beacon. Files are the truth, not the screen.
6. **Economy.** The expensive model for judgement; bulk work goes to cheap models through [omp](https://github.com/can1357/oh-my-pi) at a hundredth of the price. The beacon says which engine sits in each seat.
7. **Credentials live in one file with mode 600**, never in a repo. A hub page can collect a token straight into that directory.
8. **Publishing has a gate.** A publisher seat stages a fresh tree, scans it, rewrites the README for a stranger, notes what was removed, and pushes on an explicit OK. This repo went through it.

## Today

The infrastructure group keeps the box coherent. The chess group pulled every online game I have played into a database, ran an engine over them, and wrote [a first coaching report](https://davidjrb.github.io/chess/6sep2026/). A couple of personal groups I won't go into.

One VM, a few hundred lines of Python and bash, a git repo of markdown, and herdr. The agents do the work; the setup makes sure someone is always in the seat, reachable from a phone, and that nothing that happens is lost.

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
