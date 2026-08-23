# HMAS-1 architecture (full central plan → one vote → execute, else DMAS)

Paper mapping (Chen et al., arXiv:2309.15943, Fig. 3b): HMAS-1 is the hybrid
variant of **DMAS**. A central LLM proposes a **full natural-language mission
plan** (ordered `STEP` blocks, e.g. pick, then drop). The **last STEP is
always `FINISHED`**. Each robot votes **once** on that whole plan
(`AGREE` / `DISAGREE`) with **no debate**. Unanimous `AGREE` executes the
work STEPs in order. After each original work STEP, every executor that
actually moved reports `STEP_OK` or `STEP_FAILED`; any `STEP_FAILED` drops
the original plan. The `FINISHED` STEP then ends the mission if all checks
passed. `DISAGREE` or a **different `PLAN` discards the original mission
plan**; the fleet then continues as a **peer (DMAS) network** on the same
star broker (the planner no longer proposes).

```
   central planner → full mission (STEP 1 .. k, FINISHED)     [once]
        │
        ▼  one AGREE / DISAGREE each (in parallel, no huddle)
   all AGREE ──► execute STEP 1 → STEP_OK/FAILED check ──► … ──► FINISHED
        │                         │
        │                         any STEP_FAILED
        DISAGREE / new PLAN       │
        ▼                         ▼
   original plan dropped ──► DMAS: huddle, then PLAN/AGREE/FINISHED until done
```

- **Planner** (`planner_agent.py`): proposes the original mission once (map
  tools only), appending `FINISHED` if the model omitted it. After a rejected
  plan or a failed per-STEP executor check it stays silent; `HMAS1Session`
  chairs DMAS turn-taking.
- **Robots** (`robot_agent.py`): discussion LLM for the original vote / DMAS
  talk (read-only map tools); huddle LLM for spoken DMAS turns; executor LLM
  per dispatched work STEP and the following `STEP_OK` / `STEP_FAILED` check.
- **Helpers** (`dialogue.py`): multi-step PLAN parser, one-shot vote prompt.
- **Transport**: `architectures/centralized/agent_bus.py` (star broker only).
  DMAS fallback is the DMAS protocol over that star, not a second mesh.

A work `STEP` leg is ordinary language. Inside one STEP a leg is at most one
drive plus at most one pick or drop.

Limits: `PAPER_MAX_PLAN_STEPS` (12) executed work STEPs, `PAPER_MAX_SYNTAX_RETRIES`
(3). Hitting a limit before FINISH falls through to DMAS. Hitting a DMAS limit
ends the mission as a failure.

Run: `./launch_hmas1.sh` (optional `--agents 2|4|6|8`)
