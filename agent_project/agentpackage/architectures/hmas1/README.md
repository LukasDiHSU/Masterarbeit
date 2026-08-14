# HMAS-1 architecture (central multi-step plan → robot vote → EXECUTE)

Paper mapping (Chen et al., arXiv:2309.15943, Fig. 3b): HMAS-1 is the hybrid
variant of **DMAS**. A central LLM proposes a **short multi-step plan** (a
chunk of actions per robot). The robot agents then talk in **fixed turn
order** with all prior comments concatenated into the next prompt. They
**follow that plan** (`AGREE`) unless they see an exception, in which case
they vote **`DISAGREE`** and may send a corrected `EXECUTE`. The chunk runs
once every robot has agreed; a rules-based verifier checks it before anything
moves; the resulting state opens the next planning chunk.

```
   ┌──────────────────────── planning chunk n ───────────────────────┐
   │  read world state ─► central planner proposes a multi-step plan │
   │        │                                                        │
   │        ▼   SmallDeliveryRobot_0 → _1 → _2 → …  (turn taking)    │
   │  AGREE (follow) or DISAGREE / corrected EXECUTE on exceptions   │
   │  chunk runs when every robot has agreed and the verifier passes │
   │        │                                                        │
   │        ▼  each robot runs ITS next action (in parallel steps)   │
   │  a failed action stops the rest of the chunk and replans        │
   └──────────────────────────► planning chunk n+1 ──────────────────┘
```

- **Planner** (`planner_agent.py`): owns the deterministic outer loop
  (`HMAS1Session`) and the central LLM that proposes one chunk per iteration.
- **Robots** (`robot_agent.py`): LLM dialogue partners during planning
  (default: AGREE), deterministic executors afterwards (`EXECUTE_ACTION:
  <action>` runs MCP primitives without an LLM call).
- **Protocol** (`../../paper_protocol.py`): world state text, available-action
  list, EXECUTE parser (several actions joined with `;`), verifier that
  simulates later steps, primitives, state-action history.
- **Helpers** (`dialogue.py`): participant resolution and the prompt layout
  (task description, step history, current state, robot state & capability,
  agent specialized prompt, communication instruction, syntax feedback).
- **Transport**: `architectures/centralized/agent_bus.py` (star broker only).

Actions are symbolic and come from the active world:
`move_to(<station>)`, `pick(<station>)`, `drop(<station>)`, `wait()`.
`pick`/`drop` in a later step of the same chunk are legal if an earlier
`move_to` would put the robot there. Two robots must not target the same
station in the same step.

A mission fails when the dialogue finds no agreed chunk within
`PAPER_MAX_DIALOGUE_ROUNDS` (3), when a speaker fails the syntax check
`PAPER_MAX_SYNTAX_RETRIES` (3) times too often to finish a vote, or when
`PAPER_MAX_PLAN_STEPS` (12) chunks are reached. Chunk length is
`HMAS1_CHUNK_STEPS` (default 4).

Contrast with **HMAS-2** (central plan → parallel feedback → re-plan) and plain
**conflict_based** (event-gated mesh peers, no central primer).

Run: `./launch_hmas1.sh` (optional `--agents 2|4|6|8`)
