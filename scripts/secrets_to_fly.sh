#!/usr/bin/env bash
set +x # first line of work: never trace, not even under `bash -x`, because secret values pass through variables
# Push RestaurantOS secrets to a Fly app without printing a single value (ARCHITECTURE §3 "Secrets").
#
#   scripts/secrets_to_fly.sh <staging|prod> [--dry-run] [--apply] [--rotate NAME]...
#                             [--without meta|twilio|elevenlabs]...
#   scripts/secrets_to_fly.sh prod --github-ops-token   also store RESTOS_OPS_TOKEN as a GitHub secret (ops.yml)
#
# Vendor secrets come from the gitignored files in the repo root, read the way config.py reads them in dev:
# meta_muse_api_key (the raw key), twilio_credentials (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_JUDGE_NUMBER,
# TWILIO_PUBLIC_NUMBER lines) and elevenlabs_api_key (the raw key, or ELEVENLABS_API_KEY / TTS_VOICE_ID lines).
# Generated secrets (RESTOS_SESSION_SECRET, RESTOS_ADMIN_TOKENS, RESTOS_JUDGE_PASSCODE, RESTOS_OPS_TOKEN,
# RESTOS_DEMO_DTMF_CODE) are made once and kept in ~/.restaurantos/<app>.env (mode 600, outside the repo), where the
# founders find their admin tokens and the judge passcode; a re-run reuses them and --rotate NAME regenerates one.
# Once applied, --rotate RESTOS_ADMIN_TOKENS also voids every ros_admin cookie minted with an old token (the cookie
# carries a fingerprint of it), so admins log in again; --rotate RESTOS_SESSION_SECRET logs out every judge as well.
# Values only travel through a pipe into `fly secrets import`: never argv, never the terminal. Secrets are staged and
# take effect on the next deploy; --apply restarts the machine now. --dry-run lists the names and touches nothing.
#
# Overrides: FLY_BIN (flyctl binary), RESTOS_KEY_FILES_DIR (vendor key files), RESTOS_SECRETS_DIR (generated store),
# RESTOS_ADMIN_NAMES (default "vamsi akshitha"), RESTOS_GITHUB_REPO (default vamsikrishna2421/restaurantos-ai).
set -euo pipefail
umask 077
export LC_ALL=C

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
GENERATED="RESTOS_SESSION_SECRET RESTOS_ADMIN_TOKENS RESTOS_JUDGE_PASSCODE RESTOS_OPS_TOKEN RESTOS_DEMO_DTMF_CODE"

die() { printf 'secrets_to_fly: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; }

# ----- parsing helpers: every value goes to stdout only to be captured by $(...), never shown ------------------------

trim() {
  local s=$1
  s=${s#"${s%%[![:space:]]*}"}
  s=${s%"${s##*[![:space:]]}"}
  printf '%s' "$s"
}

strip_quotes() { # like Python's str.strip("'\"")
  local s=$1
  while :; do case $s in \"* | \'*) s=${s#?} ;; *) break ;; esac; done
  while :; do case $s in *\" | *\') s=${s%?} ;; *) break ;; esac; done
  printf '%s' "$s"
}

kv_get() { # kv_get FILE KEY: the last non-empty value of KEY=VALUE lines (config._parse_kv)
  local file=$1 want=$2 line key value found=""
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "$line")
    case $line in '' | '#'*) continue ;; esac
    line=${line#export }
    case $line in *=*) ;; *) continue ;; esac
    key=$(trim "${line%%=*}")
    value=$(strip_quotes "$(trim "${line#*=}")")
    if [ "$key" = "$want" ] && [ -n "$value" ]; then found=$value; fi
  done < "$file"
  printf '%s' "$found"
}

raw_lines() { # raw_lines FILE: non-blank, non-comment lines; several stay joined, so validation refuses them
  local file=$1 line out=""
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "$line")
    case $line in '' | '#'*) continue ;; esac
    if [ -n "$out" ]; then
      out="$out
$line"
    else
      out=$line
    fi
  done < "$file"
  printf '%s' "$out"
}

valid() { # non-empty printable ASCII without whitespace or control characters (config.clean_secret)
  case $1 in '' | *[![:graph:]]*) return 1 ;; esac
  return 0
}

