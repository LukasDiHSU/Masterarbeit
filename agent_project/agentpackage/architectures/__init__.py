"""Four interchangeable multi-agent coordination architectures.

- ``centralized``: a star topology. A single master agent is the only agent
  with delegation authority; every message between robots is routed through
  a central broker.
- ``decentralized``: a full peer-to-peer mesh. There is no master and no
  broker; every robot agent is a symmetric peer that can talk directly to
  any other peer.
- ``hybrid``: starts centralized (a planner agent collects the user's goal,
  drafts a plan, and makes the first delegation round through a central
  broker), then hands out a peer table so all robot agents open direct mesh
  links and continue collaborating decentrally for the rest of the task.
- ``shared_pool``: decentralized via a shared, turn-based blackboard. There
  is no addressing -- every agent and the user post to and read from the
  same broadcast log -- but the pool enforces a fixed speaking order and
  ends the round once every agent votes to stop.
"""
