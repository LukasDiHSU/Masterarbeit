"""HMAS-1 dialogue helpers.

A central LLM planner proposes a full multi-step mission plan. Robots vote
per STEP (AGREE = execute this step as written). The last STEP is always
FINISHED: unanimous AGREE ends the mission. DISAGREE or a different PLAN
discards the original plan and the fleet continues as a peer (PMAS) network.
"""

from __future__ import annotations

import re
from typing import Any

from ...config import TB_IDS, resolve_robot_id, robot_peer_name
from ...instructions import (
    DMAS_EXECUTION_REPORT,
    hmas1_planner_closing,
    hmas1_planner_role,
    hmas1_robot_closing,
    hmas1_robot_role,
)
from ...paper_protocol import MAX_PLAN_STEPS, Environment, StepHistory, build_planning_prompt
from ..dmas.protocol import parse_legs, verify_plan

EXECUTE_DISPATCH_PREFIX = "EXECUTE_ACTION:"
_EXECUTE_RE = re.compile(r"^\s*EXECUTE\b[:\s]*", re.IGNORECASE)
_PLAN_RE = re.compile(r"^\s*PLAN\b[:\s]*", re.IGNORECASE)
_STEP_RE = re.compile(r"^STEP\s+(\d+)\s*:?\s*(.*)$", re.IGNORECASE)
AGREE_RE = re.compile(r"^\s*AGREE\b", re.IGNORECASE)
DISAGREE_RE = re.compile(r"^\s*DISAGREE\b", re.IGNORECASE)
_FINISH_LINE_RE = re.compile(r"^(finish(?:ed)?|done)\b", re.IGNORECASE)
_FINISH_LEG_RE = re.compile(
    r"^(finish(?:ed)?|done|task[_\s-]?complete)\b", re.IGNORECASE
)

MissionPlan = list[dict[str, str]]
FINISH_LEG = "FINISHED"


def looks_agree(text: str) -> bool:
    return bool(AGREE_RE.search(text or ""))


def looks_disagree(text: str) -> bool:
    return bool(DISAGREE_RE.search(text or ""))


def finish_step(participants: list[str]) -> dict[str, str]:
    return {peer: FINISH_LEG for peer in participants}


def is_finish_leg(leg: str) -> bool:
    return bool(_FINISH_LEG_RE.match((leg or "").strip()))


def is_finish_step(legs: dict[str, str] | None) -> bool:
    if not legs:
        return False
    return all(is_finish_leg(leg) for leg in legs.values())


def legs_text(legs: dict[str, str], participants: list[str]) -> str:
    if is_finish_step(legs):
        return FINISH_LEG
    return "\n".join(f"{robot}: {legs.get(robot, '(no leg)')}" for robot in participants)


def mission_text(steps: MissionPlan, participants: list[str]) -> str:
    blocks = []
    for index, legs in enumerate(steps, start=1):
        blocks.append(f"STEP {index}")
        blocks.append(legs_text(legs, participants))
    return "\n".join(blocks)


def same_legs(left: dict[str, str], right: dict[str, str], participants: list[str]) -> bool:
    return legs_text(left, participants) == legs_text(right, participants)


def restates_original_step(
    replacement: MissionPlan,
    *,
    current: dict[str, str],
    remaining: MissionPlan,
    original: MissionPlan,
    participants: list[str],
) -> bool:
    """True when a PLAN block is the original step/plan, not a new assignment."""
    rendered = mission_text(replacement, participants)
    if rendered == mission_text(original, participants):
        return True
    if rendered == mission_text(remaining, participants):
        return True
    return len(replacement) == 1 and same_legs(replacement[0], current, participants)


def _clean_line(line: str) -> str:
    return (line or "").strip().strip("*_`>#").lstrip("-•* ").strip()


def _extract_plan_body(text: str) -> str:
    """Return the block after PLAN or EXECUTE, or the whole text."""
    lines = (text or "").splitlines()
    for index, raw in enumerate(lines):
        line = _clean_line(raw)
        match = _PLAN_RE.match(line)
        if match:
            rest = line[match.end() :]
            return "\n".join([rest, *lines[index + 1 :]])
        match = _EXECUTE_RE.match(line)
        if match:
            rest = line[match.end() :]
            return "\n".join([rest, *lines[index + 1 :]])
    return text or ""


def _chunk_is_finish(chunk: list[str], participants: list[str]) -> bool:
    """True for a STEP that is only FINISHED (keyword or every leg)."""
    legs = parse_legs("\n".join(chunk), participants)
    if legs:
        return all(is_finish_leg(leg) for leg in legs.values())
    lines = [_clean_line(line) for line in chunk if _clean_line(line)]
    return bool(lines) and all(_FINISH_LINE_RE.match(line) for line in lines)


