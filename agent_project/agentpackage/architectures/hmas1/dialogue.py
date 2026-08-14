"""HMAS-1 dialogue helpers (Chen et al., arXiv:2309.15943, Fig. 1/3b).

A central LLM planner proposes a short multi-step plan; the robots' LLMs then
take turns. They follow that plan (AGREE) unless they see an exception, in
which case they vote DISAGREE and may send a corrected EXECUTE. The chunk
runs once every robot has agreed on the plan on the table.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ...config import TB_IDS, resolve_robot_id, robot_peer_name
from ...instructions import (
    hmas1_planner_closing,
    hmas1_planner_role,
    hmas1_robot_closing,
    hmas1_robot_role,
)
from ...paper_protocol import (
    ACTION_SYNTAX,
    Environment,
    StepHistory,
    build_planning_prompt,
)

EXECUTE_DISPATCH_PREFIX = "EXECUTE_ACTION:"

try:
    CHUNK_STEPS = max(1, int(os.getenv("HMAS1_CHUNK_STEPS", "4").strip()))
except (TypeError, ValueError):
    CHUNK_STEPS = 4

AGREE_RE = re.compile(r"^\s*AGREE\b", re.IGNORECASE | re.MULTILINE)
DISAGREE_RE = re.compile(r"^\s*DISAGREE\b", re.IGNORECASE | re.MULTILINE)


def looks_agree(text: str) -> bool:
    return bool(AGREE_RE.search(text or ""))


def looks_disagree(text: str) -> bool:
    return bool(DISAGREE_RE.search(text or ""))


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
                "Use 'all' or a comma-separated list like "
                "SmallDeliveryRobot_0 or SmallDeliveryRobot_0,SmallDeliveryRobot_1."
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
        role_line=hmas1_planner_role(),
        closing_instruction=hmas1_planner_closing(
            chunk_steps=CHUNK_STEPS, action_syntax=ACTION_SYNTAX
        ),
        syntax_feedback=syntax_feedback,
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
        role_line=hmas1_robot_role(
            speaker=speaker,
            order=" -> ".join(participants),
            round_idx=round_idx,
            max_rounds=max_rounds,
            peers=", ".join(others) or "(none)",
        ),
        initial_plan=initial_plan,
        dialogue=dialogue,
        closing_instruction=hmas1_robot_closing(
            chunk_steps=CHUNK_STEPS, action_syntax=ACTION_SYNTAX
        ),
        syntax_feedback=syntax_feedback,
    )


def build_execute_dispatch(action_text: str, *, step_index: int, chunk_step: int = 1) -> str:
    return (
        f"{EXECUTE_DISPATCH_PREFIX} {action_text}\n"
        f"(planning chunk {step_index}, action {chunk_step}; "
        "the fleet agreed on this plan)"
    )
