#!/usr/bin/env bash
# Run RestaurantOS on the founders' laptop (the primary local runner).
#
#   scripts/dev.sh                build the web app if needed, then serve http://127.0.0.1:8000
#   scripts/dev.sh --tunnel       also publish it through a Cloudflare quick tunnel (https://<name>.trycloudflare.com)
#   scripts/dev.sh --new-tunnel   the same with a fresh tunnel (a new URL)
#   scripts/dev.sh --stop-tunnel  close the tunnel and exit
#   options: --build (force the web build), --no-build, --port N (default 8000)
#
# With a tunnel the order is always tunnel first, then app: cloudflared starts, its trycloudflare URL is parsed from its
# output and exported to the app as RESTOS_PUBLIC_BASE_URL (CSP, the socket Origin check, Twilio stream URLs), and
# client IPs come from CF-Connecting-IP (RESTOS_BEHIND_PROXY=cloudflare). The tunnel runs in its own session, so Ctrl-C
# stops only the app and the next `scripts/dev.sh --tunnel` reuses the same URL. Open that URL, not 127.0.0.1.
# The AI mode is whatever the environment says: mock by default; RESTOS_AI_MODE=live plus RESTOS_LIVE_TAG=<tag> for
# real Meta calls. This script prints no secrets and never touches Twilio: pointing the number at a tunnel is a
# separate step that needs founder approval.
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$REPO_ROOT"

PY=${RESTOS_DEV_PYTHON:-$REPO_ROOT/.venv/bin/python}
CLOUDFLARED=${CLOUDFLARED_BIN:-cloudflared}
STATE_DIR=${RESTOS_DEV_STATE_DIR:-$HOME/.restaurantos/dev}
TUNNEL_WAIT_S=${RESTOS_TUNNEL_WAIT_S:-45}
PID_FILE=$STATE_DIR/tunnel.pid
URL_FILE=$STATE_DIR/tunnel.url
LOG_FILE=$STATE_DIR/tunnel.log

die() { printf 'dev: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

TUNNEL=0
NEW_TUNNEL=0
STOP=0
BUILD=auto
PORT=${RESTOS_PORT:-8000}
while [ $# -gt 0 ]; do
  case $1 in
    --tunnel) TUNNEL=1 ;;
    --new-tunnel) TUNNEL=1; NEW_TUNNEL=1 ;;
    --stop-tunnel) STOP=1 ;;
    --build) BUILD=force ;;
    --no-build) BUILD=skip ;;
    --port) [ $# -ge 2 ] || die "--port needs a number"; PORT=$2; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
case $PORT in '' | *[!0-9]*) die "--port must be a number" ;; esac

# ----- tunnel ----------------------------------------------------------------------------------------------------

tunnel_pid() { # the recorded tunnel's pid while that process is still cloudflared ($1 = port, or any)
  local pid args
  [ -f "$PID_FILE" ] || return 1
  pid=$(cat "$PID_FILE")
  case $pid in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$pid" 2> /dev/null || return 1
  args=$(ps -p "$pid" -o args= 2> /dev/null || true)
  case $args in *cloudflared*) ;; *) return 1 ;; esac
  case $1 in any) ;; *) case $args in *"127.0.0.1:$1"*) ;; *) return 1 ;; esac ;; esac
  printf '%s' "$pid"
}

stop_tunnel() {
  local pid
  if pid=$(tunnel_pid any); then
    kill "$pid" 2> /dev/null || true
    say "tunnel stopped"
  fi
  rm -f "$PID_FILE" "$URL_FILE"
}

start_tunnel() {
  local pid url waited=0
  command -v "$CLOUDFLARED" > /dev/null || die "cloudflared not found: brew install cloudflared"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  rm -f "$URL_FILE"
  # A new session (setsid): the terminal's Ctrl-C reaches the app, never the tunnel.
  "$PY" -c 'import os, sys
try:
    os.setsid()
except OSError:
    os.setpgid(0, 0)
os.execvp(sys.argv[1], sys.argv[1:])' \
    "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" < /dev/null > "$LOG_FILE" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  say "starting a Cloudflare quick tunnel to 127.0.0.1:$PORT (log: $LOG_FILE)"
  while [ "$waited" -lt "$TUNNEL_WAIT_S" ]; do
    url=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2> /dev/null \
      | grep -v '^https://api\.' | head -n 1 || true)
    if [ -n "$url" ]; then
      printf '%s\n' "$url" > "$URL_FILE"
      return 0
    fi
    kill -0 "$pid" 2> /dev/null || die "cloudflared exited before publishing a URL; see $LOG_FILE"
    sleep 1
    waited=$((waited + 1))
  done
  stop_tunnel
  die "no trycloudflare URL after ${TUNNEL_WAIT_S}s; see $LOG_FILE"
}

