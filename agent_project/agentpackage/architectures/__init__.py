"""Five interchangeable multi-agent coordination architectures.

- ``centralized``: a star topology. A single master agent is the only agent
  with delegation authority; every message between robots is routed through
  a central broker.
- ``conflict_based``: peers work alone by default on a mesh; negotiation
  opens only when MCP events (e.g. ``conflict``, ``box_missing``) involve
  them, and only toward the event's participant subset.
- ``hmas1`` (HMAS-1): a central planner primes an initial plan, then robots
  discuss in turn order (DMAS-style); dialogue ends on EXECUTE. Star broker only.
- ``hmas2`` (HMAS-2): centralized planner proposes a fleet plan; each robot
  has a local LLM that returns AGREE/DISAGREE feedback; the planner re-plans
  until consensus, then sends execute instructions (still over the star broker).
- ``shared_pool``: decentralized via a shared, turn-based blackboard. There
  is no addressing -- every agent and the user post to and read from the
  same broadcast log -- but the pool enforces a fixed speaking order and
  ends the round as soon as any agent posts ``DONE``.
"""
