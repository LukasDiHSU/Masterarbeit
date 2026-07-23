"""Hybrid architecture: centralized planning phase, then a decentralized
execution phase.

Phase 1 (centralized): a :class:`PlannerAgent` collects the user's goal from
a human, drafts a plan, and delegates the first round of tasks to the robot
agents through a central broker -- exactly like the ``centralized``
architecture.

Phase 2 (decentralized): once the planner has issued its first instructions,
it calls ``activate_decentralized_phase`` which broadcasts a peer table to
every robot (and to itself). From that point on, every agent -- including
the planner -- opens direct mesh links and can talk to any other agent
without going back through the broker. The broker keeps running so late
joiners / control messages still work, but it is no longer required for
robot-to-robot coordination.
"""
