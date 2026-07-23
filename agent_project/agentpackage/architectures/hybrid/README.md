# Hybrid architecture (centralized planning → decentralized execution)

```
 Phase 1 (star, like `centralized`)      Phase 2 (mesh, like `decentralized`)
                                          ─────────────────────────────────
        ┌──────────┐                        robot_tb1 ──── robot_tb2
 user ► │ planner  │                            │   \   planner   /  │
        └────┬─────┘                            │     \   /  \   /   │
     ask_robot_tbN │ (broker)                    │       \/    \/     │
   ┌─────┬────┴────┬─────┐                       │       /\    /\     │
   ▼     ▼         ▼     ▼                       │     /   \  /   \   │
 tb1   tb2       tb3    tb4                   robot_tb4 ──── robot_tb3
   └─────┴────┬────┴─────┘
              ▼
        central broker           activate_decentralized_phase()
                                  broadcasts a peer table, then every
                                  agent (planner included) opens direct
                                  mesh links -- see `decentralized/mesh_bus.py`
```

- **Phase 1 — centralized.** `planner_agent.py` behaves exactly like the
  `centralized` architecture's master: it is the only agent the human talks
  to, and it delegates the first round of tasks to `robot_agent.py` workers
  through the same broker (`agentpackage/architectures/centralized/agent_bus.py`,
  reused as-is).
- **The handoff.** Once the planner has given its first instructions, it
  calls its own tool `activate_decentralized_phase()` exactly once. That
  broadcasts a static `name -> (host, port)` peer table to every robot (and
  to itself) over the broker, then boots a `MeshNode` for itself. Each
  robot, on receiving that control message, does the same
  (`phase_bus.py: PhasedBus.activate_mesh`).
- **Phase 2 — decentralized.** From that point on, `PhasedBus.ask()` and
  `.send()` automatically route to whichever peer is reachable over the
  mesh; the broker keeps running (so late messages / re-planning still
  work) but is no longer required for robot-to-robot coordination. Robot
  agents gain two additional tools, `ask_peer` and `ask_all_peers`, which
  only succeed once the mesh is active — before that they tell the LLM the
  decentralized phase hasn't started yet, so it keeps working locally.
- The planner itself becomes just another peer after the handoff: it can
  still be reached for re-planning or fleet-wide questions, but is no
  longer a mandatory hop for every message.

This models a common real-world pattern: a coordinator is useful to bootstrap
a shared plan and initial task assignment, but once every agent knows what
to do and who else is doing what, forcing every message through that
coordinator only adds latency and a single point of failure — so the fleet
switches to direct collaboration for execution.

Run: `./launch_hybrid.sh`
