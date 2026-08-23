# shellcheck shell=bash
# Shared terminal launch + experiment logging for architecture launch_*.sh scripts.
#
# After PROJECT_ROOT is set and AGENT_COUNT is known, call:
#   source .../_launch_common.sh
#   init_experiment_session "<architecture>"
#   detect_terminal   # or it runs from init
#   launch_window "Title" "command..."
#   wait_for_tcp host port name
#
# Every window is tee'd into:
#   <repo>/tasks/experiments/runs/_active/<session_id>/logs/*.log
# Save later with:
#   ./agentpackage/architectures/save_experiment.sh <run_name> [--stop]

REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
EXPERIMENT_RUNS_ROOT="${EXPERIMENT_RUNS_ROOT:-$REPO_ROOT/tasks/experiments/runs}"
USE_TABS="${USE_TABS:-1}"
TABS_STARTED=0
TERMINAL=""
LAUNCH_LOG_INDEX=0
EXPERIMENT_SESSION_ID=""
EXPERIMENT_SESSION_DIR=""
EXPERIMENT_LOG_DIR=""
EXPERIMENT_PID_FILE=""
EXPERIMENT_META_FILE=""

# Active map for MCP list_stations / get_map_info (items/{AGENT_WORLD}.json).
# Set it in agent_project/.env or before launch, e.g.
#   AGENT_WORLD=bottleneck_1 ./launch_hmas1.sh --agents 2
AGENT_WORLD="${AGENT_WORLD:-stations}"
export AGENT_WORLD

# A typo here is invisible later: the MCP server falls back to the built-in
# A–D stations and every agent plans on landmarks that are not on the map.
_AGENT_WORLD_ITEMS="${AGENT_WORLDS_DIR:-$REPO_ROOT/worlds}/items/$AGENT_WORLD.json"
if [ ! -f "$_AGENT_WORLD_ITEMS" ]; then
  echo "WARNING: no landmarks file for AGENT_WORLD='$AGENT_WORLD'" >&2
  echo "         expected $_AGENT_WORLD_ITEMS" >&2
  echo "         list_stations will fall back to the built-in A-D stations." >&2
fi

_slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//; s/_+/_/g'
}

detect_terminal() {
  if command -v gnome-terminal >/dev/null 2>&1; then
    TERMINAL="gnome-terminal"
  elif command -v konsole >/dev/null 2>&1; then
    TERMINAL="konsole"
  elif command -v xfce4-terminal >/dev/null 2>&1; then
    TERMINAL="xfce4-terminal"
  elif command -v xterm >/dev/null 2>&1; then
    TERMINAL="xterm"
  else
    echo "No supported terminal found (gnome-terminal/konsole/xfce4-terminal/xterm)." >&2
    return 1
  fi

  if [[ "$USE_TABS" -eq 1 && "$TERMINAL" != "gnome-terminal" && "$TERMINAL" != "konsole" ]]; then
    echo "Tabs are only supported for gnome-terminal and konsole. Falling back to separate windows."
    USE_TABS=0
  fi
  return 0
}

init_experiment_session() {
  local architecture="$1"
  local stamp
  stamp="$(date +%Y%m%d_%H%M%S)"
  EXPERIMENT_SESSION_ID="${architecture}_${stamp}"
  mkdir -p "$EXPERIMENT_RUNS_ROOT/_active"
  EXPERIMENT_SESSION_DIR="$EXPERIMENT_RUNS_ROOT/_active/$EXPERIMENT_SESSION_ID"
  EXPERIMENT_LOG_DIR="$EXPERIMENT_SESSION_DIR/logs"
  EXPERIMENT_PID_FILE="$EXPERIMENT_SESSION_DIR/pids.txt"
  EXPERIMENT_META_FILE="$EXPERIMENT_SESSION_DIR/meta.json"
  mkdir -p "$EXPERIMENT_LOG_DIR"
  : >"$EXPERIMENT_PID_FILE"
  printf '%s\n' "$EXPERIMENT_SESSION_DIR" >"$EXPERIMENT_RUNS_ROOT/_active/CURRENT"

  cat >"$EXPERIMENT_META_FILE" <<EOF
{
  "session_id": "$EXPERIMENT_SESSION_ID",
  "architecture": "$architecture",
  "agent_count": ${AGENT_COUNT:-0},
  "agent_world": "${AGENT_WORLD:-}",
  "started_at": "$(date -Iseconds)",
  "project_root": "$PROJECT_ROOT",
  "status": "running",
  "model": "${AGENT_MODEL:-}",
  "timeout_sec": ${EXPERIMENT_TIMEOUT_SEC:-null}
}
EOF

  cat >"$EXPERIMENT_SESSION_DIR/README.txt" <<EOF
Active experiment session: $EXPERIMENT_SESSION_ID
Architecture: $architecture
Agents: ${AGENT_COUNT:-?}

Terminal output is appended under logs/ while the run is live.

When you want to stop and keep this trial:
  cd "$PROJECT_ROOT"
  ./agentpackage/architectures/save_experiment.sh <your_run_name>
  # or with stop:
  ./agentpackage/architectures/save_experiment.sh <your_run_name> --stop
EOF

  export EXPERIMENT_SESSION_DIR EXPERIMENT_LOG_DIR EXPERIMENT_SESSION_ID EXPERIMENT_TIMEOUT_SEC
  echo "Experiment logging → $EXPERIMENT_SESSION_DIR"
  echo "  Save later: ./agentpackage/architectures/save_experiment.sh <run_name> [--stop]"
  if [[ -n "${EXPERIMENT_TIMEOUT_SEC:-}" ]]; then
    echo "  Mission timeout: ${EXPERIMENT_TIMEOUT_SEC}s (starts when you send the prompt, not at launch)"
  fi
}

