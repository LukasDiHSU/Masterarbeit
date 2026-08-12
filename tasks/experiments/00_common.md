# Common rules (all experiments)

Source: Zwischenpräsentation (architectures, criteria, protocol notes).

## Architectures — who gets the prompt

Pptx names → current code:

| Pptx | Code architecture | Entry point | Agents started | How robots get work |
|---|---|---|---|---|
| Zentral | **centralized** | `master` | 1 master + N robot workers | Master delegates via `ask_robot` / `ask_all_robots` / `ask_selected_robots` |
| Dezentral | **conflict_based** | mission CLI / one peer | N peer robots, no master | Solo by default; mesh negotiation only on conflict events |
| Hybrid | **HMAS-1** | `planner` | 1 planner + N robots | Planner sends one initial plan; robots discuss in turn order until EXECUTE |
| (Hybrid variant) | **HMAS-2** | `planner` | 1 planner + N robots | Planner collects AGREE/DISAGREE, then EXECUTE |
| Pool | **shared_pool** | pool CLI | N pool agents + pool server | Round-robin posts; no addressing; end with `DONE` |

Run every scenario × difficulty on **all five** architectures when possible.
Minimum fair set for the thesis comparison (pptx slide set): centralized, conflict_based, HMAS-1, shared_pool.

## Shared MCP context

Every robot agent uses the shared MCP server (`agentpackage/mcpserver.py`):
stations/boxes inventory, `rank_stations_by_distance`, `navigate_to_pose`,
`drive_distance` (recovery), poses, whiteboard. Conflict-based also uses events.

Do **not** paste full coordinate tables into the user prompt — agents look them up
with tools (`list_stations`, `get_station`, `rank_stations_by_distance`, …).

**Station capacity:** each station holds at most **one** box. Empty pads have
`box_id=null` and `available=false`. `drop_box` fails on occupied stations
(`station_occupied`). For opposing swaps (e.g. A↔C), clear destinations (pick
first / stage) before dropping. Robots also hold at most one box.

Every prompt starts with the map/world name (e.g. `You are on the stations map
(world: stations).`). Keep that line when pasting.

For CLIs that treat Enter as “send”, use the **single-line** files under
[prompts/](prompts/) (one physical line per difficulty) instead of the
multi-line blocks in the scenario markdown.

**Timing (all architectures — wall-clock until the system thinks it is done):**
- **centralized** / **HMAS-1** / **HMAS-2** — master/planner chat prints `elapsed until done` after each user message’s final reply.
- **conflict_based** — Mission CLI: paste prompt → all peers in parallel; clock stops when every peer has replied.
- **shared_pool** — pool CLI: from your post until an agent posts `DONE` in the execute phase (also logs first agent reply). Discuss ends only when all agents `AGREE`.
- All append to `timings.log` under the active experiment session (`tasks/experiments/runs/_active/…`, kept after `save_experiment.sh`).

**Discussion architectures:**
- **HMAS-1** — planner primes once; robots discuss turn-by-turn until unanimous `AGREE`, then execute. Replan via `start_discussion_round` / `NEED_DISCUSSION`.
- **shared_pool** — same AGREE→execute idea on the blackboard; `start_discussion_round` reopens discuss.

Robot names in prompts: `SmallDeliveryRobot_0` … `SmallDeliveryRobot_{N-1}`
(legacy `tb1`/`robot_tb1` still resolve, but prefer remroc ids).

## What to give the agent structure (every run)

| Input | Centralized | Conflict-based | HMAS-1 / HMAS-2 | Shared pool |
|---|---|---|---|---|
| **User prompt** | → `master` chat | → mission CLI / one peer | → `planner` chat | → pool CLI |
| **Scenario** | remroc world + matching `worlds/items/<name>.json` / MCP stations | same | same | same |
| **Active robots** | start only the N workers you need; prompt names participants | start only N peers | same N robots | start only N pool agents |
| **Do not** | message workers directly for the scored run | paste the same prompt into every peer | skip consensus / dialogue before execute | address one robot as if private DM |

## Metrics (every run)

From pptx “Kriterien” + monitor notes:

1. **TSR** — task success rate (`successful runs / all runs`)
2. **Token usage** — in / out / total (Usage Monitor)
3. **End-to-end time** — wall-clock from prompt → success or timeout
4. **Steps** — LLM calls and/or inter-agent messages until success
5. **Communication load** — inter-agent message count (Usage Monitor)
6. **Scalability** — compare the same scenario across Easy / Medium / Hard (2 / 4 / 6 robots)

Also keep Agent Trace (UDP `:9901`) for qualitative tool-use / failure analysis.

## Protocol

- **Repeats:** ≥ 5 runs per cell `(scenario × difficulty × architecture)`
- **Constants:** same model, same temperature, fresh agent processes each run
- **Timeouts:** Easy 10 min · Medium 15 min · Hard 20 min (timeout = failure)
- **Stack:** ROS 2 + Modern Gazebo (remroc / SmallDeliveryRobot), MCP + architecture launch script

## Fleet size note

`AGENT_COUNT` ∈ {2, 4, 6, 8}. Easy/Medium fit today. Hard (6 robots) needs
`--agents 6` (or `AGENT_COUNT=6`) on the launch script.
