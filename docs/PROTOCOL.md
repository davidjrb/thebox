# The seats protocol

The site repo is checked out at `/srv/www`. Caddy serves it; the hub reads it to build the home page.
**If it is in this repo, it is on the home page.** Push to publish (a git webhook pulls within seconds;
the hub also pulls every 10 minutes).

## Layout

```
README.md                      this file
board.md                       the board (items with their notes attached) — the hub edits this
<seat>/SEAT.md                 the seat's charter: what it is for, standing orders, where its files are
<seat>/seat.json               the seat's beacon (see below)
<seat>/<page>/index.html       a page the seat produced
<seat>/<page>/page.json        the page's manifest — pages without one are listed as UNFILED
wiki/NN-topic.md               the wiki: numbered markdown pages, one topic each
```

URLs: `/<seat>/<page>/` for pages, `/<seat>/` for the seat card.
Top-level names `send inbox board search api static healthz` are reserved for the hub.

## A seat

A seat is a standing role that outlives whichever process currently occupies it. **Seat `X` lives in
`~/X`** (engines key resume, memory and transcripts on the working directory), its herdr workspace is
labelled `X` (agent name: lowercase `x`), its inbox is `~/inbox/X/`. `seat X` in a shell focuses the
workspace or creates it and starts the seat's engine. Attach to everything at once with `herdr`.

Rules:

* **A seat is named for its mission, not its engine.** Renaming is one command, `seat rename <old> <new>`:
  it stops an idle agent, moves home/inbox/transcripts (leaving a `~/<old>` compat symlink), relabels the
  herdr workspace, git-mv's the record here and pushes, and restarts the engine resumed. Only the prose in
  SEAT.md is manual.
* **Any engine can occupy a seat**: Claude Code, omp with any model, plain bash. Say which in `seat.json`
  `kind`, and put the launch command in `cmd` so `seat X` can start it; without `cmd`, `seat X` starts
  Claude Code.
* **One home per agent.** Never point two agents at one directory, and don't run standing sessions from `~`.
  If a session must start life in `~`, record its transcript id in the charter so it can be moved with
  `SEAT_ARGS="--resume <id>" seat X`.
* **A seat may hold several tabs.** Each tab is one agent in its own subdirectory of the seat's home
  (`~/X/<tab>`), listed under `tabs` in `seat.json` (`name, agent, cwd, kind, cmd, resume`). Agent name =
  the tab name, lowercase. Single-tab seats keep agent = lowercase seat name.
* **Every agent runs without permission prompts.** An agent left in ask mode is gated by a permission
  classifier that blocks infrastructure work non-deterministically.
* **No ghosts.** A herdr workspace is either a seat (charter + `seat.json` here) or ephemeral: close it
  when the experiment ends. The home page shows unfiled workspaces with a red dot; red dots get filed or
  closed, not left to accumulate.
* **Separation of duties.** Each seat owns its workload; the coordinator does not do it for them. The
  coordinator stands up seats, wires plumbing, keeps the box coherent and escalates. When something in a
  domain breaks, it goes to *that seat*, not to whoever noticed. Route it, don't absorb it.
* **Workloads self-heal by routing, not by silence.** A timer or service a seat owns should, on failure,
  wake that seat rather than fail quietly: `OnFailure=` → a handler that files an inbox note **and**
  prompts the owning tab (`bin/seat-alert`).

The home page shows every seat with a live dot coloured by herdr's view of its agent:

* green — idle/done · blue (pulsing) — working · red (pulsing) — **blocked**, waiting on an approval or question
* yellow — a pane is running but herdr can't classify the agent
* grey — seat record exists, no workspace running
* red card — a herdr workspace exists with **no** seat directory here (file it or close it)

`seat.json` (all keys optional except that the file must exist):

```json
{
  "title": "one line: what this seat is",
  "kind": "claude-code | omp | shell | …",
  "cmd": "omp --yolo --model … (what `seat X` starts; omit for Claude Code)",
  "status": "one line: what it is doing right now (the beacon)",
  "updated": "2026-09-06",
  "repos": ["org/…"],
  "tabs": [{ "name": "coach", "agent": "coach", "cwd": "~/Chess/coach", "kind": "claude-code", "resume": "<transcript id>" }]
}
```

Update `status`/`updated` when your state changes and push: that *is* the status report.

## A page

Drop `index.html` in `<seat>/<page>/` and add `page.json`:

```json
{ "title": "First look", "description": "one line for the index", "date": "2026-09-06", "tags": ["report"] }
```

Pages must be self-contained (inline CSS/JS, or assets in the same directory). They are static;
anything that must *write* goes through the hub's API instead of a page-specific backend.

## The board (`board.md`)

Plain markdown, one `##` heading per item, notes as a list *directly under the item*:

```markdown
## [ ] Order the part  #hardware  (2026-09-06)
- 2026-09-06 14:02 — called supplier, quote pending

## [x] Enable HTTPS on the tailnet  #infra  (2026-09-06)
- 2026-09-06 03:30 — done
```

Edit it by hand or on the hub's board page; either way the hub commits it.

## Inbox (not in this repo)

Messages sent from the hub land in `~/inbox/<seat>/<time>_<slug>/note.md` with attachments alongside,
plus `~/inbox/<seat>/INBOX.md` as an index. When a message is handled: `touch READ` in its directory.
The herdr nudge (an `agent prompt` naming the note) is only a doorbell; the file is the message.
