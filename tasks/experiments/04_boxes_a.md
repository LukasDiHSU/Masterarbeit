# Experiment 4 — Boxes_a (NL → structured motion / pathfinding)

**Map / world:** `boxes_a` (`worlds/items/boxes_a.json`)  
**What to test (pptx notes):** natural language → structured commands; few-shot movement planning; pathfinding among sparse obstacles.  
**Success:** robots reach the named landmarks / complete the stated collection task without getting stuck.

Landmarks: `north cache`, `south cache`, `east cache`, `collection area`.

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

| Level | Robots | Focus |
|---|---|---|
| **Easy** | 1–2 | single path / simple collect |
| **Medium** | 4 | allocate caches → collection |
| **Hard** | 6 | parallel collects + avoid clutter |

## Prompts

**CLI-ready (one line each, no line breaks):** [prompts/04_boxes_a.txt](prompts/04_boxes_a.txt)

### Easy

```text
You are on the boxes_a map (world: boxes_a).
Send SmallDeliveryRobot_0 to the north cache, then to the collection area.
Use tools to look up landmark coordinates and navigate. Report when done.
```

### Medium

```text
You are on the boxes_a map (world: boxes_a).
There are three caches (north, south, east) and one collection area.
Allocate SmallDeliveryRobot_0..SmallDeliveryRobot_3 so that each cache is visited and material is brought to the collection area (or robots rendezvous there after visiting their cache).
Look up landmarks via tools. Avoid collisions. Report when the task is complete.
```

### Hard

```text
You are on the boxes_a map (world: boxes_a).
With six robots, cover north/south/east caches and bring everything to the collection area as quickly as possible.
Allocate work, navigate with tools, resolve conflicts. Report when complete.
```

## Extra checks

- Did agents look up landmark coordinates instead of inventing them?
- Pathfinding quality on a sparsely obstructed map (compare to Boxes_b).
