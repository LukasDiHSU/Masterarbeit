# agentpackage — three multi-agent robot fleet architectures

This project controls a fleet of four TurtleBot3 robots (`tb1`..`tb4`) over
ROS 2 / Nav2, exposed to LLM agents through an MCP tool server
(`agentpackage/mcpserver.py`). On top of that shared, architecture-agnostic
tool layer, three different multi-agent coordination strategies are
implemented so they can be compared directly (see `agentpackage/architectures/`):

1. **Centralized** — a single master agent is the only one that may
   delegate; every message is routed through one central broker (star
   topology).
2. **Decentralized** — there is no master and no broker; every robot agent
   is a symmetric peer that can talk directly to any other peer (full mesh).
3. **Hybrid** — starts centralized (a planner collects the goal, drafts a
   plan, and gives the first round of instructions through the broker),
   then hands out a peer address table so every agent — including the
   former planner — opens direct mesh links and keeps collaborating
   decentrally for the rest of the mission.
4. **Shared pool (blackboard, turn-based)** — decentralized with no
   addressing. Every agent and the human user connect to one shared
   broadcast log; new joiners are replayed the full history, and every post
   goes to everyone. The pool itself enforces a fixed speaking order
   (`robot_tb1 -> robot_tb2 -> robot_tb3 -> robot_tb4 -> repeat`); a user
   message always restarts the round at the front, and the round ends once
   every agent has voted to stop in a row.

## Usage monitor: token usage + message counts

Every `launch_*.sh` script now also opens a **"Usage Monitor"** terminal
(`python -m agentpackage.monitor`) showing a live-updating table across
every running agent, instead of interleaving usage lines into each agent's
own conversation:

```
=== Agent usage monitor === (Ctrl+C to quit)

AGENT            ARCHITECTURE     MESSAGES  LLM CALLS    IN TOK   OUT TOK  TOTAL TOK
--------------------------------------------------------------------------------------
master           centralized             4          3       412       128        540
robot_tb1        centralized             2          2       201        64        265
robot_tb2        centralized             2          2       198        60        258
...
--------------------------------------------------------------------------------------
TOTAL            5 agent(s)             12          9      1024       340       1364
```

How it works:
- `agentpackage/BaseAgents.py` tags every agent with its architecture and
  reports its **cumulative** token usage (`usage_metadata` summed across
  every LLM call, including intermediate tool-calling round-trips within a
  single request) after every `invoke()`.
- Each transport also counts **inter-agent messages** and reports its
  cumulative count: `agent_bus.BusClient.send()` (centralized),
  `mesh_bus.MeshNode.send()` (decentralized), and the shared pool server's
  accepted-post handler (rejected out-of-turn posts don't count, since they
  never reached anyone). The hybrid architecture's `PhasedBus` reuses both
  of these directly, tagged `architecture="hybrid"`.
- Reporting is one-way UDP, fire-and-forget: nothing breaks if the monitor
  isn't running, and cumulative (not delta) snapshots make it robust to any
  dropped packet -- you just miss one intermediate update, never drift.
- Run it standalone anywhere with `python -m agentpackage.monitor
  [--host HOST] [--port PORT]` (default `127.0.0.1:9900`, `AGENT_MONITOR_HOST`
  / `AGENT_MONITOR_PORT` env vars) -- e.g. point every architecture's agents
  at the same monitor instance for one dashboard across an entire session.

## Shared building blocks

- `agentpackage/mcpserver.py` — FastMCP server exposing ROS 2 tools
  (robot poses, Nav2 goals, item/whiteboard state) over SSE.
- `agentpackage/mcp_client.py` — loads those MCP tools into LangChain agents.
- `agentpackage/BaseAgents.py` — thin wrapper around
  `langchain.agents.create_agent` with a per-agent checkpointer, blocking
  `invoke()`, and a simple REPL (`run_persistent_chat`).
- `agentpackage/config.py` — model name, `tb*` ↔ fleet-id mapping, and the
  deterministic peer/port tables used by the decentralized and hybrid mesh.

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
# Centralized: 1 broker + 4 robot workers + 1 master
./agentpackage/architectures/centralized/launch_centralized.sh

# Decentralized: 4 symmetric peers, no broker, no master
./agentpackage/architectures/decentralized/launch_decentralized.sh

# Hybrid: 1 broker + 4 robots + 1 planner; starts centralized, then
# call `activate_decentralized_phase` (a tool the planner calls itself)
# to switch every agent onto a direct peer mesh.
./agentpackage/architectures/hybrid/launch_hybrid.sh

# Shared pool: 1 pool server + 4 pool agents + 1 human CLI, no addressing
./agentpackage/architectures/shared_pool/launch_shared_pool.sh
```

Without ROS 2 running, the robot-control MCP tools simply come back empty
and the agents fall back to their peer/delegation tools only — useful for
testing the coordination logic in isolation (see each architecture's tests
below).

## Testing the coordination logic without an LLM or ROS 2

The transports (`agent_bus.py`, `mesh_bus.py`, `phase_bus.py`) are plain
Python/socket code with no LLM dependency, so they can be exercised
directly, e.g.:

```python
from agentpackage.architectures.decentralized.mesh_bus import MeshNode

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
