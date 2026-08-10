"""Turn-based DMAS-style dialogue helpers for HMAS-1 (paper protocol).

Central planner primes with an initial plan; robots then speak in fixed
order. Each turn's prompt includes the initial plan plus every prior
comment. Dialogue ends when a robot's reply starts with ``EXECUTE``.
"""

from __future__ import annotations

import re
from typing import Any

from ...config import TB_IDS, resolve_robot_id, robot_peer_name

DEFAULT_MAX_ROUNDS = 3

_EXECUTE_RE = re.compile(r"^\s*EXECUTE\b", re.IGNORECASE | re.MULTILINE)


def resolve_participants(recipients: str) -> list[str] | dict[str, Any]:
    raw = recipients.strip()
    if raw.lower() in {"all", "*", "everyone", "fleet"}:
        return [robot_peer_name(rid) for rid in TB_IDS]

    tokens = [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]
    peers: list[str] = []
    unknown: list[str] = []
    for tok in tokens:
        rid = resolve_robot_id(tok)
        if rid is None:
            unknown.append(tok)
            continue
        name = robot_peer_name(rid)
        if name not in peers:
            peers.append(name)
    if unknown or not peers:
        return {
            "error": "invalid_recipients",
            "message": (
                "Use 'all' or a comma-separated list like "
                "SmallDeliveryRobot_0 or SmallDeliveryRobot_0,SmallDeliveryRobot_1."
            ),
            "unknown": unknown,
            "valid": list(TB_IDS),
        }
    # Keep fleet order, not input order — paper turn-taking is ordered.
    order = [robot_peer_name(rid) for rid in TB_IDS]
    return [p for p in order if p in peers]


def looks_like_execute(text: str) -> bool:
    return bool(_EXECUTE_RE.search(text or ""))


def format_dialogue_history(turns: list[dict[str, str]]) -> str:
    if not turns:
        return "(no robot comments yet — you speak first after the initial plan)"
    lines: list[str] = []
    for i, turn in enumerate(turns, start=1):
        lines.append(f"[Turn {i} — {turn['speaker']}]\n{turn['text'].strip()}")
    return "\n\n".join(lines)


def build_turn_prompt(
    *,
    initial_plan: str,
    participants: list[str],
    history: list[dict[str, str]],
    speaker: str,
    round_idx: int,
    max_rounds: int,
) -> str:
    order = " -> ".join(participants)
    example_lines = "\n".join(f"  {p}: ..." for p in participants[: min(2, len(participants))])
    return (
        "HMAS-1 TURN-BASED DIALOGUE (after the planner's single priming plan).\n"
        f"Participants (speak in this order): {order}\n"
        f"Round {round_idx}/{max_rounds}. It is now YOUR turn ({speaker}).\n"
        "\n"
        "The central planner already sent ONE initial plan and will not confirm or "
        "re-plan mid-dialogue. Treat the plan below as the only primer:\n"
        f"{initial_plan.strip()}\n"
        "\n"
        "Dialogue so far (oldest first; prior robot comments only):\n"
        f"{format_dialogue_history(history)}\n"
        "\n"
        "YOUR TURN:\n"
        "- Briefly discuss / refine the plan from your robot's perspective "
        "(use MCP tools if you need local state). Keep it as short but precise as possible.\n"
        "- OR, if the fleet should act now, reply starting with EXECUTE on its own "
        "line, then one action line per participant, e.g.:\n"
        "  EXECUTE\n"
        f"{example_lines}\n"
        "Do not execute MCP navigation/pickup yet during discussion — only after "
        "EXECUTE has been decided and you later receive an execute instruction.\n"
    )


def parse_execute_actions(text: str, participants: list[str]) -> dict[str, str]:
    """Best-effort map peer name -> action line from an EXECUTE reply."""
    actions: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or _EXECUTE_RE.match(stripped):
            continue
        for peer in participants:
            prefixes = (f"{peer}:", f"{peer} -", f"{peer}—", f"{peer} –")
            lower = stripped.lower()
            for pref in prefixes:
                if lower.startswith(pref.lower()):
                    actions[peer] = stripped[len(pref) :].strip()
                    break
            else:
                continue
            break
        # also accept bare id without trailing punctuation variants already covered
        for tb in TB_IDS:
            peer = robot_peer_name(tb)
            if peer in actions:
                continue
            for pref in (f"{tb}:", f"{tb} -"):
                if stripped.lower().startswith(pref):
                    actions[peer] = stripped[len(pref) :].strip()
                    break
    return actions
