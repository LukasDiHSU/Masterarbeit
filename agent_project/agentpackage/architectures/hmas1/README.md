# HMAS-1 architecture (central plan → robot vote → MCP execute)

Paper mapping (Chen et al., arXiv:2309.15943, Fig. 3b): HMAS-1 is the hybrid
variant of **DMAS**. A central LLM proposes a **short natural-language plan**
(one leg per robot). The robot agents then talk in **fixed turn order**. They
**follow that plan** (`AGREE`) unless they see an exception, in which case they
vote **`DISAGREE`** and may send a corrected `PLAN`. The round runs once every
robot has agreed; each robot then carries out **its own leg with MCP tools**
(same as DMAS). The resulting state opens the next round.

```
   ┌──────────────────────── planning round n ───────────────────────┐
   │  read world state ─► central planner proposes one leg / robot   │
   │        │                                                        │
   │        ▼   SmallDeliveryRobot_0 → _1 → _2 → …  (turn taking)    │
   │  AGREE (follow) or DISAGREE / corrected PLAN on exceptions      │
   │        │                                                        │
   │        ▼  each robot runs ITS leg via MCP (in parallel)         │
   └──────────────────────────► planning round n+1 ──────────────────┘
```

- **Planner** (`planner_agent.py`): owns the outer loop (`HMAS1Session`) and
  the central LLM that proposes one plan per round (map inspection tools only).
- **Robots** (`robot_agent.py`): discussion LLM during voting (read-only map
  tools), executor LLM afterwards (`navigate_to_pose` / `pickup_box` / …).
- **Helpers** (`dialogue.py`): participant resolution, PLAN parser (DMAS-style
  `Name: <leg>` lines), prompt layout.
- **Transport**: `architectures/centralized/agent_bus.py` (star broker only).

Legs are ordinary language, e.g. `drive through the gap to the east side`.
Station ids and coordinates must come from the world snapshot or map tools —
never from a `move_to`/`pick` DSL.

A mission fails when the dialogue finds no agreed plan within
`PAPER_MAX_DIALOGUE_ROUNDS` (3), or when `PAPER_MAX_PLAN_STEPS` (12) rounds
are reached.

Contrast with **HMAS-2** (central plan → parallel feedback → re-plan) and plain
**conflict_based** (event-gated mesh peers, no central primer).

Run: `./launch_hmas1.sh` (optional `--agents 2|4|6|8`)
