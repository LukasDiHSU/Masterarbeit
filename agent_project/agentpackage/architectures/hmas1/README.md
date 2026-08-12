# HMAS-1 architecture (central prime → AGREE discussion → execute)

Paper mapping (Chen et al.): HMAS-1 is the hybrid variant of **DMAS**. A
central LLM proposes an **initial plan** that primes discussion; robots then
speak in **fixed turn order**. Each turn's prompt includes the initial plan
plus every prior comment. Discussion continues until **every** participant's
latest message starts with **AGREE**. Then execute actions are dispatched.
Robots (or the planner) may call **`start_discussion_round`** to open another
discuss→AGREE→execute cycle. Robots do not free-form mesh-chat.

```
        ┌──────────┐
 user ► │ planner  │  propose_and_discuss(initial plan)
        └────┬─────┘
             │ primes, then orchestrates turns (broker)
     SmallDeliveryRobot_0 → _1 → _2 → …  until all AGREE
             │
             ▼
      dispatch EXECUTE APPROVED to each participant
             │
             ▼  (optional) start_discussion_round / NEED_DISCUSSION
      another discuss → AGREE → execute cycle
```

- **Planner** (`planner_agent.py`): drafts **one** initial plan and calls
  `propose_and_discuss` once. For replans: `start_discussion_round`. Also
  handles robot `START_DISCUSSION` bus requests.
- **Robots** (`robot_agent.py`): discuss with `AGREE:` / `DISAGREE:`; on
  `EXECUTE APPROVED`, run MCP tools; may call `start_discussion_round` or
  end a reply with `NEED_DISCUSSION:`.
- **Helpers** (`dialogue.py`): participant resolution, turn prompts, AGREE
  consensus, execute parse.
- **Transport**: `architectures/centralized/agent_bus.py` (star broker only).

Contrast with **HMAS-2** (central plan → parallel AGREE/DISAGREE → re-plan)
and plain **conflict_based** (event-gated mesh peers, no central primer).

Run: `./launch_hmas1.sh` (optional `--agents 2|4|6|8`)
