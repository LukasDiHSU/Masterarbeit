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
| HMAS-1 | `agent_project/agentpackage/architectures/hmas1` | Central full mission plan ending with FINISHED; per-STEP AGREE; else PMAS peers | Planner proposes once; robots vote each STEP; AGREE on FINISHED ends; DISAGREE/new PLAN discards it and they plan as equals |
| HMAS-2 | `agent_project/agentpackage/architectures/hmas2` | Star with local review: central plan → AGREE/DISAGREE → re-plan until consensus → execute | Only the central planner |
| AgentNet | `agent_project/agentpackage/architectures/agentnet` | Mesh without a planner; robots take turns, execute a short chunk, meet again | Nobody delegates a whole mission — they agree on the next few actions together. Agent 0 only chairs the speaking order. Ends when every robot says FINISHED |

See [`agent_project/README.md`](agent_project/README.md) for setup and how
to run each architecture, and the `README.md` inside each architecture
folder for the design rationale behind that specific variant.

Fleet size is configurable via `AGENT_COUNT` / `--agents` (**2, 4, 6, or 8**
working robots; default 4). Architectures that have a master/planner add
that leader on top of N workers.
