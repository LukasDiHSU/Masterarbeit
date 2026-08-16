# Common rules (Q1 semantic tour)

Compare the five coordination architectures on **one Q1** with three
**specialists** (not a remroc multi-robot fleet).

Specialist names: `navigator`, `lidar`, `camera`.
Physical nav id: `q1`.

## Architectures — who gets the prompt

| Architecture | Entry point | Agents started | How work is assigned |
|---|---|---|---|
| **centralized** | `master` | 1 master + 3 specialists | Master delegates via `ask_robot` / `ask_all_robots` |
| **conflict_based** | mission CLI | 3 peers, no master | Solo by specialty; mesh negotiation on `detection_conflict` / `nav_aborted` |
| **HMAS-1** | `planner` | 1 planner + 3 specialists | Planner proposes one leg per specialist; AGREE / DISAGREE; then execute |
| **HMAS-2** | `planner` | 1 planner + 3 specialists | Planner collects AGREE/DISAGREE, then EXECUTE |
| **agentnet** | task CLI | 3 mesh nodes; `navigator` chairs | Agree on a short action chunk, execute, meet again; done when every specialist says `FINISHED` |

Run every difficulty on **all five** architectures.

## Shared MCP context

Bring-up: `agent_project/launch.sh` (Gazebo open world + ATB waypoint nav +
sensor summarizer), then an architecture `launch_*.sh` with
`AGENT_PLATFORM=q1` `AGENT_WORLD=open` (these are the defaults on this branch).
Q1 launch scripts force `AGENT_COUNT=3`.

Legal coordinates (agents must not invent object x/y):

| Source | Tool | Who |
|---|---|---|
| Self pose | `get_robot_pose` | navigator (if this fails, assume spawn `(0, 0)`) |
| Occupancy | `get_occupancy_map` | navigator, master, planners — yaml + ASCII walls/free space; **no object outlines**; `.` cells are explore poses |
| Sensed object | `get_semantic_lidar_objects` | lidar — **only** source of object x/y, and only when Q1 is **close** |
| Vision | `get_camera_image` | camera — RGB still for the multimodal model (no xyz) |

There are **no stations, boxes, look pads, or multi-robot fleet ids**. Planted
landmark xyz in `worlds/items/open.json` are **scoring-only** (humans / scorer),
not agent-facing. A run that drives to a guessed corner is a protocol failure.

Protocol: **sense → get closer → sense**. Semantic lidar is 360° but only
returns a class from close range. The camera is forward-facing: it looks at
the picture and has no xyz. If the next tour class is missing, call
`get_occupancy_map`, drive to a free cell, and sense again. Stopping after
one missed scan is a failed run. Do not invent coordinates. The occupancy
map must not be used as an object map.

For CLIs that treat Enter as “send”, use the **single-line** files under
[prompts/](prompts/).

**Timing:** centralized / HMAS print `elapsed until done` after the final reply.
conflict_based Mission CLI stops when every specialist has replied. AgentNet
task CLI: from handing the mission to `navigator` until the mesh reports back.

## What to give the agent structure (every run)

| Input | Centralized | Conflict-based | HMAS-1 / HMAS-2 | AgentNet |
|---|---|---|---|---|
| **User prompt** | → `master` chat | → mission CLI | → `planner` chat | → task CLI (enters at `navigator`) |
| **Scenario** | Q1 `open` world | same | same | same |
| **Agents** | master + 3 specialists | 3 peers | planner + 3 specialists | 3 nodes |
| **Do not** | message specialists directly for the scored run | paste into every peer separately (CLI does that) | skip consensus before execute | hand the mission to anyone but `navigator` |

## Metrics (every run)

1. **TSR** — task success rate (`successful runs / all runs`)
2. **Token usage** — in / out / total (Usage Monitor)
3. **End-to-end time** — wall-clock from prompt → success or timeout
4. **Steps** — LLM calls and/or inter-agent messages until success
5. **Communication load** — inter-agent message count (Usage Monitor)
6. **Scalability** — Easy / Medium / Hard = tour length (2 / 3 / 4 stops), always 3 specialists

Also keep Agent Trace (UDP `:9901`) for qualitative tool-use / failure analysis.

## Protocol

- **Repeats:** ≥ 5 runs per cell `(difficulty × architecture)`
- **Constants:** same model, same temperature, fresh agent processes each run
- **Timeouts:** Easy 10 min · Medium 15 min · Hard 20 min (timeout = failure)
- **Stack:** ROS 2 + Gazebo Q1 (`./launch.sh`) + MCP + architecture launch script
