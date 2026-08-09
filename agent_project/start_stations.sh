#!/usr/bin/env bash
set -euo pipefail

# Start the stations scenario. Do NOT put this in commands.txt —
# launch_tb3_stack.sh already runs commands.txt after Nav2 is up.

pkill -9 -f 'ros2|gz|gazebo|rviz|nav2|component_container' || true

cd "$(dirname "${BASH_SOURCE[0]}")"
source /opt/ros/jazzy/setup.bash
source ~/robot_ws/install/setup.bash
export TURTLEBOT3_MODEL=burger

SCENARIO=stations ./launch_tb3_stack.sh
