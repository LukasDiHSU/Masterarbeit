#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

set +u
source /opt/ros/jazzy/setup.bash
source /opt/atb_hsu/setup.bash
set -u

export AGENT_PLATFORM="${AGENT_PLATFORM:-q1}"
export AGENT_WORLD="${AGENT_WORLD:-open}"
export AGENT_NAV_ROBOT="${AGENT_NAV_ROBOT:-q1}"
export AGENT_MAP_FRAME="${AGENT_MAP_FRAME:-open}"
export AGENT_SEM_LIDAR_FOV_DEG="${AGENT_SEM_LIDAR_FOV_DEG:-360}"

pkill -f 'gz sim' 2>/dev/null || true
pkill -f gz-sim 2>/dev/null || true
pkill -f rviz2 2>/dev/null || true
pkill -f component_container_isolated 2>/dev/null || true
pkill -f superfour 2>/dev/null || true
pkill -f q1_sensor_summarizer 2>/dev/null || true
# Older launch.sh published utm_32n→local and stole ATB's occupancy frame.
pkill -f 'static_transform_publisher .*--child-frame-id local' 2>/dev/null || true
pkill -f 'tf_open_to_utm' 2>/dev/null || true
sleep 1

# Map-frame alias for MCP/summarizer only. Do NOT publish utm_32n→local:
# ATB localization already owns `local`; a second parent makes the occupancy
# gridmap jump.
ros2 run tf2_ros static_transform_publisher \
  --x -458054.71 --y -5429310.68 --z 0 \
  --frame-id open --child-frame-id utm_32n \
  --ros-args -r __node:=tf_open_to_utm &
TF1=$!

cd "$PROJECT_ROOT"
SUMMARIZER_PY="$(command -v python3)"
PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" "$SUMMARIZER_PY" -m agentpackage.q1_sensor_summarizer &
SUM_PID=$!

cleanup() {
  kill "$TF1" "$SUM_PID" 2>/dev/null || true
}
trap cleanup EXIT

ros2 launch q1_sim sim_q1_waypoint_navigation.launch.py \
  simulate_with_gazebo:=True \
  use_sim_time:=True \
  world:=open \
  launch_gazebo:=True \
  use_localization:=True \
  show_rviz:=True
