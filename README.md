# Masterarbeit

This repository extends the multi-robot agent system built during the
[Studienarbeit](https://github.com/LukasDiHSU/Studienarbeit) with a
comparison of three multi-agent coordination architectures, implemented
side by side on top of the same ROS 2 / MCP tooling and the same LLM agent
base class:

| Architecture | Location | Topology | Who can delegate |
|---|---|---|---|
| Centralized | `agent_project/agentpackage/architectures/centralized` | Star (hub-and-spoke) via a central broker | Only the master agent |
| Decentralized | `agent_project/agentpackage/architectures/decentralized` | Full peer-to-peer mesh | Every robot agent (symmetric peers) |
| Hybrid | `agent_project/agentpackage/architectures/hybrid` | Starts as a star, switches to a mesh after the first planning round | Planner first, then every agent |
| Shared pool | `agent_project/agentpackage/architectures/shared_pool` | Shared broadcast log (blackboard), turn-enforced; no addressing | Nobody delegates -- every agent (and the user) reads/writes the same pool, but only the agent whose turn it is may post; a full round of "end" votes stops the conversation |

See [`agent_project/README.md`](agent_project/README.md) for setup and how
to run each architecture, and the `README.md` inside each architecture
folder for the design rationale behind that specific variant.
