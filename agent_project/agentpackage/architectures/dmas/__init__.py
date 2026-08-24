"""DMAS: decentralized multi-agent system with round-based consensus.

Every robot has its own LLM and there is no central planner. A mission runs in
rounds: the first turns are a spoken huddle (no PLAN/AGREE), then someone
puts a PLAN on the table and the others AGREE. Agreed legs run in parallel,
and the new world state opens the next round. The mission ends when every
robot answers FINISHED.

Deliberately small legs (at most one drive plus at most one pick or drop) keep
the fleet re-synchronised: nobody runs far ahead on a plan the others have
already outgrown.
"""
