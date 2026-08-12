# Per-run checklist

Copy for each trial.

```text
[ ] Architecture: centralized | conflict_based | HMAS-1 | HMAS-2 | shared_pool
[ ] Scenario: stations | bottleneck | cross | boxes_a | boxes_b
[ ] Difficulty: easy | medium | hard
[ ] AGENT_COUNT / robots online: ________
[ ] Prompt variant pasted exactly once at the architecture entry point
[ ] Fresh processes / reset stations if needed
[ ] Usage Monitor running (:9900)
[ ] Agent Trace running (:9901)
[ ] Start timestamp recorded
[ ] Success? yes / no / timeout
[ ] End timestamp
[ ] Tokens in/out/total
[ ] LLM calls
[ ] Inter-agent messages
[ ] Logs saved with save_experiment.sh <run_name> [--stop]
[ ] Notes (deadlock, priority break, >2 in crossing, no handoff, over-sensing, …)
```

## Saving a run

While an architecture launch is active, every terminal is tee'd under
`tasks/experiments/runs/_active/<session>/logs/`.

When the trial ends (success, failure, or timeout):

```bash
cd agent_project
./save_experiment.sh stations_easy_hmas2_r1 --stop --note "P1/P2 delivered; one nav abort"
```

That copies the session into `tasks/experiments/runs/<run_name>/` (Agent Trace,
Usage Monitor, robots, planner/master, MCP, …) and optionally signals the
terminal shells to stop.

## Suggested execution order

1. **Boxes_a Easy** (1–2 robots) — sanity-check MCP + Nav2 + each architecture boots
2. **Stations Easy → Medium** — core allocation comparison
3. **Bottleneck Easy → Medium** — coordination / priority
4. **Cross Medium** — allocation + hard constraint
5. **Boxes_b Medium** — harder nav under same agent stack
6. **Hard cells** — with `AGENT_COUNT=6`
7. **Extensions** — see [extensions.md](extensions.md)
