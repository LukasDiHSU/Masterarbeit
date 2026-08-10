from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import configure_logging
import json
import math
import os
import shutil
import subprocess
import time
from copy import deepcopy


configure_logging("WARNING")
mcp = FastMCP("Tools")

ROS_CLI_TIMEOUT_SEC = float(os.environ.get("MCP_ROS_CLI_TIMEOUT_SEC", "5"))
NAV_TIMEOUT_SEC = float(os.environ.get("MCP_NAV_TIMEOUT_SEC", "180"))
TOOL_TEXT_MAX_CHARS = int(os.environ.get("MCP_TOOL_TEXT_MAX_CHARS", "1200"))


def _clip(text: str, limit: int = TOOL_TEXT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


# --- Station / box inventory -------------------------------------------------
# Each station may hold at most one box. available=True means the box is still
# at the station and can be picked up.

_DEFAULT_STATIONS = [
    {"id": "station_A", "name": "Station A", "x": 1.0, "y": 2.0, "box_id": "box_1", "available": True, "last_box_id": "box_1"},
    {"id": "station_B", "name": "Station B", "x": 3.5, "y": 1.0, "box_id": "box_2", "available": True, "last_box_id": "box_2"},
    {"id": "station_C", "name": "Station C", "x": 5.2, "y": 3.8, "box_id": "box_3", "available": True, "last_box_id": "box_3"},
    {"id": "station_D", "name": "Station D", "x": 0.0, "y": 0.0, "box_id": None, "available": False, "last_box_id": None},
]

STATIONS: list[dict] = deepcopy(_DEFAULT_STATIONS)
# robot_id -> box_id currently held (or None)
HELD_BY: dict[str, str | None] = {}

# --- Shared whiteboard + event log ------------------------------------------
WHITEBOARD: list[str] = []
EVENTS: list[dict] = []


def _emit_event(event_type: str, **payload) -> dict:
    event = {"type": event_type, "ts": time.time(), **payload}
    EVENTS.append(event)
    return event


def _find_station(station_id: str) -> dict | None:
    for station in STATIONS:
        if station["id"] == station_id:
            return station
    return None


def _yaw_to_quaternion(yaw: float) -> dict[str, float]:
    half = yaw * 0.5
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}


def _nav_outcome(stdout: str, stderr: str, returncode: int) -> tuple[str, str]:
    """Return (status, reason) where status is succeeded|aborted|failed."""
    text = f"{stdout}\n{stderr}".upper()
    if "SUCCEEDED" in text or "STATUS: 4" in text:
        return "succeeded", "goal_succeeded"
    if any(tok in text for tok in ("ABORTED", "CANCELED", "CANCELLED", "STATUS: 5", "STATUS: 6")):
        reason = "aborted"
        for line in (stdout + "\n" + stderr).splitlines():
            if line.strip():
                reason = line.strip()
                break
        return "aborted", reason
    if returncode != 0:
        return "failed", _clip((stderr or stdout or f"returncode={returncode}").strip())
    return "failed", "unknown_nav_result"


# --- Whiteboard --------------------------------------------------------------

@mcp.tool()
def add_to_whiteboard(message: str) -> str:
    """Add a message to the shared whiteboard."""
    WHITEBOARD.append(message)
    return "Message added to whiteboard."


@mcp.tool()
def get_whiteboard() -> str:
    """Get all messages on the whiteboard."""
    return json.dumps(WHITEBOARD)


# --- Events ------------------------------------------------------------------

@mcp.tool()
def get_events(since_index: int = 0) -> str:
    """Return events from the shared event log starting at since_index (0-based).

    Event types include box_missing and nav_aborted. Use len(events) + since_index
    as the next since_index to poll incrementally.
    """
    start = max(0, int(since_index))
    slice_ = EVENTS[start:]
    return json.dumps(
        {"since_index": start, "next_index": start + len(slice_), "events": slice_},
        indent=2,
    )


