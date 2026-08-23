# Experiment 3 — Cross (delivery + shared crossing constraint)

**Map / world:** `cross` (`worlds/items/cross.json`)  
**What to test:** task allocation plus a hard shared-resource rule (max 2 robots in the center crossing).  
**Success:** all packages delivered; never more than two robots in the crossing at once; opposing traffic resolved.

Landmarks: `station_A` (SW), `station_B` (NW), `station_C` (NE), `station_D` (SE), `crossing`. Same A–D layout as Stations.

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

From pptx notes:

| Level | Robots | Packages |
|---|---|---|
| **Easy** | 2 | 2 |
| **Medium** | 4 | 4 |
| **Hard** | 8 | 6 |

## Initial MCP station / box state

`_DEFAULT_STATIONS` in [`agent_project/agentpackage/mcpserver.py`](../../agent_project/agentpackage/mcpserver.py)
is currently set for **Cross Hard**. Package `Pn` is `box_n`. Pads may hold several boxes; `drop_box` appends.

| Station | Easy | Medium | Hard (current MCP default) |
|---|---|---|---|
| **A** | `box_1` | `box_1` | `box_1` (P1), `box_5` (P5) |
| **B** | `box_2` | `box_2` | `box_2` (P2) |
| **C** | empty | `box_3` | `box_3` (P3), `box_6` (P6) |
| **D** | empty | `box_4` | `box_4` (P4) |

- **Easy:** P1 A→C and P2 B→D (sources occupied, destinations free).
- **Medium:** four pads, one box each (P1–P4). Edit `_DEFAULT_STATIONS` before that run.
- **Hard:** sources already hold every package. A has P1 and P5; C has P3 and P6. Destinations may receive a second box without picking first. When a pad lists more than one box, `pickup_box` needs `box_id` (`box_1` or `P1`).

Leave `crossing` empty — it is not a drop pad.

**Rule:** stations may hold several boxes. Robots hold at most one. No more than two robots in the center crossing at once.

## Prompts

Paste as the **single** user message at the architecture entry point.

**CLI-ready (one line each, no line breaks):** [prompts/03_cross.txt](prompts/03_cross.txt)

### Easy (2 robots / 2 packages)

```text
You are on the cross map (world: cross).
Deliver package P1 from station A to station C, and package P2 from station B to station D.
Allocate the orders among the robots and complete all deliveries.
Each station holds only one box — drop only on empty pads.
No more than two robots may occupy the center crossing at the same time.
Report when all deliveries are done.
```

### Medium (4 robots / 4 packages)

```text
You are on the cross map (world: cross_1).
Deliver package P1 from station A to C, package P2 from B to D, package P3 from C to A, and package P4 from D to B.
Allocate the orders among the robots and complete all deliveries.
Each station holds only one box — destinations must be empty before drop.
No more than two robots may occupy the center crossing at the same time.
Report when all deliveries are done.
```

### Hard (8 robots / 6 packages)

```text
You are on the cross map (world: cross_1) with eight robots (SmallDeliveryRobot_0..SmallDeliveryRobot_7).
Deliver these packages (package Pn is box_n):
- P1: A → C
- P2: B → D
- P3: C → A
- P4: D → B
- P5: A → B
- P6: C → D
A starts with box_1 and box_5, B with box_2, C with box_3 and box_6, D with box_4.
Allocate the orders among the robots and complete all deliveries.
Stations may hold several boxes — drop_box is allowed on a pad that already has boxes.
When a station lists more than one box, pass box_id to pickup_box (box_1 or P1).
No more than two robots may occupy the center crossing at the same time.
Report when all deliveries are done.
```

## What to give each architecture

Same pattern as Stations: prompt only at the architecture entry point; start exactly N robots.

## Extra checks

- Was the max-2-in-crossing rule violated (trace / sim observation)?
- Did opposing deliveries (e.g. A↔C and B↔D) get sequenced or detoured?
