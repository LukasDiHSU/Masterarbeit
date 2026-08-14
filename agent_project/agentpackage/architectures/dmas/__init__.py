"""DMAS: decentralized multi-agent system with round-based consensus.

Every robot has its own LLM and there is no central planner. A mission runs in
rounds: the robots discuss in turn order until they all agree on a plan that
gives each of them one short leg, they carry those legs out in parallel, and
the resulting world state opens the next round. The mission ends when every
robot answers FINISHED.

Deliberately small legs (at most one drive plus at most one pick or drop) keep
the fleet re-synchronised: nobody runs far ahead on a plan the others have
already outgrown.
"""
