#!/bin/bash
#
# Keep the Git Synapse stack running on macOS.
#
# Invoked by a LaunchAgent at login and then every few minutes as a watchdog.
# Idempotent by design: if everything is already up it does nothing and exits 0,
# so running it on a short interval is cheap.
#
# The step that actually matters is starting Colima. Compose's
# `restart: unless-stopped` brings containers back whenever the Docker daemon
# returns, but Colima is a user-level VM that does NOT start at login on its
# own -- so without this, a reboot leaves the whole stack down and the in-container
# scheduler never fires.

set -uo pipefail

PROJECT_DIR="${GIT_SYNAPSE_DIR:-$HOME/Documents/git-synapse}"
COLIMA_PROFILE="${COLIMA_PROFILE:-default}"
LOG="${GIT_SYNAPSE_LOG:-$HOME/Library/Logs/git-synapse-daemon.log}"

# LaunchAgents get a minimal PATH; Homebrew is not on it.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

mkdir -p "$(dirname "$LOG")"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

for tool in colima docker; do
    command -v "$tool" >/dev/null 2>&1 || { log "FATAL: $tool not on PATH"; exit 1; }
done

[ -f "$PROJECT_DIR/docker-compose.yml" ] || { log "FATAL: no compose file in $PROJECT_DIR"; exit 1; }

# --- 1. Colima ---------------------------------------------------------------
if ! colima status --profile "$COLIMA_PROFILE" >/dev/null 2>&1; then
    log "colima '$COLIMA_PROFILE' is down; starting"
    if ! colima start --profile "$COLIMA_PROFILE" >>"$LOG" 2>&1; then
        log "ERROR: colima start failed"
        exit 1
    fi
    log "colima started"
fi

# The VM can report running before the Docker socket accepts connections.
for _ in $(seq 1 30); do
    docker info >/dev/null 2>&1 && break
    sleep 2
done
if ! docker info >/dev/null 2>&1; then
    log "ERROR: docker daemon unreachable after colima start"
    exit 1
fi

# --- 2. Keep the GitHub credential fresh -------------------------------------
# `gh auth token` issues short-lived ghu_ tokens. When one expires every fetch
# fails, and before this was hardened the fallback re-clone deleted 213 working
# mirrors. The daemon runs on the host, where gh is authenticated, so it can
# refresh the value the containers read.
cd "$PROJECT_DIR" || exit 1
if command -v gh >/dev/null 2>&1 && [ -f .env ]; then
    fresh=$(gh auth token 2>/dev/null || true)
    current=$(sed -n 's/^GITHUB_TOKEN=//p' .env | head -1)
    if [ -n "$fresh" ] && [ "$fresh" != "$current" ]; then
        tmp=$(mktemp)
        sed "s|^GITHUB_TOKEN=.*|GITHUB_TOKEN=$fresh|" .env > "$tmp" && mv "$tmp" .env
        log "refreshed GITHUB_TOKEN in .env; restarting scheduler to pick it up"
        docker compose up -d --force-recreate scheduler >>"$LOG" 2>&1 || \
            log "WARNING: scheduler restart after token refresh failed"
    fi
fi

# --- 3. The stack ------------------------------------------------------------

# Long-running services only. `cli` sits behind a compose profile and must not
# be started here.
EXPECTED="postgres api scheduler mcp"
missing=""
for svc in $EXPECTED; do
    state=$(docker compose ps --status running --services 2>/dev/null | grep -Fx "$svc" || true)
    [ -z "$state" ] && missing="$missing $svc"
done

if [ -n "$missing" ]; then
    log "starting services:$missing"
    if docker compose up -d >>"$LOG" 2>&1; then
        log "compose up complete"
    else
        log "ERROR: compose up failed"
        exit 1
    fi
else
    # Quiet on the happy path, so a 5-minute watchdog does not spam the log.
    exit 0
fi

# --- 4. Confirm the API answers ---------------------------------------------
port="$(grep -E '^API_PUBLISHED_PORT=' .env 2>/dev/null | cut -d= -f2)"
port="${port:-8080}"
for _ in $(seq 1 30); do
    if curl -fsS --max-time 3 "http://localhost:${port}/api/health" >/dev/null 2>&1; then
        log "git-synapse healthy on :${port}"
        exit 0
    fi
    sleep 2
done
log "WARNING: stack started but API not healthy on :${port} yet"
exit 0
