# shellcheck shell=bash
# Shared AGENT_COUNT parsing for architecture launch_*.sh scripts.
# Usage: source this file, then call parse_agent_count "$@"
# Afterward: AGENT_COUNT is set/exported; remaining args are in LAUNCH_ARGS.

# Sourced first by every launch script, so .env is in the environment before any
# default (AGENT_COUNT here, AGENT_WORLD in _launch_common.sh) is applied.
_AGENTS_ARCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_load_dotenv.sh
source "$_AGENTS_ARCH_DIR/_load_dotenv.sh"
_AGENT_PROJECT_DIR="$(cd "$_AGENTS_ARCH_DIR/../.." && pwd)"
_load_dotenv "$_AGENT_PROJECT_DIR/.env"
if [ ! -f "$_AGENT_PROJECT_DIR/.env" ] && [ -f "$_AGENT_PROJECT_DIR/.env.example" ]; then
  echo "note: no agent_project/.env found (.env.example is only a template," \
       "its values are NOT read). Using the shell environment." >&2
fi

_parse_timeout_to_sec() {
  # Bare number = minutes. Also 15m / 15min, 90s / 90sec, 1h / 1hr.
  local raw="${1,,}"
  raw="${raw// /}"
  local n unit
  if [[ ! "$raw" =~ ^([0-9]+)([a-z]*)$ ]]; then
    return 1
  fi
  n="${BASH_REMATCH[1]}"
  unit="${BASH_REMATCH[2]}"
  case "$unit" in
    ""|m|min|mins|minute|minutes) echo $((n * 60)) ;;
    s|sec|secs|second|seconds) echo "$n" ;;
    h|hr|hrs|hour|hours) echo $((n * 3600)) ;;
    *) return 1 ;;
  esac
}

parse_agent_count() {
  local requested="${AGENTS:-${AGENT_COUNT:-4}}"
  local timeout_raw="${EXPERIMENT_TIMEOUT:-}"
  local existing_sec="${EXPERIMENT_TIMEOUT_SEC:-}"
  LAUNCH_ARGS=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agents)
        if [[ $# -lt 2 ]]; then
          echo "error: --agents requires a value (2|3|4|6|8)" >&2
          return 1
        fi
        requested="$2"
        shift 2
        ;;
      --agents=*)
        requested="${1#*=}"
        shift
        ;;
      --timeout)
        if [[ $# -lt 2 ]]; then
          echo "error: --timeout requires a value (e.g. 15, 15m, 90s, 1h)" >&2
          return 1
        fi
        timeout_raw="$2"
        shift 2
        ;;
      --timeout=*)
        timeout_raw="${1#*=}"
        shift
        ;;
      *)
        LAUNCH_ARGS+=("$1")
        shift
        ;;
    esac
  done

  EXPERIMENT_TIMEOUT_SEC=""
  if [[ -n "$timeout_raw" ]]; then
    local parsed
    parsed="$(_parse_timeout_to_sec "$timeout_raw")" || {
      echo "error: --timeout must look like 15, 15m, 90s, or 1h (got ${timeout_raw})" >&2
      return 1
    }
    if [[ "$parsed" -le 0 ]]; then
      echo "error: --timeout must be > 0 (got ${timeout_raw})" >&2
      return 1
    fi
    EXPERIMENT_TIMEOUT_SEC="$parsed"
  elif [[ -n "$existing_sec" ]]; then
    if [[ ! "$existing_sec" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
      echo "error: EXPERIMENT_TIMEOUT_SEC must be seconds (got ${existing_sec})" >&2
      return 1
    fi
    EXPERIMENT_TIMEOUT_SEC="$existing_sec"
  fi
  export EXPERIMENT_TIMEOUT_SEC

  case "$requested" in
    2|3|4|6|8)
      AGENT_COUNT="$requested"
      export AGENT_COUNT
      ;;
    *)
      echo "error: AGENT_COUNT/AGENTS/--agents must be 2, 3, 4, 6, or 8 (got ${requested})" >&2
      return 1
      ;;
  esac

  AGENT_PLATFORM="${AGENT_PLATFORM:-q1}"
  export AGENT_PLATFORM
  ROBOT_IDS=()
  if [ "$AGENT_PLATFORM" = "q1" ]; then
    if [ "$AGENT_COUNT" != "3" ]; then
      echo "note: AGENT_PLATFORM=q1 uses 3 specialists; forcing AGENT_COUNT=3" >&2
      AGENT_COUNT=3
      export AGENT_COUNT
    fi
    ROBOT_IDS=(navigator lidar camera)
  else
    if [ "$AGENT_COUNT" = "3" ]; then
      echo "error: remroc AGENT_COUNT must be 2, 4, 6, or 8 (got 3)" >&2
      return 1
    fi
    local i
    for ((i=0; i<AGENT_COUNT; i++)); do
      ROBOT_IDS+=("SmallDeliveryRobot_$i")
    done
  fi
}
