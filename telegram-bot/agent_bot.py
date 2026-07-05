#!/usr/bin/env python3
"""
Telegram bot: only accepts messages from the allowed user (see config); forwards
them to Cursor agent via cursor-sdk and sends the agent's response back. Accepts
photos and file attachments (e.g. PDF); files are saved under telegram-bot/received_documents/.
Uses in-process SDK Agent handles with session registry in .cursor_agent_sessions.json;
active session pointer in .cursor_agent_session. All other users are dropped.

Config: create telegram-bot/config from config.example with TELEGRAM_BOT_TOKEN,
TELEGRAM_ALLOWED_USER_ID, and CURSOR_API_KEY. Run from a terminal outside Cursor.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
from typing import Optional

import config_loader
import sessions
from agent_pool import AgentPool
from agent_runner import run_agent_streaming

RICH_CHUNK = 32768  # sendRichMessage limit
PLAIN_CHUNK = 4096  # sendMessage limit
DEFAULT_AGENT_MODEL = config_loader.DEFAULT_AGENT_MODEL

BASE = "https://api.telegram.org/bot"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SESSION_FILE = os.path.join(SCRIPT_DIR, ".cursor_agent_session")
SESSIONS_FILE = os.path.join(SCRIPT_DIR, ".cursor_agent_sessions.json")
MODEL_FILE = config_loader.MODEL_FILE
CHAT_ID_FILE = os.path.join(SCRIPT_DIR, "chat_id")
OFFSET_FILE = os.path.join(SCRIPT_DIR, ".telegram_offset")
RECEIVED_IMAGES_DIR = os.path.join(SCRIPT_DIR, "received_images")
RECEIVED_DOCUMENTS_DIR = os.path.join(SCRIPT_DIR, "received_documents")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
PENDING_IMAGES_DIR = os.path.join(SCRIPT_DIR, "pending_images")
PENDING_ATTACHMENTS_DIR = os.path.join(SCRIPT_DIR, "pending_attachments")


def load_config():
    return config_loader.load_bot_config()


def load_model() -> str:
    return config_loader.load_model(MODEL_FILE)


def api(token, method, **params):
    url = f"{BASE}{token}/{method}"
    data = json.dumps(params).encode() if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def send_chat_action(token, chat_id, action="typing"):
    try:
        api(token, "sendChatAction", chat_id=chat_id, action=action)
    except Exception:
        pass


def collapse_blank_lines(text: str) -> str:
    """Collapse runs of consecutive blank lines (empty or whitespace-only) into a single blank line."""
    if not text:
        return text
    lines = text.split("\n")
    result = []
    in_blank_run = False
    for line in lines:
        if not line.strip():
            if not in_blank_run:
                result.append("")
                in_blank_run = True
        else:
            result.append(line)
            in_blank_run = False
    return "\n".join(result)


def send_message(token, chat_id, text, *, use_rich=True):
    """Send text to Telegram. Agent replies use sendRichMessage (GFM); system text uses plain sendMessage."""
    chunk_size = RICH_CHUNK if use_rich else PLAIN_CHUNK
    for i in range(0, len(text), chunk_size):
        part = text[i : i + chunk_size]
        if use_rich:
            try:
                api(
                    token,
                    "sendRichMessage",
                    chat_id=chat_id,
                    rich_message={"markdown": part},
                )
                continue
            except urllib.error.HTTPError as e:
                print("sendRichMessage failed: %s" % e.read().decode(), file=sys.stderr)
        for j in range(0, len(part), PLAIN_CHUNK):
            api(token, "sendMessage", chat_id=chat_id, text=part[j : j + PLAIN_CHUNK])


def send_photo(token, chat_id, photo_path: str, caption: Optional[str] = None) -> None:
    """Send a local file as a photo. photo_path must be absolute or relative to cwd."""
    if not os.path.isfile(photo_path):
        return
    url = f"{BASE}{token}/sendPhoto"
    with open(photo_path, "rb") as f:
        photo_data = f.read()
    boundary = "----FormBoundary" + os.urandom(16).hex()
    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"photo\"; filename=\"image.png\"\r\n"
        f"Content-Type: image/png\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + photo_data + tail
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read().decode())
    except Exception:
        pass


def send_document(token, chat_id, file_path: str) -> None:
    """Send a local file as a document."""
    if not os.path.isfile(file_path):
        return
    url = f"{BASE}{token}/sendDocument"
    with open(file_path, "rb") as f:
        file_data = f.read()
    name = os.path.basename(file_path)
    boundary = "----FormBoundary" + os.urandom(16).hex()
    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + file_data + tail
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            json.loads(r.read().decode())
    except Exception:
        pass


def send_pending_images(token, chat_id) -> None:
    """Send any images in PENDING_IMAGES_DIR as photos, then delete them."""
    if not os.path.isdir(PENDING_IMAGES_DIR):
        return
    try:
        for name in sorted(os.listdir(PENDING_IMAGES_DIR)):
            path = os.path.join(PENDING_IMAGES_DIR, name)
            if not os.path.isfile(path):
                continue
            lower = name.lower()
            if not (lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".jpeg") or lower.endswith(".gif") or lower.endswith(".webp")):
                continue
            try:
                send_photo(token, chat_id, path)
            except Exception:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def send_pending_attachments(token, chat_id) -> None:
    """Send any files in PENDING_ATTACHMENTS_DIR (images as photo, others as document), then delete them."""
    if not os.path.isdir(PENDING_ATTACHMENTS_DIR):
        return
    try:
        for name in sorted(os.listdir(PENDING_ATTACHMENTS_DIR)):
            path = os.path.join(PENDING_ATTACHMENTS_DIR, name)
            if not os.path.isfile(path):
                continue
            lower = name.lower()
            try:
                if any(lower.endswith(ext) for ext in _IMAGE_EXTENSIONS):
                    send_photo(token, chat_id, path)
                else:
                    send_document(token, chat_id, path)
            except Exception:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
    except OSError:
        pass


def load_session() -> Optional[str]:
    if os.path.isfile(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                sid = f.read().strip() or None
            if sid and sessions.is_legacy_cli_id(sid):
                return None
            return sid
        except Exception:
            pass
    return None


def save_session(session_id: Optional[str]) -> None:
    try:
        if session_id:
            with open(SESSION_FILE, "w") as f:
                f.write(session_id)
        elif os.path.isfile(SESSION_FILE):
            os.remove(SESSION_FILE)
    except Exception as e:
        print("Could not save session: %s" % e, file=sys.stderr)


def save_chat_id(chat_id: int) -> None:
    """Persist chat_id so run_reminders.py can send scheduled messages to the user."""
    try:
        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))
    except Exception as e:
        print("Could not save chat_id: %s" % e, file=sys.stderr)


def _safe_document_filename(name: str) -> str:
    base = os.path.basename((name or "").strip()) or "file"
    if base in (".", ".."):
        base = "file"
    if len(base) > 180:
        root, ext = os.path.splitext(base)
        base = (root[:160] + ext) if ext else root[:180]
    return base


def download_telegram_file(token: str, file_id: str, dest_path: str) -> bool:
    """Download a Telegram file by file_id to dest_path. Returns True on success."""
    try:
        out = api(token, "getFile", file_id=file_id)
        if not out.get("ok"):
            return False
        file_path = (out.get("result") or {}).get("file_path")
        if not file_path:
            return False
        url = "https://api.telegram.org/file/bot%s/%s" % (token, file_path)
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print("Download file failed: %s" % e, file=sys.stderr)
        return False


def download_telegram_photo(token: str, file_id: str, dest_path: str) -> bool:
    """Download a Telegram photo by file_id to dest_path. Returns True on success."""
    return download_telegram_file(token, file_id, dest_path)


def load_offset() -> int:
    """Load last getUpdates offset so restarts don't re-process the same message."""
    if os.path.isfile(OFFSET_FILE):
        try:
            with open(OFFSET_FILE) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            pass
    return 0