e164() { # +<8..15 digits>
  local digits=${1#+}
  [ "${1:0:1}" = "+" ] || return 1
  case $digits in '' | *[!0-9]*) return 1 ;; esac
  [ ${#digits} -ge 8 ] && [ ${#digits} -le 15 ]
}

gen_hex() { openssl rand -hex "$1"; }
gen_dtmf() {
  local n
  n=$(openssl rand -hex 4)
  printf '%06d' $((0x$n % 1000000))
}

# ----- arguments -------------------------------------------------------------------------------------------------

TARGET=""
DRY_RUN=0
APPLY=0
GITHUB_OPS=0
ROTATE=" "
WITHOUT=" "
while [ $# -gt 0 ]; do
  case $1 in
    staging | prod) TARGET=$1 ;;
    --dry-run) DRY_RUN=1 ;;
    --apply) APPLY=1 ;;
    --github-ops-token) GITHUB_OPS=1 ;;
    --rotate)
      [ $# -ge 2 ] || die "--rotate needs a NAME"
      case " $GENERATED " in *" $2 "*) ;; *) die "--rotate takes one of: $GENERATED" ;; esac
      ROTATE="$ROTATE$2 "
      shift
      ;;
    --without)
      [ $# -ge 2 ] || die "--without needs meta, twilio or elevenlabs"
      case $2 in meta | twilio | elevenlabs) ;; *) die "--without takes meta, twilio or elevenlabs" ;; esac
      WITHOUT="$WITHOUT$2 "
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done
[ -n "$TARGET" ] || die "name the target: staging or prod (see --help)"
case $TARGET in
  prod) APP=restaurantos ;;
  staging) APP=restaurantos-staging ;;
esac
[ "$GITHUB_OPS" = 0 ] || [ "$TARGET" = prod ] || die "--github-ops-token is for prod: ops.yml checks prod only"
skipped() { case $WITHOUT in *" $1 "*) return 0 ;; esac; return 1; }

KEYS_DIR=${RESTOS_KEY_FILES_DIR:-$REPO_ROOT}
STORE_DIR=${RESTOS_SECRETS_DIR:-$HOME/.restaurantos}
STORE="$STORE_DIR/$APP.env"
ADMIN_NAMES=${RESTOS_ADMIN_NAMES:-vamsi akshitha}
for name in $ADMIN_NAMES; do
  case $name in [a-z]*) ;; *) die "RESTOS_ADMIN_NAMES: '$name' must start with a lowercase letter" ;; esac
  case $name in *[!a-z0-9_-]*) die "RESTOS_ADMIN_NAMES: '$name' may use a-z, 0-9, _ and - only" ;; esac
done

FLY=""
if [ "$DRY_RUN" = 0 ]; then
  FLY=${FLY_BIN:-}
  if [ -z "$FLY" ]; then
    FLY=$(command -v fly || command -v flyctl || true)
  fi
  [ -n "$FLY" ] || die "flyctl not found: brew install flyctl, then fly auth login"
fi
command -v openssl > /dev/null || die "openssl not found"

# ----- generated secrets: reuse the store, generate what is missing or rotated ------------------------------------

STORE_CHANGED=0
GEN_NEW=""
GEN_KEPT=""
for name in $GENERATED; do
  value=$(kv_get "$STORE" "$name")
  case $ROTATE in *" $name "*) value="" ;; esac
  if [ -n "$value" ]; then
    GEN_KEPT="$GEN_KEPT $name"
  else
    case $name in
      RESTOS_SESSION_SECRET | RESTOS_OPS_TOKEN) value=$(gen_hex 32) ;;
      RESTOS_JUDGE_PASSCODE) value=$(gen_hex 12) ;;
      RESTOS_DEMO_DTMF_CODE) value=$(gen_dtmf) ;;
      RESTOS_ADMIN_TOKENS)
        value=""
        for who in $ADMIN_NAMES; do
          value="$value${value:+,}$who:$(gen_hex 24)"
        done
        ;;
    esac
    GEN_NEW="$GEN_NEW $name"
    STORE_CHANGED=1
  fi
  printf -v "v_$name" '%s' "$value"
done

# ----- the secrets for Fly, in order -------------------------------------------------------------------------------

OUT_NAMES=()
OUT_VALUES=()
add() { # add NAME VALUE: queue one secret; a malformed value is refused by name only
  valid "$2" || die "$1 is not a single printable token (whitespace or control characters inside); fix its source"
  OUT_NAMES+=("$1")
  OUT_VALUES+=("$2")
}

FROM_META=""
if ! skipped meta; then
  [ -f "$KEYS_DIR/meta_muse_api_key" ] || die "meta_muse_api_key not found in $KEYS_DIR (or pass --without meta)"
  add MODEL_API_KEY "$(trim "$(cat "$KEYS_DIR/meta_muse_api_key")")"
  FROM_META=MODEL_API_KEY
fi

FROM_TWILIO=""
TW="$KEYS_DIR/twilio_credentials"
if skipped twilio; then
  :
elif [ ! -f "$TW" ]; then
  say "note: twilio_credentials not found in $KEYS_DIR: phone secrets skipped (the phone bridge stays off)"
