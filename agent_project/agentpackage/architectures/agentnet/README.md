# AgentNet architecture (DMAS variant)

Paper mapping (Chen et al., arXiv:2309.15943, Fig. 3a): fully decentralized
multi-robot planning. There is **no central planner**. The robots take turns
until they agree on the next few actions, execute that chunk, and meet again
with the new state.

AgentNet here is that DMAS loop on the existing peer-to-peer mesh (not the
Yang et al. router/executor network).

```
   user ──► navigator (chairs turn-taking only)
                     │
              discuss in order  navigator → lidar → …
                     │
              first valid EXECUTE chunk
                     │
              each specialist runs its next 1..K actions
                     │
              meet again ──► until every specialist says FINISHED
```

- **`protocol.py`**: prompts, chunk size (`AGENTNET_CHUNK_STEPS`, default 2),
  mission wrapping.
- **`session.py`**: the loop — discuss, verify, dispatch, reconvene.
- **`net_agent.py`**: one mesh node. Dialogue uses the LLM; agreed actions run
  as MCP primitives with no extra LLM call.
- **`task_cli.py`**: human entry. The mission always goes to
  `navigator`; the CLI waits until the specialists report back.
- **Transport**: `MeshNode` from `../conflict_based/mesh_bus.py`.

Answer formats, strictly parsed:

```
EXECUTE
navigator: navigate(<centroid_x>,<centroid_y>)
lidar: sense()
camera: sense()
```

```
FINISHED
```

A chunk is rejected unless every specialist has a line, every action is in the
available list at that step, and the first step is not all `wait()`. After a
rejected EXECUTE the same speaker gets the reason and one retry.

The mission ends only when **every** specialist replies `FINISHED` in the same
discussion round. One specialist saying it is not enough.

`navigator` only chairs the speaking order. It does not propose a privileged
plan; it speaks in the same turn as everyone else.

Run: `./launch_agentnet.sh`