launch_window() {
  local title="$1"
  local cmd="$2"
  local idx slug logfile wrapped

  if [[ -z "${EXPERIMENT_LOG_DIR:-}" ]]; then
    echo "error: call init_experiment_session before launch_window" >&2
    return 1
  fi
  if [[ -z "${TERMINAL:-}" ]]; then
    detect_terminal || return 1
  fi

  idx="$(printf '%02d' "$LAUNCH_LOG_INDEX")"
  LAUNCH_LOG_INDEX=$((LAUNCH_LOG_INDEX + 1))
  slug="$(_slugify "$title")"
  logfile="$EXPERIMENT_LOG_DIR/${idx}_${slug}.log"

  {
    echo "===== $title ====="
    echo "started: $(date -Iseconds)"
    echo "command: $cmd"
    echo
  } >"$logfile"

  # Record shell PID, stream stdout/stderr through tee, then keep the tab open.
  wrapped="cd \"$PROJECT_ROOT\" && [ -f .venv/bin/activate ] && source .venv/bin/activate; export PYTHONUNBUFFERED=1; export EXPERIMENT_SESSION_DIR=\"$EXPERIMENT_SESSION_DIR\"; export EXPERIMENT_LOG_DIR=\"$EXPERIMENT_LOG_DIR\"; export EXPERIMENT_TIMEOUT_SEC=\"${EXPERIMENT_TIMEOUT_SEC:-}\"; echo \$\$ >> \"$EXPERIMENT_PID_FILE\"; echo \"[shell_pid=\$\$] $title\" >> \"$logfile\"; set +e; { $cmd; } 2>&1 | tee -a \"$logfile\"; echo; echo \"[exit] \$(date -Iseconds)\" | tee -a \"$logfile\"; exec bash"

  if [[ "$USE_TABS" -eq 1 ]]; then
    case "$TERMINAL" in
      gnome-terminal)
        if [[ "$TABS_STARTED" -eq 0 ]]; then
          gnome-terminal --window --title="$title" -- bash -lc "$wrapped"
          TABS_STARTED=1
        else
          gnome-terminal --tab --title="$title" -- bash -lc "$wrapped"
        fi
        return 0
        ;;
      konsole)
        if [[ "$TABS_STARTED" -eq 0 ]]; then
          konsole -p tabtitle="$title" -e bash -lc "$wrapped"
          TABS_STARTED=1
        else
          konsole --new-tab -p tabtitle="$title" -e bash -lc "$wrapped"
        fi
        return 0
        ;;
    esac
  fi

  case "$TERMINAL" in
    gnome-terminal)
      gnome-terminal --title="$title" -- bash -lc "$wrapped"
      ;;
    konsole)
      konsole --new-tab -p tabtitle="$title" -e bash -lc "$wrapped"
      ;;
    xfce4-terminal)
      xfce4-terminal --title="$title" --command="bash -lc $(printf '%q' "$wrapped")"
      ;;
    xterm)
      xterm -T "$title" -e bash -lc "$wrapped"
      ;;
  esac
}

wait_for_tcp() {
  # MCP (Uvicorn + ROS tool init) often needs >10s; the old 30×0.25s (~7.5s)
  # default made launch_*.sh exit via set -e before opening robot windows.
  local host="$1"
  local port="$2"
  local name="$3"
  local tries="${4:-120}"
  local delay="${5:-0.5}"
  local i

  echo "Waiting for $name at $host:$port ..."
  for ((i=1; i<=tries; i++)); do
    if python - "$host" "$port" <<'WAITPY' >/dev/null 2>&1
import socket, sys
h = sys.argv[1]
p = int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.4)
try:
    s.connect((h, p))
    print("ok")
except Exception:
    raise SystemExit(1)
finally:
    s.close()
WAITPY
    then
      echo "$name is up."
      return 0
    fi
    if (( i % 10 == 0 )); then
      echo "  still waiting for $name (attempt $i/$tries) ..."
    fi
    sleep "$delay"
  done

  echo "Timed out waiting for $name at $host:$port" >&2
  echo "  Check the $name tab (bind error / ROS hang). Agents will not start." >&2
  return 1
}

require_display_or_print() {
  # Caller prints the manual commands, then we exit.
  if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    return 1
  fi
  return 0
}
