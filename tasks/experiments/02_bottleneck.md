# Experiment 2 — Bottleneck (crossing / priority)

**Map / world:** `bottleneck` (`worlds/items/bottleneck.json`)  
**What to test:** crossing a narrow gap, priority rules, deadlock avoidance.  
**Success:** robots that must swap/cross reach their target sides; priorities respected; no permanent deadlock.

Landmarks: `station_west`, `station_east`, `gap`.

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
The two robots should exchange their positions by crossing the bottleneck (west ↔ east).
Use tools to look up west/east stations and the gap. Coordinate so you do not deadlock in the gap.
Report when both robots have reached the opposite side.
```

### Medium (4 robots / prioritize one side)

Pptx prompt (Bottleneck slide):

```text
You are on the bottleneck map (world: bottleneck).
The robots should exchange their positions by crossing the bottleneck.
SmallDeliveryRobot_0 and SmallDeliveryRobot_2 should be prioritized.
Use tools to look up west/east stations and the gap. Avoid deadlock in the gap.
Report when all robots have reached their target sides.
```

### Hard (6 robots / mixed priorities)

```text
You are on the bottleneck map (world: bottleneck).
Six robots must cross the bottleneck to exchange sides (west ↔ east).
Priorities: SmallDeliveryRobot_0 and SmallDeliveryRobot_2 highest; then _1 and _3; then _4 and _5.
Only as many robots as safely fit may be in the gap at once. Resolve conflicts; no permanent deadlock.
Report when all robots have reached their target sides.
```

## What to give each architecture

| | Easy | Medium | Hard |
|---|---|---|---|
| **Centralized** | Prompt → master | Prompt → master | Prompt → master |
| **Conflict-based** | Prompt → CLI / one peer | same | same |
| **HMAS-1 / HMAS-2** | Prompt → planner | same | same |
| **Shared pool** | Prompt → pool CLI | same | same |

## Extra checks

- Was the priority rule actually followed (not just stated)?
- Did robots deadlock in the gap?
- Did conflict-based open negotiation only when blocked / conflicted?
