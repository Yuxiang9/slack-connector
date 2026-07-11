#!/usr/bin/env python3
"""
Slack <-> Claude Code bridge.

Run one copy of this script on each cluster. Each copy watches ONE Slack
channel and executes Claude Code locally on that cluster, keeping a
persistent conversation per channel.

Configuration is read from environment variables (or a .env file next to
this script). See .env.example.

In-channel commands:
  !new           start a fresh Claude session (forgets prior context)
  !model <name>  switch model (e.g. opus, sonnet); !model default reverts
  !status        show cluster name, workdir, session id, queue length
  !help          show this help
Anything else is sent to Claude Code as a prompt.
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent / ".env")

BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]          # xoxb-...
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]          # xapp-... (Socket Mode)
CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]        # e.g. C0123456789
CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "cluster")
WORKDIR = Path(os.environ.get("CLAUDE_WORKDIR", str(Path.home()))).expanduser()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
PERMISSION_MODE = os.environ.get("PERMISSION_MODE", "acceptEdits")
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "1800"))
EXTRA_ARGS = os.environ.get("CLAUDE_EXTRA_ARGS", "").split()
# Comma-separated Slack user IDs allowed to talk to the bridge.
# Empty = anyone in the channel (NOT recommended for shared workspaces).
ALLOWED_USERS = {u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()}
# true = reply in a thread under the prompt; false = reply directly in channel
REPLY_IN_THREAD = os.environ.get("REPLY_IN_THREAD", "true").lower() in ("1", "true", "yes")
# Post a short usage message to the channel when the bridge connects.
STARTUP_MESSAGE = os.environ.get("STARTUP_MESSAGE", "true").lower() in ("1", "true", "yes")
STATE_FILE = Path(os.environ.get("STATE_FILE",
                                 Path(__file__).resolve().parent / ".bridge_state.json"))
# Journal written by notify.py; unseen entries are replayed to Claude at the
# start of its next turn so the session knows about out-of-band notifications.
EVENTS_FILE = Path(os.environ.get("EVENTS_FILE",
                                  Path(__file__).resolve().parent / "events.jsonl"))

SLACK_CHUNK = 3500  # stay well under Slack's message size limit

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bridge")

# --------------------------------------------------------------------------
# Session state (persisted so restarts keep conversation continuity)
# --------------------------------------------------------------------------

_state_lock = threading.Lock()


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    with _state_lock:
        STATE_FILE.write_text(json.dumps(state, indent=2))


state = load_state()  # {"session_id": "...", "model": "...", "events_offset": N}


def pending_events() -> tuple[str, int]:
    """Journal entries Claude hasn't seen yet, as display lines, plus the
    byte offset to record once a turn consumes them."""
    offset = state.get("events_offset", 0)
    try:
        size = EVENTS_FILE.stat().st_size
    except OSError:
        return "", offset
    if offset > size:  # journal was truncated or rotated
        offset = 0
    if offset >= size:
        return "", offset
    with EVENTS_FILE.open() as f:
        f.seek(offset)
        raw = f.read()
    lines = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
            lines.append(f"[{ev.get('time', '?')}] {ev.get('text', '')}")
        except json.JSONDecodeError:
            lines.append(line)
    return "\n".join(lines), size

# --------------------------------------------------------------------------
# Claude Code invocation
# --------------------------------------------------------------------------


def one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def summarize_tool(name: str, tool_input) -> str:
    """Compact human-readable line for a tool call, e.g. 'Bash: nvidia-smi'."""
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "pattern", "url",
                    "query", "description", "prompt", "skill"):
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                return f"{name}: {one_line(v, 150)}"
    return name


def run_claude(prompt: str, on_activity=None) -> tuple[str, str | None]:
    """Run one Claude Code turn, streaming activity as it happens.

    on_activity, if given, is called with a short line for each thinking
    block, tool call, and intermediate message. Returns (reply_text,
    session_id)."""
    cmd = [CLAUDE_BIN, "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", PERMISSION_MODE]
    if state.get("session_id"):
        cmd += ["--resume", state["session_id"]]
    if state.get("model"):
        cmd += ["--model", state["model"]]
    cmd += EXTRA_ARGS

    log.info("Running Claude Code in %s (resume=%s)",
             WORKDIR, state.get("session_id", "none"))
    proc = subprocess.Popen(
        cmd,
        cwd=str(WORKDIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    timed_out = threading.Event()

    def kill_on_timeout():
        timed_out.set()
        proc.kill()

    killer = threading.Timer(TIMEOUT_SECONDS, kill_on_timeout)
    killer.start()

    stderr_chunks: list[str] = []
    stderr_reader = threading.Thread(
        target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    stderr_reader.start()
    result_payload = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "assistant" and on_activity:
                for block in (ev.get("message") or {}).get("content", []):
                    btype = block.get("type")
                    if btype == "thinking":
                        txt = one_line(block.get("thinking") or "", 200)
                        if txt:
                            on_activity(f"_:thought_balloon: {txt}_")
                    elif btype == "text":
                        txt = one_line(block.get("text") or "", 300)
                        if txt:
                            on_activity(f":speech_balloon: {txt}")
                    elif btype == "tool_use":
                        on_activity(":wrench: `" + summarize_tool(
                            block.get("name") or "?", block.get("input")) + "`")
            elif etype == "result":
                result_payload = ev
        proc.wait()
    finally:
        killer.cancel()

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, TIMEOUT_SECONDS)
    if result_payload is None:
        stderr_reader.join(timeout=5)
        err = ("".join(stderr_chunks)).strip()[-2000:]
        raise RuntimeError(
            f"claude exited with code {proc.returncode} and no result:\n{err}")

    reply = result_payload.get("result") or json.dumps(result_payload, indent=2)
    reply += stats_footer(result_payload)
    return reply, result_payload.get("session_id") or state.get("session_id")


def stats_footer(payload: dict) -> str:
    """One-line footer with duration / model / cost, using whichever
    fields this Claude Code version happens to emit."""
    parts = []
    ms = payload.get("duration_ms")
    if isinstance(ms, (int, float)):
        parts.append(f"{ms / 1000:.0f}s" if ms >= 10000 else f"{ms / 1000:.1f}s")
    model = payload.get("model")
    if not model and isinstance(payload.get("modelUsage"), dict):
        models = list(payload["modelUsage"].keys())
        model = ", ".join(models) if models else None
    if model:
        parts.append(str(model))
    cost = payload.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"${cost:.4f}")
    turns = payload.get("num_turns")
    if isinstance(turns, int):
        parts.append(f"{turns} turns")
    return ("\n\n_" + " · ".join(parts) + "_") if parts else ""


# --------------------------------------------------------------------------
# Slack helpers
# --------------------------------------------------------------------------


def chunk(text: str, size: int = SLACK_CHUNK):
    text = text.strip() or "(Claude returned an empty response)"
    for i in range(0, len(text), size):
        yield text[i:i + size]


def post(client, thread_ts: str, text: str):
    for piece in chunk(text):
        client.chat_postMessage(channel=CHANNEL_ID,
                                thread_ts=thread_ts if REPLY_IN_THREAD else None,
                                text=piece)


class Progress:
    """Live activity feed: one Slack message updated in place as Claude
    thinks and runs tools. chat.update is throttled to stay inside
    Slack rate limits."""

    UPDATE_EVERY = 2.0  # seconds
    MAX_CHARS = 3400

    def __init__(self, client, thread_ts: str):
        self.client = client
        self.thread_ts = thread_ts if REPLY_IN_THREAD else None
        self.lines: list[str] = []
        self.ts = None            # ts of the progress message once posted
        self.last_update = 0.0
        self.lock = threading.Lock()

    def add(self, line: str):
        with self.lock:
            self.lines.append(line)
            now = time.monotonic()
            if self.ts is not None and now - self.last_update < self.UPDATE_EVERY:
                return
            self.last_update = now
            self._flush()

    def finish(self, footer: str):
        with self.lock:
            if self.ts is None and not self.lines:
                return
            self.lines.append(footer)
            self._flush()

    def _flush(self):
        text = "\n".join(self.lines)
        if len(text) > self.MAX_CHARS:
            text = "…" + text[-self.MAX_CHARS:]
        try:
            if self.ts is None:
                resp = self.client.chat_postMessage(
                    channel=CHANNEL_ID, thread_ts=self.thread_ts, text=text)
                self.ts = resp["ts"]
            else:
                self.client.chat_update(channel=CHANNEL_ID, ts=self.ts, text=text)
        except Exception:
            log.warning("Progress update failed", exc_info=True)


# --------------------------------------------------------------------------
# Worker: process messages strictly one at a time so the session
# never interleaves two prompts.
# --------------------------------------------------------------------------

work_q: "queue.Queue[dict]" = queue.Queue()


def worker(client):
    while True:
        job = work_q.get()
        thread_ts = job["ts"]
        try:
            client.reactions_add(channel=CHANNEL_ID, name="hourglass_flowing_sand",
                                 timestamp=thread_ts)
        except Exception:
            pass
        progress = Progress(client, thread_ts)
        events, events_offset = pending_events()
        prompt = job["text"]
        if events:
            prompt = (
                "[Automated notifications posted to this Slack channel since "
                "your last turn (from notify.py/watch.sh — background context, "
                "not written by the user):]\n"
                f"{events}\n\n[User message:]\n{prompt}")
        try:
            reply, sid = run_claude(prompt, on_activity=progress.add)
            dirty = False
            if sid and sid != state.get("session_id"):
                state["session_id"] = sid
                dirty = True
            if events_offset != state.get("events_offset", 0):
                state["events_offset"] = events_offset
                dirty = True
            if dirty:
                save_state(state)
            progress.finish(":white_check_mark: _done — reply below_")
            post(client, thread_ts, reply)
            emoji = "white_check_mark"
        except subprocess.TimeoutExpired:
            progress.finish(":warning: _timed out_")
            post(client, thread_ts,
                 f":warning: Claude Code timed out after {TIMEOUT_SECONDS}s. "
                 "The task may still be incomplete. Consider raising "
                 "CLAUDE_TIMEOUT_SECONDS or breaking the task into steps.")
            emoji = "x"
        except Exception as e:
            log.exception("Claude invocation failed")
            progress.finish(":x: _failed_")
            post(client, thread_ts, f":x: Error running Claude Code:\n```{e}```")
            emoji = "x"
        try:
            client.reactions_remove(channel=CHANNEL_ID, name="hourglass_flowing_sand",
                                    timestamp=thread_ts)
            client.reactions_add(channel=CHANNEL_ID, name=emoji, timestamp=thread_ts)
        except Exception:
            pass
        work_q.task_done()


# --------------------------------------------------------------------------
# Slack event handling
# --------------------------------------------------------------------------

app = App(token=BOT_TOKEN)

HELP_TEXT = (
    f"*Claude Code bridge — `{CLUSTER_NAME}`*\n"
    f"Workdir: `{WORKDIR}`\n\n"
    "Post any message here and it is sent to Claude Code on this cluster.\n"
    "Replies arrive in a thread under your message.\n\n"
    "Commands:\n"
    "• `!new` — start a fresh session (forget prior context)\n"
    "• `!model <name>` — switch model (e.g. `!model opus`, `!model sonnet`, "
    "`!model haiku`); `!model default` reverts to your account default; "
    "`!model` alone shows the current setting\n"
    "• `!status` — show bridge status\n"
    "• `!help` — this message"
)


@app.event("message")
def on_message(event, client):
    # Only our channel, only top-level human messages.
    if event.get("channel") != CHANNEL_ID:
        return
    # Skip bot messages (incl. our own) and edits/joins/etc. — but accept
    # thread replies and "also send to channel" broadcasts as prompts.
    if event.get("bot_id"):
        return
    if event.get("subtype") not in (None, "thread_broadcast"):
        return

    user = event.get("user", "")
    if ALLOWED_USERS and user not in ALLOWED_USERS:
        log.info("Ignoring message from unauthorized user %s", user)
        return

    text = (event.get("text") or "").strip()
    ts = event["ts"]
    if not text:
        return

    # Strip an @mention of the bot if present.
    if text.startswith("<@"):
        text = text.split(">", 1)[-1].strip()

    low = text.lower()
    if low == "!help":
        post(client, ts, HELP_TEXT)
        return
    if low == "!new":
        state.pop("session_id", None)
        save_state(state)
        post(client, ts, ":sparkles: Started a fresh Claude session.")
        return
    if low == "!model" or low.startswith("!model "):
        arg = text[len("!model"):].strip()
        if not arg:
            post(client, ts,
                 f"Current model: `{state.get('model', 'account default')}`\n"
                 "Set one with `!model opus`, `!model sonnet`, `!model haiku`, "
                 "or a full model name. `!model default` reverts.")
        elif arg.lower() in ("default", "reset", "none"):
            state.pop("model", None)
            save_state(state)
            post(client, ts, ":gear: Model reverted to your account default. "
                             "Applies from your next message.")
        else:
            state["model"] = arg
            save_state(state)
            post(client, ts, f":gear: Model set to `{arg}`. "
                             "Applies from your next message.")
        return
    if low == "!status":
        events, _ = pending_events()
        n_events = len(events.splitlines()) if events else 0
        post(client, ts,
             f"Cluster: `{CLUSTER_NAME}`\nWorkdir: `{WORKDIR}`\n"
             f"Session: `{state.get('session_id', 'none (fresh)')}`\n"
             f"Model: `{state.get('model', 'account default')}`\n"
             f"Queued messages: {work_q.qsize()}\n"
             f"Notifications Claude hasn't seen yet: {n_events}\n"
             f"Permission mode: `{PERMISSION_MODE}`")
        return

    log.info("Queueing prompt from %s (%d chars)", user, len(text))
    try:  # instant receipt so queued messages are never silently pending
        client.reactions_add(channel=CHANNEL_ID, name="eyes", timestamp=ts)
    except Exception:
        pass
    work_q.put({"text": text, "ts": ts})


@app.event("app_mention")
def on_mention(event):
    # Handled via the message event; this handler just prevents
    # "unhandled request" warnings.
    pass


# --------------------------------------------------------------------------


def startup_text() -> str:
    sid = state.get("session_id")
    events, _ = pending_events()
    n_events = len(events.splitlines()) if events else 0
    lines = [
        f":large_green_circle: *Claude Code bridge online — `{CLUSTER_NAME}`*",
        f"Anything you post here runs as a Claude Code prompt on this "
        f"machine (workdir `{WORKDIR}`), one message at a time.",
        "",
        "• Session: " + (f"resuming `{sid}`" if sid else "fresh (no prior context)"),
        f"• Model: `{state.get('model', 'account default')}` · "
        f"Permission mode: `{PERMISSION_MODE}`",
    ]
    if n_events:
        lines.append(f"• {n_events} notification(s) queued for Claude — "
                     "delivered with your next message")
    lines += [
        "",
        "Commands: `!new` fresh session · `!model <name>` switch model · "
        "`!status` bridge status · `!help` full help",
        "_Tip: wrap long jobs with `watch.sh run \"label\" <cmd>` — completion/"
        "crash reports land here and Claude sees them too._",
    ]
    return "\n".join(lines)


def main():
    if not WORKDIR.is_dir():
        raise SystemExit(f"CLAUDE_WORKDIR does not exist: {WORKDIR}")
    client = app.client
    threading.Thread(target=worker, args=(client,), daemon=True).start()
    if STARTUP_MESSAGE:
        try:
            client.chat_postMessage(channel=CHANNEL_ID, text=startup_text())
        except Exception:
            log.warning("Could not post startup message", exc_info=True)
    log.info("Bridge for '%s' online. Channel=%s Workdir=%s",
             CLUSTER_NAME, CHANNEL_ID, WORKDIR)
    SocketModeHandler(app, APP_TOKEN).start()


if __name__ == "__main__":
    main()