# agentpackage — multi-agent robot fleet architectures

This project controls a fleet of remroc `SmallDeliveryRobot_0`..`_N-1` robots
(`N` is `AGENT_COUNT` ∈ {2,4,6,8}, default 4) over
ROS 2 / Nav2, exposed to LLM agents through an MCP tool server
(`agentpackage/mcpserver.py`). On top of that shared, architecture-agnostic
tool layer, several multi-agent coordination strategies are implemented so
they can be compared directly (see `agentpackage/architectures/`):

1. **Centralized** — a single master agent is the only one that may
   delegate; every message is routed through one central broker (star
   topology).
2. **Conflict-based** — peers work alone by default (no master/broker for
   routine work); mesh negotiation opens only when MCP events such as
   conflicts involve them, and only toward that event’s participants.
3. **HMAS-1** — central planner primes an initial plan; robots then discuss
   in fixed turn order (each turn sees the plan plus prior comments) until
   one outputs EXECUTE; actions are dispatched over the star broker.
4. **HMAS-2** — central planner proposes a fleet plan; each robot’s local
   LLM reviews its assignment (`AGREE` / `DISAGREE`); the planner re-plans
   until consensus, then sends execute instructions (star broker only).
5. **Shared pool (blackboard, turn-based)** — shared log with no
   addressing. Every agent and the human user connect to one shared
   broadcast log; new joiners are replayed the full history, and every post
   goes to everyone. The pool itself enforces a fixed speaking order
   (`SmallDeliveryRobot_0 -> … -> _N-1 -> repeat`); a user
   message always restarts the round at the front, and the round ends as
   soon as any agent posts the token `DONE`.

## Usage monitor + agent trace

Every `launch_*.sh` script opens a **"Usage Monitor"** terminal
(`python -m agentpackage.monitor`) showing a live-updating table across
every running agent, instead of interleaving usage lines into each agent's
own conversation:

```
=== Agent usage monitor === (Ctrl+C to quit)

AGENT                    ARCHITECTURE     MESSAGES  LLM CALLS    IN TOK   OUT TOK  TOTAL TOK
--------------------------------------------------------------------------------------
master                   centralized             4          3       412       128        540
SmallDeliveryRobot_0     centralized             2          2       201        64        265
SmallDeliveryRobot_1     centralized             2          2       198        60        258
...
--------------------------------------------------------------------------------------
TOTAL            5 agent(s)             12          9      1024       340       1364
```

It also opens an **"Agent Trace"** terminal (`python -m agentpackage.trace_monitor`)
with a scrolling log of each agent's LLM text and tool start/end (UDP port
`9901` by default), so you can watch reasoning and tool use live — including
during long calls like `navigate_to_pose`.

How it works:
- `agentpackage/BaseAgents.py` tags every agent with its architecture and
  reports its **cumulative** token usage (`usage_metadata` summed across
  every LLM call, including intermediate tool-calling round-trips within a
  single request) after every `invoke()`. The same callback path emits
  live `trace` packets (LLM replies, tool_start, tool_end, turn_end).
- Each transport also counts **inter-agent messages** and reports its
  cumulative count: `agent_bus.BusClient.send()` (centralized),
  `mesh_bus.MeshNode.send()` (conflict_based), and the shared pool server's
  accepted-post handler (rejected out-of-turn posts don't count, since they
  never reached anyone). HMAS-1 and HMAS-2 reuse the centralized `BusClient`
  tagged `architecture="HMAS-1"` / `"HMAS-2"`.
- Reporting is one-way UDP, fire-and-forget: nothing breaks if the monitor
  isn't running, and cumulative (not delta) snapshots make it robust to any
  dropped packet -- you just miss one intermediate update, never drift.
- Run standalone: `python -m agentpackage.monitor [--host HOST] [--port PORT]`
  (default `127.0.0.1:9900`, `AGENT_MONITOR_HOST` / `AGENT_MONITOR_PORT`) and
  `python -m agentpackage.trace_monitor` (default port `9901`,
  `AGENT_TRACE_MONITOR_PORT`).

## Shared building blocks

- `agentpackage/mcpserver.py` — FastMCP server exposing remroc ROS 2 tools
  over SSE: `list_worlds`, `get_map_info` (reads repo `worlds/`),
  `list_robots`, `get_robot_pose` / `get_all_robot_poses`,
  `distance_to_station`, `rank_stations_by_distance`, `get_peer_distances` (after nav failure),
  `drive_distance` (open-loop cmd_vel), `get_laser_snapshot`, `navigate_to_pose`,
  plus station/box inventory and whiteboard/events.
