"""Iterative multi-robot planning protocol (Chen et al., arXiv:2309.15943).

Planning is performed incrementally: LLM agents converge on actions, a
rules-based verifier checks them against the available-action list, the
actions are mapped onto pre-defined robot primitives, and the resulting
state is fed back as context for the next meeting.

Used by AgentNet (a short chunk of symbolic actions, no planner; the fleet
reconvenes after the chunk and ends only when every robot says FINISHED).
HMAS-1 uses the world snapshot and StepHistory from this module, but plans
in natural language as a full multi-step mission. Robots vote per STEP;
rejecting a step discards that plan and continues as PMAS.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .config import ROBOT_IDS, nav_id_for_tb, robot_peer_name
from .instructions import GROUND_TRUTH
from .mcp_client import call_mcp_tool


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


# Paper failure conditions: no consensus within the dialogue limit, syntax
# checking beyond its limit, or too many planning iterations.
MAX_PLAN_STEPS = _env_int("PAPER_MAX_PLAN_STEPS", 12)
MAX_DIALOGUE_ROUNDS = _env_int("PAPER_MAX_DIALOGUE_ROUNDS", 3)
MAX_SYNTAX_RETRIES = _env_int("PAPER_MAX_SYNTAX_RETRIES", 3)

# Stop short of the pad: pickup/drop need <1.8 m, Nav2 needs the pad clear.
APPROACH_OFFSET_M = 1.2

# Mirrors MCP_MANIP_RADIUS_M so the action list never offers a pick/drop the
# server would reject with too_far_from_station.
try:
    NEAR_RADIUS_M = float(os.getenv("MCP_MANIP_RADIUS_M", "1.8"))
except ValueError:
    NEAR_RADIUS_M = 1.8

EXECUTE_RE = re.compile(r"^\s*EXECUTE\b", re.IGNORECASE | re.MULTILINE)
TASK_COMPLETE_RE = re.compile(r"^\s*TASK_COMPLETE\b", re.IGNORECASE | re.MULTILINE)
FINISHED_RE = re.compile(r"^\s*FINISHED\b", re.IGNORECASE | re.MULTILINE)

_ACTION_RE = re.compile(
    r"\b(?P<verb>move_to|pick|drop|wait)\s*\(\s*(?P<arg>[^)]*?)\s*\)",
    re.IGNORECASE,
)

VERBS_WITH_TARGET = ("move_to", "pick", "drop")
ACTION_SYNTAX = (
    "move_to(<station_id>) | pick(<station_id>) | drop(<station_id>) | wait()"
)


@dataclass(frozen=True)
class Action:
    """One primitive assigned to one robot for the current planning step."""

    robot: str
    verb: str
    target: str = ""

    def text(self) -> str:
        return "wait()" if self.verb == "wait" else f"{self.verb}({self.target})"


def parse_action(text: str, robot: str) -> Action | None:
    match = _ACTION_RE.search(text or "")
    if match is None:
        return None
    verb = match.group("verb").lower()
    target = (match.group("arg") or "").strip().strip("'\"")
    if verb == "wait":
        return Action(robot=robot, verb="wait")
    if not target:
        return None
    return Action(robot=robot, verb=verb, target=target)


def _as_list(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
    if isinstance(payload, list):
        return [v for v in payload if isinstance(v, dict)]
    return []


def approach_pose(
    station: dict[str, Any],
    robot_x: float,
    robot_y: float,
    offset_m: float = APPROACH_OFFSET_M,
) -> dict[str, float]:
    """Point offset from the pad toward the robot (mirrors the MCP ranking tool)."""
    sx = float(station.get("x", 0.0))
    sy = float(station.get("y", 0.0))
    dx = float(robot_x) - sx
    dy = float(robot_y) - sy
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return {"x": round(sx + offset_m, 3), "y": round(sy, 3), "yaw": 0.0}
    return {
        "x": round(sx + offset_m * dx / dist, 3),
        "y": round(sy + offset_m * dy / dist, 3),
        "yaw": 0.0,
    }


class Environment:
    """Text view of the world plus the available-action list and primitives."""

    def __init__(self, participants: list[str] | None = None):
        self.participants = list(participants or [robot_peer_name(r) for r in ROBOT_IDS])
        self._lock = threading.Lock()
        self.world_id: str = ""
        self.stations: list[dict[str, Any]] = []
        self.poses: dict[str, dict[str, float]] = {}
        self.held: dict[str, str | None] = {}
        self.last_error: str = ""

    def refresh(self) -> None:
        stations_payload = call_mcp_tool("list_stations")
        poses_payload = call_mcp_tool("get_all_robot_poses")
        held_payload = call_mcp_tool("get_held_boxes")

        stations = _as_list(stations_payload, "stations")
        poses: dict[str, dict[str, float]] = {}
        for entry in _as_list(poses_payload, "poses"):
            if not entry.get("success"):
                continue
            rid = str(entry.get("robot_id", ""))
            if rid:
                poses[rid] = {
                    "x": float(entry.get("x", 0.0)),
                    "y": float(entry.get("y", 0.0)),
                    "yaw": float(entry.get("yaw", 0.0)),
                }
        held = held_payload if isinstance(held_payload, dict) else {}
        held = {k: v for k, v in held.items() if not k.startswith("error")}

        with self._lock:
            self.stations = stations
            self.poses = poses
            self.held = held
            if isinstance(stations_payload, dict):
                self.world_id = str(stations_payload.get("world_id", "") or self.world_id)
            self.last_error = "" if stations else f"list_stations: {stations_payload}"

    def station(self, station_id: str) -> dict[str, Any] | None:
        wanted = (station_id or "").strip().lower()
        with self._lock:
            for s in self.stations:
                if str(s.get("id", "")).lower() == wanted:
                    return dict(s)
            for s in self.stations:
                if str(s.get("name", "")).lower() == wanted:
                    return dict(s)
        return None

    def holds(self, robot: str) -> str | None:
        nav = nav_id_for_tb(robot)
        with self._lock:
            return self.held.get(nav) or self.held.get(robot)

    def pose(self, robot: str) -> dict[str, float] | None:
        nav = nav_id_for_tb(robot)
        with self._lock:
            return self.poses.get(nav) or self.poses.get(robot)

    def station_at(self, robot: str) -> str | None:
        """Station the robot currently stands close enough to manipulate."""
        pose = self.pose(robot)
        if pose is None:
            return None
        with self._lock:
            stations = list(self.stations)
        for station in stations:
            distance = math.hypot(
                pose["x"] - float(station.get("x", 0.0)),
                pose["y"] - float(station.get("y", 0.0)),
            )
            if distance <= NEAR_RADIUS_M:
                return str(station.get("id"))
        return None

    def state_text(self) -> str:
        with self._lock:
            stations = list(self.stations)
            poses = dict(self.poses)
            held = dict(self.held)
            world = self.world_id
        lines = [f"World: {world or 'unknown'}"]
        lines.append("Stations (id, x, y, box):")
        for s in stations:
            box = s.get("box_id")
            lines.append(
                f"  - {s.get('id')} at ({s.get('x')}, {s.get('y')}): "
                + (f"holds {box}" if box else "empty")
            )
        lines.append("Robots (position, carrying):")
        for robot in self.participants:
            nav = nav_id_for_tb(robot)
            pose = poses.get(nav)
            where = (
                f"({round(pose['x'], 2)}, {round(pose['y'], 2)})" if pose else "(unknown)"
            )
            at = self.station_at(robot)
            standing = f", standing at {at}" if at else ", not at any station"
            carrying = held.get(nav) or "nothing"
            lines.append(f"  - {robot} at {where}{standing}, carrying {carrying}")
        return "\n".join(lines)

    def available_actions(self, robot: str) -> list[str]:
        """Every action this robot may be assigned in the current state.

        pick/drop appear only for the station the robot already stands at,
        because the MCP primitives refuse to manipulate from a distance.
        """
        with self._lock:
            stations = list(self.stations)
        carrying = self.holds(robot)
        here = self.station_at(robot)
        actions = [f"move_to({s.get('id')})" for s in stations]
        for station in stations:
            station_id = str(station.get("id"))
            if here is None or station_id != here:
                continue
            if carrying and not station.get("box_id"):
                actions.append(f"drop({station_id})")
            elif not carrying and station.get("box_id"):
                actions.append(f"pick({station_id})")
        actions.append("wait()")
        return actions

    def action_menu_text(self, participants: list[str] | None = None) -> str:
        names = list(participants or self.participants)
        lines = [
            "Available actions this step — use EXACTLY these strings. A robot can "
            "only pick/drop at the station it already stands at, so send it there "
            "with move_to first and manipulate in the next step:"
        ]
        for robot in names:
            lines.append(f"  {robot}: {', '.join(self.available_actions(robot))}")
        return "\n".join(lines)

    def execute(self, assignment: dict[str, Action]) -> dict[str, Any]:
        """Run every assigned primitive in parallel (robots act simultaneously)."""
        results: dict[str, Any] = {}
        if not assignment:
            return results
        with ThreadPoolExecutor(max_workers=len(assignment)) as pool:
            futures = {
                pool.submit(execute_action, action): robot
                for robot, action in assignment.items()
            }
            for future in as_completed(futures):
                robot = futures[future]
                try:
                    results[robot] = future.result()
                except Exception as e:
                    results[robot] = {
                        "ok": False,
                        "error": f"{type(e).__name__}: {e}",
                    }
        return results

    def snapshot(self) -> Environment:
        """Independent copy used to check later actions in a multi-step plan."""
        other = Environment(list(self.participants))
        with self._lock:
            other.world_id = self.world_id
            other.stations = deepcopy(self.stations)
            other.poses = deepcopy(self.poses)
            other.held = deepcopy(self.held)
            other.last_error = self.last_error
        return other

    def apply_assignment(self, assignment: dict[str, Action]) -> None:
        """Advance this environment as if every action in the set succeeded."""
        for action in assignment.values():
            self._apply_one(action)

    def _station_ref(self, station_id: str) -> dict[str, Any] | None:
        wanted = (station_id or "").strip().lower()
        with self._lock:
            for s in self.stations:
                if str(s.get("id", "")).lower() == wanted:
                    return s
            for s in self.stations:
                if str(s.get("name", "")).lower() == wanted:
                    return s
        return None

    def _apply_one(self, action: Action) -> None:
        nav = nav_id_for_tb(action.robot)
        if action.verb == "wait":
            return
        if action.verb == "move_to":
            station = self.station(action.target)
            if station is None:
                return
            with self._lock:
                self.poses[nav] = {
                    "x": float(station.get("x", 0.0)),
                    "y": float(station.get("y", 0.0)),
                    "yaw": 0.0,
                }
            return
        station = self._station_ref(action.target)
        if station is None:
            return
        with self._lock:
            if action.verb == "pick":
                box = station.get("box_id")
                station["box_id"] = None
                station["available"] = False
                self.held[nav] = box
            elif action.verb == "drop":
                box = self.held.get(nav) or self.held.get(action.robot)
                station["box_id"] = box
                station["available"] = bool(box)
                self.held[nav] = None
                self.held[action.robot] = None


def execute_action(action: Action) -> dict[str, Any]:
    """Map one symbolic action onto MCP primitives. No LLM involved."""
    nav = nav_id_for_tb(action.robot)
    if action.verb == "wait":
        return {"ok": True, "action": action.text(), "detail": "no motion"}

    if action.verb == "move_to":
        station = _lookup_station(action.target)
        if station is None:
            return {
                "ok": False,
                "action": action.text(),
                "error": "unknown_station",
            }
        pose = call_mcp_tool("get_robot_pose", {"robot_id": nav})
        if isinstance(pose, dict) and pose.get("success"):
            goal = approach_pose(station, float(pose["x"]), float(pose["y"]))
        else:
            goal = approach_pose(station, float(station.get("x", 0.0)) + 1.0,
                                 float(station.get("y", 0.0)))
        result = call_mcp_tool(
            "navigate_to_pose",
            {"robot_id": nav, "x": goal["x"], "y": goal["y"], "yaw": goal["yaw"]},
        )
        ok = bool(isinstance(result, dict) and result.get("success"))
        return {
            "ok": ok,
            "action": action.text(),
            "goal": goal,
            "error": None if ok else _short_error(result),
        }

    tool = "pickup_box" if action.verb == "pick" else "drop_box"
    result = call_mcp_tool(tool, {"robot_id": nav, "station_id": action.target})
    ok = bool(isinstance(result, dict) and result.get("success"))
    return {
        "ok": ok,
        "action": action.text(),
        "error": None if ok else _short_error(result),
    }


def _lookup_station(station_id: str) -> dict[str, Any] | None:
    payload = call_mcp_tool("get_station", {"station_id": station_id})
    if isinstance(payload, dict) and not payload.get("error"):
        station = payload.get("station") if isinstance(payload.get("station"), dict) else payload
        if station.get("x") is not None:
            return station
    return None


def _short_error(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("error", "reason", "message"):
            if result.get(key):
                return str(result[key])[:160]
    return str(result)[:160]


def parse_execute_block(
    text: str, participants: list[str]
) -> tuple[dict[str, Action], list[str]]:
    """Extract ``robot -> Action`` from a reply that contains EXECUTE."""
    body = EXECUTE_RE.split(text or "", maxsplit=1)
    tail = body[-1] if len(body) > 1 else (text or "")
    assignment: dict[str, Action] = {}
    problems: list[str] = []

    decoded = _try_json_object(tail)
    if decoded:
        for robot, raw in decoded.items():
            resolved = _match_participant(robot, participants)
            if resolved is None:
                problems.append(f"unknown robot {robot!r} in EXECUTE block")
                continue
            action = parse_action(str(raw), resolved)
            if action is None:
                problems.append(f"{resolved}: cannot parse action from {raw!r}")
            else:
                assignment[resolved] = action
        return assignment, problems

    for line in tail.splitlines():
        stripped = line.strip().lstrip("-*• ").strip()
        if not stripped:
            continue
        resolved = None
        for participant in participants:
            if stripped.lower().startswith(participant.lower()):
                resolved = participant
                break
        if resolved is None:
            continue
        action = parse_action(stripped[len(resolved):], resolved)
        if action is None:
            problems.append(
                f"{resolved}: no valid action in {stripped[:80]!r} "
                f"(expected {ACTION_SYNTAX})"
            )
            continue
        assignment[resolved] = action
    return assignment, problems


def parse_action_sequence(text: str, robot: str) -> list[Action]:
    """Every ``move_to`` / ``pick`` / ``drop`` / ``wait`` on one line, in order."""
    actions: list[Action] = []
    for match in _ACTION_RE.finditer(text or ""):
        verb = match.group("verb").lower()
        target = (match.group("arg") or "").strip().strip("'\"")
        if verb == "wait":
            actions.append(Action(robot=robot, verb="wait"))
        elif target:
            actions.append(Action(robot=robot, verb=verb, target=target))
    return actions


def parse_plan_block(
    text: str, participants: list[str]
) -> tuple[dict[str, list[Action]], list[str]]:
    """Extract ``robot -> [Action, …]`` from a reply that contains EXECUTE.

    One line per robot; several actions on that line are separated by
    ``;`` (commas also work). A single-action EXECUTE from HMAS-1 still parses.
    """
    body = EXECUTE_RE.split(text or "", maxsplit=1)
    if len(body) < 2:
        return {}, []
    tail = body[-1]
    plan: dict[str, list[Action]] = {}
    problems: list[str] = []

    decoded = _try_json_object(tail)
    if decoded:
        for robot, raw in decoded.items():
            resolved = _match_participant(robot, participants)
            if resolved is None:
                problems.append(f"unknown robot {robot!r} in EXECUTE block")
                continue
            if isinstance(raw, list):
                joined = "; ".join(str(item) for item in raw)
            else:
                joined = str(raw)
            seq = parse_action_sequence(joined, resolved)
            if not seq:
                problems.append(f"{resolved}: cannot parse actions from {raw!r}")
            else:
                plan[resolved] = seq
        return plan, problems

    for line in tail.splitlines():
        stripped = line.strip().lstrip("-*• ").strip()
        if not stripped or stripped.lower().startswith("step "):
            continue
        resolved = None
        for participant in participants:
            if stripped.lower().startswith(participant.lower()):
                resolved = participant
                break
        if resolved is None:
            continue
        seq = parse_action_sequence(stripped[len(resolved):], resolved)
        if not seq:
            problems.append(
                f"{resolved}: no valid action in {stripped[:80]!r} "
                f"(expected {ACTION_SYNTAX}, several joined with ';')"
            )
            continue
        plan[resolved] = seq
    return plan, problems


def pad_plan(
    plan: dict[str, list[Action]], participants: list[str]
) -> dict[str, list[Action]]:
    length = max((len(seq) for seq in plan.values()), default=0)
    padded: dict[str, list[Action]] = {}
    for robot in participants:
        seq = list(plan.get(robot) or [])
        while len(seq) < length:
            seq.append(Action(robot=robot, verb="wait"))
        padded[robot] = seq[:length]
    return padded


def plan_assignments(
    plan: dict[str, list[Action]],
) -> list[dict[str, Action]]:
    if not plan:
        return []
    length = max(len(seq) for seq in plan.values())
    return [{robot: seq[i] for robot, seq in plan.items()} for i in range(length)]


def plan_text(plan: dict[str, list[Action]], participants: list[str]) -> str:
    lines = []
    for robot in participants:
        seq = plan.get(robot) or []
        if not seq:
            continue
        joined = "; ".join(action.text() for action in seq)
        lines.append(f"{robot}: {joined}")
    return "\n".join(lines)


def verify_plan(
    plan: dict[str, list[Action]],
    participants: list[str],
    env: Environment,
    *,
    max_steps: int,
) -> list[str]:
    """Check every step of a chunk against the state the previous step would leave."""
    errors: list[str] = []
    missing = [p for p in participants if p not in plan]
    if missing:
        errors.append(
            "missing a line for: " + ", ".join(missing)
            + " — every robot needs at least one action (use wait() if it should hold)."
        )
        return errors
    length = max(len(seq) for seq in plan.values())
    if length < 1:
        errors.append("EXECUTE needs at least one action per robot.")
        return errors
    if length > max_steps:
        errors.append(
            f"this chunk has {length} actions per robot; plan at most {max_steps} "
            "and meet again after they run."
        )
        return errors

    padded = pad_plan(plan, participants)
    simulated = env.snapshot()
    for index, assignment in enumerate(plan_assignments(padded), start=1):
        step_errors = verify_assignment(assignment, participants, simulated)
        for err in step_errors:
            errors.append(f"step {index}: {err}")
        if step_errors:
            break
        simulated.apply_assignment(assignment)
    return errors


def _try_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        decoded = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) and decoded else None


def _match_participant(token: str, participants: list[str]) -> str | None:
    low = (token or "").strip().lower()
    for participant in participants:
        if participant.lower() == low:
            return participant
    for participant in participants:
        if low.startswith(participant.lower()):
            return participant
    return None


def verify_assignment(
    assignment: dict[str, Action],
    participants: list[str],
    env: Environment,
) -> list[str]:
    """Rules-based syntactic check (paper §III): complete, legal, and useful."""
    errors: list[str] = []

    missing = [p for p in participants if p not in assignment]
    if missing:
        errors.append(
            "missing an action for: " + ", ".join(missing)
            + " — every robot needs exactly one action (use wait() if it should hold)."
        )

    for robot, action in assignment.items():
        if robot not in participants:
            errors.append(f"{robot} is not part of this step")
            continue
        allowed = env.available_actions(robot)
        if action.text() not in allowed:
            errors.append(
                f"{robot}: {action.text()} is not in its available action list. "
                f"Allowed: {', '.join(allowed)}"
            )

    if assignment and all(a.verb == "wait" for a in assignment.values()):
        errors.append(
            "every robot was assigned wait() — a step must move the task forward, "
            "so assign a real action to at least one robot."
        )

    for verb in VERBS_WITH_TARGET:
        seen: dict[str, str] = {}
        for robot, action in assignment.items():
            if action.verb != verb:
                continue
            other = seen.get(action.target)
            if other:
                errors.append(
                    f"{other} and {robot} both got {verb}({action.target}) — "
                    "two robots cannot share one station in the same step."
                )
            else:
                seen[action.target] = robot
    return errors


def assignment_text(assignment: dict[str, Action], participants: list[str]) -> str:
    return "\n".join(
        f"{robot}: {assignment[robot].text()}"
        for robot in participants
        if robot in assignment
    )


def results_text(results: dict[str, Any]) -> str:
    lines = []
    for robot, result in sorted(results.items()):
        if not isinstance(result, dict):
            lines.append(f"{robot}: {result}")
            continue
        status = "ok" if result.get("ok") else f"FAILED ({result.get('error')})"
        lines.append(f"{robot}: {result.get('action')} -> {status}")
    return "\n".join(lines)


@dataclass
class StepRecord:
    index: int
    state: str
    actions: str
    outcome: str


@dataclass
class StepHistory:
    """State-action pair history (the paper's best token/performance trade-off)."""

    steps: list[StepRecord] = field(default_factory=list)
    keep: int = 4

    def add(self, state: str, actions: str, outcome: str) -> None:
        self.steps.append(
            StepRecord(index=len(self.steps) + 1, state=state, actions=actions, outcome=outcome)
        )

    def text(self) -> str:
        if not self.steps:
            return "(no actions executed yet — this is the first planning step)"
        chunks = []
        for record in self.steps[-self.keep :]:
            chunks.append(
                f"[Step {record.index}]\nState was:\n{record.state}\n"
                f"Actions taken:\n{record.actions}\nResult:\n{record.outcome}"
            )
        return "\n\n".join(chunks)

    def repeats_recent(self, actions: str, lookback: int = 2) -> bool:
        """True when this exact action set already ran the last ``lookback`` steps.

        Re-running it a third time cannot change anything, so the plan checker
        rejects it instead of letting the fleet spin.
        """
        recent = [s.actions for s in self.steps[-lookback:]]
        return len(recent) == lookback and all(a == actions for a in recent)


def build_planning_prompt(
    *,
    task: str,
    env: Environment,
    history: StepHistory,
    participants: list[str],
    step_index: int,
    speaker: str | None = None,
    role_line: str,
    dialogue: list[dict[str, str]] | None = None,
    initial_plan: str = "",
    syntax_feedback: str = "",
    closing_instruction: str,
    include_action_menu: bool = True,
) -> str:
    """Assemble the paper's prompt components in a fixed order."""
    parts = [
        role_line,
        "",
        "[Task Description]",
        task.strip(),
        "",
        "[Step History] (state-action pairs from earlier planning steps)",
        history.text(),
        "",
        f"[Current State] (planning step {step_index}/{MAX_PLAN_STEPS})",
        env.state_text(),
    ]
    if include_action_menu:
        parts += [
            "",
            "[Robot State & Capability]",
            env.action_menu_text(participants),
        ]
    parts += [
        "",
        "[Ground Truth]",
        GROUND_TRUTH,
    ]
    if initial_plan.strip():
        parts += ["", "[Central Planner Initial Plan]", initial_plan.strip()]
    if dialogue is not None:
        parts += ["", "[Dialogue This Step]", format_dialogue(dialogue)]
    if syntax_feedback.strip():
        parts += ["", "[Plan Syntactic Checking Feedback]", syntax_feedback.strip()]
    if speaker:
        parts += ["", f"[Your Turn: {speaker}]"]
    parts += ["", "[Communication Instruction]", closing_instruction.strip()]
    return "\n".join(parts)


def format_dialogue(dialogue: list[dict[str, str]]) -> str:
    if not dialogue:
        return "(nobody has spoken yet this step)"
    return "\n\n".join(
        f"[{turn.get('speaker')}] {str(turn.get('text', '')).strip()}"
        for turn in dialogue
    )
