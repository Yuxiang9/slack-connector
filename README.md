# slack-connector

A Slack ↔ [Claude Code](https://claude.com/claude-code) bridge. Run one copy
on each machine you want to control; each copy watches **one** Slack channel
and runs every message you post there as a prompt through the local `claude`
CLI, replying in the channel with live progress (thinking, tool calls) and the
final answer. The conversation persists across messages and restarts, so you
can drive long-running work — training runs, evals, debugging — from your
phone.

```
you (Slack, anywhere) ──▶ #my-gpu-box channel ──▶ bridge.py (on the machine) ──▶ claude CLI
                                   ◀── live progress + replies ◀──
```

**In-channel commands:** `!new` (fresh session) · `!model <name>` · `!status` · `!help`.
Anything else is sent to Claude Code.

**Also included:**

- `notify.py` — one-liner to post a message to the channel from scripts/cron:
  `python notify.py "training done"`.
- `watch.sh` — wrap or attach to a long-running job and get STARTED /
  COMPLETED / ANOMALY notifications in Slack (detects tracebacks, OOM, NaN
  losses, crashed tmux sessions).

Notifications sent through `notify.py`/`watch.sh` are also journaled to
`events.jsonl`, and the bridge replays unseen entries to Claude at the start
of its next turn — so you can ask "how did the training go?" and Claude knows
what the watchdog reported (including the log file path, which it can read).

## Security — read this first

Whoever can post in the bridged channel can execute commands **as your user on
your machine** (subject to `PERMISSION_MODE`). Mitigations built in:

- `ALLOWED_USERS` restricts the bridge to listed Slack member IDs — always set it.
- Use a private channel.
- `PERMISSION_MODE=acceptEdits` (default) lets Claude edit files but not run
  arbitrary commands; `bypassPermissions` gives it full autonomy — opt in
  deliberately.
- `.env` (your tokens) is git-ignored; never commit it.

## Human setup (one time per machine, ~10 minutes)

The setup is split between you and a Claude Code agent. **You** do the parts
that need a browser and secrets (this section); the **agent** does the rest by
following [AGENT.md](AGENT.md).

> **One Slack app per machine.** Socket Mode delivers each event to only one
> open connection per app, so if two machines share an app, they silently
> steal messages from each other. Repeat step 1 for every machine.

### 1. Create the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App**
   → **From a manifest** → pick your workspace → paste the manifest below
   (change both `name` fields to something machine-specific, e.g.
   `claude-gpu153`) → **Create**.

   ```yaml
   display_information:
     name: claude-bridge
   features:
     bot_user:
       display_name: claude-bridge
       always_online: true
   oauth_config:
     scopes:
       bot:
         - chat:write
         - reactions:write
         - channels:history
         - groups:history
         - app_mentions:read
   settings:
     event_subscriptions:
       bot_events:
         - message.channels
         - message.groups
         - app_mention
     socket_mode_enabled: true
   ```

2. **App-level token:** Basic Information → *App-Level Tokens* → Generate,
   with scope `connections:write`. Copy the `xapp-...` token.
3. **Install & bot token:** Install App → *Install to Workspace* → allow.
   Copy the **Bot User OAuth Token** (`xoxb-...`) from OAuth & Permissions.

### 2. Create the channel

1. Create a channel for this machine (e.g. `#claude-gpu153`; private
   recommended) and `/invite @<your-bot>` into it.
2. Copy the **channel ID**: channel name → *About* tab → bottom of the panel
   (starts with `C`).
3. Copy your own **member ID**: your Slack profile → `...` → *Copy member ID*
   (starts with `U`).

### 3. Put the repo and tokens on the machine

```bash
git clone <this-repo> && cd slack-connector
cp .env.example .env
```

Edit `.env` and paste `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`,
`SLACK_CHANNEL_ID`, and `ALLOWED_USERS` (your member ID). You can fill in the
rest too, or leave it for the agent to ask about.

Also make sure Claude Code itself is installed and logged in on the machine
(`claude` runs interactively) — the agent can verify but not log in for you.

### 4. Hand off to the agent

Start Claude Code in the repo and say:

> Read AGENT.md and set up the bridge on this machine.

The agent will confirm/complete your `.env`, create the virtualenv, smoke-test
Slack connectivity, start the bridge persistently, and tell you what to post
in the channel to verify. After that, everything works from Slack.

## Requirements

- Python 3.10+
- [Claude Code CLI](https://claude.com/claude-code), authenticated
- A Slack workspace where you can create apps
- Linux/macOS with tmux or systemd (for keeping the bridge running)
