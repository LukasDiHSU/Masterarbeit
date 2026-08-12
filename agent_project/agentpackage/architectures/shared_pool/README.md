# Shared message pool architecture (blackboard, turn-based)

```
                     ┌─────────────────────┐
        user ───────►│                     │◄─────────── SmallDeliveryRobot_0 (turn 1)
                      │   shared message    │
   SmallDeliveryRobot_3 ────────►│        pool         │◄─────────── SmallDeliveryRobot_1 (turn 2)
      (turn 4)        │  (append-only log,   │
        SmallDeliveryRobot_2 ───►│  broadcast to all,   │
      (turn 3)        │  turn enforced)      │
                      └─────────────────────┘

   Phases: discuss (until all AGREE) → execute (MCP) → DONE ends round
   start_discussion_round reopens discuss.
```

- **`message_pool.py`**: TCP blackboard + turn order + **phase machine**:
  - **discuss**: agents post `AGREE:` / `DISAGREE:`; when every turn-taker’s
    latest post AGREEs → switch to **execute**.
  - **execute**: agents may act; **DONE** ends the round.
  - **`start_discussion`** control (agent tool) reopens discuss anytime.
  - A human post always restarts discuss from the front of the turn order.
- **`pool_agent.py`**: `post_to_pool`, `read_pool`, `start_discussion_round`,
  plus MCP. Turn prompts depend on `pool.phase`.
- **`pool_cli.py`**: human entry point; prints phase / turn / timings.

Run: `./launch_shared_pool.sh` (optional `--agents 2|4|6|8`)
