# hub — the home page of a box of seats

One small Flask service behind Caddy. Static pages are files in `/srv/www` (a checkout of the *site repo*);
the hub does only what static files can't: build the home page from what's on disk, deliver
messages to seats, render inboxes, edit the board, search, and pull on push.

| URL | what |
|---|---|
| `/` | search box, tools, one card per **work group** (herdr workspace) with **one row per tab** (herdr dot/status, tab, agent, engine, what it's doing, send · inbox · terminal), beacon, unread, pages; recent changes; board preview; **"Where things live"** from herdr's real pane cwds; all pages + unfiled collapsed at the bottom |
| `/<seat>/` | seat page: tab rows (with per-tab "peek"), SEAT.md, seat.json, pages, inbox |
| `/<seat>/<page>/` | static page from `/srv/www` (Caddy serves it; the hub can too) |
| `/send` | prompt + attachments → `~/inbox/<seat>/<time>_<slug>/note.md`, then a one-line herdr nudge (`herdr agent prompt`) |
| `/inbox/`, `/inbox/<seat>/` | every seat's messages; notes rendered; attachments served; mark read/unread |
| `/board` | items with notes attached to the row; writes `board.md` and commits to the site repo |
| `/search?q=` | ripgrep over `/srv/www` and `~/inbox` |
| `/api/sessions`, `/api/status` | JSON for agents (`sessions`: each workspace with its `tabs[]`) |
| `/api/capture?session=&lines=&tab=` | last N lines of a pane (`tab` picks the tab; default: the group's agent pane) |
| `/api/response?session=` | last assistant message of an omp session (breadcrumb-based) |
| `/api/send` | multipart: `session`, `prompt`, `mode` (prompt/ping), `files`, optional `tab` (which tab's agent gets the nudge; the note always lands in the group's inbox) |
| `/api/board` | JSON `{action: add|note|toggle|retitle|delete, …}` |
| `/api/secret` | POST `{name, value}` → `~/.config/hub/<name>` (0600); lets a page collect a token without it touching an inbox note or git. Value is never logged or echoed. **Unauthenticated write**, like `/api/send`: it relies on Caddy's basic auth (`deploy/auth.caddy.example`) being enabled in front of the hub |
| `/api/inbox/<seat>/<msg>/read` | POST; `undo=1` to mark unread |
| `/api/deploy` | gitea webhook (HMAC-SHA256 `X-Gitea-Signature`, or `?token=`) → `git pull --ff-only` |

## Files it owns

- `/srv/www` — checkout of the site repo; the hub commits `board.md` there as user `hub`.
- `~/inbox/<seat>/<YYYYMMDD-HHMMSS>_<slug>/{note.md, attachments…, READ}` and `~/inbox/<seat>/INBOX.md`.
- `~/.config/hub/hub-deploy-secret` — webhook secret (generated on first run).

## Run / deploy

```
sudo cp deploy/hub.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now hub
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy
```
Deps: `python3-flask python3-markdown ripgrep caddy` (Debian 13 packages). Binds `127.0.0.1:8090`;
Caddy fronts it on :80 (and tailnet HTTPS). Env: `HUB_WWW`, `HUB_INBOX`, `HUB_GITEA_URL`, `HUB_WWW_REPO_URL`, `HUB_OWNER` (how you are named in notes and nudges), `HUB_BIND`, `HUB_PORT`.
The unit files in `deploy/` use `USER` as a placeholder for the account that owns the seats; replace it.
ttyd is the upstream static binary (not in Debian 13); herdr from https://herdr.dev.

## Work groups

A seat is a herdr **workspace** (label = seat name) holding one **tab per agent**. herdr is the source of
live structure — `herdr_seats()` joins `workspace list` + `tab list` + `agent list` + `pane list` into
`{label: {status, tabs: [{name, status, agent, kind, pane_id, cwd, title}]}}`. `seat_rows()` matches
those tabs to `tabs` in `seat.json` **by tab name** and only takes standing metadata from the record
(engine `kind`, `cmd`, `resume`, promised `cwd`); a record-only tab (in seat.json, not running) is a grey
row at the end, a single unlabelled tab takes the seat's name. Inbox and alert routing are still per
group (per-tab inbox is future work).

## Browser terminal (`/terminal/?arg=<group>&tab=<tab>`)

ttyd (canvas renderer, `/tty/`) gets every URL `?arg=` as one argv, so the wrapper frames
`/tty/?arg=<group>&arg=<tab>`; `deploy/ttyd-seat.sh` resolves group → tab (label, or number) → pane and
runs `herdr agent attach <pane_id>` (no tab: the group's agent pane; `all`: the full herdr UI). The hub page
adds what phones need: a viewport meta (ttyd ships none — the page is unreadably tiny without it), font
size via ttyd's exposed `window.term`, a key row (⏎ nl, arrows, PgUp/PgDn, and Ctrl/Alt/Esc/… behind ⋯)
driven through `/api/keys` → `herdr pane send-keys` on that tab's pane, and **touch scrolling**: one-finger
drags are turned into wheel events inside xterm.js.

Two things that are easy to get wrong:
- **herdr handles the mouse natively**: wheel goes to the pane app if it asked for the mouse,
  else herdr scrolls pane history itself. (The old tmux `mouse on` requirement is gone.)
- **Claude Code 2.1 runs in the alternate screen** and scrolls its *own* transcript on wheel events;
  the multiplexer keeps almost no history for it, so history scrolling shows nothing. Wheel is the right primitive.
  (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` restores plain-terminal behaviour if ever wanted.)
- Don't CSS-transform the iframe and don't resize it on button presses — Firefox Android's compositor blanks the page.

## Design notes

- **Seats, not processes.** Cards are named after seat directories in `/srv/www`; the herdr workspace
  with the same name is the current occupant, its tabs are the rows. A workspace without a seat shows red.
- **herdr is the truth for what runs, seat.json for what should.** Rows come from herdr; the record only
  decorates them. "Where things live" is drawn from real pane cwds, so a tab started in the wrong
  directory shows up as such instead of being hidden by the `~/<seat>` naming rule.
- **Files are the message.** The pane nudge is a single line that points at `note.md`; if the
  nudge is lost, the message isn't.
- **Nothing orphaned.** Every HTML file under `/srv/www` is listed; ones without `page.json` are
  listed under "unfiled" instead of disappearing.
- No auth — LAN and personal tailnet only. Don't expose :80 further without adding Caddy basic-auth.
