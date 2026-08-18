"""DMAS round protocol: everything that works without an LLM.

Holds the message envelopes that travel over the mesh, the parser and checker
for a robot's turn, the consensus state machine of one round, and the prompt
builders. Keeping this free of transport and LLM code makes the protocol
testable with scripted replies.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

from ...instructions import (
    DMAS_ANSWER_FORMAT,
    DMAS_EXECUTION_REPORT,
    dmas_execution_how,
    dmas_turn_rules,
)

ARCHITECTURE = "dmas"

TURN_PREFIX = "DMAS_TURN"
EXECUTE_PREFIX = "DMAS_EXECUTE"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


# A mission that needs more rounds than this counts as failed.
MAX_ROUNDS = _env_int("DMAS_MAX_ROUNDS", 12)
# Turns spent discussing one round; after this the last proposed plan is executed.
MAX_TURNS_PER_ROUND = _env_int("DMAS_MAX_TURNS_PER_ROUND", 12)
# Re-asks of the same robot after an unusable or rejected answer.
MAX_SYNTAX_RETRIES = _env_int("DMAS_MAX_SYNTAX_RETRIES", 2)

TURN_TIMEOUT = float(os.getenv("DMAS_TURN_TIMEOUT", "300"))
EXECUTE_TIMEOUT = float(os.getenv("DMAS_EXECUTE_TIMEOUT", "900"))

# Kinds of turn a robot may take.
PLAN = "plan"
AGREE = "agree"
FINISHED = "finished"
INVALID = "invalid"

# What the round does after a turn.
CONTINUE = "continue"
EXECUTE = "execute"
DONE = "done"

_PLAN_RE = re.compile(r"^PLAN\b[:\s]*", re.IGNORECASE)
_AGREE_RE = re.compile(r"^AGREE\b", re.IGNORECASE)
_FINISHED_RE = re.compile(r"^FINISH(?:ED)?\b", re.IGNORECASE)
_IDLE_RE = re.compile(
    r"^(wait|hold|idle|stay|stand|remain|nothing|none|no action|-|—)\b",
    re.IGNORECASE,
)
_MANIP_RE = re.compile(r"\b(pick|pickup|drop|place)\b", re.IGNORECASE)
# Already standing at/near the named station (approach poses included).
_ALREADY_THERE_M = 2.5
NOOP_PLAN_MSG = (
    "this PLAN would not change the world (everyone waits or is already at "
    "the station they would drive to). If [World State Now] already satisfies "
    "the Mission goal, answer FINISHED."
)


def _clean_line(line: str) -> str:
    """Strip the markdown and bullet noise models put around keywords."""
    return (line or "").strip().strip("*_`>#").lstrip("-•* ").strip()


def _match_participant(token: str, participants: list[str]) -> str | None:
    low = (token or "").strip().strip("*_`'\"").lower()
    for participant in participants:
        if participant.lower() == low:
            return participant
    for participant in participants:
        if low.startswith(participant.lower()):
            return participant
    return None


# --- messages on the mesh ---------------------------------------------------

def turn_message(*, round_index: int, turn_index: int, prompt: str) -> str:
    return f"{TURN_PREFIX} " + json.dumps(
        {"round": round_index, "turn": turn_index, "prompt": prompt},
        ensure_ascii=False,
    )


def execute_message(*, round_index: int, leg: str) -> str:
    return f"{EXECUTE_PREFIX} " + json.dumps(
        {"round": round_index, "leg": leg}, ensure_ascii=False
    )


def parse_message(text: str) -> tuple[str, dict[str, Any]] | None:
    """Split an incoming mesh message into ``(kind, payload)``."""
    body = (text or "").strip()
    for prefix in (TURN_PREFIX, EXECUTE_PREFIX):
        if not body.startswith(prefix):
            continue
        try:
            payload = json.loads(body[len(prefix) :].strip())
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return prefix, payload
    return None


# --- one robot's turn -------------------------------------------------------

@dataclass
class Turn:
    kind: str
    legs: dict[str, str] = field(default_factory=dict)
    note: str = ""
    raw: str = ""

    def summary(self) -> str:
        if self.kind == PLAN:
            return f"PLAN ({len(self.legs)} legs)"
        return self.kind.upper()


def parse_turn(text: str, participants: list[str]) -> Turn:
    lines = (text or "").splitlines()
    for index, raw_line in enumerate(lines):
        line = _clean_line(raw_line)
        if not line:
            continue
        match = _PLAN_RE.match(line)
        if match:
            # Legs may start on the keyword line ("PLAN: Robot_0: …") or below it.
            rest = [line[match.end() :], *lines[index + 1 :]]
            return Turn(kind=PLAN, legs=_parse_legs(rest, participants), raw=text)
        if _FINISHED_RE.match(line):
            return Turn(kind=FINISHED, note=line, raw=text)
        if _AGREE_RE.match(line):
            return Turn(kind=AGREE, note=line, raw=text)
    return Turn(kind=INVALID, raw=text)


def parse_legs(text: str, participants: list[str]) -> dict[str, str]:
    """``Name: leg`` lines from a PLAN/EXECUTE body."""
    return _parse_legs((text or "").splitlines(), participants)


def _parse_legs(lines: list[str], participants: list[str]) -> dict[str, str]:
    legs: dict[str, str] = {}
    for raw_line in lines:
        line = _clean_line(raw_line)
        if not line or ":" not in line:
            continue
        head, _, rest = line.partition(":")
        who = _match_participant(head, participants)
        if who is None or not rest.strip():
            continue
        legs.setdefault(who, rest.strip())
    return legs


def looks_idle(leg: str) -> bool:
    return bool(_IDLE_RE.match((leg or "").strip()))


def _station_ids_in_leg(leg: str, env: Any) -> list[str]:
    text = (leg or "").lower()
    found: list[str] = []
    stations = list(getattr(env, "stations", None) or [])
    for station in stations:
        sid = str(station.get("id") or "")
        if sid and sid.lower() in text and sid not in found:
            found.append(sid)
    return found


def _already_at(robot: str, station_id: str, env: Any) -> bool:
    at = env.station_at(robot) if hasattr(env, "station_at") else None
    if at and at.lower() == station_id.lower():
        return True
    pose = env.pose(robot) if hasattr(env, "pose") else None
    station = env.station(station_id) if hasattr(env, "station") else None
    if pose is None or station is None:
        return False
    return math.hypot(
        float(pose.get("x", 0.0)) - float(station.get("x", 0.0)),
        float(pose.get("y", 0.0)) - float(station.get("y", 0.0)),
    ) <= _ALREADY_THERE_M


def _leg_is_noop(leg: str, robot: str, env: Any | None) -> bool:
    if looks_idle(leg):
        return True
    if env is None or _MANIP_RE.search(leg or ""):
        return False
    dests = _station_ids_in_leg(leg, env)
    return bool(dests) and all(_already_at(robot, dest, env) for dest in dests)


def verify_plan(
    legs: dict[str, str],
    participants: list[str],
    *,
    env: Any | None = None,
) -> list[str]:
    """Rules-based check of a proposed plan; empty list means accepted."""
    errors: list[str] = []
    missing = [p for p in participants if p not in legs]
    if missing:
        errors.append(
            "no leg for " + ", ".join(missing) + " — every robot needs exactly one "
            "line '<RobotName>: <leg>'; write 'wait' for a robot that should hold."
        )
    if legs and all(_leg_is_noop(text, robot, env) for robot, text in legs.items()):
        errors.append(NOOP_PLAN_MSG)
    return errors


# --- consensus within one round --------------------------------------------

class Consensus:
    """Tracks the plan on the table and who backs it.

    A new plan replaces the old one and resets the votes, so a single objection
    is always constructive: whoever disagrees has to propose something better.
    """

    def __init__(self, participants: list[str]):
        self.participants = list(participants)
        self.proposal: dict[str, str] = {}
        self.proposer: str = ""
        self.agreed: set[str] = set()
        self.finished: set[str] = set()

    def apply(self, speaker: str, turn: Turn) -> str:
        if turn.kind == PLAN:
            self.proposal = dict(turn.legs)
            self.proposer = speaker
            self.agreed = {speaker}
            self.finished.clear()
        elif turn.kind == AGREE:
            if self.finished and not self.proposal:
                # A FINISHED is already on the table: AGREE means "yes, we are done".
                self.finished.add(speaker)
            else:
                self.finished.discard(speaker)
                if self.proposal:
                    self.agreed.add(speaker)
        elif turn.kind == FINISHED:
            self.agreed.discard(speaker)
            self.proposal = {}
            self.proposer = ""
            self.finished.add(speaker)

        if len(self.finished) == len(self.participants):
            return DONE
        if self.proposal and len(self.agreed) == len(self.participants):
            return EXECUTE
        return CONTINUE

    def missing_votes(self) -> list[str]:
        return [p for p in self.participants if p not in self.agreed]

    def table_text(self) -> str:
        if self.finished and not self.proposal:
            names = ", ".join(sorted(self.finished))
            return (
                f"{names} said FINISHED (mission complete). "
                "If [World State Now] meets the goal, answer FINISHED or AGREE. "
                "Do not put a new PLAN on the table."
            )
        if not self.proposal:
            return "(no plan on the table yet — somebody has to propose one)"
        lines = [f"Proposed by {self.proposer}:"]
        for robot in self.participants:
            lines.append(f"  {robot}: {self.proposal.get(robot, '(no leg)')}")
        backing = ", ".join(sorted(self.agreed)) or "(nobody yet)"
        lines.append(f"Backed by: {backing}")
        missing = self.missing_votes()
        if missing:
            lines.append(f"Still missing: {', '.join(missing)}")
        return "\n".join(lines)


# --- round history ----------------------------------------------------------

@dataclass
class RoundRecord:
    index: int
    legs: dict[str, str]
    results: dict[str, str]
    state_changed: bool = True


def format_history(records: list[RoundRecord], *, keep: int = 8) -> str:
    if not records:
        return "(nothing has been executed yet — this is the first round)"
    lines: list[str] = []
    older = records[:-keep]
    if older:
        lines.append(f"(rounds 1-{older[-1].index} omitted)")
    for record in records[-keep:]:
        lines.append(f"Round {record.index}:")
        for robot, leg in record.legs.items():
            result = " ".join((record.results.get(robot, "") or "").split())
            lines.append(f"  {robot}: {leg}")
            lines.append(f"    -> {result[:240] or '(no report)'}")
        if not record.state_changed:
            lines.append("  ! the world did not change in this round")
    return "\n".join(lines)


def format_dialogue(dialogue: list[dict[str, str]]) -> str:
    if not dialogue:
        return "(nobody has spoken yet this round)"
    return "\n\n".join(
        f"{entry['speaker']} ({entry['kind']}):\n{entry['text'].strip()}"
        for entry in dialogue
    )


# --- prompts ----------------------------------------------------------------

ANSWER_FORMAT = DMAS_ANSWER_FORMAT


def build_turn_prompt(
    *,
    speaker: str,
    mission: str,
    participants: list[str],
    state_text: str,
    history: list[RoundRecord],
    dialogue: list[dict[str, str]],
    consensus: Consensus,
    round_index: int,
    feedback: str = "",
) -> str:
    parts = [
        f"You are {speaker}. It is your turn in the fleet discussion "
        f"(round {round_index} of at most {MAX_ROUNDS}).",
        "",
        "[Mission]",
        mission.strip(),
        "",
        "[Start vs now]",
        "The Mission describes the original task and the STARTING layout. "
        "Poses named there are not current and are not targets by themselves. "
        "[World State Now] is where every robot stands THIS round. If that "
        "current state already achieves the goal, answer FINISHED. Do not "
        "undo completed work.",
        "",
        "[World State Now]",
        state_text.strip(),
        "",
        "[What Happened In Earlier Rounds]",
        format_history(history),
        "",
        "[Discussion This Round]",
        format_dialogue(dialogue),
        "",
        "[Plan On The Table]",
        consensus.table_text(),
    ]
    if feedback.strip():
        parts += ["", "[Your Last Answer Was Rejected]", feedback.strip()]
    parts += [
        "",
        "[Rules]",
        dmas_turn_rules(max_turns=MAX_TURNS_PER_ROUND),
        "",
        f"[Your Turn: {speaker}]",
        ANSWER_FORMAT,
    ]
    return "\n".join(parts)


def build_execution_prompt(
    *,
    speaker: str,
    nav_id: str,
    leg: str,
    round_index: int,
) -> str:
    return (
        f"The fleet agreed on this round (round {round_index}). Carry out YOUR "
        "leg now, nothing more.\n"
        "\n"
        "[Your Leg]\n"
        f"{leg.strip()}\n"
        "\n"
        "[How]\n"
        f"{dmas_execution_how(speaker=speaker, nav_id=nav_id)}\n"
        "\n"
        "[Report]\n"
        f"{DMAS_EXECUTION_REPORT}"
    )
