# Shared message pool architecture (blackboard)

```
                     ┌─────────────────────┐
        user ───────►│                     │◄─────────── SmallDeliveryRobot_0 (discuss turn 1)
                      │   shared message    │
   SmallDeliveryRobot_3 ────────►│        pool         │◄─────────── SmallDeliveryRobot_1 (discuss turn 2)
      (discuss turn 4)│  (append-only log,   │
        SmallDeliveryRobot_2 ───►│  broadcast to all)  │
      (discuss turn 3)│                     │
                      └─────────────────────┘

   Phases: discuss (turn-based until all AGREE)
        → execute (ALL agents wake together, work in parallel)
        → DONE ends round
   start_discussion_round reopens discuss.
```

- **`message_pool.py`**: TCP blackboard + **phase machine**:
  - **discuss**: turn order enforced; agents post `AGREE:` / `DISAGREE:`; when every
    turn-taker’s latest post AGREEs → switch to **execute**.
  - **execute**: server sends `execute` to **all** agents at once (no turn order);
    agents may post status freely; **DONE** ends the round.
  - **`start_discussion`** control (agent tool) reopens discuss anytime.
  - A human post always restarts discuss from the front of the turn order.
- **`pool_agent.py`**: `post_to_pool`, `read_pool`, `start_discussion_round`,
  plus MCP. Discuss uses turn prompts; execute uses a shared wake signal.
- **`pool_cli.py`**: human entry point; prints phase / turn / parallel execute / timings.

Run: `./launch_shared_pool.sh` (optional `--agents 2|4|6|8`)
