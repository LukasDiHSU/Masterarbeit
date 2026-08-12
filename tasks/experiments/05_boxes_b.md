# Experiment 5 — Boxes_b (denser obstacles / corridors)

**Map / world:** `boxes_b` (`worlds/items/boxes_b.json`)  
**What to test:** same NL→motion / allocation task as Boxes_a, but denser corridors — harder navigation and tighter coordination.  
**Success:** same as Boxes_a on this map.

Landmarks: `NW cache`, `NE cache`, `SE cache`, `collection area`.

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

| Level | Robots | Focus |
|---|---|---|
| **Easy** | 1–2 | one corridor path |
| **Medium** | 4 | three caches → collection |
| **Hard** | 6 | parallel under congestion |

## Prompts

**CLI-ready (one line each, no line breaks):** [prompts/05_boxes_b.txt](prompts/05_boxes_b.txt)

### Easy

```text
You are on the boxes_b map (world: boxes_b).
Send SmallDeliveryRobot_0 to the NW cache, then to the collection area.
Use tools to look up landmark coordinates and navigate. Report when done.
```

### Medium

```text
You are on the boxes_b map (world: boxes_b).
There are three caches (NW, NE, SE) and one collection area.
Allocate SmallDeliveryRobot_0..SmallDeliveryRobot_3 so each cache is visited and robots finish at the collection area.
Look up landmarks via tools. Corridors are tight — avoid head-on conflicts. Report when complete.
```

### Hard

```text
You are on the boxes_b map (world: boxes_b).
With six robots, cover NW/NE/SE caches and gather at the collection area as quickly as possible.
Allocate, navigate, resolve corridor conflicts. Report when complete.
```

## Extra checks

- Compare TSR / time / tokens to Boxes_a Medium under the same architecture.
- More nav aborts / peer blocking expected — recovery via `drive_distance` after failed `navigate_to_pose` is allowed.
