#!/usr/bin/env bash
# Fly.io release flow (ARCHITECTURE §3): build a commit once on staging, smoke it, promote that same image to prod.
#
#   scripts/deploy.sh init <staging|prod>      first setup: app, 3 GB ros_data volume, secrets, .volume-id, first boot
#   scripts/deploy.sh staging [--ref REF]      build the commit (default HEAD) on Fly's builder, deploy staging, smoke
#   scripts/deploy.sh prod [--image REF]       promote the image staging runs to prod (no rebuild), smoke
#   scripts/deploy.sh rollback <staging|prod> --image REF   redeploy an earlier image (fly releases --app APP --image)
#   scripts/deploy.sh phrases <staging|prod>   release step: pre-render the phrase audio on the machine (P15b's script)
#   scripts/deploy.sh status <staging|prod>
#
# Options: --dry-run (print the fly commands, change nothing), --yes (no prompts), --skip-smoke, --no-live-turn,
# --force (prod even when calls may be active), --hotfix (prod from Nov 4 12:00 ET, when only hotfixes go out),
# --ci (GitHub Actions: python3, health-only smoke). Builds come from a clean checkout of the commit, so the image
# never holds uncommitted or untracked files and its git_sha means something. Prod never deploys while calls are
# active (admin view of /api/status, checked by scripts/smoke_deploy.py --check-idle).
# Needs flyctl logged in (`fly auth login`, or FLY_API_TOKEN; promotion reads staging's image, so a CI token must reach
# both apps). Prints no secrets.
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$REPO_ROOT"

REGION=iad
VOLUME=ros_data
VOLUME_GB=3
STAGING_APP=restaurantos-staging
PROD_APP=restaurantos
JUDGING_STARTS_EPOCH=1793811600 # 2026-11-04T17:00:00Z = Nov 4 12:00 ET

die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

COMMAND=""
TARGET=""
REF=HEAD
IMAGE=""
DRY_RUN=0
YES=0
SKIP_SMOKE=0
NO_LIVE_TURN=0
FORCE=0
HOTFIX=0
CI_MODE=0
while [ $# -gt 0 ]; do
  case $1 in
    init | staging | prod | rollback | phrases | status)
      if [ -z "$COMMAND" ]; then COMMAND=$1; elif [ -z "$TARGET" ]; then TARGET=$1; else die "unexpected $1"; fi
      ;;
    --ref) [ $# -ge 2 ] || die "--ref needs a commit"; REF=$2; shift ;;
    --image) [ $# -ge 2 ] || die "--image needs an image reference"; IMAGE=$2; shift ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) YES=1 ;;
    --skip-smoke) SKIP_SMOKE=1 ;;
    --no-live-turn) NO_LIVE_TURN=1 ;;
    --force) FORCE=1 ;;
    --hotfix) HOTFIX=1 ;;
    --ci) CI_MODE=1 ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
[ -n "$COMMAND" ] || { usage; exit 2; }
case $COMMAND in
  staging | prod) [ -z "$TARGET" ] || die "$COMMAND takes no target"; TARGET=$COMMAND ;;
  *) case $TARGET in staging | prod) ;; *) die "$COMMAND needs a target: staging or prod" ;; esac ;;
esac

app_of() { if [ "$1" = prod ]; then printf '%s' "$PROD_APP"; else printf '%s' "$STAGING_APP"; fi; }
config_of() { if [ "$1" = prod ]; then printf fly.toml; else printf fly.staging.toml; fi; }
APP=$(app_of "$TARGET")
CONFIG=$(config_of "$TARGET")

PY=.venv/bin/python
if [ "$CI_MODE" = 1 ] || [ ! -x "$PY" ]; then PY=$(command -v python3 || command -v python || true); fi
[ -n "$PY" ] || die "no python found"
FLY=${FLY_BIN:-$(command -v fly || command -v flyctl || true)}
[ -n "$FLY" ] || die "flyctl not found: brew install flyctl, then fly auth login"
# A deploy token (FLY_API_TOKEN) may not answer whoami; only an interactive login is checked here.
if [ "$DRY_RUN" = 0 ] && [ -z "${FLY_API_TOKEN:-}${FLY_ACCESS_TOKEN:-}" ]; then
  "$FLY" auth whoami > /dev/null 2>&1 || die "flyctl is not logged in: fly auth login (or set FLY_API_TOKEN)"
fi

# ----- helpers ---------------------------------------------------------------------------------------------------

run() { # a change on Fly: printed, then run unless --dry-run
  printf '+ %s\n' "$*" >&2
  [ "$DRY_RUN" = 1 ] || "$@"
}