def parse_hmas1_plan(
    text: str,
    participants: list[str],
    *,
    max_steps: int = MAX_PLAN_STEPS,
    ensure_finish: bool = False,
) -> tuple[MissionPlan | None, list[str]]:
    """Parse a multi-step PLAN. A single block without STEP headers is one step.

    The last STEP may be FINISHED (keyword or every robot's leg). With
    ``ensure_finish`` a missing last FINISHED STEP is appended.
    """
    body = _extract_plan_body(text)
    lines = body.splitlines()
    chunks: list[list[str]] = []
    current: list[str] = []
    saw_step = False
    for raw in lines:
        cleaned = _clean_line(raw)
        match = _STEP_RE.match(cleaned)
        if match:
            saw_step = True
            if current and any(_clean_line(line) for line in current):
                chunks.append(current)
            leftover = (match.group(2) or "").strip()
            current = [leftover] if leftover else []
            continue
        current.append(raw)
    if current and any(_clean_line(line) for line in current):
        chunks.append(current)
    if not saw_step:
        chunks = [lines]

    steps: MissionPlan = []
    errors: list[str] = []
    step_no = 0
    for chunk in chunks:
        if _chunk_is_finish(chunk, participants):
            step_no += 1
            steps.append(finish_step(participants))
            continue
        legs = parse_legs("\n".join(chunk), participants)
        if not legs:
            named = any(
                any(peer.lower() in _clean_line(line).lower() for peer in participants)
                for line in chunk
            )
            if not named:
                continue
        step_no += 1
        step_errors = verify_plan(legs, participants)
        if step_errors:
            errors.extend(f"step {step_no}: {err}" for err in step_errors)
        else:
            steps.append(legs)

    if errors:
        return None, errors
    if not steps:
        return None, []
    if any(is_finish_step(step) for step in steps[:-1]):
        return None, ["FINISHED may only be the last STEP"]
    if ensure_finish and not is_finish_step(steps[-1]):
        steps = [*steps, finish_step(participants)]
    work_steps = sum(1 for step in steps if not is_finish_step(step))
    if work_steps > max_steps:
        return None, [
            f"plan has {work_steps} work STEP blocks but at most {max_steps} remain"
        ]
    return steps, []


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
        closing_instruction=hmas1_planner_closing(max_steps=MAX_PLAN_STEPS),
        syntax_feedback=syntax_feedback,
        include_action_menu=False,
    )


def build_step_vote_prompt(
    *,
    task: str,
    env: Environment,
    history: StepHistory,
    participants: list[str],
    speaker: str,
    original: MissionPlan,
    current: dict[str, str],
    step_index: int,
    n_steps: int,
    round_idx: int,
    max_rounds: int,
    dialogue: list[dict[str, str]],
    syntax_feedback: str = "",
) -> str:
    others = [p for p in participants if p != speaker]
    finish = is_finish_step(current)
    now_header = (
        f"[Vote on STEP {step_index} of {n_steps} NOW — FINISH]\n"
        "The original plan says the mission is done. AGREE ends it."
        if finish
        else f"[Vote on STEP {step_index} of {n_steps} NOW]"
    )
    highlighted = (
        "[Original Mission Plan]\n"
        + mission_text(original, participants)
        + f"\n\n{now_header}\n"
        + legs_text(current, participants)
    )
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
            current_step=step_index,
            n_steps=n_steps,
            finish_step=finish,
        ),
        initial_plan=highlighted,
        dialogue=dialogue,
        closing_instruction=hmas1_robot_closing(finish_step=finish),
        syntax_feedback=syntax_feedback,
        include_action_menu=False,
    )


def build_execute_dispatch(
    leg: str, *, step_index: int, n_steps: int | None = None
) -> str:
    total = f"/{n_steps}" if n_steps else ""
    return (
        f"{EXECUTE_DISPATCH_PREFIX} {leg.strip()}\n"
        f"(original plan STEP {step_index}{total}; execute this step as written)"
    )


def build_execution_prompt(*, speaker: str, nav_id: str, leg: str, round_index: int) -> str:
    return (
        f"The fleet agreed to execute STEP {round_index} of the original "
        "plan. Carry out YOUR leg now, nothing more.\n"
        "\n"
        "[Your Leg]\n"
        f"{leg.strip()}\n"
        "\n"
        "[How]\n"
        f"- You are {speaker}. Use robot_id '{nav_id}' for navigate_to_pose, "
        "drive_distance, pickup_box and drop_box.\n"
        "- The other robots are carrying out their own legs of this STEP at "
        "the same time. Do not do their work and do not run later STEPs — "
        "those are dispatched separately.\n"
        "- Navigate to a station before picking or dropping there.\n"
        "- If the same tool fails twice with the same arguments, stop and "
        "report what blocked you.\n"
        "\n"
        "[Report]\n"
        f"{DMAS_EXECUTION_REPORT}"
    )
