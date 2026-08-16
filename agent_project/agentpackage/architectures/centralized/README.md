# Centralized architecture (star / hub-and-spoke)

```
                 ┌────────────┐
   user ───────► │   master   │
                 └─────┬──────┘
              ask_robot │  (only the master has delegation tools)
        ┌──────────┬────────┴──────────┐
        ▼          ▼                   ▼
   navigator     lidar              camera      (specialists only)
        │          │                   │
        └──────────┴────────┬──────────┘
                            ▼
                 central broker (agent_bus.py)
```

- **Broker (`agent_bus.py` / `agent_broker.py`)**: a single TCP process that
  every agent registers with by name. It forwards a message's `to` field to
  that agent's connection — agents never connect to each other directly.
- **`master_agent.py`**: the only agent with delegation tools
  (`ask_robot`, `ask_all_robots`,
  `ask_selected_robots_parallel`). It is the single point of coordination
  and the single point of failure: if the master or the broker goes down,
  the fleet cannot coordinate at all.
- **`robot_agent.py`**: purely reactive workers. They only ever answer
  `agent_request` messages that arrive from the broker and reply to
  whichever `from` sent them — they have no tools to talk to each other.

This is the architecture carried over unchanged (module names aside) from
the Studienarbeit; it is the baseline the `conflict_based` and `hmas1`
(HMAS-1) architectures build on.

Run: `./launch_centralized.sh` (Q1: 3 specialists; remroc: `--agents 2|4|6|8`)
