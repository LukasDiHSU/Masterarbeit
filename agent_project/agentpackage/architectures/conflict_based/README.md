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

- **Solo by default.** Each `robot_peer_agent` works alone with MCP tools.
  Peer talk is `negotiate_with` on conflict events only (whiteboard = storage).
- **Before ending:** `report_done_and_confirm(summary)` tells every peer what
  this robot did and collects AGREE/DISAGREE on whether the fleet mission is
  finished. The agent may claim done only if `all_agree` is true.
- **Event gate.** A background poller reads the MCP event log. Tool failures
  (nav abort, box_missing, station_occupied, drive_failed, …) emit events.
  When an event’s participants include this peer — including a
  `blocking_robot` on nav failure — negotiation unlocks **only** toward that
  subset via `negotiate_with` / `end_negotiation`.
- **Mesh transport** (`mesh_bus.py`) stays as the point-to-point channel for
  those gated negotiations — not as a always-on chat fabric.
- **`mission_cli.py`**: mesh name `CLI`. Paste a prompt (no command prefix) —
  it is sent to **all** robots in parallel as solo missions. They negotiate
  on conflict events and must `report_done_and_confirm` before finishing.

## Token / communication hypothesis

Event-triggered coordination should use fewer inter-agent messages and LLM
tokens than round-based shared-pool deliberation when conflicts are sparse,
while still negotiating when bottlenecks / missing boxes involve a subset.

Run: `./launch_conflict_based.sh` (optional `--agents 2|4|6|8`)
