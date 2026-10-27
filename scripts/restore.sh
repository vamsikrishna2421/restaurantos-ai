#!/usr/bin/env bash
# Restore runbook (ARCHITECTURE_OPS §11); rehearse it once on staging before Oct 31.
#
#   scripts/restore.sh <staging|prod> --from DIR [--new-volume] [--image REF] [--db FILE] [--no-ledger]
#                      [--dry-run] [--yes]
#
# DIR is an off-host copy made by scripts/backup_offhost.sh: ros.db (else the newest backups/ros-*.db),
# ai_calls.jsonl, caps.json and, when present, vendor_usage.jsonl. Steps:
#   1. check the copy against its MANIFEST.sha256 and pick the database;
#   2. --new-volume (the host or its volume is lost): destroy the app's machines and ros_data volumes, then create a
#      fresh 3 GB volume. Without it the existing volume is restored in place;
#   3. boot the same image in maintenance mode (RESTOS_MAINTENANCE=1: the image's CMD sleeps, the app stays down);
#   4. move the current files to /data/pre-restore-<time>/, upload the copy, check every SHA-256 on the machine;
#   5. write the .volume-id sentinel if it is missing, deploy the image normally, run the smoke test.
# --no-ledger restores without ai_calls.jsonl: SpendGuard then starts in deterministic mode (spent = total cap) until
# an admin posts the baseline reconciled against the Meta dashboard to /api/admin/caps (the command is printed).
# The app keeps its URL, so Twilio needs no change; a restore onto another host means re-pointing both numbers with
# scripts/twilio_setup.py --target <url>, a separate step that needs founder approval. Prints no secrets.
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$REPO_ROOT"

die() { printf 'restore: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

TARGET=""
FROM=""
NEW_VOLUME=0
IMAGE=""
DB=""
NO_LEDGER=0
DRY_RUN=0
YES=0
while [ $# -gt 0 ]; do
  case $1 in
    staging | prod) TARGET=$1 ;;
    --from) [ $# -ge 2 ] || die "--from needs a folder"; FROM=$2; shift ;;
    --image) [ $# -ge 2 ] || die "--image needs an image reference"; IMAGE=$2; shift ;;
    --db) [ $# -ge 2 ] || die "--db needs a file"; DB=$2; shift ;;
    --new-volume) NEW_VOLUME=1 ;;
    --no-ledger) NO_LEDGER=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) YES=1 ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
[ -n "$TARGET" ] || die "name the target: staging or prod (see --help)"
[ -n "$FROM" ] || die "--from DIR is required (a folder made by scripts/backup_offhost.sh)"
APP=restaurantos-staging
CONFIG=fly.staging.toml
if [ "$TARGET" = prod ]; then APP=restaurantos; CONFIG=fly.toml; fi

PY=.venv/bin/python
[ -x "$PY" ] || PY=$(command -v python3 || true)
[ -n "$PY" ] || die "no python found"
FLY=${FLY_BIN:-$(command -v fly || command -v flyctl || true)}
[ -n "$FLY" ] || die "flyctl not found: brew install flyctl, then fly auth login"
# A deploy token (FLY_API_TOKEN) may not answer whoami; only an interactive login is checked here.
if [ "$DRY_RUN" = 0 ] && [ -z "${FLY_API_TOKEN:-}${FLY_ACCESS_TOKEN:-}" ]; then
  "$FLY" auth whoami > /dev/null 2>&1 || die "flyctl is not logged in: fly auth login"
fi

run() {
  printf '+ %s\n' "$*" >&2
  [ "$DRY_RUN" = 1 ] || "$@"
}
remote() { run "$FLY" ssh console --app "$APP" --command "$1"; }
json_field() { "$PY" -c "import json, sys; data = json.load(sys.stdin) or []; $1"; }
sha256_of() {
  if command -v shasum > /dev/null; then shasum -a 256 "$1"; else sha256sum "$1"; fi | awk '{ print $1 }'
}

# ----- 1. the off-host copy ------------------------------------------------------------------------------------