@mcp.tool()
def clear_events() -> str:
    """Clear the shared event log (for a new experiment run)."""
    EVENTS.clear()
    return "Event log cleared."


@mcp.tool()
def emit_conflict(robot_ids: str, reason: str = "conflict", detail: str = "") -> str:
    """Manually emit a multi-robot conflict event (comma-separated robot_ids).

    Used for bottleneck / negotiation experiments when you want to open
    event-triggered communication between a subset of robots.
    """
    participants = [p.strip() for p in robot_ids.split(",") if p.strip()]
    if len(participants) < 2:
        return json.dumps(
            {
                "error": "need_at_least_two_robots",
                "message": "Pass comma-separated robot_ids, e.g. SmallDeliveryRobot_0,SmallDeliveryRobot_1",
            }
        )
    event = _emit_event(
        "conflict",
        participants=participants,
        reason=reason,
        detail=detail,
        message=f"Conflict among {participants}: {reason}",
    )
    return json.dumps({"success": True, "event": event}, indent=2)


# --- Stations / boxes --------------------------------------------------------

@mcp.tool()
def list_stations() -> str:
    """List every station: id, name, pose, box_id (or null), and whether a box is available."""
    return json.dumps(STATIONS, indent=2)


@mcp.tool()
def list_available_boxes() -> str:
    """List stations that currently have a pickable box (available=true and box_id set)."""
    available = [
        {
            "station_id": s["id"],
            "station_name": s["name"],
            "box_id": s["box_id"],
            "x": s["x"],
            "y": s["y"],
        }
        for s in STATIONS
        if s.get("available") and s.get("box_id")
    ]
    return json.dumps(available, indent=2)


@mcp.tool()
def get_station(station_id: str) -> str:
    """Get one station by id, including whether its box is still available."""
    station = _find_station(station_id)
    if station is None:
        return json.dumps({"error": "station_not_found", "station_id": station_id})
    return json.dumps(station, indent=2)


@mcp.tool()
def get_held_boxes() -> str:
    """Return which robot currently holds which box (robot_id -> box_id or null)."""
    return json.dumps(HELD_BY, indent=2)


@mcp.tool()
def pickup_box(robot_id: str, station_id: str) -> str:
    """Pick up the box at a station for this robot.

    If the box is not there (already taken / empty station), emits a box_missing
    event and returns an error JSON so event-triggered coordination can react.
    """
    robot = robot_id.strip()
    station = _find_station(station_id)
    if station is None:
        return json.dumps({"error": "station_not_found", "station_id": station_id})

    if HELD_BY.get(robot):
        return json.dumps(
            {
                "error": "robot_already_holding",
                "robot_id": robot,
                "box_id": HELD_BY[robot],
            }
        )

    box_id = station.get("box_id")
    if not station.get("available") or not box_id:
        event = _emit_event(
            "box_missing",
            station_id=station_id,
            robot_id=robot,
            expected_box=box_id or station.get("last_box_id"),
            message=f"No box available at {station_id} for {robot}",
        )
        return json.dumps(
            {
                "error": "box_missing",
                "station_id": station_id,
                "robot_id": robot,
                "event": event,
            }
        )

    station["last_box_id"] = box_id
    station["available"] = False
    station["box_id"] = None
    HELD_BY[robot] = box_id
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "station_id": station_id,
            "box_id": box_id,
            "message": f"{robot} picked up {box_id} from {station_id}",
        }
    )


@mcp.tool()
def drop_box(robot_id: str, station_id: str) -> str:
    """Drop the box the robot is holding onto a station (must be empty)."""
    robot = robot_id.strip()
    station = _find_station(station_id)
    if station is None:
        return json.dumps({"error": "station_not_found", "station_id": station_id})

    box_id = HELD_BY.get(robot)
    if not box_id:
        return json.dumps({"error": "robot_not_holding", "robot_id": robot})

    if station.get("box_id") or station.get("available"):
        return json.dumps(
            {
                "error": "station_occupied",
                "station_id": station_id,
                "box_id": station.get("box_id"),
            }
        )

    station["box_id"] = box_id
    station["last_box_id"] = box_id
    station["available"] = True
    HELD_BY[robot] = None
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "station_id": station_id,
            "box_id": box_id,
            "message": f"{robot} dropped {box_id} at {station_id}",
        }
    )


