#!/bin/bash
# Keep Colima and the compose stack running; invoked by the LaunchAgent.
set -uo pipefail

PROJECT_DIR="${GIT_SYNAPSE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
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

cd "$PROJECT_DIR" || exit 1
if [ -x "$PROJECT_DIR/scripts/refresh-token.sh" ]; then
    if ! "$PROJECT_DIR/scripts/refresh-token.sh" >>"$LOG" 2>&1; then
        log "WARNING: could not refresh the GitHub token; the mounted file is unchanged"
    fi
fi

# --- 3. The stack ------------------------------------------------------------

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
