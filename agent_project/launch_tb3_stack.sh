#!/usr/bin/env bash
set -euo pipefail

# Launches three terminals:
# 1) tb3_world launch
# 2) tb3_nav2 launch (with optional startup delay)
# 3) commands.txt execution
#
# Optional scenario override (pillars removed + custom obstacles):
#   SCENARIO=open|boxes_a|boxes_b|bottleneck|stations|cross|rooms ./launch_tb3_stack.sh
# Empty SCENARIO (default) uses the stock TurtleBot3 world/map.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMANDS_FILE="$PROJECT_ROOT/commands.txt"
NAV2_DELAY_SECONDS="${NAV2_DELAY_SECONDS:-5}"
POSE_ESTIMATION_DELAY_SECONDS="${POSE_ESTIMATION_DELAY_SECONDS:-10}"
SCENARIO="${SCENARIO:-}"
MAPS_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)/maps"
ACTIVE_SCENARIO_FILE="$PROJECT_ROOT/.active_scenario"

if [[ ! -f "$COMMANDS_FILE" ]]; then
  echo "Missing commands file: $COMMANDS_FILE" >&2
  exit 1
fi

WORLD_LAUNCH_ARGS=""
NAV2_LAUNCH_ARGS=""
GZ_RESOURCE_PREFIX=""

if [[ -n "$SCENARIO" ]]; then
  SCENARIO_DIR="$MAPS_ROOT/scenarios/$SCENARIO"
  WORLD_FILE="$SCENARIO_DIR/world.sdf"
  MAP_FILE="$SCENARIO_DIR/map.yaml"
  MODELS_DIR="$MAPS_ROOT/models"

  if [[ ! -f "$WORLD_FILE" ]]; then
    echo "Scenario world not found: $WORLD_FILE" >&2
    echo "Available scenarios:" >&2
    ls -1 "$MAPS_ROOT/scenarios" 2>/dev/null || true
    exit 1
  fi
  if [[ ! -f "$MAP_FILE" ]]; then
    echo "Scenario map not found: $MAP_FILE" >&2
    exit 1
  fi

  WORLD_LAUNCH_ARGS="world:=$WORLD_FILE"
  NAV2_LAUNCH_ARGS="map:=$MAP_FILE"
  GZ_RESOURCE_PREFIX="$MODELS_DIR"
  printf '%s\n' "$SCENARIO" > "$ACTIVE_SCENARIO_FILE"
  echo "Using scenario '$SCENARIO'"
  echo "  world: $WORLD_FILE"
  echo "  map:   $MAP_FILE"
else
  rm -f "$ACTIVE_SCENARIO_FILE"
  echo "Using stock TurtleBot3 world/map (set SCENARIO=stations|bottleneck|cross|rooms|… to override)"
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

launch_window() {
  local title="$1"
  local cmd="$2"
  local env_prefix=""
  if [[ -n "$GZ_RESOURCE_PREFIX" ]]; then
    env_prefix="export GZ_SIM_RESOURCE_PATH=\"$GZ_RESOURCE_PREFIX\${GZ_SIM_RESOURCE_PATH:+:\$GZ_SIM_RESOURCE_PATH}\"; "
  fi
  local wrapped="${env_prefix}cd \"$PROJECT_ROOT\" && $cmd; exec bash"
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

launch_window "TB3 World" \
  "ros2 launch tb3_multi_robot tb3_world.launch.py $WORLD_LAUNCH_ARGS"

launch_window "TB3 Nav2" \
  "sleep \"$NAV2_DELAY_SECONDS\" && ros2 launch tb3_multi_robot tb3_nav2.launch.py $NAV2_LAUNCH_ARGS"

launch_window "TB Commands" \
  "sleep \"$POSE_ESTIMATION_DELAY_SECONDS\" && bash \"$COMMANDS_FILE\""

echo "Launched TB3 world, TB3 nav2, and commands.txt in separate terminals."
