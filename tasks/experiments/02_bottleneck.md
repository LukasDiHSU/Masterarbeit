# Experiment 2 — Bottleneck (crossing / priority)

**Map / world:** `bottleneck` (`worlds/items/bottleneck.json`)  
**What to test:** crossing a narrow gap, priority rules, deadlock avoidance.  
**Success:** each robot that must cross finishes at a **parking spot on the opposite side** (not on a spawn pose); priorities respected; no permanent deadlock.

Landmarks: `station_west`, `station_east`, `gap`, plus six parking goals
(`park_west_n` / `_c` / `_s` and `park_east_n` / `_c` / `_s`).

| Side | Spawn column (do not park here) | Parking (back wall) |
|---|---|---|
| **West** | `x=-5`, `y∈{-3,-1,1,3}` | `park_west_s/c/n` at `x=-6.8`, `y∈{-5.5,0,5.5}` — for robots that **started east** |
| **East** | `x=5`, `y∈{-3,-1,1,3}` | `park_east_s/c/n` at `x=6.8`, `y∈{-5.5,0,5.5}` — for robots that **started west** |

Worlds `bottleneck_1` / `bottleneck_2` use the same ids, scaled. One robot per parking spot. After the gap, approach along `y≈±5.5`, not along the spawn column.

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

From pptx notes:

| Level | Robots | Priority rule |
|---|---|---|
| **Easy** | 2 | none |
| **Medium** | 4 | prioritize one side / selected robots |
| **Hard** | 6 | different priorities per robot/group |

## Prompts

**CLI-ready (one line each, no line breaks):** [prompts/02_bottleneck.txt](prompts/02_bottleneck.txt)

### Easy (2 robots / no priority)

```text
You are on the bottleneck map (world: bottleneck).
The two robots should exchange sides by crossing the bottleneck gap once.
SmallDeliveryRobot_0 starts west and must finish at one east parking spot
(park_east_n, park_east_c, or park_east_s).
SmallDeliveryRobot_1 starts east and must finish at one west parking spot
(park_west_n, park_west_c, or park_west_s).
Look up xy with list_stations. Do not navigate onto original spawn poses.
One robot per parking spot. Only one robot in the gap at a time.
Report when both robots are parked on the opposite side.
```

### Medium (4 robots / prioritize one side)

Pptx prompt (Bottleneck slide):

```text
You are on the bottleneck map (world: bottleneck).
Robots on the west side must cross to east parking spots, and robots on the
east side must cross to west parking spots. Each robot crosses the gap once.
SmallDeliveryRobot_0 and SmallDeliveryRobot_1 start west;
SmallDeliveryRobot_2 and SmallDeliveryRobot_3 start east.
Assign distinct park_east_* / park_west_* goals from list_stations.
Do not drive onto original spawn poses. Only one robot in the gap at a time.
Report when all robots are parked on the opposite side.
```

### Hard (6 robots / mixed priorities)

```text
You are on the bottleneck map (world: bottleneck).
Six robots must cross the bottleneck once and park on the opposite side.
West parking: park_west_n, park_west_c, park_west_s (robots that started east).
East parking: park_east_n, park_east_c, park_east_s (robots that started west).
Assign one unique parking spot per robot via list_stations.
Do not navigate onto original spawn poses. Only one robot in the gap at a time.
Report when all robots are parked on their target side.
```

## What to give each architecture

| | Easy | Medium | Hard |
|---|---|---|---|
| **Centralized** | Prompt → master | Prompt → master | Prompt → master |
| **Conflict-based** | Prompt → CLI / one peer | same | same |
| **HMAS-1 / HMAS-2** | Prompt → planner | same | same |
| **AgentNet** | Prompt → task CLI | same | same |

## Extra checks

- Did robots park on `park_*` goals instead of spawn poses?
- Did robots deadlock in the gap?
- Did conflict-based open negotiation only when blocked / conflicted?
