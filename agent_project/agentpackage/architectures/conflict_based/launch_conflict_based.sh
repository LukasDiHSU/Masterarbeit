#!/usr/bin/env bash
set -euo pipefail

# Conflict-based architecture: shared MCP server, four solo
# peer agents, and a mission CLI. Peers work alone by default; mesh
# negotiation opens only when MCP events (box_missing, conflict, ...) involve
# them. No fleet-wide broadcast roundtable.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

MCP_HOST="0.0.0.0"
MCP_PORT="8000"
MCP_CONNECT_HOST="127.0.0.1"
MESH_HOST="127.0.0.1"
MESH_BASE_PORT="9101"
MESH_CLI_PORT="9099"
MONITOR_HOST="127.0.0.1"
MONITOR_PORT="9900"
USE_TABS=1

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  cat <<EOF
No graphical terminal detected (DISPLAY/WAYLAND_DISPLAY unset).
Run these in separate shells or inside tmux:

cd "$PROJECT_ROOT" && python -m agentpackage.monitor --host "$MONITOR_HOST" --port "$MONITOR_PORT"
cd "$PROJECT_ROOT" && python -m agentpackage.mcpserver --host "$MCP_HOST" --port "$MCP_PORT"
cd "$PROJECT_ROOT" && AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb1 --host "$MESH_HOST" --base-port "$MESH_BASE_PORT"
cd "$PROJECT_ROOT" && AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb2 --host "$MESH_HOST" --base-port "$MESH_BASE_PORT"
cd "$PROJECT_ROOT" && AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb3 --host "$MESH_HOST" --base-port "$MESH_BASE_PORT"
cd "$PROJECT_ROOT" && AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb4 --host "$MESH_HOST" --base-port "$MESH_BASE_PORT"
cd "$PROJECT_ROOT" && python -m agentpackage.architectures.conflict_based.mission_cli --host "$MESH_HOST" --base-port "$MESH_BASE_PORT" --cli-port "$MESH_CLI_PORT"
EOF
  exit 1
fi

TERMINAL=""
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
  exit 1
fi

if [[ "$USE_TABS" -eq 1 && "$TERMINAL" != "gnome-terminal" && "$TERMINAL" != "konsole" ]]; then
  echo "Tabs are only supported for gnome-terminal and konsole. Falling back to separate windows."
  USE_TABS=0
fi

TABS_STARTED=0
launch_window() {
  local title="$1"
  local cmd="$2"
  local wrapped="cd \"$PROJECT_ROOT\" && [ -f .venv/bin/activate ] && source .venv/bin/activate; $cmd; exec bash"

  if [[ "$USE_TABS" -eq 1 ]]; then
    case "$TERMINAL" in
      gnome-terminal)
        if [[ "$TABS_STARTED" -eq 0 ]]; then
          gnome-terminal --window --title="$title" -- bash -lc "$wrapped"
          TABS_STARTED=1
        else
          gnome-terminal --tab --title="$title" -- bash -lc "$wrapped"
        fi
        return
        ;;
      konsole)
        if [[ "$TABS_STARTED" -eq 0 ]]; then
          konsole -p tabtitle="$title" -e bash -lc "$wrapped"
          TABS_STARTED=1
        else
          konsole --new-tab -p tabtitle="$title" -e bash -lc "$wrapped"
        fi
        return
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
      xfce4-terminal --title="$title" --command="bash -lc '$wrapped'"
      ;;
    xterm)
      xterm -T "$title" -e bash -lc "$wrapped"
      ;;
  esac
}

MCP_URL="http://$MCP_CONNECT_HOST:$MCP_PORT/sse"
SHARED_ENV="AGENT_MCP_URL=$MCP_URL"

wait_for_tcp() {
  local host="$1"
  local port="$2"
  local name="$3"
  local tries="${4:-30}"
  local delay="${5:-0.25}"

  for ((i=1; i<=tries; i++)); do
    if python - "$host" "$port" <<'PY' >/dev/null 2>&1
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
PY
    then
      return 0
    fi
    sleep "$delay"
  done

  echo "Timed out waiting for $name at $host:$port" >&2
  return 1
}

echo "Launching CONFLICT-BASED architecture..."
echo "  MCP bind:    $MCP_HOST:$MCP_PORT"
echo "  MCP connect: $MCP_URL"
echo "  Mesh base:   $MESH_HOST:$MESH_BASE_PORT (tb1..tb4 use consecutive ports)"
echo "  Mission CLI: $MESH_HOST:$MESH_CLI_PORT"
echo "  Usage monitor: $MONITOR_HOST:$MONITOR_PORT (UDP)"

launch_window "Usage Monitor" \
  "python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
launch_window "MCP Server (shared)" \
  "python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""

wait_for_tcp "$MCP_CONNECT_HOST" "$MCP_PORT" "MCP server"

launch_window "Peer tb4" \
  "$SHARED_ENV python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb4 --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\""
launch_window "Peer tb3" \
  "$SHARED_ENV python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb3 --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\""
launch_window "Peer tb2" \
  "$SHARED_ENV python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb2 --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\""
launch_window "Peer tb1" \
  "$SHARED_ENV python -m agentpackage.architectures.conflict_based.robot_peer_agent --tb-id tb1 --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\""

wait_for_tcp "$MESH_HOST" "$((MESH_BASE_PORT + 0))" "peer tb1"
wait_for_tcp "$MESH_HOST" "$((MESH_BASE_PORT + 1))" "peer tb2"
wait_for_tcp "$MESH_HOST" "$((MESH_BASE_PORT + 2))" "peer tb3"
wait_for_tcp "$MESH_HOST" "$((MESH_BASE_PORT + 3))" "peer tb4"

launch_window "Mission CLI" \
  "python -m agentpackage.architectures.conflict_based.mission_cli --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\" --cli-port \"$MESH_CLI_PORT\""

echo "Done. Assign solo missions with the Mission CLI; peers negotiate only on events."