else
  for pair in TWILIO_ACCOUNT_SID:RESTOS_TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN:RESTOS_TWILIO_AUTH_TOKEN; do
    value=$(kv_get "$TW" "${pair%%:*}")
    if [ -n "$value" ]; then
      add "${pair#*:}" "$value"
      FROM_TWILIO="$FROM_TWILIO ${pair#*:}"
    else
      say "note: twilio_credentials has no ${pair%%:*}"
    fi
  done
  judge=$(kv_get "$TW" TWILIO_JUDGE_NUMBER)
  public=$(kv_get "$TW" TWILIO_PUBLIC_NUMBER)
  judge=${judge:-$public}
  public=${public:-$judge} # one number may serve both lanes (founder decision, Oct 27)
  if [ -n "$judge" ]; then
    e164 "$judge" && e164 "$public" || die "twilio_credentials: the phone numbers must be E.164 (+15551234567)"
    add RESTOS_PHONE_NUMBERS "{\"judge\":\"$judge\",\"public\":\"$public\"}"
    FROM_TWILIO="$FROM_TWILIO RESTOS_PHONE_NUMBERS"
  fi
fi

FROM_ELEVEN=""
EL="$KEYS_DIR/elevenlabs_api_key"
if skipped elevenlabs; then
  :
elif [ ! -f "$EL" ]; then
  say "note: elevenlabs_api_key not found in $KEYS_DIR: TTS skipped (replies fall back to captions and browser speech)"
else
  key=$(kv_get "$EL" ELEVENLABS_API_KEY)
  voice=$(kv_get "$EL" TTS_VOICE_ID)
  [ -n "$key" ] || [ -n "$voice" ] || key=$(raw_lines "$EL")
  if [ -n "$key" ]; then
    add RESTOS_ELEVENLABS_API_KEY "$key"
    FROM_ELEVEN=RESTOS_ELEVENLABS_API_KEY
  fi
  if [ -n "$voice" ]; then
    add RESTOS_TTS_VOICE_ID "$voice"
    FROM_ELEVEN="$FROM_ELEVEN RESTOS_TTS_VOICE_ID"
  fi
fi

for name in $GENERATED; do
  var="v_$name"
  add "$name" "${!var}"
done

emit() {
  local i
  for ((i = 0; i < ${#OUT_NAMES[@]}; i++)); do
    printf '%s=%s\n' "${OUT_NAMES[$i]}" "${OUT_VALUES[$i]}"
  done
}

# ----- report (names only), persist the store, push -----------------------------------------------------------------

mode="staged; applied on the next deploy"
[ "$APPLY" = 0 ] || mode="applied now (machines restart)"
[ "$DRY_RUN" = 0 ] || mode="dry run: nothing sent, nothing stored"
say "secrets_to_fly: $APP <- ${#OUT_NAMES[@]} secrets ($mode)"
[ -z "$FROM_META" ] || say "  from meta_muse_api_key:  $FROM_META"
[ -z "$FROM_TWILIO" ] || say "  from twilio_credentials:$FROM_TWILIO"
[ -z "$FROM_ELEVEN" ] || say "  from elevenlabs_api_key: $FROM_ELEVEN"
[ -z "$GEN_NEW" ] || say "  generated now:          $GEN_NEW"
[ -z "$GEN_KEPT" ] || say "  kept from $STORE:$GEN_KEPT"
[ "$DRY_RUN" = 0 ] || exit 0

if [ "$STORE_CHANGED" = 1 ]; then
  mkdir -p "$STORE_DIR"
  chmod 700 "$STORE_DIR"
  case "$(cd "$STORE_DIR" && pwd -P)/" in
    "$REPO_ROOT"/*) die "RESTOS_SECRETS_DIR must be outside the repo ($STORE_DIR)" ;;
  esac
  tmp=$(mktemp "$STORE_DIR/.$APP.env.XXXXXX")
  {
    printf '# RestaurantOS secrets generated for %s by scripts/secrets_to_fly.sh. Private: never commit.\n' "$APP"
    for name in $GENERATED; do
      var="v_$name"
      printf '%s=%s\n' "$name" "${!var}"
    done
  } > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$STORE"
  say "  stored the generated values in $STORE (mode 600)"
fi

args=(secrets import --app "$APP")
[ "$APPLY" = 1 ] || args+=(--stage)
emit | "$FLY" "${args[@]}"

if [ "$GITHUB_OPS" = 1 ]; then
  command -v gh > /dev/null || die "gh not found: brew install gh, then gh auth login"
  printf '%s' "$v_RESTOS_OPS_TOKEN" \
    | gh secret set RESTOS_OPS_TOKEN --repo "${RESTOS_GITHUB_REPO:-vamsikrishna2421/restaurantos-ai}"
  say "  RESTOS_OPS_TOKEN stored as a GitHub Actions secret (the only RestaurantOS secret GitHub holds)"
fi
say "done: admin tokens, judge passcode and DTMF code are in $STORE"
