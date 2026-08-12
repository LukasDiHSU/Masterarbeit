#!/usr/bin/env bash
set -euo pipefail

# Launches conflict-based mesh: MCP + AGENT_COUNT solo peers + mission CLI.
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
MESH_HOST="127.0.0.1"
MESH_BASE_PORT="9101"
MESH_CLI_PORT="9099"
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
  for ((i=0; i<AGENT_COUNT; i++)); do
    echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=http://$MCP_CONNECT_HOST:$MCP_PORT/sse python -m agentpackage.architectures.conflict_based.robot_peer_agent --robot-id SmallDeliveryRobot_$i --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\""
  done
  echo "cd \"$PROJECT_ROOT\" && AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.conflict_based.mission_cli --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\" --cli-port \"$MESH_CLI_PORT\""
  exit 1
fi

detect_terminal || exit 1
init_experiment_session "conflict_based"

MCP_URL="http://$MCP_CONNECT_HOST:$MCP_PORT/sse"
SHARED_ENV="AGENT_COUNT=$AGENT_COUNT AGENT_MCP_URL=$MCP_URL"

echo "Launching CONFLICT-BASED architecture..."
echo "  MCP bind:    $MCP_HOST:$MCP_PORT"
echo "  MCP connect: $MCP_URL"
echo "  Mesh base:   $MESH_HOST:$MESH_BASE_PORT (SmallDeliveryRobot_0..N-1)"
echo "  Agents: $AGENT_COUNT peers + mission CLI"
echo "  Usage monitor: $MONITOR_HOST:$MONITOR_PORT (UDP)"
echo "  Agent trace:   $MONITOR_HOST:$TRACE_MONITOR_PORT (UDP)"

launch_window "Usage Monitor" \
  "python -m agentpackage.monitor --host \"$MONITOR_HOST\" --port \"$MONITOR_PORT\""
launch_window "Agent Trace" \
  "python -m agentpackage.trace_monitor --host \"$MONITOR_HOST\" --port \"$TRACE_MONITOR_PORT\""
launch_window "MCP Server (shared)" \
  "python -m agentpackage.mcpserver --host \"$MCP_HOST\" --port \"$MCP_PORT\""

wait_for_tcp "$MCP_CONNECT_HOST" "$MCP_PORT" "MCP server"
for ((i=AGENT_COUNT-1; i>=0; i--)); do
  launch_window "Peer SmallDeliveryRobot_$i" \
    "$SHARED_ENV python -m agentpackage.architectures.conflict_based.robot_peer_agent --robot-id SmallDeliveryRobot_$i --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\""
done
sleep 1
launch_window "Mission CLI" \
  "AGENT_COUNT=$AGENT_COUNT python -m agentpackage.architectures.conflict_based.mission_cli --host \"$MESH_HOST\" --base-port \"$MESH_BASE_PORT\" --cli-port \"$MESH_CLI_PORT\""

echo "Done. Check the opened terminal windows."
echo "Save this run: ./agentpackage/architectures/save_experiment.sh <run_name> [--stop]"