def save_offset(offset: int) -> None:
    """Persist getUpdates offset so a crash during agent run doesn't cause re-processing."""
    try:
        with open(OFFSET_FILE, "w") as f:
            f.write(str(offset))
    except Exception as e:
        print("Could not save offset: %s" % e, file=sys.stderr)


def register_bot_commands(token: str) -> None:
    commands = [
        {"command": "new", "description": "Start a new agent session"},
        {"command": "chats", "description": "List agent conversations"},
        {"command": "resume", "description": "Resume a previous session by id"},
        {"command": "summarize", "description": "Summarize current conversation"},
        {"command": "status", "description": "Show session id"},
        {"command": "model", "description": "Show or set agent model"},
        {"command": "help", "description": "List commands"},
    ]
    try:
        api(token, "setMyCommands", commands=commands)
    except Exception as e:
        print("setMyCommands failed: %s" % e, file=sys.stderr)


def main():
    config_loader.ensure_cursor_api_key()
    token, allowed_user_id = load_config()
    register_bot_commands(token)
    model = load_model()
    pool = AgentPool(REPO_ROOT, model)
    offset = load_offset()
    if offset:
        print("Resuming from update offset %s." % offset, file=sys.stderr)
    session_id = load_session()
    registry = sessions.load_registry(SESSIONS_FILE, session_file=SESSION_FILE)
    if session_id and registry.active_id != session_id:
        sessions.set_active(SESSIONS_FILE, session_id, session_file=SESSION_FILE)
    if session_id and sessions.is_legacy_cli_id(session_id):
        session_id = None
        save_session(None)
    if session_id:
        if not pool.warm(session_id):
            sessions.drop_session(SESSIONS_FILE, session_id, session_file=SESSION_FILE)
            session_id = None
            save_session(None)
    print("Agent bot running (cursor-sdk). Only user_id=%s accepted." % allowed_user_id, file=sys.stderr)
    print("Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            try:
                out = api(token, "getUpdates", offset=offset, timeout=30)
            except urllib.error.URLError as e:
                print("API error: %s" % e, file=sys.stderr)
                time.sleep(5)
                continue
            if not out.get("ok"):
                print("API not ok: %s" % out, file=sys.stderr)
                time.sleep(5)
                continue
            updates = out.get("result", [])
            if not updates:
                continue
            batch_texts = []
            batch_image_paths = []
            batch_document_paths = []
            chat_id = None
            for i, upd in enumerate(updates):
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                uid = (msg.get("from") or {}).get("id")
                if uid != allowed_user_id:
                    continue
                if chat_id is None:
                    chat_id = msg["chat"]["id"]
                text = (msg.get("text") or "").strip()
                if text:
                    batch_texts.append(text)
                photos = msg.get("photo") or []
                if photos:
                    file_id = photos[-1].get("file_id")
                    if file_id:
                        os.makedirs(RECEIVED_IMAGES_DIR, exist_ok=True)
                        local_name = "photo_%s_%s.jpg" % (upd["update_id"], i)
                        dest_path = os.path.join(RECEIVED_IMAGES_DIR, local_name)
                        if download_telegram_photo(token, file_id, dest_path):
                            batch_image_paths.append(os.path.join("telegram-bot", "received_images", local_name))
                    caption = (msg.get("caption") or "").strip()
                    if caption:
                        batch_texts.append(caption)
                doc = msg.get("document")
                if isinstance(doc, dict) and doc.get("file_id"):
                    os.makedirs(RECEIVED_DOCUMENTS_DIR, exist_ok=True)
                    orig_name = doc.get("file_name") or "file"
                    safe = _safe_document_filename(orig_name)
                    local_name = "doc_%s_%s_%s" % (upd["update_id"], i, safe)
                    dest_path = os.path.join(RECEIVED_DOCUMENTS_DIR, local_name)
                    if download_telegram_file(token, doc["file_id"], dest_path):
                        batch_document_paths.append(
                            os.path.join("telegram-bot", "received_documents", local_name)
                        )
                    cap = (msg.get("caption") or "").strip()
                    if cap:
                        batch_texts.append(cap)
            offset = updates[-1]["update_id"] + 1
            save_offset(offset)
            if not batch_texts and not batch_image_paths and not batch_document_paths:
                continue
            if chat_id is None:
                continue
            save_chat_id(chat_id)

            from commands import handle_commands, parse_telegram_command

            command_texts = [t for t in batch_texts if parse_telegram_command(t)]
            non_command_texts = [t for t in batch_texts if not parse_telegram_command(t)]

            if command_texts:
                result = handle_commands(
                    command_texts,
                    session_id=session_id,
                    token=token,
                    chat_id=chat_id,
                    send_message=send_message,
                    session_file=SESSION_FILE,
                    sessions_file=SESSIONS_FILE,
                    model_file=MODEL_FILE,
                    default_model=DEFAULT_AGENT_MODEL,
                    repo_root=REPO_ROOT,
                    agent_pool=pool,
                )
                if result.session_id is not None:
                    session_id = result.session_id
                    save_session(session_id)
                agent_prompt = result.agent_prompt
                if non_command_texts:
                    extra = "\n\n".join(non_command_texts)
                    agent_prompt = f"{agent_prompt}\n\n{extra}" if agent_prompt else extra
                elif not agent_prompt and not batch_image_paths and not batch_document_paths:
                    continue
                text = agent_prompt or ""
            else:
                text = "\n\n".join(batch_texts) if batch_texts else ""

            if batch_image_paths:
                text += "\n\n[User sent %d image(s). They are in the workspace at: %s. Look at them and respond accordingly.]" % (
                    len(batch_image_paths),
                    ", ".join(batch_image_paths),
                )
            if batch_document_paths:
                text += "\n\n[User sent %d file(s). They are in the workspace at: %s. Read the file(s) and respond accordingly.]" % (
                    len(batch_document_paths),
                    ", ".join(batch_document_paths),
                )

            if not text.strip():
                continue

            summarize_mode = "plan" if command_texts and any(
                parse_telegram_command(t) and parse_telegram_command(t)[0] == "summarize" for t in command_texts
            ) else None

            if command_texts:
                print("Running agent after command(s): %s..." % ", ".join(command_texts[:3]), file=sys.stderr)
            elif len(batch_texts) > 1:
                print("Running agent for %d messages as one prompt (%s...)..." % (len(batch_texts), text[:50]), file=sys.stderr)
            else:
                print("Running agent for prompt: %s..." % text[:60], file=sys.stderr)
            send_chat_action(token, chat_id, "typing")
            run_result = run_agent_streaming(
                pool,
                text,
                session_id,
                token,
                chat_id,
                model=load_model(),
                agent_mode=summarize_mode,
                send_message=send_message,
                send_chat_action=send_chat_action,
                collapse_blank_lines=collapse_blank_lines,
                send_pending_attachments=send_pending_attachments,
                send_pending_images=send_pending_images,
                logs_dir=LOGS_DIR,
            )
            session_id = run_result.session_id
            save_session(session_id)
            if summarize_mode:
                record_user_text = ""
            elif command_texts:
                record_user_text = "\n\n".join(non_command_texts) if non_command_texts else ""
            else:
                record_user_text = "\n\n".join(batch_texts) if batch_texts else ""
            sessions.record_exchange(
                SESSIONS_FILE,
                session_id,
                user_text=record_user_text,
                assistant_text=run_result.assistant_text,
                session_file=SESSION_FILE,
            )
    finally:
        pool.close_all()


if __name__ == "__main__":
    main()
