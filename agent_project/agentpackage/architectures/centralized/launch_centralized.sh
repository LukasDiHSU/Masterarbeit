#!/usr/bin/env bash
set -euo pipefail

# Launches centralized (star): MCP + broker + AGENT_COUNT robots + master.
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
BROKER_HOST="127.0.0.1"
BROKER_PORT="8765"
MONITOR_HOST="127.0.0.1"
MONITOR_PORT="9900"
TRACE_MONITOR_PORT="9901"
USE_TABS=1

if ! require_display_or_print; then
  echo "No graphical terminal detected (DISPLAY/WAYLAND_DISPLAY unset)."
  echo "Run these in separate shells or inside tmux (AGENT_COUNT=$AGENT_COUNT):"
  echo
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.trace_monitor --host \"$MONITOR_HOST\" --port \"$TRACE_MONITOR_PORT\""
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""
  echo "cd \"$PROJECT_ROOT\" && python -m agentpackage.architectures.centralized.agent_broker --host \"$BROKER_HOST\" --port \"$BROKER_PORT\""
  for ((i=0; i<AGENT_COUNT; i++)); do
    echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.centralized.robot_agent --robot-id SmallDeliveryRobot_$i --host \"$BROKER_HOST\" --port \"$BROKER_PORT\""
  done
  echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.centralized.master_agent --host \"$BROKER_HOST\" --port \"$BROKER_PORT\""
  exit 1
fi

detect_terminal || exit 1
init_experiment_session "centralized"

MCP_URL="http://$MCP_CONNECT_HOST:$MCP_PORT/sse"
SHARED_ENV="AGENT_COUNT=$AGENT_COUNT AGENT_WORLD=$AGENT_WORLD AGENT_MCP_URL=$MCP_URL"

echo "Launching CENTRALIZED (star) architecture..."
echo "  MCP bind:    $MCP_HOST:$MCP_PORT"
echo "  MCP connect: $MCP_URL"
echo "  AGENT_WORLD: $AGENT_WORLD"
echo "  Broker: $BROKER_HOST:$BROKER_PORT"
echo "  Agents: $AGENT_COUNT robots + master"
echo "  Usage monitor: $MONITOR_HOST:$MONITOR_PORT (UDP)"
echo "  Agent trace:   $MONITOR_HOST:$TRACE_MONITOR_PORT (UDP)"

launch_window "Usage Monitor" \
  "python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
launch_window "Agent Trace" \
  "python -m agentpackage.trace_monitor --host \"$MONITOR_HOST\" --port \"$TRACE_MONITOR_PORT\""
launch_window "MCP Server (shared)" \
  "AGENT_WORLD=$AGENT_WORLD python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""
launch_window "Agent Broker" \
  "python -m agentpackage.architectures.centralized.agent_broker --host \"$BROKER_HOST\" --port \"$BROKER_PORT\""

wait_for_tcp "$MCP_CONNECT_HOST" "$MCP_PORT" "MCP server"
wait_for_tcp "$BROKER_HOST" "$BROKER_PORT" "Agent broker"
for ((i=0; i<AGENT_COUNT; i++)); do
  launch_window "Robot SmallDeliveryRobot_$i" \
    "$SHARED_ENV python -m agentpackage.architectures.centralized.robot_agent --robot-id SmallDeliveryRobot_$i --host \"$BROKER_HOST\" --port \"$BROKER_PORT\""
done
launch_window "Master Agent" \
  "AGENT_COUNT=$AGENT_COUNT AGENT_WORLD=$AGENT_WORLD python -m agentpackage.architectures.centralized.master_agent --host \"$BROKER_HOST\" --port \"$BROKER_PORT\""

echo "Done. Check the opened terminal windows."
echo "Save this run: ./agentpackage/architectures/save_experiment.sh <run_name> [--stop]"
