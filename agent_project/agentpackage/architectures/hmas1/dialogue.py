"""HMAS-1 dialogue helpers.

A central LLM planner proposes a short natural-language plan (one leg per
robot, DMAS-style). The robots take turns: they follow it (AGREE) unless they
see an exception (DISAGREE / corrected PLAN). Agreed legs are carried out with
MCP tools, then the new state opens the next round.
"""

from __future__ import annotations

import re
from typing import Any

from ...config import TB_IDS, is_q1_platform, peer_id_example, resolve_robot_id, robot_peer_name
from ...instructions import (
    DMAS_EXECUTION_REPORT,
    Q1_EXECUTION_REPORT,
    dmas_execution_how,
    hmas1_planner_closing,
    hmas1_planner_role,
    hmas1_robot_closing,
    hmas1_robot_role,
    q1_dmas_execution_how,
    q1_hmas1_planner_closing,
    q1_hmas1_planner_role,
    q1_hmas1_robot_closing,
    q1_hmas1_robot_role,
)
from ...paper_protocol import Environment, StepHistory, build_planning_prompt
from ..dmas.protocol import PLAN, parse_legs, parse_turn, verify_plan

EXECUTE_DISPATCH_PREFIX = "EXECUTE_ACTION:"
_EXECUTE_RE = re.compile(r"^\s*EXECUTE\b", re.IGNORECASE | re.MULTILINE)
AGREE_RE = re.compile(r"^\s*AGREE\b", re.IGNORECASE | re.MULTILINE)
DISAGREE_RE = re.compile(r"^\s*DISAGREE\b", re.IGNORECASE | re.MULTILINE)


def looks_agree(text: str) -> bool:
    return bool(AGREE_RE.search(text or ""))


def looks_disagree(text: str) -> bool:
    return bool(DISAGREE_RE.search(text or ""))


def legs_text(legs: dict[str, str], participants: list[str]) -> str:
    return "\n".join(f"{robot}: {legs.get(robot, '(no leg)')}" for robot in participants)


def parse_hmas1_plan(
    text: str, participants: list[str]
) -> tuple[dict[str, str] | None, list[str]]:
    """Accept a DMAS-style PLAN or an EXECUTE block with ``Name: leg`` lines."""
    turn = parse_turn(text, participants)
    legs = dict(turn.legs) if turn.kind == PLAN else {}
    if not legs and _EXECUTE_RE.search(text or ""):
        body = _EXECUTE_RE.split(text or "", maxsplit=1)
        if len(body) > 1:
            legs = parse_legs(body[-1], participants)
    if not legs:
        return None, []
    errors = verify_plan(legs, participants)
    if errors:
        return None, errors
    return legs, []


def resolve_participants(recipients: str) -> list[str] | dict[str, Any]:
    raw = (recipients or "").strip()
    if raw.lower() in {"all", "*", "everyone", "fleet", ""}:
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
                "Use 'all' or a comma-separated list of specialist names like "
                f"{peer_id_example()}."
            ),
            "unknown": unknown,
            "valid": list(TB_IDS),
        }
    order = [robot_peer_name(rid) for rid in TB_IDS]
    return [p for p in order if p in peers]


def build_central_plan_prompt(
    *,
    task: str,
    env: Environment,
    history: StepHistory,
    participants: list[str],
    step_index: int,
    syntax_feedback: str = "",
) -> str:
    return build_planning_prompt(
        task=task,
        env=env,
        history=history,
        participants=participants,
        step_index=step_index,
        role_line=q1_hmas1_planner_role() if is_q1_platform() else hmas1_planner_role(),
        closing_instruction=(
            q1_hmas1_planner_closing() if is_q1_platform() else hmas1_planner_closing()
        ),
        syntax_feedback=syntax_feedback,
        include_action_menu=False,
    )


def build_turn_prompt(
    *,
    task: str,
    env: Environment,
    history: StepHistory,
    participants: list[str],
    speaker: str,
    step_index: int,
    round_idx: int,
    max_rounds: int,
    initial_plan: str,
    dialogue: list[dict[str, str]],
    syntax_feedback: str = "",
) -> str:
    others = [p for p in participants if p != speaker]
    return build_planning_prompt(
        task=task,
        env=env,
        history=history,
        participants=participants,
        step_index=step_index,
        speaker=speaker,
        role_line=(
            q1_hmas1_robot_role(
                speaker=speaker,
                order=" -> ".join(participants),
                round_idx=round_idx,
                max_rounds=max_rounds,
                peers=", ".join(others) or "(none)",
            )
            if is_q1_platform()
            else hmas1_robot_role(
                speaker=speaker,
                order=" -> ".join(participants),
                round_idx=round_idx,
                max_rounds=max_rounds,
                peers=", ".join(others) or "(none)",
            )
        ),
        initial_plan=initial_plan,
        dialogue=dialogue,
        closing_instruction=(
            q1_hmas1_robot_closing() if is_q1_platform() else hmas1_robot_closing()
        ),
        syntax_feedback=syntax_feedback,
        include_action_menu=False,
    )


def build_execute_dispatch(leg: str, *, step_index: int) -> str:
    return (
        f"{EXECUTE_DISPATCH_PREFIX} {leg.strip()}\n"
        f"(planning round {step_index}; the specialists agreed on this plan)"
    )


def build_execution_prompt(*, speaker: str, nav_id: str, leg: str, round_index: int) -> str:
    how = (
        q1_dmas_execution_how(speaker=speaker, nav_id=nav_id)
        if is_q1_platform()
        else dmas_execution_how(speaker=speaker, nav_id=nav_id)
    )
    report = Q1_EXECUTION_REPORT if is_q1_platform() else DMAS_EXECUTION_REPORT
    who = "specialists" if is_q1_platform() else "fleet"
    return (
        f"The {who} agreed on this round (round {round_index}). Carry out YOUR "
        "leg now, nothing more.\n"
        "\n"
        "[Your Leg]\n"
        f"{leg.strip()}\n"
        "\n"
        "[How]\n"
        f"{how}\n"
        "\n"
        "[Report]\n"
        f"{report}"
    )
