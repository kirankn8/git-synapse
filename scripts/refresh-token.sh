#!/usr/bin/env bash
# Write this machine's GitHub credential where the containers read it.
set -uo pipefail

TOKEN_FILE="${GIT_SYNAPSE_TOKEN_FILE:-$HOME/.git-synapse/github-token}"
QUIET="${GIT_SYNAPSE_TOKEN_QUIET:-0}"

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$1" >&2; }

find_gh() {
    command -v gh 2>/dev/null && return 0
    for p in /opt/homebrew/bin/gh /usr/local/bin/gh "$HOME/.local/bin/gh"; do
        [ -x "$p" ] && printf '%s' "$p" && return 0
    done
    return 1
}

from_command() {
    [ -n "${GIT_SYNAPSE_TOKEN_CMD:-}" ] || return 1
    eval "$GIT_SYNAPSE_TOKEN_CMD" 2>/dev/null
}

from_gh() {
    local gh; gh="$(find_gh)" || return 1
    "$gh" auth token 2>/dev/null
}

from_git_credential() {
    command -v git >/dev/null 2>&1 || return 1
    printf 'protocol=https\nhost=github.com\n\n' \
        | git credential fill 2>/dev/null \
        | sed -n 's/^password=//p'
}

from_env() { printf '%s' "${GITHUB_TOKEN:-}"; }

looks_like_token() {
    printf '%s' "$1" | grep -Eq '^(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|[0-9a-f]{40})$'
}

fresh=""
for source in from_command from_gh from_git_credential from_env; do
    candidate="$("$source" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$candidate" ] && looks_like_token "$candidate"; then
        fresh="$candidate"
        say "token from ${source#from_}"
        break
    fi
done

if [ -z "$fresh" ]; then
    say "No GitHub credential found on this machine."
    say ""
    say "  gh auth login              # the usual fix; this script then finds it"
    say "  GIT_SYNAPSE_TOKEN_CMD=...  # or name a command that prints a token"
    say ""
    say "Without one, GitHub allows 60 API requests an hour, which is enough to"
    say "add repositories one URL at a time but not to list a large organisation."
    # Never truncate a working token because the issuer was briefly unavailable.
    [ -s "$TOKEN_FILE" ] && say "Leaving the existing $TOKEN_FILE alone."
    exit 1
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
umask 077
touch "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
printf '%s' "$fresh" > "$TOKEN_FILE"
say "wrote $TOKEN_FILE (${#fresh} characters); containers pick it up on their next request"
