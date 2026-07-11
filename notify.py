#!/usr/bin/env python3
"""Post a one-off message to the bridge's Slack channel.

Usage: python notify.py "some text"
Useful from cron jobs / training scripts to ping yourself.

Every message is also appended to events.jsonl next to this script; the
bridge replays unseen entries to Claude at the start of its next turn, so
the Claude session knows about notifications it didn't send itself.
"""
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

text = sys.argv[1]

# Journal first: even if the Slack post fails, Claude should learn about it.
with (HERE / "events.jsonl").open("a") as f:
    f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "text": text}) + "\n")

WebClient(token=os.environ["SLACK_BOT_TOKEN"]).chat_postMessage(
    channel=os.environ["SLACK_CHANNEL_ID"], text=text)
print("sent")
