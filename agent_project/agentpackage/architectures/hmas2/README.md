# HMAS-2 architecture (central plan → local feedback → execute)

Paper mapping (Chen et al.): HMAS-2 is the hybrid variant of **CMAS**. A
central LLM proposes the fleet plan; each robot has a local LLM that checks
its assigned action and returns **AGREE** / **DISAGREE**. On disagreement the
central agent re-plans. Only after consensus does the planner send
**EXECUTE** instructions. Robots never talk to each other — traffic stays on
the centralized star broker.

```
                         ┌──────────────┐
                  user ► │   planner    │
                         └──────┬───────┘
                collect_feedback │ / ask_* (broker)
              ┌─────┬─────┴─────┬─────┐
              ▼     ▼           ▼     ▼
            SDR_0 SDR_1       SDR_2  SDR_3
           (review AGREE/DISAGREE, then execute)
```

- **Central planner** (`planner_agent.py`): drafts one fleet plan, calls
  `collect_feedback` (sends a `PLAN REVIEW REQUEST`), revises until
  `all_agree`, then sends execute messages with `ask_robot` /
  `ask_all_robots` / `ask_selected_robots`.
- **Local robots** (`robot_agent.py`): on review, prefer zero tools (at most
  `rank_stations_by_distance` once) and reply `AGREE:` / `DISAGREE:`; on
  execute, navigate/hold with minimal tools. Event tools (`get_events`) are
  for conflict-based only and are not exposed here. No peer messaging.
- **Transport**: reuses `architectures/centralized/agent_bus.py` + broker.

Contrast with **HMAS-1** (central multi-step plan; robots AGREE or DISAGREE on exceptions)
and plain **centralized** / CMAS (central assigns with no local review loop).

Run: `./launch_hmas2.sh` (optional `--agents 2|4|6|8`)
