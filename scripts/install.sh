#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${GIT_SYNAPSE_REPO_URL:-https://github.com/kirankn8/git-synapse.git}"
DEFAULT_DIR="${GIT_SYNAPSE_DIR:-$HOME/git-synapse}"

resolve_ref() {
  if [[ -n "${GIT_SYNAPSE_REF:-}" ]]; then
    printf '%s' "$GIT_SYNAPSE_REF"
    return
  fi
  local tag=""
  if [[ "$REPO_URL" == "https://github.com/kirankn8/git-synapse.git" ]]; then
    tag="$(curl -fsSL --max-time 8 https://api.github.com/repos/kirankn8/git-synapse/releases/latest 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1 || true)"
  fi
  if [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf '%s' "$tag"
  else
    printf '%s' main
  fi
}

info() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then return; fi
    case "$(uname -s)" in
      Darwin) open -a Docker >/dev/null 2>&1 || true ;;
      Linux) sudo systemctl start docker >/dev/null 2>&1 || true ;;
    esac
    for _ in $(seq 1 30); do
      if docker info >/dev/null 2>&1; then return; fi
      sleep 2
    done
    die "Docker is installed but is not running. Start Docker Desktop (macOS) or the Docker service (Linux), then run this command again."
  fi

  printf 'Docker and Docker Compose are required. Install them now? [Y/n] '
  read -r answer
  answer=${answer:-Y}
  [[ "$answer" =~ ^[Yy]$ ]] || die "Install Docker, then run this command again: https://docs.docker.com/get-docker/"

  case "$(uname -s)" in
    Darwin)
      command -v brew >/dev/null 2>&1 || die "Homebrew is not installed. Install Docker Desktop manually: https://www.docker.com/products/docker-desktop/"
      brew install --cask docker
      open -a Docker >/dev/null 2>&1 || true
      ;;
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y docker.io docker-compose-plugin
        sudo systemctl enable --now docker >/dev/null 2>&1 || true
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y docker docker-compose-plugin
        sudo systemctl enable --now docker >/dev/null 2>&1 || true
      else
        die "No supported package manager found. Install Docker Engine and Compose: https://docs.docker.com/engine/install/"
      fi
      ;;
    *)
      die "Install Docker Desktop, then run this command again: https://www.docker.com/products/docker-desktop/"
      ;;
  esac

  for _ in $(seq 1 30); do
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then return; fi
    sleep 2
  done
  die "Docker was installed but is not running yet. Start Docker Desktop and run this command again."
}

install_docker

TARGET_DIR="${1:-$DEFAULT_DIR}"
TARGET_DIR="${TARGET_DIR/#\~/$HOME}"
INSTALL_REF="$(resolve_ref)"
if [[ -e "$TARGET_DIR" && ! -d "$TARGET_DIR" ]]; then die "$TARGET_DIR exists and is not a directory."; fi

if [[ -d "$TARGET_DIR/.git" ]]; then
  info "Updating Git Synapse in $TARGET_DIR"
  if [[ -n "$(git -C "$TARGET_DIR" status --porcelain)" ]]; then
    info "Local changes found; leaving the existing checkout untouched."
  else
    git -C "$TARGET_DIR" fetch --tags origin
    git -C "$TARGET_DIR" checkout --detach "$INSTALL_REF"
    git -C "$TARGET_DIR" pull --ff-only origin "$INSTALL_REF" 2>/dev/null || true
  fi
elif [[ -e "$TARGET_DIR" && -n "$(find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  die "$TARGET_DIR is not empty and is not a Git Synapse checkout. Choose another directory: $0 /path/to/git-synapse"
else
  info "Downloading Git Synapse to $TARGET_DIR"
  mkdir -p "$(dirname "$TARGET_DIR")"
  git clone --branch "$INSTALL_REF" "$REPO_URL" "$TARGET_DIR"
fi

cd "$TARGET_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
  info "Created .env from .env.example (it was not overwritten if it already existed)."
fi

info "Starting Git Synapse"
docker compose up -d --build

PORT="$(sed -n 's/^API_PUBLISHED_PORT=//p' .env | tail -n 1)"
PORT="${PORT:-8080}"
URL="http://localhost:${PORT}"
printf 'Waiting for Git Synapse at %s' "$URL"
for _ in $(seq 1 60); do
  if curl -fsS "$URL/api/health" >/dev/null 2>&1; then
    printf ' ready.\n'
    info "Git Synapse is ready"
    printf 'Open: %s\n' "$URL"
    printf '\nNothing else to do: the dashboard is there.\n'
    printf '\nThis deployment answers anyone who can reach it. To require a sign-in,\n'
    printf 'set both in %q/.env and restart:\n' "$TARGET_DIR"
    printf '  ADMIN_EMAIL=you@example.com\n'
    printf '  ADMIN_PASSWORD=something-long-and-unguessable\n'
    printf '\n  cd %q && docker compose up -d\n' "$TARGET_DIR"
    exit 0
  fi
  printf '.'
  sleep 2
done
printf '\n'
docker compose ps
die "Git Synapse did not become healthy. Inspect logs with: cd $(printf '%q' "$TARGET_DIR") && docker compose logs --tail=100 api"
