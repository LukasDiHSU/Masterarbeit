# Decentralized architecture (peer-to-peer mesh)

```
        robot_tb1 ───────────── robot_tb2
            │  \                 /   │
            │    \             /     │
            │      \         /       │
            │        \     /         │
            │          \ /           │
            │          / \           │
            │        /     \         │
            │      /         \       │
            │    /             \     │
            │  /                 \   │
        robot_tb4 ───────────── robot_tb3

   (every robot is a symmetric peer; a human can talk to any one of them)
```

- **No broker, no master.** `mesh_bus.py`'s `MeshNode` gives every agent its
  own listening socket plus outbound connections to every peer it needs.
  For each unordered pair of peers, exactly one physical TCP connection is
  opened — the peer whose name sorts first dials out, the other accepts —
  and both sides reuse that single connection for requests *and* replies in
  either direction. There is no third party in the loop and no single point
  of failure: if one robot goes offline, the remaining peers can still talk
  to each other.
- **`robot_peer_agent.py`**: every peer has the exact same local
  robot-control tools as the centralized worker, plus `ask_peer(name, msg)`
  and `ask_all_peers(msg)` — the same delegation power the centralized
  master alone had. Each peer's system prompt makes clear there is no
  coordinator: it must decide for itself when to act locally versus ask a
  neighbor.
- Any peer's terminal is a valid entry point for a human — coordination
  then happens directly between whichever robots are involved, without
  ever routing through a "central" agent.

Run: `./launch_decentralized.sh`
