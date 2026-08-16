#!/usr/bin/env bash
set -euo pipefail

# Bring up Gazebo (open arena + labeled objects), spawn Q1, start the sensor
# summarizer. Start your Nav2 stack yourself if it is not launched here.
#
# Usage:
#   ./launch_q1_stack.sh
#   Q1_NAV2_LAUNCH="your_pkg your_nav2.launch.py" ./launch_q1_stack.sh

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
WORLD_SDF="$REPO_ROOT/worlds/extra_maps/open/world.sdf"
MODELS_DIR="$REPO_ROOT/worlds/models"
ATB_ASSETS="/opt/atb_hsu/share/iosb_gazebo_ressources/worlds/assets"

if [[ ! -f "$WORLD_SDF" ]]; then
  echo "error: missing $WORLD_SDF" >&2
  exit 1
fi

# ROS/ATB setup.bash reads optional vars (e.g. AMENT_TRACE_SETUP_FILES) that
# are unset; nounset must be off while sourcing them.
set +u
if [[ -f /opt/ros/jazzy/setup.bash ]]; then
  # shellcheck disable=SC1091
  source /opt/ros/jazzy/setup.bash
fi
if [[ -f /opt/atb_hsu/setup.bash ]]; then
  # shellcheck disable=SC1091
  source /opt/atb_hsu/setup.bash
fi
set -u

export AGENT_PLATFORM="${AGENT_PLATFORM:-q1}"
export AGENT_WORLD="${AGENT_WORLD:-open}"
export AGENT_NAV_ROBOT="${AGENT_NAV_ROBOT:-q1}"
export GZ_SIM_RESOURCE_PATH="${MODELS_DIR}:${ATB_ASSETS}:${GZ_SIM_RESOURCE_PATH:-}"

echo "Q1 stack"
echo "  world:     $WORLD_SDF"
echo "  platform:  $AGENT_PLATFORM"
echo "  AGENT_WORLD=$AGENT_WORLD"
echo "  GZ_SIM_RESOURCE_PATH=$GZ_SIM_RESOURCE_PATH"

gz sim -r -v 0 "$WORLD_SDF" &
GZ_PID=$!
echo "Gazebo pid $GZ_PID"
sleep 6

# Spawn Q1 into the already-running simulator (flat_world spawn pose = origin).
ros2 launch qx_description qx_gazebo.launch.py \
  launch_gazebo:=False \
  model_name:=q1 \
  world:=flat_world \
  use_sim_time:=True \
  visualize:=False \
  show_joint_state_publisher_gui:=False &
QX_PID=$!
echo "qx_gazebo pid $QX_PID"
sleep 4

# Semantic conversion / extra ATB sensor bridge (optional if already covered).
if ros2 pkg prefix q1_sim >/dev/null 2>&1; then
  ros2 launch q1_sim superfour_localization_and_mapping.launch.py \
    world:=flat_world \
    use_sim_time:=True \
    use_localization:=True &
  LOC_PID=$!
  echo "localization/mapping pid $LOC_PID"
fi

# Summarizer needs system Python with rclpy (not the agent venv).
SUMMARIZER_PY="${PROJECT_ROOT}/.venv/bin/python"
if ! "$SUMMARIZER_PY" -c "import rclpy" >/dev/null 2>&1; then
  SUMMARIZER_PY="$(command -v python3)"
fi
PYTHONPATH="$PROJECT_ROOT:${PYTHONPATH:-}" "$SUMMARIZER_PY" -m agentpackage.q1_sensor_summarizer &
SUM_PID=$!
echo "summarizer pid $SUM_PID ($SUMMARIZER_PY)"

if [[ -n "${Q1_NAV2_LAUNCH:-}" ]]; then
  # shellcheck disable=SC2086
  ros2 launch ${Q1_NAV2_LAUNCH} &
  NAV_PID=$!
  echo "Nav2 pid $NAV_PID (${Q1_NAV2_LAUNCH})"
else
  echo
  echo "Start your Nav2 stack in another terminal."
  echo "  Expected action: /q1/navigate_to_pose"
  echo "  Map: $REPO_ROOT/worlds/maps/open.yaml"
  echo "  Then: AGENT_PLATFORM=q1 AGENT_WORLD=open ./agentpackage/architectures/centralized/launch_centralized.sh --agents 3"
fi

echo
echo "PIDs: gazebo=$GZ_PID qx=$QX_PID summarizer=$SUM_PID"
echo "Stop: kill $GZ_PID $QX_PID $SUM_PID ${LOC_PID:-} ${NAV_PID:-}"
wait
