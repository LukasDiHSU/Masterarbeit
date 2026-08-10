# shellcheck shell=bash
# Shared AGENT_COUNT parsing for architecture launch_*.sh scripts.
# Usage: source this file, then call parse_agent_count "$@"
# Afterward: AGENT_COUNT is set/exported; remaining args are in LAUNCH_ARGS.

parse_agent_count() {
  local requested="${AGENTS:-${AGENT_COUNT:-4}}"
  LAUNCH_ARGS=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agents)
        if [[ $# -lt 2 ]]; then
          echo "error: --agents requires a value (2|4|6|8)" >&2
          return 1
        fi
        requested="$2"
        shift 2
        ;;
      --agents=*)
        requested="${1#*=}"
        shift
        ;;
      *)
        LAUNCH_ARGS+=("$1")
        shift
        ;;
    esac
  done

  case "$requested" in
    2|4|6|8)
      AGENT_COUNT="$requested"
      export AGENT_COUNT
      ;;
    *)
      echo "error: AGENT_COUNT/AGENTS/--agents must be 2, 4, 6, or 8 (got ${requested})" >&2
      return 1
      ;;
  esac
}
