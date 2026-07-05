"""Telegram slash commands for agent_bot."""
from __future__ import annotations

import re
import shutil
import subprocess
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# Only match /word at start — not /newbot
_COMMAND_RE = re.compile(r"^/([a-zA-Z][a-zA-Z0-9_]*)((?:@[\w]+)?(?:\s+(.*))?|\s*)$", re.DOTALL)

SUMMARIZE_PROMPT = (
    "Summarize our conversation so far for the user. "
    "Use concise bullet points: main topics, decisions, open items, and any files touched. "
    "Do not start new work."
)

_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

KNOWN_COMMANDS = frozenset({"new", "summarize", "help", "status", "resume", "model"})

_MODEL_NAME_RE = re.compile(r"^[\w.-]{1,64}$")

# Tier / speed suffixes to drop from the short list (still valid via /model <slug>).
_SKIP_SLUG_PARTS = ("-fast", "-low", "-medium", "-none", "-mini")

_PROVIDER_ORDER = (
    "cursor",
    "openai",
    "anthropic",
    "google",
    "xai",
    "moonshot",
    "zhipu",
    "other",
)

_FAMILY_ORDER: Dict[str, tuple[str, ...]] = {
    "cursor": ("auto", "composer"),
    "openai": ("gpt", "codex", "nano"),
    "anthropic": ("opus", "sonnet", "fable", "haiku", "claude"),
    "google": ("pro", "flash", "flash-lite", "gemini"),
    "xai": ("default",),
    "moonshot": ("default",),
    "zhipu": ("default",),
    "other": ("default",),
}

HELP_TEXT = """Commands:
/new — start a fresh Cursor agent session
/new <prompt> — new session, then run <prompt>
/resume <session-id> — switch back to a previous session
/summarize — summarize the current conversation (read-only)
/status — show current session id
/model — show current model and latest per provider
/model all — full model list
/model <slug> — set model (e.g. auto, gpt-5.2)
/help — this message"""


