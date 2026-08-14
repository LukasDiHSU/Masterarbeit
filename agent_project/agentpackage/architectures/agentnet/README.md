# AgentNet architecture (DMAS variant)

Paper mapping (Chen et al., arXiv:2309.15943, Fig. 3a): fully decentralized
multi-robot planning. There is **no central planner**. The robots take turns
until they agree on the next few actions, execute that chunk, and meet again
with the new state.

AgentNet here is that DMAS loop on the existing peer-to-peer mesh (not the
Yang et al. router/executor network).

```
   user ──► SmallDeliveryRobot_0 (chairs turn-taking only)
                     │
              discuss in order  _0 → _1 → … → _0 → …
                     │
              first valid EXECUTE chunk
                     │
              each robot runs its next 1..K actions
                     │
              meet again ──► until every robot says FINISHED
```

- **`protocol.py`**: prompts, chunk size (`AGENTNET_CHUNK_STEPS`, default 2),
  mission wrapping.
- **`session.py`**: the loop — discuss, verify, dispatch, reconvene.
- **`net_agent.py`**: one mesh node. Dialogue uses the LLM; agreed actions run
  as MCP primitives with no extra LLM call.
- **`task_cli.py`**: human entry. The mission always goes to
  `SmallDeliveryRobot_0`; the CLI waits until the fleet reports back.
- **Transport**: `MeshNode` from `../conflict_based/mesh_bus.py`.

Answer formats, strictly parsed:

```
EXECUTE
SmallDeliveryRobot_0: move_to(station_A); pick(station_A)
SmallDeliveryRobot_1: move_to(station_C); wait()
```

```
FINISHED
```

A chunk is rejected unless every robot has a line, every action is in the
available list at that step (later actions are checked against the state the
earlier ones would leave), two robots do not share a station in the same
step, and the first step is not all `wait()`. After a rejected EXECUTE the
same speaker gets the reason and one retry.

The mission ends only when **every** robot replies `FINISHED` in the same
discussion round. One robot saying it is not enough.

Agent 0 only chairs the speaking order. It does not propose a privileged
plan; it speaks in the same turn as everyone else.

Run: `./launch_agentnet.sh` (optional `--agents 2|4|6|8`)