[ -d "$FROM" ] || die "--from $FROM is not a folder"
FROM=$(cd "$FROM" && pwd -P)
if [ -f "$FROM/MANIFEST.sha256" ]; then
  if command -v shasum > /dev/null; then check=(shasum -a 256 -c); else check=(sha256sum -c); fi
  (cd "$FROM" && "${check[@]}" MANIFEST.sha256 > /dev/null) || die "$FROM does not match its MANIFEST.sha256"
else
  say "note: $FROM has no MANIFEST.sha256; the files are not checked against the copy's own hashes"
fi
if [ -z "$DB" ]; then
  DB="$FROM/ros.db"
  [ -f "$DB" ] || DB=$(ls -1t "$FROM"/backups/ros-*.db 2> /dev/null | head -n 1 || true)
fi
[ -n "$DB" ] && [ -f "$DB" ] || die "no database in $FROM (ros.db or backups/ros-*.db); pass --db FILE"

FILES=("$DB" /data/ros.db)
if [ -f "$FROM/ai_calls.jsonl" ] && [ "$NO_LEDGER" = 0 ]; then
  FILES+=("$FROM/ai_calls.jsonl" /data/ai_calls.jsonl)
elif [ "$NO_LEDGER" = 0 ]; then
  die "no ai_calls.jsonl in $FROM: rerun with --no-ledger to boot in deterministic mode"
