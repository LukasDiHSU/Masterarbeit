"""Turn-based DMAS-style dialogue helpers for HMAS-1 (paper protocol).

Central planner primes with an initial plan; robots then speak in fixed
order. Each turn's prompt includes the initial plan plus every prior
comment. Discussion continues until every participant's latest message
starts with ``AGREE``; then execute is dispatched. ``EXECUTE`` alone no
longer ends discussion early.
"""

from __future__ import annotations

import re
from typing import Any

from ...config import TB_IDS, resolve_robot_id, robot_peer_name

DEFAULT_MAX_ROUNDS = 5

_AGREE_RE = re.compile(r"^\s*AGREE\b", re.IGNORECASE | re.MULTILINE)
_DISAGREE_RE = re.compile(r"^\s*DISAGREE\b", re.IGNORECASE | re.MULTILINE)
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


def looks_like_agree(text: str) -> bool:
    return bool(_AGREE_RE.search(text or ""))


def looks_like_disagree(text: str) -> bool:
    return bool(_DISAGREE_RE.search(text or ""))


def looks_like_execute(text: str) -> bool:
    """Legacy helper; EXECUTE no longer ends HMAS-1 discussion by itself."""
    return bool(_EXECUTE_RE.search(text or ""))


def format_dialogue_history(turns: list[dict[str, str]]) -> str:
    if not turns:
        return "(no robot comments yet — you speak first after the initial plan)"
    lines: list[str] = []
    for i, turn in enumerate(turns, start=1):
        lines.append(f"[Turn {i} — {turn['speaker']}]\n{turn['text'].strip()}")
    return "\n\n".join(lines)


def latest_stances(
    history: list[dict[str, str]], participants: list[str]
) -> dict[str, str]:
    """Map each participant to their most recent dialogue text (if any)."""
    latest: dict[str, str] = {}
    for turn in history:
        speaker = turn.get("speaker", "")
        if speaker in participants:
            latest[speaker] = turn.get("text", "")
    return latest


def all_participants_agree(
    history: list[dict[str, str]], participants: list[str]
) -> bool:
    """True when every participant has spoken and their latest message AGREEs."""
    if not participants:
        return False
    latest = latest_stances(history, participants)
    if len(latest) < len(participants):
        return False
    return all(looks_like_agree(latest[p]) for p in participants)


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
    return (
        "HMAS-1 TURN-BASED DISCUSSION (after the planner's priming plan).\n"
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
        "YOUR TURN — discuss until the fleet AGREEs:\n"
        "- Refine the plan from YOUR perspective. Prefer few or zero MCP tools "
        "(get_station / list_available_boxes only if needed).\n"
        "- Do NOT navigate, pickup, or drop yet.\n"
        "- Do NOT end the discussion alone with EXECUTE — that is ignored for "
        "ending the round.\n"
        "- If you still disagree with the current plan, reply starting with "
        "DISAGREE: and a short reason / alternative.\n"
        "- If you accept the current plan (including peers' refinements so far), "
        "reply starting with AGREE: then a short summary of YOUR role and any "
        "concrete coords you will use later.\n"
        "- Discussion ends only when EVERY participant's latest message starts "
        "with AGREE; then the system dispatches execute to everyone.\n"
    )


def parse_execute_actions(text: str, participants: list[str]) -> dict[str, str]:
    """Best-effort map peer name -> action line from an AGREE/EXECUTE block."""
    actions: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or _EXECUTE_RE.match(stripped) or _AGREE_RE.match(stripped):
            # Keep text after AGREE: on the same line as a possible action hint
            if _AGREE_RE.match(stripped):
                rest = _AGREE_RE.sub("", stripped, count=1).lstrip(": ").strip()
                if rest and ":" in rest:
                    stripped = rest
                else:
                    continue
            else:
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
        for tb in TB_IDS:
            peer = robot_peer_name(tb)
            if peer in actions:
                continue
            for pref in (f"{tb}:", f"{tb} -"):
                if stripped.lower().startswith(pref):
                    actions[peer] = stripped[len(pref) :].strip()
                    break
    return actions


def agreed_plan_text(
    initial_plan: str,
    history: list[dict[str, str]],
    participants: list[str],
) -> str:
    """Combine initial plan with each participant's latest AGREE text."""
    latest = latest_stances(history, participants)
    parts = [f"Initial plan:\n{initial_plan.strip()}", "Agreed stances:"]
    for peer in participants:
        parts.append(f"- {peer}: {(latest.get(peer) or '').strip()}")
    return "\n".join(parts)
