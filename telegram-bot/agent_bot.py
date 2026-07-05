#!/usr/bin/env python3
"""
Telegram bot: only accepts messages from the allowed user (see config); forwards
them to Cursor agent and sends the agent's response back. Accepts photos and file
attachments (e.g. PDF); files are saved under telegram-bot/received_documents/.
Uses --output-format stream-json
and --resume to keep one conversation session across restarts (session_id stored in
.cursor_agent_session). All other users are dropped.

Config: create telegram-bot/config from config.example with TELEGRAM_BOT_TOKEN and
TELEGRAM_ALLOWED_USER_ID. Run from a terminal outside Cursor.
"""

import os
import sys
import time
import json
import subprocess
import threading
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional, Tuple

TYPING_INTERVAL = 4  # Telegram typing indicator lasts ~5s; re-send before it expires
RICH_CHUNK = 32768  # sendRichMessage limit
PLAIN_CHUNK = 4096  # sendMessage limit
DEFAULT_AGENT_TIMEOUT = 0  # 0 = unlimited; set CURSOR_AGENT_TIMEOUT in config or env to limit

BASE = "https://api.telegram.org/bot"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config")
SESSION_FILE = os.path.join(SCRIPT_DIR, ".cursor_agent_session")
CHAT_ID_FILE = os.path.join(SCRIPT_DIR, "chat_id")
OFFSET_FILE = os.path.join(SCRIPT_DIR, ".telegram_offset")
RECEIVED_IMAGES_DIR = os.path.join(SCRIPT_DIR, "received_images")
RECEIVED_DOCUMENTS_DIR = os.path.join(SCRIPT_DIR, "received_documents")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
PENDING_IMAGES_DIR = os.path.join(SCRIPT_DIR, "pending_images")
PENDING_ATTACHMENTS_DIR = os.path.join(SCRIPT_DIR, "pending_attachments")


def get_agent_timeout() -> int:
    """Agent subprocess timeout in seconds. Config file or env CURSOR_AGENT_TIMEOUT, else default."""
    timeout = None
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "CURSOR_AGENT_TIMEOUT" and v:
                        try:
                            timeout = int(v)
                        except ValueError:
                            pass
                        break
    if timeout is None:
        try:
            timeout = int(os.environ.get("CURSOR_AGENT_TIMEOUT", str(DEFAULT_AGENT_TIMEOUT)))
        except ValueError:
            timeout = DEFAULT_AGENT_TIMEOUT
    return timeout if timeout > 0 else 0  # 0 = unlimited


def load_config() -> Tuple[str, int]:
    """Load TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID from config file or env."""
    token = None
    user_id = None
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k == "TELEGRAM_BOT_TOKEN" and v:
                        token = v
                    elif k == "TELEGRAM_ALLOWED_USER_ID" and v:
                        try:
                            user_id = int(v)
                        except ValueError:
                            pass
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in %s or env." % CONFIG_FILE, file=sys.stderr)
        sys.exit(1)
    if user_id is None:
        uid_env = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
        if uid_env:
            try:
                user_id = int(uid_env)
            except ValueError:
                pass
        if user_id is None:
            print("Set TELEGRAM_ALLOWED_USER_ID in %s or env." % CONFIG_FILE, file=sys.stderr)
            sys.exit(1)
    return token, user_id


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
        if not line.strip():  # blank: empty or whitespace-only
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
                return f.read().strip() or None
        except Exception:
            pass
    return None


def save_session(session_id: Optional[str]) -> None:
    if session_id:
        try:
            with open(SESSION_FILE, "w") as f:
                f.write(session_id)
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


def _parse_session_and_final_output(full_stdout: str, full_stderr: str, returncode: int) -> Tuple[str, Optional[str]]:
    """Parse full stdout for session_id and final displayable result. Returns (response_text, session_id)."""
    session_id = None
    response_text = None
    for line in full_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = obj.get("session_id") or obj.get("sessionId") or obj.get("chatId")
        if sid:
            session_id = str(sid)
        if "result" in obj and isinstance(obj["result"], str):
            response_text = obj["result"].strip()
        elif response_text is None:
            for key in ("text", "content", "response", "message", "output"):
                if key in obj and isinstance(obj[key], str):
                    response_text = obj[key]
                    break
    if response_text is None and full_stdout:
        try:
            obj = json.loads(full_stdout.strip().split("\n")[-1] or "{}")
            session_id = session_id or obj.get("session_id") or obj.get("sessionId")
            response_text = obj.get("result") or obj.get("text") or obj.get("content") or full_stdout
            if isinstance(response_text, dict):
                response_text = response_text.get("content", str(response_text))
        except (json.JSONDecodeError, IndexError):
            response_text = full_stdout
    if returncode != 0 and not response_text:
        response_text = full_stderr or "Agent exited with code %s" % returncode
    return response_text or "(no output)", session_id


