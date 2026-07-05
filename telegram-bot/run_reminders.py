#!/usr/bin/env python3
"""
Check reminders.json for due reminders and send them via Telegram.
If a reminder has "prompt", run the Cursor agent with that prompt and send its
reply; otherwise send "text" as a fixed message. Uses the same config and
chat_id as agent_bot.py (chat_id is written by the bot when you message it).
"""

import os
import sys
import json
import urllib.request
from datetime import datetime

from cursor_sdk import Agent, LocalAgentOptions, LocalSendOptions, SendOptions

import config_loader

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CHAT_ID_FILE = os.path.join(SCRIPT_DIR, "chat_id")
REMINDERS_FILE = os.path.join(SCRIPT_DIR, "reminders.json")
BASE = "https://api.telegram.org/bot"

REMINDER_INSTRUCTION = (
    " [Your reply will be sent to the user on Telegram. "
    "Do not run any script or command that sends a Telegram message yourself—just output the message content in your reply.]"
)


def load_config():
    return config_loader.load_reminder_config()


def load_reminders():
    if not os.path.isfile(REMINDERS_FILE):
        return []
    try:
        with open(REMINDERS_FILE) as f:
            data = json.load(f)
        return data.get("reminders", data) if isinstance(data, dict) else data
    except (json.JSONDecodeError, OSError):
        return []


def save_reminders(reminders):
    with open(REMINDERS_FILE, "w") as f:
        json.dump({"reminders": reminders}, f, indent=2)


def send_message(token, chat_id, text):
    url = f"{BASE}{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def run_agent_prompt(prompt):
    """Run Cursor agent with the given prompt (no session). Return response text or error string."""
    if not (prompt or "").strip():
        return "(empty prompt)"
    full_prompt = (prompt.strip() + REMINDER_INSTRUCTION).strip()
    timeout_sec = config_loader.get_agent_timeout()
    model = config_loader.load_model()
    agent = None
    try:
        agent = Agent.create(
            model=model,
            local=LocalAgentOptions(cwd=REPO_ROOT),
        )
        run = agent.send(
            full_prompt,
            SendOptions(local=LocalSendOptions(force=True), model=model),
        )
        if timeout_sec:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run.text)
                try:
                    result = future.result(timeout=timeout_sec)
                except concurrent.futures.TimeoutError:
                    try:
                        run.cancel()
                    except Exception:
                        pass
                    return "Agent timed out after %d seconds." % timeout_sec
        else:
            result = run.text()
        return (result or "").strip() or "(no output)"
    except Exception as e:
        return "Error running agent: %s" % e
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass


def main():
    config_loader.ensure_cursor_api_key()
    token, chat_id = load_config()
    if not token:
        print("run_reminders: no token", file=sys.stderr)
        sys.exit(0)
    if chat_id is None:
        print("run_reminders: no chat_id (message the bot once)", file=sys.stderr)
        sys.exit(0)
    now = datetime.now()
    reminders = load_reminders()
    due = []
    remaining = []
    for r in reminders:
        if not isinstance(r, dict):
            remaining.append(r)
            continue
        at_str = r.get("at")
        if not at_str:
            remaining.append(r)
            continue
        try:
            at = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
            if at.tzinfo:
                at = at.astimezone().replace(tzinfo=None)
        except (ValueError, TypeError):
            remaining.append(r)
            continue
        if at <= now:
            due.append(r)
        else:
            remaining.append(r)
    if due:
        save_reminders(remaining)
    for r in due:
        try:
            if r.get("prompt"):
                body = run_agent_prompt(r["prompt"])
                send_message(token, chat_id, "⏰ " + body)
                print("Sent prompt reminder (%d chars)" % len(body), file=sys.stderr)
            else:
                text = r.get("text") or "(reminder)"
                send_message(token, chat_id, "⏰ " + text)
                print("Sent text reminder: %s" % text[:50], file=sys.stderr)
        except Exception as e:
            print("Failed to send reminder: %s" % e, file=sys.stderr)


if __name__ == "__main__":
    main()
