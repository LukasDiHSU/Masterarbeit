# Conflict-based architecture

```
   R1 plan/move alone     R2 plan/move alone     R3 plan/move alone
            │                      │                      │
            └──────── only if event involves them ────────┘
                              │
                    CONFLICT / box_missing
                              │
                         R1 ↔ R2 negotiate
                              │
                         R3 stays silent
```

- **Solo by default.** Each `robot_peer_agent` works alone with MCP tools
  (`list_available_boxes`, `navigate_to_pose`, `pickup_box`, …). There is
  **no** `ask_all_peers` and no continuous discussion.
- **Event gate.** A background poller reads the MCP event log
  (`box_missing`, `nav_aborted`, `conflict`). When an event’s participants
  include this peer, negotiation unlocks **only** toward that subset via
  `negotiate_with` / `end_negotiation`.
- **Mesh transport** (`mesh_bus.py`) stays as the point-to-point channel for
  those gated negotiations — not as a always-on chat fabric.
- **`mission_cli.py`**: assign a solo mission to one robot
  (`mission SmallDeliveryRobot_0 …`) or inject a multi-robot conflict
  (`conflict SmallDeliveryRobot_0,SmallDeliveryRobot_1 bottleneck`). Replaces the old fleet-wide broadcast CLI.

## Token / communication hypothesis

Event-triggered coordination should use fewer inter-agent messages and LLM
tokens than round-based shared-pool deliberation when conflicts are sparse,
while still negotiating when bottlenecks / missing boxes involve a subset.

Run: `./launch_conflict_based.sh` (optional `--agents 2|4|6|8`)
