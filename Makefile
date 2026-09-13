# Convenience wrappers around docker compose. Everything here is optional --
# the compose commands underneath work fine on their own.

.DEFAULT_GOAL := help
COMPOSE := docker compose
CLI := $(COMPOSE) run --rm cli

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: up
up:  ## Start postgres, api, scheduler and mcp
	$(COMPOSE) up -d --build

.PHONY: down
down:  ## Stop all services (data is preserved)
	$(COMPOSE) down

.PHONY: nuke
nuke:  ## Stop everything and delete the database and mirrors
	$(COMPOSE) down -v

.PHONY: logs
logs:  ## Tail logs from every service
	$(COMPOSE) logs -f --tail=80

.PHONY: token
token:  ## Hand this machine's GitHub credential to the containers
	@./scripts/refresh-token.sh

.PHONY: psql
psql:  ## Open a psql shell
	$(COMPOSE) exec postgres psql -U git_synapse -d git_synapse

.PHONY: test
test:  ## Run the test suite against the compose database
	POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=$${POSTGRES_PUBLISHED_PORT:-55432} \
	PYTHONPATH=src .venv/bin/python -m pytest -q

.PHONY: skills-install
skills-install:  ## Make the skill available in every repo (symlink into ~/.claude/skills)
	@mkdir -p "$$HOME/.claude/skills"
	@for d in skills/*/; do \
		name=$$(basename "$$d"); \
		target="$$HOME/.claude/skills/$$name"; \
		if [ -e "$$target" ] && [ ! -L "$$target" ]; then \
			echo "SKIP $$name: a real directory already exists there"; \
		else \
			ln -sfn "$(CURDIR)/$$d" "$$target"; \
			echo "linked $$name -> $(CURDIR)/$$d"; \
		fi; \
	done
	@echo "symlinked, so editing skills/ updates it everywhere"

.PHONY: skills-uninstall
skills-uninstall:  ## Remove the user-level skill symlink
	@for d in skills/*/; do \
		name=$$(basename "$$d"); \
		target="$$HOME/.claude/skills/$$name"; \
		[ -L "$$target" ] && rm -f "$$target" && echo "unlinked $$name" || true; \
	done

.PHONY: hostname-install
hostname-install:  ## Map http://git-synapse to this stack in /etc/hosts (needs sudo)
	@if grep -qE '^[0-9.]+[[:space:]]+.*\bgit-synapse\b' /etc/hosts; then \
		echo "already present:"; grep -nE '\bgit-synapse\b' /etc/hosts; \
	else \
		printf '%s\n' \
		  '' \
		  '# Git Synapse — change-coupling statistics (added by make hostname-install)' \
		  '127.0.0.1       git-synapse git-synapse.test' \
		  | sudo tee -a /etc/hosts >/dev/null && \
		echo "added: git-synapse, git-synapse.test -> 127.0.0.1"; \
	fi
	@echo "verify: curl -sS http://git-synapse/api/health"

.PHONY: hostname-uninstall
hostname-uninstall:  ## Remove the /etc/hosts entry (needs sudo)
	@sudo sed -i '' '/added by make hostname-install/d; /^127\.0\.0\.1[[:space:]]*git-synapse git-synapse\.test$$/d' /etc/hosts
	@echo "removed git-synapse from /etc/hosts"

.PHONY: daemon-install
daemon-install:  ## Install the macOS LaunchAgent (starts Colima + stack at login)
	@mkdir -p "$$HOME/Library/LaunchAgents"
	@sed -e "s|__SCRIPT__|$(CURDIR)/scripts/git-synapse-daemon.sh|g" \
	     -e "s|__PROJECT__|$(CURDIR)|g" \
	     -e "s|__LOG__|$$HOME/Library/Logs/git-synapse-launchd.log|g" \
	     scripts/com.git-synapse.stack.plist.template \
	     > "$$HOME/Library/LaunchAgents/com.git-synapse.stack.plist"
	@chmod +x scripts/git-synapse-daemon.sh
	@plutil -lint "$$HOME/Library/LaunchAgents/com.git-synapse.stack.plist"
	@launchctl unload "$$HOME/Library/LaunchAgents/com.git-synapse.stack.plist" 2>/dev/null || true
	@launchctl load -w "$$HOME/Library/LaunchAgents/com.git-synapse.stack.plist"
	@echo "installed: com.git-synapse.stack (runs at login, re-checks every 5 min)"

.PHONY: daemon-uninstall
daemon-uninstall:  ## Remove the macOS LaunchAgent (leaves containers running)
	@launchctl unload "$$HOME/Library/LaunchAgents/com.git-synapse.stack.plist" 2>/dev/null || true
	@rm -f "$$HOME/Library/LaunchAgents/com.git-synapse.stack.plist"
	@echo "removed: com.git-synapse.stack"

.PHONY: daemon-status
daemon-status:  ## Show LaunchAgent state, recent daemon log, and next scheduled runs
	@echo "── launchctl ──"
	@launchctl list | grep -i git-synapse || echo "  not loaded"
	@echo "── daemon log (last 8) ──"
	@tail -8 "$$HOME/Library/Logs/git-synapse-daemon.log" 2>/dev/null || echo "  no log yet"
	@echo "── next scheduled runs ──"
	@$(COMPOSE) logs scheduler 2>/dev/null | grep -oE "(fast refresh|discovery) cron .*" | tail -2 || true

.PHONY: venv
venv:  ## Create the local dev virtualenv
	python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt pytest pytest-cov
