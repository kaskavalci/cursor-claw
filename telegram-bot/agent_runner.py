"""Run cursor-sdk Agent and stream replies to Telegram."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from cursor_sdk import LocalSendOptions, SendOptions

from agent_pool import AgentPool, is_resume_error
import config_loader

SESSION_EXPIRED_MSG = "Previous session expired. Started a new one."

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
TYPING_INTERVAL = 4
# sendRichMessage limit in agent_bot.send_message (GFM markdown).
RICH_CHUNK = 32768
_RUN_LOCK = threading.Lock()
BUSY_MSG = "Agent is still busy. Wait for the current run to finish."


@dataclass
class AgentRunResult:
    session_id: Optional[str]
    assistant_text: str


def _assistant_text_from_message(msg: Any) -> str:
    msg_type = getattr(msg, "type", None)
    if msg_type != "assistant":
        return ""
    content = getattr(getattr(msg, "message", None), "content", ())
    parts = []
    for block in content:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "".join(parts)


def _parse_segments(text: str) -> list[str]:
    """Split text into paragraphs and markdown tables (indivisible blocks)."""
    lines = text.split("\n")
    segments: list[str] = []
    buf: list[str] = []
    in_table = False

    def flush() -> None:
        nonlocal in_table
        if buf:
            segments.append("\n".join(buf))
            buf.clear()
        in_table = False

    for line in lines:
        is_table = line.strip().startswith("|")
        if is_table:
            if not in_table and buf:
                flush()
            in_table = True
            buf.append(line)
        elif in_table:
            flush()
            if line.strip():
                buf.append(line)
        elif not line.strip():
            flush()
        else:
            buf.append(line)
    flush()
    return segments


def _split_for_telegram(text: str, max_size: int = RICH_CHUNK) -> list[str]:
    """Pack text into Telegram chunks, keeping tables and paragraphs intact."""
    if len(text) <= max_size:
        return [text]
    segments = _parse_segments(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush_chunk() -> None:
        nonlocal current_len
        if current:
            chunks.append("\n\n".join(current))
            current.clear()
            current_len = 0

    for seg in segments:
        seg_len = len(seg)
        sep = 2 if current else 0
        if seg_len > max_size:
            flush_chunk()
            pos = 0
            while pos < len(seg):
                end = min(pos + max_size, len(seg))
                if end < len(seg):
                    split = seg.rfind("\n", pos, end)
                    if split <= pos:
                        split = end
                    else:
                        split += 1
                    chunks.append(seg[pos:split].rstrip())
                    pos = split
                else:
                    chunks.append(seg[pos:end])
                    pos = end
            continue
        if current and current_len + sep + seg_len > max_size:
            flush_chunk()
        if current:
            current_len += 2
        current.append(seg)
        current_len += seg_len
    flush_chunk()
    return chunks or [text]


def run_agent_streaming(
    pool: AgentPool,
    prompt: str,
    resume_session: Optional[str],
    token: str,
    chat_id: int,
    *,
    model: str,
    agent_mode: Optional[str] = None,
    send_message: Callable[..., Any],
    send_chat_action: Callable[..., Any],
    collapse_blank_lines: Callable[[str], str],
    send_pending_attachments: Callable[..., Any],
    send_pending_images: Callable[..., Any],
    logs_dir: Optional[str] = None,
) -> AgentRunResult:
    if not prompt.strip():
        send_message(token, chat_id, "(no prompt)", use_rich=False)
        return AgentRunResult(session_id=resume_session, assistant_text="")

    if not _RUN_LOCK.acquire(blocking=False):
        send_message(token, chat_id, BUSY_MSG, use_rich=False)
        return AgentRunResult(session_id=resume_session, assistant_text="")

    try:
        return _run_agent_streaming_locked(
            pool,
            prompt,
            resume_session,
            token,
            chat_id,
            model=model,
            agent_mode=agent_mode,
            send_message=send_message,
            send_chat_action=send_chat_action,
            collapse_blank_lines=collapse_blank_lines,
            send_pending_attachments=send_pending_attachments,
            send_pending_images=send_pending_images,
            logs_dir=logs_dir,
        )
    finally:
        _RUN_LOCK.release()


def _run_agent_streaming_locked(
    pool: AgentPool,
    prompt: str,
    resume_session: Optional[str],
    token: str,
    chat_id: int,
    *,
    model: str,
    agent_mode: Optional[str] = None,
    send_message: Callable[..., Any],
    send_chat_action: Callable[..., Any],
    collapse_blank_lines: Callable[[str], str],
    send_pending_attachments: Callable[..., Any],
    send_pending_images: Callable[..., Any],
    logs_dir: Optional[str] = None,
) -> AgentRunResult:
    session_id = resume_session
    if session_id:
        try:
            pool.set_active(session_id)
        except Exception as e:
            if not is_resume_error(e):
                raise
            send_message(token, chat_id, SESSION_EXPIRED_MSG, use_rich=False)
            pool.drop_session(session_id)
            session_id = pool.create_session(model=model)
    else:
        session_id = pool.create_session(model=model)

    send_opts: SendOptions | None = None
    opts_kwargs: dict[str, Any] = {"local": LocalSendOptions(force=True)}
    if agent_mode in ("plan", "agent"):
        opts_kwargs["mode"] = agent_mode
    send_opts = SendOptions(**opts_kwargs, model=model)

    timeout_sec = config_loader.get_agent_timeout()
    full_log_lines: list[str] = []
    last_assistant_ref: list[str] = [""]
    accumulated_ref: list[str] = [""]
    sent_any = False
    timed_out = False
    run_done = threading.Event()
    error_ref: list[Optional[str]] = [None]
    run_ref: list[Any] = [None]

    log_dir = logs_dir or LOGS_DIR
    os.makedirs(log_dir, exist_ok=True)
    log_name = datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".log"
    log_path = os.path.join(log_dir, log_name)

    def send_accumulated_reply(text: str) -> None:
        nonlocal sent_any
        cleaned = collapse_blank_lines(text.strip())
        if not cleaned:
            return
        last_assistant_ref[0] = cleaned
        send_pending_attachments(token, chat_id)
        send_pending_images(token, chat_id)
        for chunk in _split_for_telegram(cleaned):
            send_message(token, chat_id, chunk)
            sent_any = True

    def worker() -> None:
        nonlocal session_id, sent_any
        try:
            try:
                agent = pool.get(session_id)
            except Exception as e:
                if not is_resume_error(e):
                    raise
                pool.drop_session(session_id)
                session_id = pool.create_session(model=model)
                agent = pool.get(session_id)
            session_id = agent.agent_id
            run = agent.send(prompt, send_opts)
            run_ref[0] = run
            with open(log_path, "w") as logf:
                for msg in run.messages():
                    try:
                        if hasattr(msg, "to_json"):
                            line = json.dumps(msg.to_json()) + "\n"
                        elif isinstance(msg, dict):
                            line = json.dumps(msg) + "\n"
                        else:
                            line = repr(msg) + "\n"
                    except Exception:
                        line = repr(msg) + "\n"
                    full_log_lines.append(line)
                    logf.write(line)
                    logf.flush()

                    text = _assistant_text_from_message(msg)
                    if text:
                        accumulated_ref[0] += text

                if accumulated_ref[0].strip():
                    send_accumulated_reply(accumulated_ref[0])
                elif not sent_any:
                    try:
                        final = run.text()
                    except Exception:
                        final = ""
                    if final and final.strip():
                        send_accumulated_reply(final)
        except Exception as e:
            error_ref[0] = str(e)
            print("SDK agent error: %s" % e, file=sys.stderr)
        finally:
            run_done.set()

    t = threading.Thread(target=worker, daemon=False)
    t.start()
    last_typing = 0.0
    start_time = time.time()

    while not run_done.is_set():
        time.sleep(0.5)
        now = time.time()
        if timeout_sec and (now - start_time) >= timeout_sec:
            timed_out = True
            if run_ref[0] is not None:
                try:
                    run_ref[0].cancel()
                except Exception:
                    pass
            send_message(
                token, chat_id, "Agent timed out after %s seconds." % timeout_sec, use_rich=False
            )
            break
        if now - last_typing >= TYPING_INTERVAL:
            send_chat_action(token, chat_id, "typing")
            last_typing = now

    t.join(timeout=30)
    if not timed_out and error_ref[0]:
        send_message(token, chat_id, "Error running agent: %s" % error_ref[0], use_rich=False)
    elif not timed_out and not sent_any:
        fallback = last_assistant_ref[0] or "(Agent returned no reply.)"
        send_message(token, chat_id, fallback, use_rich=False)
        last_assistant_ref[0] = fallback

    return AgentRunResult(session_id=session_id, assistant_text=last_assistant_ref[0])