fi
[ ! -f "$FROM/caps.json" ] || FILES+=("$FROM/caps.json" /data/caps.json)
[ ! -f "$FROM/vendor_usage.jsonl" ] || FILES+=("$FROM/vendor_usage.jsonl" /data/vendor_usage.jsonl)
for ((i = 0; i < ${#FILES[@]}; i += 2)); do
  case ${FILES[$i]} in *[[:space:]]*) die "move the copy to a path without spaces: ${FILES[$i]}" ;; esac
done

# ----- 2. image, plan, confirmation ------------------------------------------------------------------------------

machine_ids() {
  "$FLY" machines list --app "$APP" --json | json_field 'print("\n".join(m.get("id", "") for m in data))'
}
volume_ids() {
  "$FLY" volumes list --app "$APP" --json \
    | json_field '
print("\n".join(v.get("id", "") for v in data if v.get("name") == "ros_data" and "destroy" not in str(v.get("state"))))'
}
if [ -z "$IMAGE" ]; then
  IMAGE=$("$FLY" machines list --app "$APP" --json | json_field '
ms = [m for m in data if m.get("state") == "started"] or data
print(((ms[0].get("config") or {}).get("image") or "") if ms else "")') || true
fi
[ -n "$IMAGE" ] || die "cannot tell which image $APP runs; pass --image (fly releases --app $APP --image)"

say "Restore plan for $APP"
say "  image:  $IMAGE"
for ((i = 0; i < ${#FILES[@]}; i += 2)); do say "  upload: ${FILES[$i]} -> ${FILES[$((i + 1))]}"; done
[ "$NO_LEDGER" = 0 ] || say "  ledger: none, deterministic mode until the reconciled baseline is posted"
if [ "$NEW_VOLUME" = 1 ]; then
  say "  volume: DESTROY the machines and ros_data volumes of $APP (with their Fly snapshots), create a fresh 3 GB one"
else
  say "  volume: restore in place (current files move to /data/pre-restore-<time>/)"
fi
if [ "$YES" = 0 ]; then
  [ -t 0 ] || die "needs confirmation: rerun with --yes"
  printf 'Type %s to continue: ' "$APP" >&2
  read -r answer
  [ "$answer" = "$APP" ] || die "cancelled"
fi

# ----- 3. volume and maintenance boot ----------------------------------------------------------------------------

if [ "$NEW_VOLUME" = 1 ]; then
  for machine in $(machine_ids); do run "$FLY" machine destroy "$machine" --app "$APP" --force; done
  for volume in $(volume_ids); do run "$FLY" volumes destroy "$volume" --app "$APP" --yes; done
  run "$FLY" volumes create ros_data --app "$APP" --region iad --size 3 --yes
fi
VOLUME_ID=$(volume_ids | head -n 1 || true)
[ "$DRY_RUN" = 0 ] || VOLUME_ID=${VOLUME_ID:-vol_dryrun}
[ -n "$VOLUME_ID" ] || die "no ros_data volume on $APP"
run "$FLY" deploy --config "$CONFIG" --app "$APP" --image "$IMAGE" --ha=false --env RESTOS_MAINTENANCE=1 --detach
if [ "$DRY_RUN" = 0 ]; then
  ready=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    if "$FLY" ssh console --app "$APP" --command "test -d /data" > /dev/null 2>&1; then ready=1; break; fi
    sleep 5
  done
  [ "$ready" = 1 ] || die "no SSH to $APP after 150 s: check fly status --app $APP"
fi

# ----- 4. move aside, upload, verify -----------------------------------------------------------------------------

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
remote "sh -c 'mkdir -p /data/pre-restore-$STAMP && for f in ros.db ros.db-wal ros.db-shm ros.db-journal \
ai_calls.jsonl caps.json vendor_usage.jsonl; do if [ -e /data/\$f ]; then mv /data/\$f /data/pre-restore-$STAMP/; fi; \
done'"
REMOTE_PATHS=""
for ((i = 0; i < ${#FILES[@]}; i += 2)); do
  printf '+ put %s %s | %s ssh sftp shell --app %s\n' "${FILES[$i]}" "${FILES[$((i + 1))]}" "$FLY" "$APP" >&2
  if [ "$DRY_RUN" = 0 ]; then
    printf 'put %s %s\n' "${FILES[$i]}" "${FILES[$((i + 1))]}" | "$FLY" ssh sftp shell --app "$APP" > /dev/null
  fi
  REMOTE_PATHS="$REMOTE_PATHS ${FILES[$((i + 1))]}"
done
if [ "$DRY_RUN" = 0 ]; then
  listing=$("$FLY" ssh console --app "$APP" --command "sha256sum$REMOTE_PATHS" 2> /dev/null | tr -d '\r' || true)
  for ((i = 0; i < ${#FILES[@]}; i += 2)); do
    want=$(sha256_of "${FILES[$i]}")
    got=$(printf '%s\n' "$listing" | awk -v p="${FILES[$((i + 1))]}" '$2 == p { print $1 }')
    if [ "$want" != "$got" ]; then
      say "Upload by hand: fly ssh sftp shell --app $APP, then 'put <file> <path>' for each file above;"
      say "then: scripts/restore.sh $TARGET --from $FROM --image $IMAGE --yes  (or finish steps 5 manually)."
      die "${FILES[$((i + 1))]} on the machine does not match ${FILES[$i]}"
    fi
  done
  say "all uploads verified (SHA-256)"
fi

# ----- 5. sentinel, normal boot, smoke ---------------------------------------------------------------------------

remote "sh -c 'test -e /data/.volume-id \
|| echo volume=$VOLUME_ID app=$APP created=$STAMP restored=1 > /data/.volume-id'"
run "$FLY" deploy --config "$CONFIG" --app "$APP" --image "$IMAGE" --ha=false
smoke=(--target "$TARGET")
[ "$NO_LEDGER" = 0 ] || smoke+=(--no-live-turn)
if [ "$DRY_RUN" = 1 ]; then
  printf '+ %s scripts/smoke_deploy.py %s\n' "$PY" "${smoke[*]}" >&2
else
  "$PY" scripts/smoke_deploy.py "${smoke[@]}"
fi

say "restored $APP from $FROM."
if [ "$NO_LEDGER" = 1 ]; then
  say "Next: post the baseline reconciled against the Meta dashboard (admin session), e.g."
  say "  POST https://$APP.fly.dev/api/admin/caps {\"spent_baseline_usd\": <USD from the Meta dashboard>}"
fi
say "Twilio: unchanged (same URL). Only after a restore onto another host: scripts/twilio_setup.py --target <url>."
say "The replaced files stay in /data/pre-restore-$STAMP/ on the volume; delete them once the restore is confirmed."
