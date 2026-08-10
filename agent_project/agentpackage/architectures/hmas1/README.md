# HMAS-1 architecture (central prime → turn-based dialogue → EXECUTE)

Paper mapping (Chen et al.): HMAS-1 is the hybrid variant of **DMAS**. A
central LLM proposes an **initial plan** that primes discussion; robots then
speak in **fixed turn order**. Each turn's prompt includes the initial plan
plus every prior comment. Dialogue ends when a robot replies with
**EXECUTE** and per-robot action lines. Those actions are then dispatched
for execution. Robots do not free-form mesh-chat.

```
        ┌──────────┐
 user ► │ planner  │  propose_and_discuss(initial plan)
        └────┬─────┘
             │ primes, then orchestrates turns (broker)
     tb1 → tb2 → tb3 → tb4 → tb1 → …  until EXECUTE
             │
             ▼
      dispatch EXECUTE actions to each participant
```

- **Planner** (`planner_agent.py`): drafts **one** initial plan and calls
  `propose_and_discuss` **once** (`participants='all'` or a single robot).
  No confirmations, no sequential per-robot plans. The tool then runs the
  robot turn loop and dispatches EXECUTE.
- **Robots** (`robot_agent.py`): know the planner only primed once; they
  discuss in turns or output EXECUTE; on `EXECUTE APPROVED`, run MCP tools.
- **Helpers** (`dialogue.py`): participant resolution, turn prompts, EXECUTE parse.
- **Transport**: `architectures/centralized/agent_bus.py` (star broker only).

Contrast with **HMAS-2** (central plan → parallel AGREE/DISAGREE → re-plan)
and plain **conflict_based** (event-gated mesh peers, no central primer).

Run: `./launch_hmas1.sh` (optional `--agents 2|4|6|8`)
