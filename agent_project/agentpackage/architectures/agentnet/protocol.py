"""DMAS protocol helpers for the AgentNet mesh (Chen et al., arXiv:2309.15943).

There is no central planner. The robots take turns until they agree on a short
chunk of actions, execute that chunk, and meet again. The mission ends only
when every robot says FINISHED in the same discussion round.
"""

from __future__ import annotations

import os

from ...config import is_q1_platform
from ...instructions import agentnet_closing, agentnet_role, q1_agentnet_closing, q1_agentnet_role
from ...paper_protocol import (
    ACTION_SYNTAX,
    Environment,
    StepHistory,
    build_planning_prompt,
)

ARCHITECTURE = "agentnet"

ASK_TIMEOUT = float(os.getenv("AGENTNET_ASK_TIMEOUT", "1800"))

try:
    CHUNK_STEPS = max(1, int(os.getenv("AGENTNET_CHUNK_STEPS", "2").strip()))
except (TypeError, ValueError):
    CHUNK_STEPS = 2

EXECUTE_DISPATCH_PREFIX = "EXECUTE_ACTION:"
MISSION_PREFIX = "AGENTNET_MISSION"


def is_mission(text: str) -> bool:
    return (text or "").strip().startswith(MISSION_PREFIX)


def mission_text(text: str) -> str:
    body = (text or "").strip()
    if body.startswith(MISSION_PREFIX):
        return body[len(MISSION_PREFIX) :].strip()
    return body


def wrap_mission(description: str) -> str:
    return f"{MISSION_PREFIX}\n{description.strip()}"


def build_turn_prompt(
    *,
    task: str,
    env: Environment,
    history: StepHistory,
    participants: list[str],
    speaker: str,
    meeting: int,
    max_meetings: int,
    round_idx: int,
    max_rounds: int,
    dialogue: list[dict[str, str]],
    syntax_feedback: str = "",
) -> str:
    others = [p for p in participants if p != speaker]
    role_fn = q1_agentnet_role if is_q1_platform() else agentnet_role
    close_fn = q1_agentnet_closing if is_q1_platform() else agentnet_closing
    return build_planning_prompt(
        task=task,
        env=env,
        history=history,
        participants=participants,
        step_index=meeting,
        speaker=speaker,
        role_line=role_fn(
            speaker=speaker,
            meeting=meeting,
            max_meetings=max_meetings,
            round_idx=round_idx,
            max_rounds=max_rounds,
            order=" -> ".join(participants),
            peers=", ".join(others) or "(none)",
        ),
        dialogue=dialogue,
        syntax_feedback=syntax_feedback,
        closing_instruction=close_fn(
            chunk_steps=CHUNK_STEPS, action_syntax=ACTION_SYNTAX
        ),
    )


def build_execute_dispatch(action_text: str, *, meeting: int, step: int) -> str:
    return (
        f"{EXECUTE_DISPATCH_PREFIX} {action_text}\n"
        f"(meeting {meeting}, step {step}; the specialists agreed on this chunk)"
    )
