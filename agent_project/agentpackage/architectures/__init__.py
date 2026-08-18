"""Five interchangeable multi-agent coordination architectures.

- ``centralized``: a star topology. A single master agent is the only agent
  with delegation authority; every message between robots is routed through
  a central broker.
- ``conflict_based``: peers work alone by default on a mesh; negotiation
  opens only when MCP events (e.g. ``conflict``, ``box_missing``) involve
  them, and only toward the event's participant subset.
- ``hmas1`` (HMAS-1): a central planner proposes a full multi-step mission
  plan once (last STEP is FINISHED); each robot votes AGREE or DISAGREE
  once on that whole plan. Unanimous AGREE executes the STEPs in order.
  DISAGREE or a different PLAN discards it and the fleet continues as DMAS
  peers. Star broker only.
- ``hmas2`` (HMAS-2): centralized planner proposes a fleet plan; each robot
  has a local LLM that returns AGREE/DISAGREE feedback; the planner re-plans
  until consensus, then sends execute instructions (still over the star broker).
- ``dmas``: decentralized turn-taking on a mesh, no planner. The mission CLI
  chairs speaking order, the robots agree on one short leg each, execute in
  parallel, and meet again. The mission ends when every robot says FINISHED.
- ``agentnet``: DMAS-style chunk planning on a mesh. Agent 0 chairs speaking
  order; the fleet agrees on a short action chunk, executes it, and reconvenes
  until every robot says FINISHED in the same round.
"""
