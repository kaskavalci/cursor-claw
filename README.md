# Cursor-Claw: Telegram ↔ Cursor Agent

Use a Telegram bot to talk to the [Cursor](https://cursor.com) agent from your phone or anywhere. Messages you send to the bot are forwarded to `cursor agent`; the agent’s reply is sent back to you in Telegram.

**Requirements:**

- Python 3
- [Cursor](https://cursor.com) with the CLI installed (`cursor` on your PATH)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Setup

### 1. Check Cursor agent

Before anything else, run the Cursor agent from a terminal and confirm it loads and works:

```bash
cursor agent
```

If it starts and you can interact with it (or at least see it respond), you’re good. Exit when done. The Telegram bot will invoke `cursor agent` for you; this step just avoids surprises later.

### 2. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (name and username).
3. Copy the **token** BotFather gives you (e.g. `123456789:ABCdefGHI...`). Keep it secret; don’t commit it.

### 3. Get your Telegram user ID

From this repo’s root:

```bash
TELEGRAM_BOT_TOKEN='your_token_here' python3 telegram-bot/echo_user_ids.py
```

Send any message to your new bot. The script will print your `user_id` (a number). Note it; you’ll need it in the next step. Stop the script with Ctrl+C.

### 4. Configure the agent bot

```bash
cp telegram-bot/config.example telegram-bot/config
```

Edit `telegram-bot/config` and set:

- `TELEGRAM_BOT_TOKEN` — the token from BotFather
- `TELEGRAM_ALLOWED_USER_ID` — the user ID from step 2 (only this user can use the bot)

**Do not commit `telegram-bot/config`.** It’s listed in `.gitignore`.

### 5. Run the bot

Open a terminal **outside** Cursor (so the agent can run in the background). From the **clone root** of this repo:

```bash
python3 telegram-bot/agent_bot.py
```

Leave it running. When you send a text message to your bot on Telegram, it will run `cursor agent` in this workspace and reply with the agent’s output.

**Hand off to systemd:** Once the bot is working, you can message the agent (e.g. "install the systemd unit with lingering so the bot runs at boot") and the agent can copy the unit files to `~/.config/systemd/user/`, run `loginctl enable-linger $USER`, and enable/start the service. After that, you can stop the script in your terminal (Ctrl+C); the bot will keep running under systemd and will start automatically at boot (and with lingering, even when you're not logged in).

### Bot commands

- `/new` — start a fresh Cursor agent session
- `/new <prompt>` — new session, then run `<prompt>`
- `/summarize` — summarize the current conversation (ask mode; read-only, does not edit files)
- `/status` — show the current session id
- `/help` — list these commands

---

## Sending images and files to the user

The agent can send you images or files on Telegram by queuing them before replying:

- **Images** (screenshots, diagrams, etc.): run `python3 telegram-bot/attach_image.py /path/to/image.png`. The image is copied to a pending directory; the next time the bot sends a reply, it will send the image(s) and then clear the queue.
- **Any file** (PDFs, text, etc.): run `python3 telegram-bot/attach_file.py /path/to/file`. Same idea—files in the queue are sent with the next reply (images as photos, other files as documents).

Example: to send a browser screenshot, the agent can run a headless browser (e.g. [clawfox](https://github.com/jes/clawfox)), take a screenshot, then run `attach_image.py` with the screenshot path before replying.

---

## Web browsing (for the agent)

When the agent needs to look something up on the web, prefer **[clawfox](https://github.com/jes/clawfox)** if it’s installed: a CLI headless browser (Chromium) that the agent can drive with commands like `clawfox go <url>`, `clawfox show`, `clawfox screenshot`, `clawfox click "text=Submit"`. Screenshots go to `~/.clawfox/screenshots/`; the agent can then run `attach_image.py` on that path to send the screenshot to you on Telegram. Clawfox uses a **persistent profile** (`~/.clawfox/browser_profile/`), so if you log in somewhere in a headful session, the agent can reuse that session later. For a visible window so you can log in, run `clawfox --headful go <url>` (put `--headful` before the subcommand); then the agent uses the same browser. With multiple tabs, `clawfox tabs` and `clawfox focus_tab <substring>` let the agent switch to the right tab.

---

## How it works

- The bot only accepts messages from the user ID in `config`; others are ignored.
- It runs `cursor agent --print --trust --force --workspace <repo_root> ...` so the agent can execute commands without interactive prompts.
- The agent session is persisted in `telegram-bot/.cursor_agent_session`, so restarts of the bot keep one continuous conversation.
- Run the bot from the repo root so `--workspace` points at your clone; open that same folder in Cursor when you want to work there.

---

## Optional: systemd and scheduled reminders

You can run the bot under systemd so it starts at boot and keeps running without a login session (user lingering). You can also run **scheduled reminders**: at a set time the Cursor agent runs a prompt (e.g. “check the Bitcoin price and tell the user”) and the reply is sent to you on Telegram. Once the bot is running (step 5 above), you can ask the agent over Telegram to install the systemd units and enable lingering, then stop the script in your terminal.

**Manual install** (or have the agent do it):

1. **Copy the unit files** (edit paths in the files if your clone is not in `~/projects/cursor-claw`):
   ```bash
   mkdir -p ~/.config/systemd/user
   cp telegram-bot/systemd/telegram-agent-bot.service telegram-bot/systemd/telegram-reminders.service telegram-bot/systemd/telegram-reminders.timer ~/.config/systemd/user/
   ```
2. **Enable lingering** (so user services run without a session):
   ```bash
   loginctl enable-linger $USER
   ```
3. **Start the bot and the reminders timer**:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now telegram-agent-bot.service
   systemctl --user enable --now telegram-reminders.timer
   ```

Reminders are stored in `telegram-bot/reminders.json` (do not commit; it’s in `.gitignore`). Each entry has `"at"` (local time, `YYYY-MM-DDTHH:MM:SS`), and either `"text"` (fixed message sent at that time) or `"prompt"` (Cursor agent runs that prompt at that time and its reply is sent to you). The Cursor agent in this workspace can add reminders when you ask (e.g. “at 9am tomorrow check the BTC price and let me know”). You must have messaged the bot at least once so `telegram-bot/chat_id` exists.

---

## Security

- **Never commit** `telegram-bot/config` or any file containing your bot token or user ID.
- Only the configured user ID can use the bot; everyone else is dropped.
- The agent runs with `--trust --force`, so it can run commands and edit files without asking. Use only with a bot that only you can message.

---

## License

Use and modify as you like. No warranty.
