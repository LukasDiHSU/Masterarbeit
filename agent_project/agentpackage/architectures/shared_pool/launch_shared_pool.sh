#!/usr/bin/env bash
set -euo pipefail

# Launches shared pool: MCP + pool server + AGENT_COUNT agents + user CLI.
# Fleet size: AGENT_COUNT / AGENTS / --agents (2|4|6|8, default 4).

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ARCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../_parse_agents.sh
source "$ARCH_DIR/_parse_agents.sh"
parse_agent_count "$@" || exit 1
set -- "${LAUNCH_ARGS[@]}"
# shellcheck source=../_launch_common.sh
source "$ARCH_DIR/_launch_common.sh"

MCP_HOST="0.0.0.0"
MCP_PORT="8000"
MCP_CONNECT_HOST="127.0.0.1"
POOL_HOST="127.0.0.1"
POOL_PORT="8866"
MONITOR_HOST="127.0.0.1"
MONITOR_PORT="9900"
TRACE_MONITOR_PORT="9901"
TURN_ORDER=""
for ((i=0; i<AGENT_COUNT; i++)); do
  if [[ -n "$TURN_ORDER" ]]; then TURN_ORDER+=","; fi
  TURN_ORDER+="SmallDeliveryRobot_$i"
done
USE_TABS=1

if ! require_display_or_print; then
  echo "No graphical terminal detected (DISPLAY/WAYLAND_DISPLAY unset)."
  echo "Run these in separate shells or inside tmux (AGENT_COUNT=$AGENT_COUNT):"
  echo
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.trace_monitor --host \"$MONITOR_HOST\" --port \"$TRACE_MONITOR_PORT\""
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""
  echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.shared_pool.message_pool --host \"$POOL_HOST\" --port \"$POOL_PORT\" --turn-order \"$TURN_ORDER\""
  for ((i=0; i<AGENT_COUNT; i++)); do
    echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.shared_pool.pool_agent --robot-id SmallDeliveryRobot_$i --host \"$POOL_HOST\" --port \"$POOL_PORT\""
  done
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.architectures.shared_pool.pool_cli --host \"$POOL_HOST\" --port \"$POOL_PORT\""
  exit 1
fi

detect_terminal || exit 1
init_experiment_session "shared_pool"

MCP_URL="http://$MCP_CONNECT_HOST:$MCP_PORT/sse"
SHARED_ENV="AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=$MCP_URL"

echo "Launching SHARED MESSAGE POOL (blackboard) architecture..."
echo "  MCP bind:    $MCP_HOST:$MCP_PORT"
echo "  MCP connect: $MCP_URL"
echo "  Pool:        $POOL_HOST:$POOL_PORT"
echo "  Agents: $AGENT_COUNT pool agents (turn order: $TURN_ORDER)"
echo "  Usage monitor: $MONITOR_HOST:$MONITOR_PORT (UDP)"
echo "  Agent trace:   $MONITOR_HOST:$TRACE_MONITOR_PORT (UDP)"

launch_window "Usage Monitor" \
  "python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
launch_window "Agent Trace" \
  "python -m agentpackage.trace_monitor --host \"$MONITOR_HOST\" --port \"$TRACE_MONITOR_PORT\""
launch_window "MCP Server (shared)" \
  "python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""
launch_window "Message Pool" \
  "AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.shared_pool.message_pool --host \"$POOL_HOST\" --port \"$POOL_PORT\" --turn-order \"$TURN_ORDER\""

wait_for_tcp "$MCP_CONNECT_HOST" "$MCP_PORT" "MCP server"
wait_for_tcp "$POOL_HOST" "$POOL_PORT" "Message pool"
for ((i=0; i<AGENT_COUNT; i++)); do
  launch_window "Pool Agent SmallDeliveryRobot_$i" \
    "$SHARED_ENV python -m agentpackage.architectures.shared_pool.pool_agent --robot-id SmallDeliveryRobot_$i --host \"$POOL_HOST\" --port \"$POOL_PORT\""
done
launch_window "User (pool_cli)" \
  "python -m agentpackage.architectures.shared_pool.pool_cli --host \"$POOL_HOST\" --port \"$POOL_PORT\""

echo "Done. Type in the 'User (pool_cli)' terminal to broadcast directly to every agent."
echo "Save this run: ./agentpackage/architectures/save_experiment.sh <run_name> [--stop]"
