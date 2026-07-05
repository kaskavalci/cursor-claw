"""Telegram slash commands for agent_bot."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# Only match /word at start — not /newbot
_COMMAND_RE = re.compile(r"^/([a-zA-Z][a-zA-Z0-9_]*)((?:@[\w]+)?(?:\s+(.*))?|\s*)$", re.DOTALL)

SUMMARIZE_PROMPT = (
    "Summarize our conversation so far for the user. "
    "Use concise bullet points: main topics, decisions, open items, and any files touched. "
    "Do not start new work."
)

KNOWN_COMMANDS = frozenset({"new", "summarize", "help", "status"})


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
