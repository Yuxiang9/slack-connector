#!/usr/bin/env python3
"""Post a one-off message to the bridge's Slack channel.

Usage: python notify.py "some text"
Useful from cron jobs / training scripts to ping yourself.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv(Path(__file__).resolve().parent / ".env")
WebClient(token=os.environ["SLACK_BOT_TOKEN"]).chat_postMessage(
    channel=os.environ["SLACK_CHANNEL_ID"], text=sys.argv[1])
print("sent")