def create_chat_session(repo_root: str) -> str:
    # create-chat is a subcommand on the `agent` binary, not a prompt to `cursor agent`
    agent_bin = shutil.which("agent") or "agent"
    out = subprocess.run(
        [agent_bin, "create-chat"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "create-chat failed")
    session_id = out.stdout.strip()
    if not session_id:
        raise RuntimeError("create-chat returned empty id")
    return session_id


def list_available_models(repo_root: str) -> str:
    agent_bin = shutil.which("agent") or "agent"
    out = subprocess.run(
        [agent_bin, "models"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "agent models failed")
    text = out.stdout.strip()
    if not text:
        raise RuntimeError("agent models returned empty list")
    return text


def parse_models_output(raw: str) -> Dict[str, str]:
    """Parse `agent models` lines into {slug: label}."""
    models: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("Available models") or line.startswith("Tip:"):
            continue
        if " - " not in line:
            continue
        slug, label = line.split(" - ", 1)
        slug = slug.strip()
        label = re.sub(r"\s*\(current\)\s*", "", label.strip())
        if slug:
            models[slug] = label
    return models


def _slug_is_shortlist_candidate(slug: str) -> bool:
    return slug == "auto" or not any(part in slug for part in _SKIP_SLUG_PARTS)


def _model_provider(slug: str) -> str:
    if slug == "auto" or slug.startswith("composer"):
        return "cursor"
    if slug.startswith("gpt-"):
        return "openai"
    if slug.startswith("claude-"):
        return "anthropic"
    if slug.startswith("gemini-"):
        return "google"
    if slug.startswith("grok"):
        return "xai"
    if slug.startswith("kimi-"):
        return "moonshot"
    if slug.startswith("glm-"):
        return "zhipu"
    return "other"


def _model_family(slug: str, provider: str) -> str:
    if slug == "auto":
        return "auto"
    if provider == "cursor":
        return "composer"
    if provider == "openai":
        if "codex" in slug:
            return "codex"
        if "nano" in slug:
            return "nano"
        return "gpt"
    if provider == "anthropic":
        for line in ("opus", "sonnet", "fable", "haiku"):
            if line in slug:
                return line
        return "claude"
    if provider == "google":
        if "lite" in slug:
            return "flash-lite"
        if "flash" in slug:
            return "flash"
        if "pro" in slug:
            return "pro"
        return "gemini"
    return "default"


def _version_key(slug: str) -> tuple[int, ...]:
    """Best-effort numeric version from slug (higher = newer)."""
    semvers: List[tuple[int, ...]] = []
    for m in re.finditer(r"(\d+)\.(\d+)(?:\.(\d+))?", slug):
        parts = [int(m.group(1)), int(m.group(2))]
        if m.group(3):
            parts.append(int(m.group(3)))
        semvers.append(tuple(parts))
    if semvers:
        return max(semvers)
    hy = re.search(r"(\d+)-(\d+)", slug)
    if hy:
        return (int(hy.group(1)), int(hy.group(2)))
    nums = [int(x) for x in re.findall(r"\d+", slug)]
    return tuple(nums) if nums else (0,)


def _tier_key(slug: str) -> tuple[int, int]:
    """Higher = stronger tier; prefer non-thinking over thinking for general picks."""
    if "thinking-max" in slug:
        return (100, 1)
    if "-max" in slug and "-max-low" not in slug and "-max-medium" not in slug:
        return (95, 1)
    if "extra-high" in slug or "-xhigh" in slug:
        return (85, 0)
    if "thinking-high" in slug:
        return (82, 1)
    if slug.endswith("-high") or "-high" in slug:
        return (80, 0)
    if "thinking" in slug:
        return (70, 1)
    return (50, 0)


def select_recommended_models(models: Dict[str, str]) -> List[str]:
    """Pick the newest top-tier slug per provider family from the account list."""
    best: Dict[tuple[str, str], tuple[tuple[int, ...], tuple[int, int], str]] = {}
    for slug in models:
        if not _slug_is_shortlist_candidate(slug):
            continue
        provider = _model_provider(slug)
        family = _model_family(slug, provider)
        key = (provider, family)
        rank = (_version_key(slug), _tier_key(slug), slug)
        if key not in best or rank > best[key]:
            best[key] = rank
    ordered: List[str] = []
    for provider in _PROVIDER_ORDER:
        for family in _FAMILY_ORDER.get(provider, ("default",)):
            entry = best.get((provider, family))
            if entry:
                ordered.append(entry[2])
    return ordered


def format_short_model_list(models: Dict[str, str]) -> str:
    """Compact list: latest top-tier model per provider family."""
    slugs = select_recommended_models(models)
    lines = [f"{slug} — {models[slug]}" for slug in slugs]
    footer = f"\n\nSend /model all for full list ({len(models)} models)."
    return "Latest per provider:\n" + "\n".join(lines) + footer


def read_model(model_file: str, default: str) -> str:
    if model_file and os.path.isfile(model_file):
        try:
            with open(model_file) as f:
                model = f.read().strip()
                if model:
                    return model
        except OSError:
            pass
    return default


def write_model(model_file: str, model: str) -> None:
    with open(model_file, "w") as f:
        f.write(model)


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
    model_file: str = "",
    default_model: str = "Auto",
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
                send_message(token, chat_id, f"Session: {sid}", use_rich=False)
            else:
                send_message(token, chat_id, "No active session. Send /new to start.", use_rich=False)
        elif name == "resume":
            session_arg = args.split()[0] if args else ""
            if not session_arg:
                send_message(token, chat_id, "Usage: /resume <session-id>", use_rich=False)
            elif not _SESSION_ID_RE.match(session_arg):
                send_message(
                    token,
                    chat_id,
                    "Invalid session id. Paste the full UUID (from /status or bot logs).",
                    use_rich=False,
                )
            else:
                sid = session_arg
                with open(session_file, "w") as f:
                    f.write(sid)
                send_message(token, chat_id, f"Resumed session: {sid}", use_rich=False)
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
        elif name == "model":
            arg = args.strip()
            arg_lower = arg.lower()
            if arg_lower in ("all", "full"):
                current = read_model(model_file, default_model)
                try:
                    available = list_available_models(repo_root)
                    text = f"Current model: {current}\n\n{available}"
                except Exception as e:
                    text = f"Current model: {current}\n\n(Could not list models: {e})"
                send_message(token, chat_id, text, use_rich=False)
            elif arg:
                model_name = arg
                if not _MODEL_NAME_RE.match(model_name):
                    send_message(
                        token,
                        chat_id,
                        "Invalid model name. Use letters, numbers, dots, hyphens, underscores (max 64 chars).",
                        use_rich=False,
                    )
                elif not model_file:
                    send_message(token, chat_id, "Model file not configured.", use_rich=False)
                else:
                    write_model(model_file, model_name)
                    send_message(token, chat_id, f"Model set to: {model_name}", use_rich=False)
            else:
                current = read_model(model_file, default_model)
                try:
                    raw = list_available_models(repo_root)
                    models = parse_models_output(raw)
                    text = f"Current model: {current}\n\n{format_short_model_list(models)}"
                except Exception as e:
                    text = f"Current model: {current}\n\n(Could not list models: {e})"
                send_message(token, chat_id, text, use_rich=False)

    return CommandResult(session_id=sid, agent_prompt=agent_prompt, handled=handled)
