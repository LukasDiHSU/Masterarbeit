# Experiment 6 — Open-arena semantic tour (Q1)

**Map / world:** `open` ([worlds/extra_maps/open/world.sdf](../../worlds/extra_maps/open/world.sdf), [worlds/items/open.json](../../worlds/items/open.json))  
**Robot:** one Q1 (HSU / ATB-EDU sensors + ATB waypoint nav).  
**What to test:** three **sensor specialists** on one robot complete an ordered GOOSE-class tour. Compare all five architectures at a **fixed** size of 3 — no 2/4/6 sweep.  
**Success:** Q1 comes within **2.5 m** of a correctly classed planted object at each stop, **in order**.

Specialists (always these three; `AGENT_PLATFORM=q1`):

| Name | Role |
|---|---|
| `navigator` | Drive Q1 (`get_occupancy_map`, `navigate_to_pose`); AgentNet chair |
| `lidar` | Range from `/q1_velodyne/points` plus 360° class centroids from `/q1_velodyne_semantic/points` |
| `camera` | Forward vision: RGB still (`get_camera_image`), semantic color still, GOOSE class histogram. No xyz |

Agents **sense → get closer → sense**. Semantic lidar is 360° (Velodyne) but only
detects an object when Q1 is **close** to it. Camera has no xyz and only sees
what is in front — the camera agent must **look at** `get_camera_image`. Drive
to a tour class only with a centroid from `get_semantic_lidar_objects`. Do not
invent object coordinates.

`get_occupancy_map` loads the world’s `map.yaml` + `.pgm` as walls and free
space. **Objects are not drawn on it.** If the next tour class is missing,
pick a free cell (`.`) away from already-seen objects, drive there, and sense
again. Stopping after one missed scan is a failed run. You may drive to another
currently reported centroid only as a viewpoint change, not as a tour stop.

Legal coordinates (agent-facing):

| Source | Tool | Use |
|---|---|---|
| Occupancy | `get_occupancy_map` | Arena walls / free space — **not** object locations |
| Drive result | `navigate_to_pose` reply | Last arrived pose if present; else assume spawn `(0, 0)` |
| Sensed object | `get_semantic_lidar_objects` | Map-frame centroid when close — **only** object x/y |

See [00_common.md](00_common.md) for entry points, metrics, and protocol.

## Difficulty

Same three specialists every time. Tour length changes:

| Level | Stops (in order) | Classes |
|---|---|---|
| **Easy** | barrel → rock | 60, 40 |
| **Medium** | rock → barrel → car | 40, 60, 12 |
| **Hard** | rock → barrel → car → container | 40, 60, 12, 58 |

**Scoring-only** planted poses (map frame; for humans / `score_semantic_tour.py`, not in the agent prompt): rock `(10.5, -10.5)`, barrel `(-10.5, 10.5)`, car `(10.5, 10.5)`, container `(-10.5, -10.5)`. Q1 spawn `(0, 0)`. Arena is ~32 m across so the Q1 lattice planner can turn.

## Bring-up

```bash
# Terminal A — Gazebo open world + Q1 + ATB nav + summarizer
cd agent_project
./launch.sh

# Terminal B — agents
AGENT_PLATFORM=q1 AGENT_WORLD=open ./agentpackage/architectures/centralized/launch_centralized.sh --agents 3
```

Paste the prompt at the architecture entry point (master / planner / mission CLI / task CLI). Task CLI / AgentNet enter at `navigator`.

Score after the run (not during):

```bash
python3 score_semantic_tour.py --tour barrel,rock
```

## Prompts

**CLI-ready (one line each):** [prompts/06_semantic_tour.txt](prompts/06_semantic_tour.txt)

Do **not** paste scoring landmark xyz into the user prompt.

Paste **only the mission**. Tool recipes live in each specialist’s system
instructions (`navigator` / `lidar` / `camera`), not in this prompt.

### Easy

```text
You are on the open arena (world: open) with one Q1 robot. Complete this tour in order: 1) nearest barrel, 2) nearest rock. Keep at least 4 m from an object when you stop in front of it. Keep working until both stops are visited. You can only sense when you are not moving. Report only after both stops are reached.
```

### Medium

```text
You are on the open arena (world: open) with one Q1 robot. Complete this tour in order: 1) nearest rock, 2) nearest barrel, 3) nearest car. Keep at least 4 m from an object when you stop in front of it. Keep working until all three stops are visited. A single far-away scan is not done. Report only after all three stops are reached.
```

### Hard

```text
You are on the open arena (world: open) with one Q1 robot. Complete this tour in order: 1) nearest rock, 2) nearest barrel, 3) nearest car, 4) nearest container. Keep at least 4 m from an object when you stop in front of it. Keep working until all four stops are visited. A single far-away scan is not done. Report only after all four stops are reached.
```

## Extra checks

- Do not invent object x/y. Explore with occupancy free cells; object goals only from close-range semantic lidar.
- Occupancy map has **no** object outlines. Treating `#` as objects is a protocol failure.
- Camera looks at the RGB still; class histogram is extra, not xyz.
- A run that stops after one far-away missed scan is a **failure**.
- Tour stops still in order once their centroids appear (visiting another detection first as a viewpoint is OK if the next class is not yet seen).
- Did camera and lidar agree on class (conflict_based should negotiate on `detection_conflict`)?
- Save: `./save_experiment.sh open_easy_centralized_r1 --stop`

Smoke test: **centralized Easy** first, then the other four architectures on Easy, then Medium / Hard.
