"""AgentNet as a DMAS variant: decentralized turn-taking on a peer-to-peer mesh.

There is no central planner. The robots agree on a short chunk of actions,
execute it, and meet again. The mission ends only when every robot says
FINISHED in the same discussion round.
"""
