# Custom TurtleBot3 arenas

Walls-only Gazebo model plus scenario worlds/maps for multi-robot Nav2 / agent tests.

## Layout

- `models/turtlebot3_arena_walls/` — outer walls only (no pillars / ROS logo)
- `scenarios/<name>/world.sdf` — Gazebo world
- `scenarios/<name>/map.yaml` + `map.pgm` — matching Nav2 occupancy map
- `scenarios/<name>/items.json` — station/item coords for the MCP agent tools
- `_base/walls_only.pgm` — base map used by the stamp tool
- `tools/stamp_obstacles_on_map.py` — bake box footprints into a map
- `tools/generate_scenarios.py` — regenerate bottleneck / stations / cross / rooms

## Scenarios

| Name | Description |
|------|-------------|
| `open` | Walls only |
| `boxes_a` | Sparse interior boxes |
| `boxes_b` | Denser corridor-like boxes |
| `bottleneck` | Thick red center divider with ~0.4 m one-robot gap |
| `stations` | Circuit A→B→C→D (center blocked; colored floor pads) |
| `cross` | Plus-shaped walls; four quadrant stations + center crossing |
| `rooms` | Four corner rooms with doorways onto a shared hall |

## Launch

From `agent_project/`:

```bash
SCENARIO=stations ./launch_tb3_stack.sh
SCENARIO=bottleneck ./launch_tb3_stack.sh
SCENARIO=cross ./launch_tb3_stack.sh
SCENARIO=rooms ./launch_tb3_stack.sh
```

`launch_tb3_stack.sh` writes `.active_scenario` so the MCP server loads matching `items.json`.

## Regenerate

```bash
python3 tools/generate_scenarios.py
```
