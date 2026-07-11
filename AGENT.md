# Agent setup guide

You are a coding agent setting up the **Slack ↔ Claude Code bridge** from
scratch on this machine. The human has already done (or should have done) the
browser-and-secrets part described in [README.md](README.md): created a Slack
app, obtained tokens, created a channel, and cloned this repo. Your job is
everything else. Follow this guide top to bottom.

## What you are setting up

`bridge.py` is a long-running daemon. It watches **one** Slack channel via
Socket Mode; every human message posted there is run as a prompt through the
local `claude` CLI (`claude -p ... --resume <session>`), and the reply is
posted back to Slack. It keeps a persistent conversation (session id stored in
`.bridge_state.json`, created automatically), streams live progress into an
in-place-updated Slack message, and processes prompts strictly one at a time.

In-channel commands: `!new` (fresh session), `!model <name>`, `!status`,
`!help`. Anything else goes to Claude.

`notify.py` posts a one-off message to the same channel (`python notify.py
"text"`). `watch.sh` uses it to report job completion/crashes to Slack. Both
also append to `events.jsonl` next to the bridge; the bridge prepends unseen
entries to Claude's next prompt (tracking a byte offset in
`.bridge_state.json`), so the Claude session learns about notifications it
didn't send itself. `watch.sh` keeps job logs under `logs/` and includes the
path in its messages so Claude can read the full log on request.

## Step 1 — Interview the user about the configuration

Configuration lives in `.env` next to `bridge.py`. If `.env` does not exist,
create it: `cp .env.example .env`.

Then go through the variables below **with the user** (ask in chat, or with
your question tool if you have one). For each variable:

- **If it is empty / still a placeholder** (`xoxb-...`, `xapp-...`), ask the
  user for the value.
- **If it already has a value, do not silently trust it.** Show it back to the
  user (for the two tokens, show only the first ~12 characters), ask whether
  it is correct for *this* machine, and update it if they say otherwise.
  Values may have been copied from another machine.

| Variable | What to ask / check |
|---|---|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token, starts with `xoxb-`. From the Slack app the user created **for this machine** (README step 1). |
| `SLACK_APP_TOKEN` | App-level token, starts with `xapp-`. Same app. |
| `SLACK_CHANNEL_ID` | Channel ID, starts with `C`. **Must be a channel dedicated to this machine** — each bridge instance owns one channel, and each machine has its own Slack app (Socket Mode drops events on shared apps). If the value looks like it came from another machine, confirm explicitly. The bot must be invited to the channel. |
| `CLUSTER_NAME` | Short label for this machine (shown in `!status`/`!help`), e.g. hostname or "gpu-153". Suggest the hostname as a default. |
| `CLAUDE_WORKDIR` | Absolute path of the project directory Claude should work in. Must exist — `bridge.py` refuses to start otherwise. Verify with the user, then check the directory exists. |
| `CLAUDE_BIN` | Usually `claude`. Set to the absolute path (`which claude` in a login shell) if `claude` is not on PATH for non-login shells — required for systemd. |
| `ALLOWED_USERS` | Comma-separated Slack member IDs (`U...`) allowed to use the bridge — at minimum the user's own ID. **Warn the user and get explicit confirmation before leaving this empty**: empty means anyone in the channel can run code on this machine. |
| `PERMISSION_MODE` | `acceptEdits` (safer default: Claude edits files but can't run arbitrary commands) or `bypassPermissions` (full autonomy). Explain the trade-off and let the user choose. |
| `REPLY_IN_THREAD` | `true` = replies in a thread under each prompt; `false` = directly in the channel. Default `true`. |
| `STARTUP_MESSAGE` | `true` (default) posts a short usage/status message to the channel whenever the bridge (re)connects. |
| `CLAUDE_TIMEOUT_SECONDS` | Max seconds per Claude turn (default 1800). Raise for long-running tasks. |
| `CLAUDE_EXTRA_ARGS` | Extra `claude` CLI flags, e.g. a tool allow-list. Usually empty. |

Do not print full token values back to the user in chat, and never write them
anywhere except `.env`.

## Step 2 — Verify prerequisites

1. **Python 3.10+** (`bridge.py` uses `str | None` syntax): `python3 --version`.
2. **Claude Code CLI** installed and **authenticated**:
   - `claude --version` must work (else fix `CLAUDE_BIN` or ask the user to install it).
   - Verify auth non-interactively: `claude -p "say ok" --output-format json`
     should return a result, not an auth error. If not authenticated, the
     *user* must run `claude` interactively once (or `claude setup-token`) —
     you cannot complete OAuth yourself; stop and ask.
3. `CLAUDE_WORKDIR` exists and is a directory.

## Step 3 — Install dependencies

```bash
cd <repo>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Step 4 — Smoke test

1. One-off Slack post:
   `.venv/bin/python notify.py "bridge setup test from <CLUSTER_NAME>"` —
   should print `sent` and appear in the channel (ask the user to confirm).
   - `invalid_auth` → bad bot token.
   - `channel_not_found` / `not_in_channel` → wrong channel ID, or the bot
     was not invited (`/invite @<bot>` in the channel).
2. Start the bridge in the foreground: `.venv/bin/python bridge.py` —
   expect `Bridge for '<name>' online.` and no traceback. An immediate
   `SocketModeHandler` auth failure → bad `xapp-` token or Socket Mode not
   enabled on the app.
3. Ask the user to post `!status` in the channel (the bridge ignores bot
   messages, so a human must send it). It should reply with
   cluster/workdir/session info. Then a trivial prompt like
   `what directory are you in?` should get 👀 → ⏳ reactions, a live progress
   message, and a reply.

## Step 5 — Run persistently

Stop the foreground test first, then keep the bridge alive across logouts.
Prefer what the machine already uses; the simplest portable option is tmux:

```bash
tmux new-session -d -s slack-bridge 'cd <repo> && .venv/bin/python bridge.py'
```

Or a systemd user service (`~/.config/systemd/user/slack-bridge.service`):

```ini
[Unit]
Description=Slack-Claude Code bridge
After=network-online.target

[Service]
WorkingDirectory=<repo>
ExecStart=<repo>/.venv/bin/python <repo>/bridge.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now slack-bridge
loginctl enable-linger $USER   # keep it running after logout
```

Note: under systemd, PATH is minimal — `CLAUDE_BIN` in `.env` must be the
absolute path of `claude`.

## Step 6 — Report back

Tell the user: how the bridge is being kept alive (tmux session name or
systemd unit), how to restart it, the channel it listens to, the permission
mode in effect, and that `!help` in the channel shows usage.

## Gotchas

- **Never run two bridge instances against the same channel** (e.g. a
  forgotten tmux copy plus a systemd unit) — every message gets processed
  twice. Check with `pgrep -af bridge.py` before starting a second one.
- **One Slack app per machine.** Socket Mode delivers each event to only one
  open connection per app; machines sharing an app silently drop messages.
- `.env` contains live secrets; keep the repo directory private
  (`chmod 700 <repo>` is reasonable) and never commit `.env` (it is
  git-ignored).
- `.bridge_state.json` holds the current session id and model override.
  Deleting it (or `!new` in Slack) simply starts a fresh conversation.
- The bridge runs Claude with `PERMISSION_MODE` inside `CLAUDE_WORKDIR`; with
  `bypassPermissions`, everyone in `ALLOWED_USERS` effectively has shell
  access to this machine.
- If prompts time out on long tasks, raise `CLAUDE_TIMEOUT_SECONDS`.
