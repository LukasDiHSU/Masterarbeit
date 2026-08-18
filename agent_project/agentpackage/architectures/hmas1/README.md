# HMAS-1 architecture (full central plan → per-STEP vote → FINISH, else PMAS)

Paper mapping (Chen et al., arXiv:2309.15943, Fig. 3b): HMAS-1 is the hybrid
variant of **DMAS**. A central LLM proposes a **full natural-language mission
plan** (ordered `STEP` blocks, e.g. pick, then drop). The **last STEP is
always `FINISHED`**. The robots do **not** ratify that plan as a whole. They
vote in **fixed turn order on the next STEP only**. `AGREE` from everyone
(and no rewritten plan) executes a work STEP in parallel via MCP, or **ends
the mission** if that STEP is `FINISHED`. `DISAGREE` or a **different `PLAN`**
**discards the original mission plan**; the fleet then continues as a **peer
(PMAS) network** on the same star broker (the planner no longer proposes).

```
   central planner → full mission (STEP 1 .. k, FINISHED)     [once]
        │
        ▼  vote on STEP i only  (_0 → _1 → …)
   all AGREE, work STEP ────────► execute STEP i ──► vote on STEP i+1
   all AGREE, FINISHED STEP ────► mission ends
        │
        DISAGREE / new PLAN
        ▼
   original plan dropped ──► PMAS: peers PLAN/AGREE/FINISHED until done
```

- **Planner** (`planner_agent.py`): proposes the original mission once (map
  tools only), appending `FINISHED` if the model omitted it. After a rejected
  STEP it stays silent; `HMAS1Session` chairs PMAS turn-taking.
- **Robots** (`robot_agent.py`): discussion LLM for votes / PMAS talk
  (read-only map tools); executor LLM per dispatched work STEP.
- **Helpers** (`dialogue.py`): multi-step PLAN parser, per-STEP vote prompts.
- **Transport**: `architectures/centralized/agent_bus.py` (star broker only).
  PMAS fallback is the DMAS protocol over that star, not a second mesh.

A work `STEP` leg is ordinary language. Inside one STEP a leg is at most one
drive plus at most one pick or drop.

Limits: `PAPER_MAX_DIALOGUE_ROUNDS` (3) vote passes per STEP,
`PAPER_MAX_PLAN_STEPS` (12) executed work STEPs, `PAPER_MAX_SYNTAX_RETRIES` (3).
Hitting a limit before FINISH falls through to PMAS. Hitting a PMAS limit
ends the mission as a failure.

Run: `./launch_hmas1.sh` (optional `--agents 2|4|6|8`)
