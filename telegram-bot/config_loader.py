"""Shared config loading for telegram-bot scripts."""
from __future__ import annotations

import os
import sys
from typing import Dict, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_FILE = os.path.join(SCRIPT_DIR, "config")
DEFAULT_AGENT_TIMEOUT = 0  # 0 = unlimited
DEFAULT_AGENT_MODEL = "auto"
MODEL_FILE = os.path.join(SCRIPT_DIR, ".cursor_agent_model")


def _parse_config_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k and v:
                values[k] = v
    return values


def get_config_path() -> str:
    return os.environ.get("TELEGRAM_BOT_CONFIG", DEFAULT_CONFIG_FILE)


def load_values() -> Dict[str, str]:
    path = get_config_path()
    values = _parse_config_file(path)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
        "CURSOR_API_KEY",
        "CURSOR_AGENT_TIMEOUT",
    ):
        env_val = os.environ.get(key)
        if env_val and key not in values:
            values[key] = env_val
    return values


def ensure_cursor_api_key() -> str:
    """Load CURSOR_API_KEY into os.environ for cursor-sdk. Exit if missing."""
    values = load_values()
    api_key = values.get("CURSOR_API_KEY") or os.environ.get("CURSOR_API_KEY")
    if not api_key:
        print(
            "Set CURSOR_API_KEY in %s or env (Cursor dashboard API key)."
            % get_config_path(),
            file=sys.stderr,
        )
        sys.exit(1)
    os.environ["CURSOR_API_KEY"] = api_key
    return api_key


def load_bot_config() -> Tuple[str, int]:
    """TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_ID. Exit if missing."""
    values = load_values()
    token = values.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in %s or env." % get_config_path(), file=sys.stderr)
        sys.exit(1)
    user_id: Optional[int] = None
    uid_raw = values.get("TELEGRAM_ALLOWED_USER_ID")
    if uid_raw:
        try:
            user_id = int(uid_raw)
        except ValueError:
            pass
    if user_id is None:
        print(
            "Set TELEGRAM_ALLOWED_USER_ID in %s or env." % get_config_path(),
            file=sys.stderr,
        )
        sys.exit(1)
    return token, user_id


def load_reminder_config() -> Tuple[Optional[str], Optional[int]]:
    """Token and chat_id for reminders; returns (None, None) if not configured."""
    values = load_values()
    token = values.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None, None
    chat_id_file = os.path.join(SCRIPT_DIR, "chat_id")
    if not os.path.isfile(chat_id_file):
        return token, None
    try:
        with open(chat_id_file) as f:
            return token, int(f.read().strip())
    except (ValueError, OSError):
        return token, None


def get_agent_timeout() -> int:
    values = load_values()
    timeout_raw = values.get("CURSOR_AGENT_TIMEOUT")
    if timeout_raw:
        try:
            timeout = int(timeout_raw)
            return timeout if timeout > 0 else 0
        except ValueError:
            pass
    try:
        timeout = int(os.environ.get("CURSOR_AGENT_TIMEOUT", str(DEFAULT_AGENT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_AGENT_TIMEOUT
    return timeout if timeout > 0 else 0


def load_model(model_file: str = MODEL_FILE) -> str:
    if os.path.isfile(model_file):
        try:
            with open(model_file) as f:
                model = f.read().strip()
                if model:
                    return model
        except OSError:
            pass
    return DEFAULT_AGENT_MODEL
