#!/usr/bin/env bash
# Off-host copy of the ledgers and the database (ARCHITECTURE_OPS §11), part of the lead's daily 09:00 ET ops check.
#
#   scripts/backup_offhost.sh [prod|staging] [--dest DIR] [--no-snapshot] [--dry-run]
#
# Copies /data/ai_calls.jsonl (required), /data/caps.json and /data/vendor_usage.jsonl, a fresh consistent snapshot of
# /data/ros.db without sandbox rows (store.backup.write_backup, run on the machine) and the newest nightly
# /data/backups/ros-*.db into DIR (default ~/restaurantos-backups/<app>/<UTC time>/, never inside the repo). Writes
# MANIFEST.sha256 for scripts/restore.sh and prints one line for BUILD_LOG. A Fly volume lives on one host, so this
# copy is what a restore starts from. Uses `fly ssh`; the copied files hold no secrets and none are printed.
set -euo pipefail
umask 077
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)

die() { printf 'backup_offhost: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

TARGET=prod
DEST=""
SNAPSHOT=1
DRY_RUN=0
while [ $# -gt 0 ]; do
  case $1 in
    staging | prod) TARGET=$1 ;;
    --dest) [ $# -ge 2 ] || die "--dest needs a folder"; DEST=$2; shift ;;
    --no-snapshot) SNAPSHOT=0 ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
if [ "$TARGET" = prod ]; then APP=restaurantos; else APP=restaurantos-staging; fi

PY=$REPO_ROOT/.venv/bin/python
[ -x "$PY" ] || PY=$(command -v python3 || true)
[ -n "$PY" ] || die "no python found"
FLY=${FLY_BIN:-$(command -v fly || command -v flyctl || true)}
[ -n "$FLY" ] || die "flyctl not found: brew install flyctl, then fly auth login"
# A deploy token (FLY_API_TOKEN) may not answer whoami; only an interactive login is checked here.
if [ "$DRY_RUN" = 0 ] && [ -z "${FLY_API_TOKEN:-}${FLY_ACCESS_TOKEN:-}" ]; then
  "$FLY" auth whoami > /dev/null 2>&1 || die "flyctl is not logged in: fly auth login"
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DEST=${DEST:-${RESTOS_BACKUP_DIR:-$HOME/restaurantos-backups}/$APP/$STAMP}
DEST=$("$PY" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DEST")
case "$DEST/" in "$REPO_ROOT"/*) die "--dest must be outside the repo: $DEST" ;; esac
[ ! -e "$DEST" ] || [ -z "$(ls -A "$DEST")" ] || die "$DEST is not empty; pick a new folder"
case $DEST in *[[:space:]]*) die "--dest must not contain spaces: $DEST" ;; esac
[ "$DRY_RUN" = 1 ] || mkdir -p "$DEST/backups"

fetch() { # fetch REMOTE LOCAL [optional]
  printf '+ %s ssh sftp get %s %s --app %s\n' "$FLY" "$1" "$2" "$APP" >&2
  [ "$DRY_RUN" = 0 ] || return 0
  if "$FLY" ssh sftp get "$1" "$2" --app "$APP" > /dev/null 2>&1 && [ -f "$2" ]; then return 0; fi
  [ "${3:-}" = optional ] || die "could not copy $1 from $APP"
  say "  (no $1 on $APP)"
}
remote() {
  printf '+ %s ssh console --app %s --command %s\n' "$FLY" "$APP" "$1" >&2
  [ "$DRY_RUN" = 0 ] || return 0
  "$FLY" ssh console --app "$APP" --command "$1"
}

say "off-host copy of $APP -> $DEST"
fetch /data/ai_calls.jsonl "$DEST/ai_calls.jsonl" # the spend ledger is the point of this copy: required
fetch /data/caps.json "$DEST/caps.json" optional
fetch /data/vendor_usage.jsonl "$DEST/vendor_usage.jsonl" optional

if [ "$SNAPSHOT" = 1 ]; then
  snapshot="from pathlib import Path; from restaurantos.store.backup import write_backup; \
write_backup(Path('/data/ros.db'), Path('/tmp/ros-offhost.db'))"
  if remote "/opt/venv/bin/python -c \"$snapshot\"" > /dev/null 2>&1; then
    fetch /tmp/ros-offhost.db "$DEST/ros.db"
    remote "rm -f /tmp/ros-offhost.db" > /dev/null 2>&1 || true
  else
    say "  warning: the fresh snapshot failed on the machine; relying on the nightly backup"
  fi
fi

latest=""
if [ "$DRY_RUN" = 0 ]; then
  latest=$("$FLY" ssh console --app "$APP" --command "sh -c 'ls -1t /data/backups/ros-*.db 2>/dev/null | head -n 1'" \
    2> /dev/null | tr -d '\r' | grep -E '^/data/backups/ros-[A-Za-z0-9._-]+\.db$' | tail -n 1 || true)
fi
if [ -n "$latest" ]; then
  fetch "$latest" "$DEST/backups/${latest##*/}"
else
  say "  (no nightly backup in /data/backups yet)"
fi

[ "$DRY_RUN" = 0 ] || exit 0
[ -f "$DEST/ros.db" ] || [ -n "$latest" ] || say "  warning: this copy has no database"
if command -v shasum > /dev/null; then sum=(shasum -a 256); else sum=(sha256sum); fi
(cd "$DEST" && find . -type f ! -name MANIFEST.sha256 | sort \
  | while IFS= read -r f; do "${sum[@]}" "$f"; done > MANIFEST.sha256)
files=$(($(wc -l < "$DEST/MANIFEST.sha256")))
kb=$(du -sk "$DEST" | awk '{ print $1 }')
ledger="no ledger"
[ ! -f "$DEST/ai_calls.jsonl" ] || ledger="ledger $(($(wc -l < "$DEST/ai_calls.jsonl"))) lines"
say "BUILD_LOG: $(date -u +%Y-%m-%dT%H:%MZ) off-host copy of $APP: $files files, ${kb} KB, $ledger -> $DEST"
