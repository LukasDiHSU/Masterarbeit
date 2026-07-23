# Shared message pool architecture (blackboard, turn-based)

```
                     ┌─────────────────────┐
        user ───────►│                     │◄─────────── robot_tb1 (turn 1)
                      │   shared message    │
   robot_tb4 ────────►│        pool         │◄─────────── robot_tb2 (turn 2)
      (turn 4)        │  (append-only log,   │
        robot_tb3 ───►│  broadcast to all,   │
      (turn 3)        │  turn enforced)      │
                      └─────────────────────┘

   Every post is broadcast to EVERY connection, including the sender.
   New joiners are replayed the full history first. There is still no
   "to" field anywhere -- but the SERVER enforces a fixed speaking order,
   so only one agent may post at a time.
```

- **`message_pool.py`**: a single small TCP server holding one in-memory,
  append-only log (`_log`, monotonically increasing `seq`). Every connected
  client — human or agent — receives every message ever posted: the server
  replays the full backlog to a new connection, then streams every new post
  live. On top of that broadcast layer, the server enforces a fixed
  round-robin speaking order (`config.POOL_TURN_ORDER`, default
  `robot_tb1 -> robot_tb2 -> robot_tb3 -> robot_tb4 -> repeat`):
  - A post from an agent whose turn it is **not** is rejected (never added
    to the log) with a private `error` reply — this is enforced by the
    server itself, not just by prompting, so two agents can never both
    believe it's their turn.
  - After each accepted turn-taker post, the server broadcasts a `turn`
    event naming who goes next.
  - A post from outside the turn order (i.e. the human, via `pool_cli.py`)
    is always accepted immediately and **restarts** the round from the
    front.
  - Each turn-taker post can carry `end_vote: true`. If every agent in the
    order votes `true` **in a row**, the server broadcasts a `round_end`
    event and nobody speaks again until the user starts a new round.
- **`pool_agent.py`**: a robot agent with the usual local robot-control
  tools plus `post_to_pool(message, agree_to_end=False)` and
  `read_pool(limit)`. It does nothing until the pool tells it (`on_turn`)
  that it's its turn; it is then shown the full conversation so far and
  asked to contribute exactly once. If the model forgets to call the tool,
  a safety net posts its final answer on its behalf so the round can never
  stall. Its system prompt tells it to set `agree_to_end=True` only when it
  genuinely thinks the whole group should stop.
- **`pool_cli.py`**: a plain, LLM-free console for the human — the direct
  "user writes to the pool" entry point. Anything you type restarts the
  round at the front of the speaking order; it also prints every message,
  whose turn it currently is, and when a round ends by consensus.

## Design trade-off worth noting for the thesis

Enforcing a strict turn order trades away some of the openness of a pure
free-for-all blackboard (where any agent may react to any message at any
time) for determinism: exactly one agent ever speaks at a time, in a known
order, so a round is easy to reason about and to log for analysis. The cost
is that a single slow or stuck agent turn blocks the whole round — the
per-turn safety-net post is a deliberate mitigation for that, not a
complete solution (there's still no hard timeout in this implementation if
you want to add one for the thesis). The speaking order itself is
configurable (`message_pool.py --turn-order robot_tb2,robot_tb1,...` or
`config.POOL_TURN_ORDER`) in case you want to compare different orderings.

Run: `./launch_shared_pool.sh`