confirm() {
  [ "$YES" = 1 ] && return 0
  [ -t 0 ] || die "$1 (needs confirmation: rerun with --yes)"
  printf '%s [y/N] ' "$1" >&2
  local reply
  read -r reply
  case $reply in y | Y | yes) return 0 ;; esac
  die "cancelled"
}

json_field() { # json_field PROGRAM: run a tiny python reader over the JSON on stdin
  "$PY" -c "import json, sys; data = json.load(sys.stdin) or []; $1"
}

current_image() { # the image of the app's started machine (registry.fly.io/<app>:<label>)
  "$FLY" machines list --app "$1" --json | json_field '
ms = [m for m in data if m.get("state") == "started"] or data
print(((ms[0].get("config") or {}).get("image") or "") if ms else "")'
}

first_volume() { # the id of the app's ros_data volume, if any
  "$FLY" volumes list --app "$1" --json | json_field '
ids = [v.get("id", "") for v in data if v.get("name") == "'"$VOLUME"'" and "destroy" not in str(v.get("state"))]
print(ids[0] if ids else "")'
}

RELEASE_DIR=""
RELEASE_TREE=""
make_release_tree() { # a clean checkout of commit $1, so the build context holds committed files only
  RELEASE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/restos-release.XXXXXX")
  RELEASE_TREE="$RELEASE_DIR/tree"
  if [ "$DRY_RUN" = 1 ]; then
    say "(dry run) would check out $1 into $RELEASE_TREE"
  else
    git worktree add --detach --quiet "$RELEASE_TREE" "$1"
  fi
}
cleanup() {
  [ -n "$RELEASE_DIR" ] || return 0
  [ ! -d "$RELEASE_TREE" ] || git worktree remove --force "$RELEASE_TREE" > /dev/null 2>&1 || true
  rm -rf "$RELEASE_DIR"
}
trap cleanup EXIT

commit_of() {
  git rev-parse --verify --quiet "$1^{commit}" > /dev/null || die "unknown commit: $1"
  git rev-parse --short=12 "$1^{commit}"
}

wait_for_ssh() {
  [ "$DRY_RUN" = 0 ] || return 0
  local i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    "$FLY" ssh console --app "$1" --command "test -d /data" > /dev/null 2>&1 && return 0
    sleep 5
  done
  die "no SSH to $1 after 150 s: check fly status --app $1"
}

write_sentinel() { # write_sentinel APP VOLUME_ID: the .volume-id the boot validator wants, written once
  local stamp
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  run "$FLY" ssh console --app "$1" \
    --command "sh -c 'test -e /data/.volume-id || echo volume=$2 app=$1 created=$stamp > /data/.volume-id; \
cat /data/.volume-id'"
}

judging_guard() {
  [ "$(date -u +%s)" -lt "$JUDGING_STARTS_EPOCH" ] || [ "$HOTFIX" = 1 ] \
    || die "judging has started (Nov 4 12:00 ET): only hotfixes go to prod; rerun with --hotfix"
}

idle_gate() { # no prod deploys while calls are active
  [ "$TARGET" = prod ] || return 0
  if [ "$DRY_RUN" = 1 ]; then say "(dry run) would check that no call is active on $APP"; return 0; fi
  local code=0
  "$PY" scripts/smoke_deploy.py --target prod --check-idle || code=$?
  case $code in
    0) ;;
    3) [ "$FORCE" = 1 ] || die "calls are active on $APP: wait for them to end (or --force)" ;;
    4) [ "$FORCE" = 1 ] || confirm "Cannot verify that no call is active on $APP. Deploy anyway?" ;;
    *) die "the idle check failed (exit $code)" ;;
  esac
}

smoke() { # smoke [SHA]
  if [ "$SKIP_SMOKE" = 1 ]; then say "smoke test skipped (--skip-smoke)"; return 0; fi
  local args=(--target "$TARGET")
  [ -z "${1:-}" ] || args+=(--expect-sha "$1")
  [ "$CI_MODE" = 0 ] || args+=(--health-only)
  [ "$NO_LIVE_TURN" = 0 ] || args+=(--no-live-turn)
  if [ "$DRY_RUN" = 1 ]; then printf '+ %s scripts/smoke_deploy.py %s\n' "$PY" "${args[*]}" >&2; return 0; fi
  "$PY" scripts/smoke_deploy.py "${args[@]}"
}

sha_of_image() { case $1 in *:git-*) printf '%s' "${1##*:git-}" ;; esac; }

# ----- commands --------------------------------------------------------------------------------------------------

