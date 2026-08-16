# agentpackage — multi-agent Q1 specialists

This project runs **three specialist agents on one Q1 robot**
(`navigator`, `lidar`, `camera`) over ROS 2 / ATB
waypoint nav, exposed through an MCP tool server (`agentpackage/mcpserver.py`).
The same five coordination architectures are compared on that split:

1. **Centralized** — a single master is the only one that may delegate.
2. **Conflict-based** — specialists work their own sensors/drive alone; mesh
   negotiation opens only on events (`detection_conflict`, `nav_aborted`).
3. **HMAS-1** — central planner proposes one natural-language leg per specialist;
   they AGREE or DISAGREE, then execute.
4. **HMAS-2** — planner collects AGREE/DISAGREE, then SEND EXECUTE.
5. **AgentNet (DMAS on a mesh)** — no planner. Specialists take turns until they
   agree on the next few actions, execute, meet again. The mission enters at
   `navigator`. It ends when every specialist says `FINISHED` in the same round.

Protocol: **sense → get closer → sense**. Object xyz come only from semantic
lidar when Q1 is near that object. The camera agent gets the RGB frame as a
picture. `get_occupancy_map` is walls/free space only.

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
navigator                centralized             2          2       201        64        265
lidar                    centralized             2          2       198        60        258
camera                   centralized             2          2       188        55        243
--------------------------------------------------------------------------------------
TOTAL            4 agent(s)             10          9      1024       340       1364
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
  cumulative count: `agent_bus.BusClient.send()` (centralized) and
  `mesh_bus.MeshNode.send()` (conflict_based and agentnet, tagged by the
  architecture that owns the node). HMAS-1 and HMAS-2 reuse the centralized
  `BusClient` tagged `architecture="HMAS-1"` / `"HMAS-2"`.
- Reporting is one-way UDP, fire-and-forget: nothing breaks if the monitor
  isn't running, and cumulative (not delta) snapshots make it robust to any
  dropped packet -- you just miss one intermediate update, never drift.
- Run standalone: `python -m agentpackage.monitor [--host HOST] [--port PORT]`
  (default `127.0.0.1:9900`, `AGENT_MONITOR_HOST` / `AGENT_MONITOR_PORT`) and
  `python -m agentpackage.trace_monitor` (default port `9901`,
  `AGENT_TRACE_MONITOR_PORT`).

## Saving experiment terminal logs

Every `launch_*.sh` tees each opened terminal into
`../tasks/experiments/runs/_active/<session>/logs/`. When a trial ends:

```bash
cd agent_project
./save_experiment.sh open_easy_hmas2_r1 --stop --note "optional note"
```

This copies the session to `tasks/experiments/runs/<run_name>/` (Agent Trace,
Usage Monitor, specialists, planner/master, MCP, …). Use `--stop` to signal the
recorded terminal shells.

## Shared building blocks

- `agentpackage/mcpserver.py` — FastMCP server: `get_robot_pose`,
  `get_occupancy_map` (yaml + walls-only grid), `navigate_to_pose`
  (ATB waypoints), `rotate_by` / `drive_forward` / `drive_distance` (cmd_vel), `get_lidar_snapshot`,
  `get_semantic_lidar_objects`, `get_camera_image` (RGB still),
  `get_semantic_camera_image`, `get_semantic_camera_classes`, plus conflict events.
- `agentpackage/mcp_client.py` — loads those MCP tools into LangChain agents
  (Q1 planners get `get_occupancy_map` only — no planted object coords).
- `agentpackage/BaseAgents.py` — thin wrapper around
  `langchain.agents.create_agent` with a per-agent checkpointer, blocking
  `invoke()`, and a simple REPL (`run_persistent_chat`).
- `agentpackage/config.py` — model name, specialist ids
  (`navigator`, `lidar`, `camera`), and mesh ports.
- `agentpackage/roles.py` — per-specialist MCP allow-list.

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
# Gazebo + Q1 + ATB nav + sensor summarizer
./launch.sh

# Centralized: 1 broker + 3 specialists + 1 master
./agentpackage/architectures/centralized/launch_centralized.sh --agents 3

# Conflict-based: 3 specialists + mission CLI; negotiate only on events
./agentpackage/architectures/conflict_based/launch_conflict_based.sh --agents 3

# HMAS-1: 1 broker + 3 specialists + 1 planner
./agentpackage/architectures/hmas1/launch_hmas1.sh --agents 3

# HMAS-2: 1 broker + 3 specialists + 1 planner
./agentpackage/architectures/hmas2/launch_hmas2.sh --agents 3

# AgentNet: 3 DMAS nodes + task CLI; mission enters at navigator
./agentpackage/architectures/agentnet/launch_agentnet.sh --agents 3
```

Defaults are `AGENT_PLATFORM=q1` and `AGENT_WORLD=open`. Specialists are always
the three roles above. Protocol: sense → get closer → sense. Object xyz come
only from `get_semantic_lidar_objects` when Q1 is close; the camera agent looks
at `get_camera_image`; `get_occupancy_map` is walls and free space (no object outlines).

Without ROS 2 running, the robot-control MCP tools simply come back empty
and the agents fall back to their peer/delegation tools only — useful for
testing the coordination logic in isolation.

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

```bash
cd agent_project
./launch.sh
```

That starts Gazebo (`world:=open`), ATB waypoint navigation, TF, and the
Q1 sensor summarizer. Then start an architecture `launch_*.sh` as above.
