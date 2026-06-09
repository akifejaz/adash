#!/usr/bin/env bash
#
# dev.sh — local helpers for the adash dashboard.
#
# Usage:
#   ./dev.sh serve              Run the FastAPI server (live GitHub data).
#   ./dev.sh build              Build the static GitHub-Pages bundle.
#   ./dev.sh static             Build, then serve the bundle on :9000.
#   ./dev.sh refresh-ui         Re-copy index.html/app.js/style.css into the
#                               existing bundle without re-extracting zips.
#   ./dev.sh clean              Remove the built bundle.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PORT="${PORT:-8765}"
STATIC_PORT="${STATIC_PORT:-9000}"
SITE_DIR="${SITE_DIR:-/tmp/adash-site}"

# Reuse `gh auth token` so users don't have to export GITHUB_TOKEN manually.
if [[ -z "${GITHUB_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
  if tok="$(gh auth token 2>/dev/null)" && [[ -n "$tok" ]]; then
    export GITHUB_TOKEN="$tok"
  fi
fi

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

ensure_deps() {
  python3 -c 'import fastapi, uvicorn' 2>/dev/null || {
    log "Installing Python dependencies"
    pip install -q -r requirements.txt
  }
}

copy_ui() {
  mkdir -p "$SITE_DIR/static"
  cp dashboard/static/index.html "$SITE_DIR/index.html"
  cp dashboard/static/app.js     "$SITE_DIR/static/app.js"
  cp dashboard/static/style.css  "$SITE_DIR/static/style.css"
}

cmd_serve() {
  ensure_deps
  log "FastAPI dashboard at http://localhost:${PORT}"
  exec uvicorn dashboard.server:app --host 0.0.0.0 --port "$PORT" --reload
}

cmd_build() {
  ensure_deps
  log "Building static bundle in $SITE_DIR"
  rm -rf "$SITE_DIR"
  copy_ui
  log "Fetching manifest from GitHub"
  python3 -m dashboard.github_source --output "$SITE_DIR/manifest.json"
  log "Extracting recipe/log/manifest for every package (this takes a few minutes)"
  python3 -m dashboard.build_static --manifest "$SITE_DIR/manifest.json" --out "$SITE_DIR" --workers 16
  log "Bundle ready ($(du -sh "$SITE_DIR" | cut -f1))"
}

cmd_static() {
  [[ -f "$SITE_DIR/manifest.json" ]] || cmd_build
  local port="$STATIC_PORT"
  # If the chosen port is busy (e.g. a previous run was left over), walk
  # forward until we find a free one rather than crashing.
  while python3 -c "import socket,sys; s=socket.socket(); 
ret=s.connect_ex(('127.0.0.1', $port)); s.close(); sys.exit(0 if ret==0 else 1)"; do
    log "Port $port is busy, trying $((port + 1))"
    port=$((port + 1))
  done
  log "Static site at http://localhost:${port}"
  cd "$SITE_DIR"
  exec python3 -m http.server "$port"
}

cmd_refresh_ui() {
  [[ -d "$SITE_DIR" ]] || die "no bundle at $SITE_DIR — run './dev.sh build' first"
  copy_ui
  log "UI files refreshed in $SITE_DIR (hard-reload the browser)"
}

cmd_clean() {
  rm -rf "$SITE_DIR"
  log "Removed $SITE_DIR"
}

case "${1:-}" in
  serve)        cmd_serve ;;
  build)        cmd_build ;;
  static)       cmd_static ;;
  refresh-ui)   cmd_refresh_ui ;;
  clean)        cmd_clean ;;
  ""|-h|--help)
    sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "unknown command: $1  (try './dev.sh --help')"
    ;;
esac