cmd_init() {
  confirm "Set up $APP: app, $VOLUME_GB GB $VOLUME volume in $REGION, secrets, .volume-id, first boot?"
  if "$FLY" status --app "$APP" > /dev/null 2>&1; then say "app $APP exists"; else
    run "$FLY" apps create "$APP" ${FLY_ORG:+--org "$FLY_ORG"}
  fi
  local vol sha label
  vol=$(first_volume "$APP" 2> /dev/null || true)
  if [ -z "$vol" ]; then
    run "$FLY" volumes create "$VOLUME" --app "$APP" --region "$REGION" --size "$VOLUME_GB" --yes
    if [ "$DRY_RUN" = 1 ]; then vol=vol_dryrun; else vol=$(first_volume "$APP"); fi
  fi
  [ -n "$vol" ] || die "no $VOLUME volume on $APP"
  if [ "$DRY_RUN" = 1 ]; then
    bash scripts/secrets_to_fly.sh "$TARGET" --dry-run
  else
    bash scripts/secrets_to_fly.sh "$TARGET"
  fi
  sha=$(commit_of "$REF")
  label="git-$sha"
  make_release_tree "$sha"
  # First boot in maintenance mode (the image's CMD sleeps), so the sentinel can be written before the app runs.
  run "$FLY" deploy "$RELEASE_TREE" --config "$RELEASE_TREE/$CONFIG" --app "$APP" --remote-only --ha=false \
    --image-label "$label" --build-arg "RESTOS_GIT_SHA=$sha" --env RESTOS_MAINTENANCE=1 --detach
  wait_for_ssh "$APP"
  write_sentinel "$APP" "$vol"
  run "$FLY" deploy "$RELEASE_TREE" --config "$RELEASE_TREE/$CONFIG" --app "$APP" --ha=false \
    --image "registry.fly.io/$APP:$label"
  smoke "$sha"
  say "$APP is up. Admin tokens and the judge passcode: ~/.restaurantos/$APP.env (secrets_to_fly.sh)."
}

cmd_staging() {
  local sha label
  sha=$(commit_of "$REF")
  label="git-$sha"
  [ -z "$(git status --porcelain --untracked-files=no)" ] \
    || say "note: uncommitted changes are not deployed; building commit $sha"
  make_release_tree "$sha"
  run "$FLY" deploy "$RELEASE_TREE" --config "$RELEASE_TREE/$CONFIG" --app "$APP" --remote-only --ha=false \
    --image-label "$label" --build-arg "RESTOS_GIT_SHA=$sha"
  smoke "$sha"
  say "staging runs registry.fly.io/$APP:$label; promote that image with: scripts/deploy.sh prod"
}

cmd_prod() {
  judging_guard
  local image=$IMAGE sha config=fly.toml
  if [ -z "$image" ]; then
    image=$(current_image "$STAGING_APP") || die "cannot read the image $STAGING_APP runs"
    case $image in
      registry.fly.io/"$STAGING_APP":git-*) ;;
      *) die "$STAGING_APP runs '${image:-nothing}', not a scripts/deploy.sh staging build; pass --image to choose" ;;
    esac
  fi
  sha=$(sha_of_image "$image")
  if [ -n "$sha" ] && git rev-parse --verify --quiet "$sha^{commit}" > /dev/null; then
    make_release_tree "$sha" # fly.toml from the image's own commit
    config="$RELEASE_TREE/fly.toml"
  fi
  confirm "Promote $image to $APP (the same image, no rebuild)?"
  idle_gate
  run "$FLY" deploy --config "$config" --app "$APP" --image "$image" --ha=false
  smoke "$sha"
  say "$APP runs $image. Roll back: scripts/deploy.sh rollback prod --image <earlier> (fly releases --app $APP --image)"
}

cmd_rollback() {
  [ -n "$IMAGE" ] || die "rollback needs --image REF; list earlier images with: fly releases --app $APP --image"
  confirm "Redeploy $IMAGE to $APP?"
  idle_gate
  run "$FLY" deploy --config "$CONFIG" --app "$APP" --image "$IMAGE" --ha=false
  smoke "$(sha_of_image "$IMAGE")"
}

cmd_phrases() {
  [ -f scripts/build_phrase_cache.py ] || die "scripts/build_phrase_cache.py (P15b) has not landed yet"
  confirm "Pre-render the phrase audio on $APP (spends ElevenLabs characters once)?"
  # As root over SSH, then hand the new files to the app user so the live TTS cache can keep writing beside them.
  run "$FLY" ssh console --app "$APP" \
    --command "sh -c 'cd /app && /opt/venv/bin/python scripts/build_phrase_cache.py && chown -R restos:restos /data'"
}

cmd_status() {
  "$FLY" status --app "$APP"
  "$PY" scripts/smoke_deploy.py --target "$TARGET" --health-only
}

"cmd_$COMMAND"
