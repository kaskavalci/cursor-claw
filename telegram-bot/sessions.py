"""Session registry for multi-conversation Telegram bot support."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_SESSIONS = 50
TITLE_MAX = 60
SUMMARY_MAX = 120
CHATS_PAGE_SIZE = 15
CHATS_MAX_CHARS = 3500

_ATTACHMENT_HINT_RE = re.compile(r"\[User sent \d+ .*\]", re.DOTALL)
_SUMMARIZE_TITLE_PREFIX = "Summarize our conversation"
_LEGACY_CLI_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SDK_SESSION_ID_RE = re.compile(r"^(agent|bc)-[0-9a-f-]+$", re.IGNORECASE)


def is_legacy_cli_id(session_id: str) -> bool:
    """True for pre-SDK CLI create-chat UUIDs (not resumable via cursor-sdk)."""
    if not session_id:
        return False
    if _SDK_SESSION_ID_RE.match(session_id):
        return False
    return bool(_LEGACY_CLI_UUID_RE.match(session_id))


def is_sdk_session_id(session_id: str) -> bool:
    return bool(_SDK_SESSION_ID_RE.match(session_id or ""))


def _filter_legacy_sessions(registry: SessionRegistry) -> bool:
    """Remove legacy CLI sessions. Returns True if registry changed."""
    before_count = len(registry.sessions)
    prev_active = registry.active_id
    registry.sessions = [s for s in registry.sessions if not is_legacy_cli_id(s.id)]
    if registry.active_id and is_legacy_cli_id(registry.active_id):
        registry.active_id = registry.sessions[-1].id if registry.sessions else None
        print(
            "Dropped legacy CLI session from registry; send /new to start an SDK session.",
            file=sys.stderr,
        )
    if len(registry.sessions) < before_count:
        print(
            "Removed %d legacy CLI session(s) from registry."
            % (before_count - len(registry.sessions)),
            file=sys.stderr,
        )
    return len(registry.sessions) != before_count or registry.active_id != prev_active


@dataclass
class SessionEntry:
    id: str
    created_at: str
    last_active_at: str
    title: str = "New chat"
    summary: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "last_active_at": self.last_active_at,
            "title": self.title,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionEntry:
        return cls(
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", "")),
            last_active_at=str(data.get("last_active_at", "")),
            title=str(data.get("title") or "New chat"),
            summary=str(data.get("summary") or ""),
        )


@dataclass
class SessionRegistry:
    active_id: Optional[str] = None
    sessions: List[SessionEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_id": self.active_id,
            "sessions": [s.to_dict() for s in self.sessions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SessionRegistry:
        sessions = [SessionEntry.from_dict(s) for s in data.get("sessions") or []]
        active = data.get("active_id")
        return cls(active_id=str(active) if active else None, sessions=sessions)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strip_attachment_hints(text: str) -> str:
    return _ATTACHMENT_HINT_RE.sub("", text).strip()


def _summary_from_assistant(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = re.sub(r"[#*_`>\[\]]+", "", text)
    if not text:
        return ""
    for sep in (". ", "! ", "? ", "\n"):
        idx = text.find(sep)
        if 0 < idx < SUMMARY_MAX:
            text = text[: idx + 1]
            break
    return _truncate(text, SUMMARY_MAX)


def truncate_text(text: str, limit: int) -> str:
    return _truncate(text, limit)


def _is_summarize_prompt(text: str) -> bool:
    return (text or "").startswith(_SUMMARIZE_TITLE_PREFIX)


def load_registry(
    sessions_file: str,
    *,
    session_file: str = "",
) -> SessionRegistry:
    """Load registry; migrate from legacy single-session file if needed."""
    if os.path.isfile(sessions_file):
        try:
            with open(sessions_file) as f:
                data = json.load(f)
            registry = SessionRegistry.from_dict(data)
            changed = _filter_legacy_sessions(registry)
            if changed:
                save_registry(sessions_file, registry)
            return registry
        except json.JSONDecodeError as e:
            print(
                "Corrupt session registry %s: %s" % (sessions_file, e),
                file=sys.stderr,
            )
            bak = sessions_file + ".bak"
            try:
                os.rename(sessions_file, bak)
                print("Renamed corrupt registry to %s" % bak, file=sys.stderr)
            except OSError as rename_err:
                print(
                    "Could not rename corrupt registry: %s" % rename_err,
                    file=sys.stderr,
                )
            return SessionRegistry()
        except OSError as e:
            print(
                "Could not read session registry %s: %s" % (sessions_file, e),
                file=sys.stderr,
            )
            return SessionRegistry()

    registry = SessionRegistry()
    if session_file and os.path.isfile(session_file):
        try:
            with open(session_file) as f:
                sid = f.read().strip()
            if sid and not is_legacy_cli_id(sid):
                now = _now_iso()
                registry.sessions.append(
                    SessionEntry(
                        id=sid,
                        created_at=now,
                        last_active_at=now,
                        title="Imported session",
                        summary="",
                    )
                )
                registry.active_id = sid
                save_registry(sessions_file, registry)
            elif sid and is_legacy_cli_id(sid):
                print(
                    "Legacy CLI session in %s ignored; send /new after SDK migration."
                    % session_file,
                    file=sys.stderr,
                )
        except OSError:
            pass
    return registry


def save_registry(sessions_file: str, registry: SessionRegistry) -> None:
    try:
        with open(sessions_file, "w") as f:
            json.dump(registry.to_dict(), f, indent=2)
            f.write("\n")
    except OSError as e:
        print("Could not save session registry: %s" % e, file=sys.stderr)


def _find_entry(registry: SessionRegistry, session_id: str) -> Optional[SessionEntry]:
    for entry in registry.sessions:
        if entry.id == session_id:
            return entry
    return None


def _prune(registry: SessionRegistry) -> None:
    if len(registry.sessions) <= MAX_SESSIONS:
        return
    registry.sessions.sort(key=lambda s: s.last_active_at)
    drop = len(registry.sessions) - MAX_SESSIONS
    removed_ids = {s.id for s in registry.sessions[:drop]}
    registry.sessions = registry.sessions[drop:]
    if registry.active_id in removed_ids:
        registry.active_id = registry.sessions[-1].id if registry.sessions else None


def register(
    sessions_file: str,
    session_id: str,
    *,
    title: str = "New chat",
    session_file: str = "",
) -> None:
    registry = load_registry(sessions_file, session_file=session_file)
    now = _now_iso()
    entry = _find_entry(registry, session_id)
    if entry:
        entry.last_active_at = now
        if title and title != "New chat":
            entry.title = _truncate(title, TITLE_MAX)
    else:
        registry.sessions.append(
            SessionEntry(
                id=session_id,
                created_at=now,
                last_active_at=now,
                title=_truncate(title, TITLE_MAX),
                summary="",
            )
        )
        _prune(registry)
    registry.active_id = session_id
    save_registry(sessions_file, registry)


def drop_session(
    sessions_file: str,
    session_id: str,
    *,
    session_file: str = "",
) -> None:
    """Remove a stale session from the registry and fix active_id."""
    registry = load_registry(sessions_file, session_file=session_file)
    registry.sessions = [s for s in registry.sessions if s.id != session_id]
    if registry.active_id == session_id:
        registry.active_id = registry.sessions[-1].id if registry.sessions else None
    save_registry(sessions_file, registry)


def set_active(
    sessions_file: str,
    session_id: str,
    *,
    session_file: str = "",
) -> Optional[SessionEntry]:
    registry = load_registry(sessions_file, session_file=session_file)
    entry = _find_entry(registry, session_id)
    if not entry:
        now = _now_iso()
        entry = SessionEntry(
            id=session_id,
            created_at=now,
            last_active_at=now,
            title="Imported session",
            summary="",
        )
        registry.sessions.append(entry)
        _prune(registry)
    entry.last_active_at = _now_iso()
    registry.active_id = session_id
    save_registry(sessions_file, registry)
    return entry


def get_entry(
    sessions_file: str,
    session_id: str,
    *,
    session_file: str = "",
) -> Optional[SessionEntry]:
    registry = load_registry(sessions_file, session_file=session_file)
    return _find_entry(registry, session_id)


def sorted_sessions(registry: SessionRegistry) -> List[SessionEntry]:
    return sorted(registry.sessions, key=lambda s: s.last_active_at, reverse=True)


def resolve_by_index(
    sessions_file: str,
    index: int,
    *,
    session_file: str = "",
) -> Optional[SessionEntry]:
    registry = load_registry(sessions_file, session_file=session_file)
    ordered = sorted_sessions(registry)
    if index < 1 or index > len(ordered):
        return None
    return ordered[index - 1]


def record_exchange(
    sessions_file: str,
    session_id: Optional[str],
    *,
    user_text: str = "",
    assistant_text: str = "",
    session_file: str = "",
) -> None:
    if not session_id:
        return
    registry = load_registry(sessions_file, session_file=session_file)
    entry = _find_entry(registry, session_id)
    now = _now_iso()
    user_clean = _strip_attachment_hints(user_text)
    title_from_user = (
        user_clean
        and not _is_summarize_prompt(user_clean)
    )
    if not entry:
        title = _truncate(user_clean, TITLE_MAX) if title_from_user else "New chat"
        entry = SessionEntry(
            id=session_id,
            created_at=now,
            last_active_at=now,
            title=title,
            summary="",
        )
        registry.sessions.append(entry)
        _prune(registry)
    else:
        entry.last_active_at = now
        if entry.title in ("New chat", "Imported session") and title_from_user:
            entry.title = _truncate(user_clean, TITLE_MAX)

    if assistant_text.strip():
        entry.summary = _summary_from_assistant(assistant_text)

    registry.active_id = session_id
    save_registry(sessions_file, registry)


def format_chats_list(
    sessions_file: str,
    *,
    session_file: str = "",
    page: int = 1,
) -> str:
    registry = load_registry(sessions_file, session_file=session_file)
    ordered = sorted_sessions(registry)
    if not ordered:
        return "No chats yet. Send /new to start."

    page = max(1, page)
    start = (page - 1) * CHATS_PAGE_SIZE
    end = start + CHATS_PAGE_SIZE
    page_items = ordered[start:end]
    if not page_items:
        return f"No chats on page {page}. Send /chats for page 1."

    total = len(ordered)
    lines = [f"Your chats ({total}):"]
    if page > 1:
        lines[0] += f" — page {page}"

    for i, entry in enumerate(page_items, start=start + 1):
        short_id = entry.id[:8] + "…"
        active = " (active)" if entry.id == registry.active_id else ""
        lines.append(f"\n{i}. {short_id}{active}")
        lines.append(f"   {entry.title}")
        if entry.summary:
            lines.append(f"   {entry.summary}")

    text = "\n".join(lines)
    if end < total:
        text += f"\n\n(Showing {start + 1}–{end} of {total}. Send /chats {page + 1} for more.)"
    elif total > CHATS_PAGE_SIZE:
        text += "\n\nSend /chats 1 for the first page."

    if len(text) > CHATS_MAX_CHARS:
        text = text[: CHATS_MAX_CHARS - 20].rstrip() + "\n\n(list truncated)"

    text += "\n\nResume: /resume <number> or /resume <full-uuid>"
    return text