- `agentpackage/mcp_client.py` — loads those MCP tools into LangChain agents.
- `agentpackage/BaseAgents.py` — thin wrapper around
  `langchain.agents.create_agent` with a per-agent checkpointer, blocking
  `invoke()`, and a simple REPL (`run_persistent_chat`).
- `agentpackage/config.py` — model name, `SmallDeliveryRobot_*` fleet ids, and the
  deterministic peer/port tables used by the conflict-based mesh.

## Setup

```bash
cd agent_project
python -m venv .venv && source .venv/bin/activate
pip install -e .
export OPENAI_API_KEY=sk-...        # never commit this; use a local .env
```

`OPENAI_API_KEY` (or your provider's key) must come from the environment or
a local, git-ignored `.env` file — see `agentpackage/__init__.py`.

## Running each architecture

Each architecture has its own `launch_*.sh` script that starts the shared
MCP server plus every agent process in its own terminal (falls back to
printing the commands if no graphical terminal is available):

```bash
# Centralized: 1 broker + N robot workers + 1 master
./agentpackage/architectures/centralized/launch_centralized.sh --agents 4

# Conflict-based: N solo peers + mission CLI; negotiate only on events
./agentpackage/architectures/conflict_based/launch_conflict_based.sh --agents 4

# HMAS-1: 1 broker + N robots + 1 planner; central initial plan then
# turn-based robot dialogue until EXECUTE.
./agentpackage/architectures/hmas1/launch_hmas1.sh --agents 4

# HMAS-2: 1 broker + N local reviewers + 1 planner; plan → AGREE/DISAGREE
# feedback loop → execute (no mesh).
./agentpackage/architectures/hmas2/launch_hmas2.sh --agents 4

# Shared pool: 1 pool server + N pool agents + 1 human CLI, no addressing
./agentpackage/architectures/shared_pool/launch_shared_pool.sh --agents 4
```

Fleet size `N` is `AGENT_COUNT` (also `AGENTS=N` or `--agents N`), allowed
values **2, 4, 6, 8** (default 4). Leaders/planners are extra where used.

Without ROS 2 running, the robot-control MCP tools simply come back empty
and the agents fall back to their peer/delegation tools only — useful for
testing the coordination logic in isolation (see each architecture's tests
below).

## Testing the coordination logic without an LLM or ROS 2

The transports (`agent_bus.py`, `mesh_bus.py`) are plain
Python/socket code with no LLM dependency, so they can be exercised
directly, e.g.:

```python
from agentpackage.architectures.conflict_based.mesh_bus import MeshNode

peers = {"a": ("127.0.0.1", 19101), "b": ("127.0.0.1", 19102)}
a = MeshNode("a", *peers["a"], {"b": peers["b"]})
b = MeshNode("b", *peers["b"], {"a": peers["a"]})
b.on_message(lambda msg: b.reply(msg, f"echo: {msg['text']}") if msg.get("type") == "agent_request" else None)
print(a.ask("b", "ping"))  # -> "echo: ping"
```

## ROS 2 bring-up

`launch_tb3_stack.sh` and `commands.txt` bring up the Gazebo/Nav2 world and
set each robot's initial AMCL pose; they are independent of which agent
architecture you run afterwards.

Custom arena scenarios (pillars removed, optional box obstacles) live in
`../maps/scenarios/`. Launch one with:

```bash
SCENARIO=open ./launch_tb3_stack.sh      # walls only
SCENARIO=boxes_a ./launch_tb3_stack.sh   # sparse boxes
SCENARIO=boxes_b ./launch_tb3_stack.sh   # denser boxes
SCENARIO=bottleneck ./launch_tb3_stack.sh # thick center divider, one-robot gap
SCENARIO=stations ./launch_tb3_stack.sh  # visit A→B→C→D around the perimeter
SCENARIO=cross ./launch_tb3_stack.sh     # four quadrants + center crossing
SCENARIO=rooms ./launch_tb3_stack.sh     # four corner rooms + central hall
```

Omit `SCENARIO` to use the stock TurtleBot3 world/map. After editing box
poses in a scenario `world.sdf`, refresh its Nav2 map with:

```bash
python3 ../maps/tools/stamp_obstacles_on_map.py \
  --base-pgm ../maps/_base/walls_only.pgm \
  --world ../maps/scenarios/<name>/world.sdf \
  --out-dir ../maps/scenarios/<name>
```

Or regenerate the built-in layouts:

```bash
python3 ../maps/tools/generate_scenarios.py
```