@mcp.tool()
def reset_stations() -> str:
    """Reset stations and held boxes to the default scenario layout."""
    STATIONS.clear()
    STATIONS.extend(deepcopy(_DEFAULT_STATIONS))
    HELD_BY.clear()
    return json.dumps({"success": True, "stations": STATIONS}, indent=2)


# --- Navigation (ROS 2 Nav2) -------------------------------------------------

@mcp.tool()
def navigate_to_pose(robot_id: str, x: float, y: float, yaw: float = 0.0) -> str:
    """Send a Nav2 NavigateToPose goal for a robot and wait for the result.

    Runs (conceptually):
      ros2 action send_goal /{robot_id}/navigate_to_pose nav2_msgs/action/NavigateToPose
      with map pose (x, y) and orientation from yaw (radians; default 0 => w=1).

    On abort/cancel/failure, emits a nav_aborted event.
    """
    robot = robot_id.strip()
    if not robot:
        return json.dumps({"error": "missing_robot_id"})

    if not shutil.which("ros2"):
        event = _emit_event(
            "nav_aborted",
            robot_id=robot,
            x=x,
            y=y,
            yaw=yaw,
            reason="ros2_not_found",
        )
        return json.dumps({"error": "ros2_not_found", "event": event})

    orientation = _yaw_to_quaternion(float(yaw))
    goal = json.dumps(
        {
            "pose": {
                "header": {"frame_id": "map"},
                "pose": {
                    "position": {"x": float(x), "y": float(y), "z": 0.0},
                    "orientation": orientation,
                },
            }
        }
    )
    action_name = f"/{robot}/navigate_to_pose"
    cmd = [
        "ros2",
        "action",
        "send_goal",
        action_name,
        "nav2_msgs/action/NavigateToPose",
        goal,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=NAV_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        event = _emit_event(
            "nav_aborted",
            robot_id=robot,
            x=x,
            y=y,
            yaw=yaw,
            reason="timeout",
            timeout_sec=NAV_TIMEOUT_SEC,
        )
        return json.dumps(
            {
                "error": "timeout",
                "tool": "navigate_to_pose",
                "robot_id": robot,
                "timeout_sec": NAV_TIMEOUT_SEC,
                "event": event,
            }
        )
    except Exception as e:
        event = _emit_event(
            "nav_aborted",
            robot_id=robot,
            x=x,
            y=y,
            yaw=yaw,
            reason=str(e),
        )
        return json.dumps(
            {
                "error": "execution_failed",
                "tool": "navigate_to_pose",
                "message": str(e),
                "event": event,
            }
        )

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    status, reason = _nav_outcome(stdout, stderr, proc.returncode)

    if status == "succeeded":
        return json.dumps(
            {
                "success": True,
                "robot_id": robot,
                "x": x,
                "y": y,
                "yaw": yaw,
                "status": status,
                "stdout": _clip(stdout, 400),
            }
        )

    event = _emit_event(
        "nav_aborted",
        robot_id=robot,
        x=x,
        y=y,
        yaw=yaw,
        reason=reason,
        status=status,
        returncode=proc.returncode,
    )
    return json.dumps(
        {
            "error": "nav_aborted" if status == "aborted" else "nav_failed",
            "robot_id": robot,
            "x": x,
            "y": y,
            "yaw": yaw,
            "status": status,
            "reason": reason,
            "stderr": _clip(stderr, 400),
            "stdout": _clip(stdout, 400),
            "event": event,
        }
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent MCP server.")
    parser.add_argument("--host", default=os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FASTMCP_PORT", "8000")))
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="sse")
