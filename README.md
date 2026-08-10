# Masterarbeit

This repository extends the multi-robot agent system built during the
[Studienarbeit](https://github.com/LukasDiHSU/Studienarbeit) with a
comparison of multi-agent coordination architectures, implemented
side by side on top of the same ROS 2 / MCP tooling and the same LLM agent
base class:

| Architecture | Location | Topology | Who can delegate |
|---|---|---|---|
| Centralized | `agent_project/agentpackage/architectures/centralized` | Star (hub-and-spoke) via a central broker | Only the master agent |
| Conflict-based | `agent_project/agentpackage/architectures/conflict_based` | Mesh peers solo by default; negotiate only on conflicts/events | Peers negotiate only when an event involves them |
| HMAS-1 | `agent_project/agentpackage/architectures/hmas1` | Central plan primes turn-based robot dialogue until EXECUTE | Planner primes; robots discuss in turns |
| HMAS-2 | `agent_project/agentpackage/architectures/hmas2` | Star with local review: central plan → AGREE/DISAGREE → re-plan until consensus → execute | Only the central planner |
| Shared pool | `agent_project/agentpackage/architectures/shared_pool` | Shared broadcast log (blackboard), turn-enforced; no addressing | Nobody delegates -- every agent (and the user) reads/writes the same pool, but only the agent whose turn it is may post; any agent saying DONE stops the conversation |

See [`agent_project/README.md`](agent_project/README.md) for setup and how
to run each architecture, and the `README.md` inside each architecture
folder for the design rationale behind that specific variant.
