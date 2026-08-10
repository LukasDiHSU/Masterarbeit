#!/usr/bin/env bash
set -euo pipefail

# Launches shared pool: MCP + pool server + AGENT_COUNT agents + user CLI.
# Fleet size: AGENT_COUNT / AGENTS / --agents (2|4|6|8, default 4).

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
# shellcheck source=../_parse_agents.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/_parse_agents.sh"
parse_agent_count "$@" || exit 1
set -- "${LAUNCH_ARGS[@]}"

MCP_HOST="0.0.0.0"
MCP_PORT="8000"
MCP_CONNECT_HOST="127.0.0.1"
POOL_HOST="127.0.0.1"
POOL_PORT="8866"
MONITOR_HOST="127.0.0.1"
MONITOR_PORT="9900"
TURN_ORDER=""
for ((i=1; i<=AGENT_COUNT; i++)); do
  if [[ -n "$TURN_ORDER" ]]; then TURN_ORDER+=","; fi
  TURN_ORDER+="robot_tb$i"
done
USE_TABS=1

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "No graphical terminal detected (DISPLAY/WAYLAND_DISPLAY unset)."
  echo "Run these in separate shells or inside tmux (AGENT_COUNT=$AGENT_COUNT):"
  echo
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""
  echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.shared_pool.message_pool --host \"$POOL_HOST\" --port \"$POOL_PORT\" --turn-order \"$TURN_ORDER\""
  for ((i=1; i<=AGENT_COUNT; i++)); do
    echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.shared_pool.pool_agent --tb-id tb$i --host \"$POOL_HOST\" --port \"$POOL_PORT\""
  done
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.architectures.shared_pool.pool_cli --host \"$POOL_HOST\" --port \"$POOL_PORT\""
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
SHARED_ENV="AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=$MCP_URL"

wait_for_tcp() {
  local host="$1"
  local port="$2"
  local name="$3"
  local tries="${4:-30}"
  local delay="${5:-0.25}"

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
      return 0
    fi
    sleep "$delay"
  done

  echo "Timed out waiting for $name at $host:$port" >&2
  return 1
}

echo "Launching SHARED MESSAGE POOL (blackboard) architecture..."
echo "  MCP bind:    $MCP_HOST:$MCP_PORT"
echo "  MCP connect: $MCP_URL"
echo "  Pool:        $POOL_HOST:$POOL_PORT"
echo "  Agents: $AGENT_COUNT pool agents (turn order: $TURN_ORDER)"
echo "  Usage monitor: $MONITOR_HOST:$MONITOR_PORT (UDP)"

launch_window "Usage Monitor" \
  "python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
launch_window "MCP Server (shared)" \
  "python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""
launch_window "Message Pool" \
  "AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.shared_pool.message_pool --host \"$POOL_HOST\" --port \"$POOL_PORT\" --turn-order \"$TURN_ORDER\""

wait_for_tcp "$MCP_CONNECT_HOST" "$MCP_PORT" "MCP server"
wait_for_tcp "$POOL_HOST" "$POOL_PORT" "Message pool"
for ((i=1; i<=AGENT_COUNT; i++)); do
  launch_window "Pool Agent tb$i" \
    "$SHARED_ENV python -m agentpackage.architectures.shared_pool.pool_agent --tb-id tb$i --host \"$POOL_HOST\" --port \"$POOL_PORT\""
done
launch_window "User (pool_cli)" \
  "python -m agentpackage.architectures.shared_pool.pool_cli --host \"$POOL_HOST\" --port \"$POOL_PORT\""

echo "Done. Type in the 'User (pool_cli)' terminal to broadcast directly to every agent."

