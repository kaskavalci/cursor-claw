# cursor-claw — Telegram bot helpers
# Run from repo root: make help

PYTHON ?= python3
SYSTEMCTL ?= systemctl --user
REMOTE ?= origin
BRANCH ?= master

BOT_SERVICE := telegram-agent-bot.service
REMINDERS_TIMER := telegram-reminders.timer
SYSTEMD_SRC := telegram-bot/systemd
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user

.PHONY: help test bot-run \
	systemd-install systemd-reload systemd-enable \
	bot-start bot-stop bot-restart bot-status bot-logs \
	deploy deploy-pull reminders-enable reminders-status

help: ## Show targets
	@grep -E '^[a-zA-Z0-9_.-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  %-18s %s\n", $$1, $$2}'

test: ## Run telegram-bot unit tests
	cd telegram-bot && $(PYTHON) -m unittest tests.test_commands tests.test_sessions -v

bot-run: ## Run agent bot in foreground (outside Cursor)
	$(PYTHON) telegram-bot/agent_bot.py

systemd-install: ## Copy user unit files to ~/.config/systemd/user
	mkdir -p $(SYSTEMD_USER_DIR)
	cp $(SYSTEMD_SRC)/telegram-agent-bot.service \
	   $(SYSTEMD_SRC)/telegram-reminders.service \
	   $(SYSTEMD_SRC)/telegram-reminders.timer \
	   $(SYSTEMD_USER_DIR)/
	@echo "Installed units. Edit paths in $(SYSTEMD_USER_DIR) if clone is not ~/projects/cursor-claw"
	@echo "Then: loginctl enable-linger \$$USER && make systemd-enable"

systemd-reload: ## systemctl --user daemon-reload
	$(SYSTEMCTL) daemon-reload

systemd-enable: systemd-reload ## Enable and start bot + reminders timer
	$(SYSTEMCTL) enable --now $(BOT_SERVICE)
	$(SYSTEMCTL) enable --now $(REMINDERS_TIMER)

bot-start: ## Start bot service
	$(SYSTEMCTL) start $(BOT_SERVICE)

bot-stop: ## Stop bot service
	$(SYSTEMCTL) stop $(BOT_SERVICE)

bot-restart: ## Restart bot (pick up code changes)
	$(SYSTEMCTL) restart $(BOT_SERVICE)

bot-status: ## Show bot service status
	$(SYSTEMCTL) status $(BOT_SERVICE) --no-pager

bot-logs: ## Tail bot journal (Ctrl+C to exit)
	journalctl --user -u $(BOT_SERVICE) -n 50 --no-pager -f

deploy-pull: ## git pull $(REMOTE) $(BRANCH)
	git pull $(REMOTE) $(BRANCH)

deploy: deploy-pull bot-restart ## Pull latest code and restart bot
	@$(MAKE) bot-status

reminders-status: ## Show reminders timer status
	$(SYSTEMCTL) status $(REMINDERS_TIMER) --no-pager
