# Experiment 3 — Cross (delivery + shared crossing constraint)

**Map / world:** `cross` (`worlds/items/cross.json`)  
**What to test:** task allocation plus a hard shared-resource rule (max 2 robots in the center crossing).  
**Success:** all packages delivered; never more than two robots in the crossing at once; opposing traffic resolved.

Landmarks: `station_nw`, `station_ne`, `station_se`, `station_sw`, `crossing`.

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

From pptx notes:

| Level | Robots | Packages |
|---|---|---|
| **Easy** | 2 | 2 |
| **Medium** | 4 | 4 |
| **Hard** | 6 | 6 |

## Prompts

**CLI-ready (one line each, no line breaks):** [prompts/03_cross.txt](prompts/03_cross.txt)

### Easy (2 robots / 2 packages)

```text
You are on the cross map (world: cross).
Deliver package P1 from NW to SE, and package P2 from NE to SW.
Allocate among SmallDeliveryRobot_0 and SmallDeliveryRobot_1.
No more than two robots may occupy the center crossing at the same time.
Look up stations via tools. Report when all deliveries are done.
```

### Medium (4 robots / 4 packages)

Pptx prompt (Cross slide):

```text
You are on the cross map (world: cross).
Deliver all packages to their assigned destination stations. Coordinate task allocation and passage through the central bottleneck. No more than two robots may occupy the bottleneck at the same time. Avoid opposing traffic conflicts and complete all deliveries as quickly as possible.

Assignments:
- P1: NW → SE
- P2: NE → SW
- P3: SE → NW
- P4: SW → NE

Use station/box tools for coordinates. Report when all deliveries are done.
```

### Hard (6 robots / 6 packages)

```text
You are on the cross map (world: cross).
Deliver these packages with at most two robots in the center crossing at once:
- P1: NW → SE
- P2: NE → SW
- P3: SE → NW
- P4: SW → NE
- P5: NW → NE
- P6: SW → SE
Allocate among SmallDeliveryRobot_0..SmallDeliveryRobot_5. Prefer parallel work; resolve crossing conflicts.
Report when all deliveries are done.
```

## What to give each architecture

Same pattern as Stations: prompt only at the architecture entry point; start exactly N robots.

## Extra checks

- Was the max-2-in-crossing rule violated (trace / sim observation)?
- Did opposing deliveries (e.g. NW↔SE and NE↔SW) get sequenced or detoured?