def run_agent_streaming(
    prompt: str,
    resume_session: Optional[str],
    token: str,
    chat_id: int,
    *,
    agent_mode: Optional[str] = None,
) -> Optional[str]:
    """
    Run cursor agent with stream-json: ignore "thinking" and "result". Send
    every assistant message as a Telegram message (no --stream-partial-output,
    so each message is a full turn). Skip whitespace-only. Typing indicator
    until process done. Raw JSON stream written to telegram-bot/logs/<timestamp>.log.
    Returns session_id for persistence.
    """
    if not prompt.strip():
        send_message(token, chat_id, "(no prompt)", use_rich=False)
        return resume_session
    cmd = [
        "cursor", "agent", "--print", "--trust", "--force",
        "--workspace", REPO_ROOT,
        "--model", "Auto",
        "--output-format", "stream-json",
    ]
    if agent_mode:
        cmd.extend(["--mode", agent_mode])
    if resume_session:
        cmd.extend(["--resume", resume_session])
    cmd.append(prompt)
    timeout_sec = get_agent_timeout()
    full_stdout_lines = []
    full_stderr_lines = []
    lock = threading.Lock()
    process_done = threading.Event()
    proc_ref = [None]  # so main thread can kill on timeout

    os.makedirs(LOGS_DIR, exist_ok=True)
    log_name = datetime.now().strftime("%Y-%m-%dT%H-%M-%S") + ".log"
    log_path = os.path.join(LOGS_DIR, log_name)

    def reader():
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            proc_ref[0] = proc
            try:
                with open(log_path, "w") as logf:
                    for line in iter(proc.stdout.readline, ""):
                        with lock:
                            full_stdout_lines.append(line)
                        logf.write(line)
                        logf.flush()
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue
                        try:
                            obj = json.loads(line_stripped)
                        except json.JSONDecodeError:
                            continue
                        msg_type = (obj.get("role") or obj.get("type") or obj.get("messageType") or "").lower()
                        if msg_type == "thinking":
                            continue
                        if msg_type == "result":
                            continue
                        if msg_type != "assistant":
                            continue
                        # Stream-json: text in message.content[].text
                        text = None
                        msg = obj.get("message")
                        if isinstance(msg, dict):
                            content = msg.get("content")
                            if isinstance(content, list):
                                parts = []
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                                        parts.append(item["text"])
                                if parts:
                                    text = "".join(parts)
                        if text is None:
                            text = (
                                obj.get("content")
                                or obj.get("text")
                                or obj.get("delta")
                                or obj.get("result")
                                or obj.get("output")
                            )
                        if not isinstance(text, str):
                            continue
                        to_send = text.strip()
                        if to_send:
                            send_pending_attachments(token, chat_id)
                            send_pending_images(token, chat_id)
                            send_message(token, chat_id, collapse_blank_lines(to_send))
            finally:
                proc.wait()
                err = proc.stderr.read() if proc.stderr else ""
                if err:
                    full_stderr_lines.append(err)
        except Exception as e:
            send_message(token, chat_id, "Error running agent: %s" % e, use_rich=False)
        finally:
            process_done.set()

    t = threading.Thread(target=reader, daemon=False)
    t.start()
    last_typing = 0.0
    start_time = time.time()
    timed_out = False

    while not process_done.is_set():
        time.sleep(1)
        now = time.time()
        if timeout_sec and (now - start_time) >= timeout_sec:
            p = proc_ref[0]
            if p and p.poll() is None:
                p.kill()
            send_message(token, chat_id, "Agent timed out after %s seconds." % timeout_sec, use_rich=False)
            timed_out = True
            break
        if now - last_typing >= TYPING_INTERVAL:
            send_chat_action(token, chat_id, "typing")
            last_typing = now

    t.join(timeout=10)
    full_stdout = "".join(full_stdout_lines)
    full_stderr = "".join(full_stderr_lines)
    response_text, session_id = _parse_session_and_final_output(full_stdout, full_stderr, 0)
    if not session_id:
        session_id = resume_session
    return session_id


def register_bot_commands(token: str) -> None:
    commands = [
        {"command": "new", "description": "Start a new agent session"},
        {"command": "summarize", "description": "Summarize current conversation"},
        {"command": "status", "description": "Show session id"},
        {"command": "help", "description": "List commands"},
    ]
    try:
        api(token, "setMyCommands", commands=commands)
    except Exception as e:
        print("setMyCommands failed: %s" % e, file=sys.stderr)


def main():
    token, allowed_user_id = load_config()
    register_bot_commands(token)
    offset = load_offset()
    if offset:
        print("Resuming from update offset %s." % offset, file=sys.stderr)
    session_id = load_session()
    if session_id:
        print("Resuming session: %s..." % session_id[:20], file=sys.stderr)
    print("Agent bot running. Only user_id=%s accepted; others dropped." % allowed_user_id, file=sys.stderr)
    print("Ctrl+C to stop.", file=sys.stderr)
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
        # Collect all new messages from allowed user (batch: e.g. 3 messages sent while idle)
        batch_texts = []
        batch_image_paths = []  # workspace-relative paths for agent
        batch_document_paths = []  # workspace-relative paths for agent (PDF etc.)
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
            # Photos: download largest size and pass path to agent so it can read the image
            photos = msg.get("photo") or []
            if photos:
                file_id = photos[-1].get("file_id")  # largest size
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
        # Advance offset past entire batch so we don't re-process
        offset = updates[-1]["update_id"] + 1
        save_offset(offset)
        if not batch_texts and not batch_image_paths and not batch_document_paths:
            continue
        if chat_id is None:
            continue
        save_chat_id(chat_id)

        from commands import handle_commands, parse_telegram_command

        # If every text line is a command, handle via command path only
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
                repo_root=REPO_ROOT,
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

        # Append attachment hints (unchanged)
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

        summarize_mode = "ask" if command_texts and any(
            parse_telegram_command(t) and parse_telegram_command(t)[0] == "summarize" for t in command_texts
        ) else None

        if command_texts:
            print("Running agent after command(s): %s..." % ", ".join(command_texts[:3]), file=sys.stderr)
        elif len(batch_texts) > 1:
            print("Running agent for %d messages as one prompt (%s...)..." % (len(batch_texts), text[:50]), file=sys.stderr)
        else:
            print("Running agent for prompt: %s..." % text[:60], file=sys.stderr)
        send_chat_action(token, chat_id, "typing")
        session_id = run_agent_streaming(
            text, session_id, token, chat_id, agent_mode=summarize_mode
        )
        save_session(session_id)


if __name__ == "__main__":
    main()
