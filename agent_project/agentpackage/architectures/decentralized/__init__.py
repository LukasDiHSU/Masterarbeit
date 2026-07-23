"""Decentralized (peer-to-peer mesh) multi-agent architecture.

There is no broker and no master. Every robot agent is a :class:`MeshNode`
peer: it listens for direct connections from other peers, dials out to peers
it needs to talk to, and exposes the exact same delegation tools
(``ask_peer`` / ``ask_all_peers``) to every agent. Any peer can be the entry
point a human talks to; from there, coordination happens directly between
robots without any single point of control or failure.
"""
