# Experiment 1 — Stations (package delivery)

**Map / world:** `stations` (`worlds/items/stations.json`, remroc stations world)  
**What to test:** task allocation, multi-leg pick→deliver, routing around a blocked center (A→B→C→D perimeter).  
**Success:** every listed package is grabbed at source and placed at destination; no unresolved conflicts.

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

From pptx notes (Stations = package delivery):

| Level | Robots | Orders |
|---|---|---|
| **Easy** | 2 (`SmallDeliveryRobot_0`, `_1`) | 2 |
| **Medium** | 4 (`_0`…`_3`) | 4 |
| **Hard** | 6 (`_0`…`_5`) | 6–8 |

## Initial MCP station / box state (set manually)

`drop_box` requires an **empty** destination. Before each run, set
`_DEFAULT_STATIONS` in [`agent_project/agentpackage/mcpserver.py`](../../agent_project/agentpackage/mcpserver.py)
(or call `reset_stations` after editing defaults) to match the difficulty:

| Station | Easy | Medium / Hard |
|---|---|---|
| **A** | `box_1`, available | `box_1`, available |
| **B** | `box_2`, available | `box_2`, available |
| **C** | empty (`box_id=null`, `available=false`) | `box_3`, available |
| **D** | empty | `box_4`, available |

- **Easy:** matches P1 A→C and P2 B→D (sources occupied, destinations free).
- **Medium / Hard:** all four pads start occupied (swaps / later legs). For opposing swaps, pick both ends before dropping so pads are free. Hard’s later orders reuse the same four boxes after earlier drops.

**Rule (all difficulties):** each station holds at most one box — never drop onto an occupied pad; check `get_station` / `list_stations` first.

## Prompts

Paste as the **single** user message at the architecture entry point.

**CLI-ready (one line each, no line breaks):** [prompts/01_stations.txt](prompts/01_stations.txt)

### Easy (2 robots / 2 packages)

```text
You are on the stations map (world: stations).
Deliver package P1 from station A to station C, and package P2 from station B to station D.
Allocate the two orders among SmallDeliveryRobot_0 and SmallDeliveryRobot_1 and complete both deliveries.
Use the station/box tools to look up coordinates. Coordinate so robots do not block each other.
Report when all deliveries are done.
```

### Medium (4 robots / 4 packages)

Pptx prompt (Stations slide):

```text
You are on the stations map (world: stations).
Deliver package P1 from station A to C, package P2 from B to D, package P3 from C to A, and package P4 from D to B.
Allocate the orders among the robots and complete all deliveries.
Each station holds only one box — destinations must be empty before drop; for swaps, pick both sources (or stage) so pads are free before dropping.
Use the station/box tools to look up coordinates. Avoid opposing traffic on the same corridor when possible.
Report when all deliveries are done.
```

### Hard (6 robots / 6–8 packages)

```text
You are on the stations map (world: stations).
Deliver these packages:
- P1: A → C
- P2: B → D
- P3: C → A
- P4: D → B
- P5: A → B
- P6: C → D
- P7: B → A
- P8: D → C
Allocate all orders among SmallDeliveryRobot_0..SmallDeliveryRobot_5. Prefer parallel deliveries; resolve corridor conflicts.
Each station holds only one box — never drop onto an occupied pad; for swaps clear destinations first.
Look up station coordinates via tools. Report when all deliveries are done.
```

## What to give each architecture

| | Easy | Medium | Hard |
|---|---|---|---|
| **Centralized** | Prompt → master. Workers: `_0`, `_1` | Prompt → master. Workers: `_0`…`_3` | Prompt → master. Workers: `_0`…`_5` |
| **Conflict-based** | Prompt → CLI / one peer; fleet `_0`…`_1` | Prompt → CLI; fleet `_0`…`_3` | Prompt → CLI; fleet `_0`…`_5` |
| **HMAS-1 / HMAS-2** | Prompt → planner | same | same |
| **Shared pool** | Prompt → pool CLI; turn order `_0` → `_1` | turn order `_0`…`_3` | turn order over all 6 |

## Extra checks

- Did allocation actually happen (not one robot doing everything)?
- Were station coords fetched via tools (not hallucinated)?
- For HMAS-1: did dialogue reach EXECUTE before navigation finished?
- For HMAS-2: did all involved robots AGREE before EXECUTE?
