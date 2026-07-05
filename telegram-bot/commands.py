"""Telegram slash commands for agent_bot."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

# Only match /word at start — not /newbot
_COMMAND_RE = re.compile(r"^/([a-zA-Z][a-zA-Z0-9_]*)((?:@[\w]+)?(?:\s+(.*))?|\s*)$", re.DOTALL)

SUMMARIZE_PROMPT = (
    "Summarize our conversation so far for the user. "
    "Use concise bullet points: main topics, decisions, open items, and any files touched. "
    "Do not start new work."
)

KNOWN_COMMANDS = frozenset({"new", "summarize", "help", "status"})

HELP_TEXT = """Commands:
/new — start a fresh Cursor agent session
/new <prompt> — new session, then run <prompt>
/summarize — summarize the current conversation (read-only)
/status — show current session id
/help — this message"""


def create_chat_session(repo_root: str) -> str:
    out = subprocess.run(
        ["cursor", "agent", "create-chat"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "create-chat failed")
    session_id = out.stdout.strip()
    if not session_id:
        raise RuntimeError("create-chat returned empty id")
    return session_id


def parse_telegram_command(text: str) -> Optional[tuple[str, str]]:
    """Return (command_name_lower, args) or None if text is not a slash command."""
    text = (text or "").strip()
    m = _COMMAND_RE.match(text)
    if not m:
        return None
    name = m.group(1).lower()
    if name == "newbot":  # BotFather command, not ours
        return None
    args = (m.group(3) or "").strip()
    return name, args


@dataclass
class CommandResult:
    """Outcome of handling one or more commands in a batch."""
    session_id: Optional[str]  # updated session, or None to leave unchanged
    agent_prompt: Optional[str]  # if set, run agent with this after commands
    handled: bool  # True if at least one command was recognized


def handle_commands(
    batch_texts: List[str],
    *,
    session_id: Optional[str],
    token: str,
    chat_id: int,
    send_message: Callable[..., Any],
    session_file: str = "",
    repo_root: str = ".",
) -> CommandResult:
    agent_prompt: Optional[str] = None
    handled = False
    sid = session_id

    for raw in batch_texts:
        parsed = parse_telegram_command(raw)
        if not parsed:
            if handled:
                continue  # skip non-commands after we've started command processing
            return CommandResult(session_id=sid, agent_prompt=None, handled=False)
        name, args = parsed
        if name not in KNOWN_COMMANDS:
            send_message(token, chat_id, f"Unknown command /{name}. Send /help.", use_rich=False)
            handled = True
            continue

        handled = True
        if name == "help":
            send_message(token, chat_id, HELP_TEXT, use_rich=False)
        elif name == "status":
            if sid:
                send_message(token, chat_id, f"Session: {sid[:8]}…", use_rich=False)
            else:
                send_message(token, chat_id, "No active session. Send /new to start.", use_rich=False)
        elif name == "new":
            try:
                sid = create_chat_session(repo_root)
                with open(session_file, "w") as f:
                    f.write(sid)
                send_message(token, chat_id, "New session started.", use_rich=False)
                if args:
                    agent_prompt = args
            except Exception as e:
                send_message(token, chat_id, f"Could not create session: {e}", use_rich=False)
        elif name == "summarize":
            if not sid:
                send_message(token, chat_id, "No active session. Send /new first.", use_rich=False)
            else:
                agent_prompt = SUMMARIZE_PROMPT

    return CommandResult(session_id=sid, agent_prompt=agent_prompt, handled=handled)
