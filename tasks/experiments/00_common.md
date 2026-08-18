# Common rules (all experiments)

Source: Zwischenpräsentation (architectures, criteria, protocol notes).

## Architectures — who gets the prompt

Pptx names → current code:

| Pptx | Code architecture | Entry point | Agents started | How robots get work |
|---|---|---|---|---|
| Zentral | **centralized** | `master` | 1 master + N robot workers | Master delegates via `ask_robot` / `ask_all_robots` / `ask_selected_robots` |
| Dezentral | **conflict_based** | mission CLI / one peer | N peer robots, no master | Solo by default; mesh negotiation only on conflict events |
| Hybrid | **HMAS-1** | `planner` | 1 planner + N robots | Planner proposes a full mission plan once (last STEP `FINISHED`); robots vote per STEP (`AGREE` = execute work / end on FINISH). `DISAGREE` / new `PLAN` discards the original plan and continues as PMAS peers |
| (Hybrid variant) | **HMAS-2** | `planner` | 1 planner + N robots | Planner collects AGREE/DISAGREE, then EXECUTE |
| Dezentral (AgentNet / DMAS) | **agentnet** | task CLI | N mesh nodes, Agent 0 chairs turns | Robots agree on a short action chunk, execute it, meet again; done when every robot says `FINISHED` |

Run every scenario × difficulty on **all five** architectures when possible.
Minimum fair set for the thesis comparison (pptx slide set): centralized, conflict_based, HMAS-1, agentnet.

## Shared MCP context

Every robot agent uses the shared MCP server (`agentpackage/mcpserver.py`):
stations/boxes inventory, `rank_stations_by_distance`, `navigate_to_pose`,
`drive_distance` (recovery), poses. Conflict-based also uses events.

**One world source of truth:** set `AGENT_WORLD` to the map you run, either in
`agent_project/.env` or per run (`AGENT_WORLD=bottleneck_1 ./…/launch_hmas1.sh
--agents 2`). Note that `.env.example` is only a template — nothing reads it.
Launch scripts load `.env` and pass the world into the MCP process, so
`list_stations` / `get_map_info("")` match `worlds/items/{AGENT_WORLD}.json`;
the launcher warns if no such file exists. Calling `get_map_info(world_id=…)` or
`set_world` also switches inventory (including non-pad landmarks like `gap`,
caches). Do not assume A–D stations on every map. Restart MCP after changing
the world.

Do **not** paste full coordinate tables into the user prompt — agents look them up
with tools (`list_stations`, `get_station`, `rank_stations_by_distance`, …).

**No invented coordinates:** every architecture's prompts carry the shared
`COORDINATE_RULE` (`agentpackage/config.py`) — an agent may only pass x/y that
came from a tool result or the user prompt, and may only drive to positions
that exist on the active map. Treat a run in which a robot navigates to a
made-up pose as a failure and note it.

**Station capacity:** each station holds at most **one** box. Empty pads have
`box_id=null` and `available=false`. `drop_box` fails on occupied stations
(`station_occupied`). For opposing swaps (e.g. A↔C), clear destinations (pick
first / stage) before dropping. Robots also hold at most one box.

**Proximity:** `pickup_box` / `drop_box` only succeed when the robot is within
`MCP_MANIP_RADIUS_M` (default 1.8 m) of the station. Always
`navigate_to_pose` (prefer `navigate_xy` from `rank_stations_by_distance`)
before pick/drop; remote teleports return `too_far_from_station`.

Every prompt starts with the map/world name (e.g. `You are on the stations map
(world: stations).`). Keep that line when pasting.

For CLIs that treat Enter as “send”, use the **single-line** files under
[prompts/](prompts/) (one physical line per difficulty) instead of the
multi-line blocks in the scenario markdown.

**Timing (all architectures — wall-clock until the system thinks it is done):**
- **centralized** / **HMAS-1** / **HMAS-2** — master/planner chat prints `elapsed until done` after each user message’s final reply.
- **conflict_based** — Mission CLI: paste prompt → all peers in parallel; clock stops when every peer has replied.
- **agentnet** — task CLI: from handing the mission to `SmallDeliveryRobot_0` until the fleet reports back (`agentnet_until_done`).
- All append to `timings.log` under the active experiment session (`tasks/experiments/runs/_active/…`, kept after `save_experiment.sh`).

**HMAS-1 (hybrid of DMAS, Chen et al. arXiv:2309.15943):**
The central planner proposes a **full natural-language mission plan** (ordered
`STEP` blocks; one leg per robot per work step: at most one drive plus at most
one pick or drop). The **last STEP is always `FINISHED`**. Robots do **not**
ratify the whole plan. They vote on **the next STEP only** (`AGREE` = execute
a work step as written, or **end the mission** on `FINISHED`). `DISAGREE` or a
**different `PLAN`** **discards the original plan**; the fleet then continues
as a **peer (PMAS) network** (`PLAN` / `AGREE` / `FINISHED`).
- Limits (env-tunable): `PAPER_MAX_PLAN_STEPS` (12 executed STEPs), `PAPER_MAX_DIALOGUE_ROUNDS` (3 vote passes per STEP), `PAPER_MAX_SYNTAX_RETRIES` (3). Hitting a limit ends the mission as a failure.

**AgentNet (DMAS variant, Chen et al. arXiv:2309.15943):** no planner. The
robots take turns until an `EXECUTE` block with 1–`AGENTNET_CHUNK_STEPS` (2)
actions per robot passes the verifier; they execute that chunk and meet again.
The mission ends only when **every** robot says `FINISHED` in the same
discussion round. Agent 0 chairs the speaking order and does not propose a
privileged plan.

Robot names in prompts: `SmallDeliveryRobot_0` … `SmallDeliveryRobot_{N-1}`
(legacy `tb1`/`robot_tb1` still resolve, but prefer remroc ids).

## What to give the agent structure (every run)

| Input | Centralized | Conflict-based | HMAS-1 / HMAS-2 | AgentNet |
|---|---|---|---|---|
| **User prompt** | → `master` chat | → mission CLI / one peer | → `planner` chat | → task CLI (always enters at `SmallDeliveryRobot_0`) |
| **Scenario** | remroc world + matching `worlds/items/<name>.json` / MCP stations | same | same | same |
| **Active robots** | start only the N workers you need; prompt names participants | start only N peers | same N robots | start only N nodes |
| **Do not** | message workers directly for the scored run | paste the same prompt into every peer | skip consensus / dialogue before execute | hand the mission to any agent but `_0` |

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