if [ "$STOP" = 1 ]; then
  stop_tunnel
  exit 0
fi

# ----- web build -------------------------------------------------------------------------------------------------

web_stale() { # true when dist is missing or older than any web source file
  local dist=src/web/dist/index.html
  [ -f "$dist" ] || return 0
  [ -n "$(find src/web/src src/web/public src/web/index.html src/web/package.json src/web/package-lock.json \
    src/web/vite.config.ts src/web/tsconfig.json -newer "$dist" -type f 2> /dev/null | head -n 1 || true)" ]
}
build_web() {
  command -v npm > /dev/null || die "npm not found: install Node 22+ (brew install node)"
  if [ ! -d src/web/node_modules ]; then
    say "installing the web dependencies (npm ci)"
    npm --prefix src/web ci --no-audit --no-fund
  fi
  say "building the web app"
  npm --prefix src/web run build
}
case $BUILD in
  force) build_web ;;
  auto) if web_stale; then build_web; fi ;;
  skip) ;;
esac

# ----- app -------------------------------------------------------------------------------------------------------

[ -x "$PY" ] || die "no Python at $PY: python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
# Before any tunnel: a tunnel to a port another program already serves would publish that program.
"$PY" -c 'import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does: a just-stopped app is not "in use"
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()' "$PORT" || die "port $PORT is already in use on 127.0.0.1 (another program?): pick a free one with --port N"
export RESTOS_HOST=127.0.0.1
export RESTOS_PORT=$PORT
# One session secret per laptop (outside the repo, 0600, never printed): a restart keeps sessions and caller hashes.
if [ -z "${RESTOS_SESSION_SECRET:-}" ]; then
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  if [ ! -s "$STATE_DIR/session_secret" ]; then
    (umask 077 && "$PY" -c 'import secrets; print(secrets.token_urlsafe(48))' > "$STATE_DIR/session_secret")
  fi
  RESTOS_SESSION_SECRET=$(cat "$STATE_DIR/session_secret")
  export RESTOS_SESSION_SECRET
fi
if [ "$TUNNEL" = 1 ]; then
  if [ "$NEW_TUNNEL" = 0 ] && tunnel_pid "$PORT" > /dev/null && [ -s "$URL_FILE" ]; then
    say "reusing the running tunnel"
  else
    stop_tunnel
    start_tunnel
  fi
  RESTOS_PUBLIC_BASE_URL=$(cat "$URL_FILE")
  export RESTOS_PUBLIC_BASE_URL
  export RESTOS_BEHIND_PROXY=cloudflare
  say ""
  say "  Public URL: $RESTOS_PUBLIC_BASE_URL"
  say "  Open it in the browser (not 127.0.0.1): the CSP and the socket Origin check name this URL."
  say "  The tunnel outlives Ctrl-C; scripts/dev.sh --stop-tunnel closes it. Twilio is not touched."
  say ""
else
  export RESTOS_PUBLIC_BASE_URL=${RESTOS_PUBLIC_BASE_URL:-http://127.0.0.1:$PORT}
  if tunnel_pid any > /dev/null && [ -s "$URL_FILE" ]; then
    say "note: a tunnel is still up at $(cat "$URL_FILE"); add --tunnel to serve through it, or --stop-tunnel"
  fi
fi
say "AI mode: ${RESTOS_AI_MODE:-mock (the default; RESTOS_AI_MODE=live and RESTOS_LIVE_TAG=<tag> for real calls)}"
say "serving $RESTOS_PUBLIC_BASE_URL -> http://127.0.0.1:$PORT (Ctrl-C stops the app)"
exec "$PY" -m restaurantos
