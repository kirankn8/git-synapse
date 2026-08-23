#!/usr/bin/env bash
# Write the current GitHub credential where the containers read it.
#
# The source is `bulwark`, not `gh auth token`: on this machine `gh` is a shell
# function that fetches the token from bulwark and passes it in the environment,
# so `gh auth token` returns nothing from any non-interactive shell -- which is
# how an empty token reached the stack once already.
#
# Containers read this file on every use, so refreshing it is enough; nothing
# needs restarting. bulwark issues short-lived credentials, hence --min, which
# asks for one valid for at least that many minutes.
set -euo pipefail

TOKEN_FILE="${GIT_SYNAPSE_TOKEN_FILE:-$HOME/.git-synapse/github-token}"
# --min is in HOURS and bulwark accepts 1-8: refresh when the current
# credential has less than this long to live.
MIN_HOURS="${GIT_SYNAPSE_TOKEN_MIN_HOURS:-4}"

fresh=$(bulwark token get --min "$MIN_HOURS" 2>/dev/null || true)
if [ -z "$fresh" ]; then
    # Never truncate a working token because the issuer was briefly unavailable.
    echo "bulwark returned nothing; leaving $TOKEN_FILE unchanged" >&2
    exit 1
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
umask 077
# Written in place rather than renamed into position. A rename is atomic on this
# filesystem but swaps the inode, and the containers see the path vanish for a
# couple of seconds across the virtiofs mount -- measured, not theorised. A
# truncate-and-write keeps the path present throughout; readers guard against a
# torn read by checking the token's shape.
touch "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
printf '%s' "$fresh" > "$TOKEN_FILE"
