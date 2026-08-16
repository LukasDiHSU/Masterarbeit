# Per-run checklist

Copy for each trial.

```text
[ ] Architecture: centralized | conflict_based | HMAS-1 | HMAS-2 | agentnet
[ ] Scenario: semantic_tour (open / Q1)
[ ] Difficulty: easy | medium | hard
[ ] Specialists online: navigator, lidar, camera
[ ] AGENT_PLATFORM=q1  AGENT_WORLD=open
[ ] Prompt variant pasted exactly once at the architecture entry point
[ ] Fresh processes
[ ] Usage Monitor running (:9900)
[ ] Agent Trace running (:9901)
[ ] Start timestamp recorded
[ ] Success? yes / no / timeout
[ ] End timestamp
[ ] Tokens in/out/total
[ ] LLM calls
[ ] Inter-agent messages
[ ] Logs saved with save_experiment.sh <run_name> [--stop]
[ ] Notes (empty sensors, sensed centroid, camera/lidar disagreement, …)
```

## Saving a run

```bash
cd agent_project
./save_experiment.sh open_easy_centralized_r1 --stop --note "optional note"
```

## Suggested execution order

1. **Easy, centralized** — smoke-test
2. **Easy** on conflict_based, HMAS-1, HMAS-2, agentnet
3. **Medium**, then **Hard**
