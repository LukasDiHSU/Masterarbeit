from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import configure_logging
import json
import math
import os
import re
import shutil
import subprocess
from typing import Literal
from pydantic import BaseModel, Field


configure_logging("WARNING")
mcp = FastMCP("Tools")
ROS_CLI_TIMEOUT_SEC = float(os.environ.get("MCP_ROS_CLI_TIMEOUT_SEC", "5"))
MOVE_ROBOT_TIMEOUT_SEC = float(os.environ.get("MCP_MOVE_ROBOT_TIMEOUT_SEC", "180"))
MOVE_ROBOT_SUCCESS_RADIUS_M = float(os.environ.get("MCP_MOVE_ROBOT_SUCCESS_RADIUS_M", "0.35"))
TOOL_TEXT_MAX_CHARS = int(os.environ.get("MCP_TOOL_TEXT_MAX_CHARS", "1200"))
MOVE_FEEDBACK_BLOCK_MAX = int(os.environ.get("MCP_MOVE_FEEDBACK_BLOCK_MAX", "3"))
MOVE_FEEDBACK_BLOCK_CHARS = int(os.environ.get("MCP_MOVE_FEEDBACK_BLOCK_CHARS", "220"))


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _sample_amcl_xy(tb: str, timeout_sec: float = 2.5) -> tuple[float, float] | None:
    cmd = ["ros2", "topic", "echo", f"/{tb}/amcl_pose", "--once"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, check=False)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout or ""
    mx = re.search(r"\bx:\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", out)
    my = re.search(r"\by:\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", out)
    if not mx or not my:
        return None
    try:
        return float(mx.group(1)), float(my.group(1))
    except ValueError:
        return None


class RobotInput(BaseModel):
    robot: Literal["tb1", "tb2", "tb3", "tb4"] = Field(
        description="Which robot to use. Must be one of: tb1, tb2, tb3, tb4."
    )


_DEFAULT_ITEMS = [
    {"id": "item_1", "name": "box", "x": 1.0, "y": 2.0, "z": 0.0},
    {"id": "item_2", "name": "cup", "x": 3.5, "y": 1.0, "z": 0.5},
    {"id": "item_3", "name": "ball", "x": 2.0, "y": 3.0, "z": 0.0},
    {"id": "item_4", "name": "collection area", "x": 0.0, "y": 0.0, "z": 0.0},
]


def _load_scenario_items() -> list[dict]:
    """Load items/stations from maps/scenarios/<SCENARIO>/items.json when set.

    SCENARIO comes from the environment, or from agent_project/.active_scenario
    (written by launch_tb3_stack.sh).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(here, "..", ".."))
    scenario = os.environ.get("SCENARIO", "").strip()
    if not scenario:
        active = os.path.join(project_root, ".active_scenario")
        if os.path.isfile(active):
            try:
                with open(active, encoding="utf-8") as f:
                    scenario = f.read().strip()
            except OSError:
                scenario = ""
    if not scenario:
        return [dict(item) for item in _DEFAULT_ITEMS]
    maps_root = os.path.abspath(os.path.join(project_root, "..", "maps"))
    items_path = os.path.join(maps_root, "scenarios", scenario, "items.json")
    if not os.path.isfile(items_path):
        return [dict(item) for item in _DEFAULT_ITEMS]
    try:
        with open(items_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return [dict(item) for item in _DEFAULT_ITEMS]


ITEMS_WITH_COORDINATES = _load_scenario_items()

WHITEBOARD = []


@mcp.tool()
def get_robot_amcl_pose(
    robot: Literal["tb1", "tb2", "tb3", "tb4"] | None = None,
    input: RobotInput | None = None,
) -> str:
    """Get the AMCL pose of a robot. Runs: ros2 topic echo /<tb>/amcl_pose --once and returns stdout."""
    tb = robot or (input.robot if input is not None else None)
    if tb is None:
        return json.dumps({"error": "missing_robot", "message": "provide tb1..tb4"})
    if not shutil.which("ros2"):
        return json.dumps({"error": "ros2_not_found"})
    cmd = ["ros2", "topic", "echo", f"/{tb}/amcl_pose", "--once"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=ROS_CLI_TIMEOUT_SEC, check=False)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timeout", "tool": "get_robot_amcl_pose", "robot": tb, "timeout_sec": ROS_CLI_TIMEOUT_SEC})
    if proc.returncode != 0:
        return json.dumps({"error": "ros2_failed", "tool": "get_robot_amcl_pose", "robot": tb, "returncode": proc.returncode, "stderr": (proc.stderr or "").strip()})
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return json.dumps({"error": "empty_pose", "tool": "get_robot_amcl_pose", "robot": tb})
    return stdout


@mcp.tool()
def add_to_whiteboard(message: str) -> str:
    """Add a message to the whiteboard."""
    WHITEBOARD.append(message)
    return "Message added to whiteboard."


@mcp.tool()
def get_whiteboard() -> str:
    """Get all messages on the whiteboard."""
    return json.dumps(WHITEBOARD)


@mcp.tool()
def get_item_coordinates() -> str:
    """Return coordinates of all known items as a JSON list with id, name, x, y, z."""
    return json.dumps(ITEMS_WITH_COORDINATES, indent=2)


@mcp.tool()
def set_item_coordinates(item_id: str, x: float, y: float, z: float) -> str:
    """Set the coordinates of an item by id."""
    for item in ITEMS_WITH_COORDINATES:
        if item["id"] == item_id:
            item["x"] = x
            item["y"] = y
            item["z"] = z
            return "Coordinates set successfully."
    return "Item not found."


@mcp.tool()
def get_item_by_name(item_name: str) -> str:
    """Get coordinates of a specific item by name. Returns id, name, x, y, z or an error if not found."""
    for item in ITEMS_WITH_COORDINATES:
        if item["name"].lower() == item_name.lower():
            return json.dumps(item)
    return json.dumps({"error": f"No item named '{item_name}' found"})


@mcp.tool()
def grab_item(item_id: str) -> str:
    """Grab an item."""
    return "Item {item_id} grabbed successfully."


@mcp.tool()
def place_item(item_id: str) -> str:
    """Place an item."""
    return "Item {item_id} placed successfully."


@mcp.tool()
def move_item(item_id: str, x: float, y: float, z: float) -> str:
    """Move an item to the given coordinates."""
    return "Item {item_id} moved successfully."


@mcp.tool()
def move_robot(robot_id: str, x: float, y: float, z: float) -> str:
    """Send a Nav2 goal and wait for completion (or timeout)."""
    if not shutil.which("ros2"):
        return json.dumps({"error": "ros2_not_found"})

    s = robot_id.strip().lower()
    if s in {"tb1", "tb2", "tb3", "tb4"}:
        tb = s
    elif s in {"robot_1", "robot_2", "robot_3", "robot_4"}:
        tb = f"tb{s[-1]}"
    elif s in {"1", "2", "3", "4"}:
        tb = f"tb{s}"
    else:
        return json.dumps({"error": "invalid_robot_id", "message": "Use 1..4, tb1..tb4, or robot_1..robot_4", "robot_id": robot_id})

    action_name = f"/{tb}/navigate_to_pose"
    goal = json.dumps(
        {
            "pose": {
                "header": {"frame_id": "map"},
                "pose": {
                    "position": {"x": float(x), "y": float(y), "z": 0.0},
                    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                },
            }
        }
    )
    base_cmd = ["ros2", "action", "send_goal", action_name, "nav2_msgs/action/NavigateToPose", goal]
    cmd = ["ros2", "action", "send_goal", "--feedback", *base_cmd[3:]]

    def _run(c: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(c, capture_output=True, text=True, timeout=MOVE_ROBOT_TIMEOUT_SEC, check=False)

    try:
        proc = _run(cmd)
    except subprocess.TimeoutExpired:
        pose = _sample_amcl_xy(tb)
        if pose is not None:
            px, py = pose
            dist = math.hypot(px - float(x), py - float(y))
            if dist <= MOVE_ROBOT_SUCCESS_RADIUS_M:
                return json.dumps({"message": "goal_reached_after_timeout", "robot": tb, "x": x, "y": y, "z": z, "success": True, "distance_to_goal_m": round(dist, 4), "success_radius_m": MOVE_ROBOT_SUCCESS_RADIUS_M})
        return json.dumps({"error": "timeout", "tool": "move_robot", "robot": tb, "timeout_sec": MOVE_ROBOT_TIMEOUT_SEC})
    except Exception as e:
        return json.dumps({"error": "execution_failed", "tool": "move_robot", "message": str(e)})

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    err_text = f"{stdout.lower()} {stderr.lower()}"
    if proc.returncode != 0 and ("unrecognized arguments" in err_text or "no such option" in err_text):
        try:
            proc = _run(base_cmd)
        except subprocess.TimeoutExpired:
            pose = _sample_amcl_xy(tb)
            if pose is not None:
                px, py = pose
                dist = math.hypot(px - float(x), py - float(y))
                if dist <= MOVE_ROBOT_SUCCESS_RADIUS_M:
                    return json.dumps({"message": "goal_reached_after_timeout", "robot": tb, "x": x, "y": y, "z": z, "success": True, "distance_to_goal_m": round(dist, 4), "success_radius_m": MOVE_ROBOT_SUCCESS_RADIUS_M})
            return json.dumps({"error": "timeout", "tool": "move_robot", "robot": tb, "timeout_sec": MOVE_ROBOT_TIMEOUT_SEC})
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        return json.dumps({"error": "ros2_failed", "tool": "move_robot", "robot": tb, "returncode": proc.returncode, "stderr": _clip(stderr, TOOL_TEXT_MAX_CHARS), "stdout": _clip(stdout, TOOL_TEXT_MAX_CHARS)})

    success = "SUCCEEDED" in stdout.upper() or "status: 4" in stdout
    feedback_blocks: list[str] = []
    current_block: list[str] = []
    collecting = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        s = line.strip().lower()
        if s.startswith("feedback:"):
            if current_block:
                feedback_blocks.append("\n".join(current_block))
            current_block = [line]
            collecting = True
            continue
        if collecting:
            if s.startswith("result:") or s.startswith("goal accepted") or s.startswith("status:"):
                feedback_blocks.append("\n".join(current_block))
                current_block = []
                collecting = False
            else:
                current_block.append(line)
    if current_block:
        feedback_blocks.append("\n".join(current_block))

    last_metrics: dict[str, float] = {}
    for block in feedback_blocks:
        for raw in block.splitlines():
            line = raw.strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip().split(" ")[0]
            if key in {"distance_remaining", "number_of_recoveries", "estimated_time_remaining", "navigation_time", "speed", "linear_velocity", "angular_velocity"}:
                try:
                    last_metrics[key] = float(value)
                except ValueError:
                    pass

    feedback_tail = [_clip(block, MOVE_FEEDBACK_BLOCK_CHARS) for block in feedback_blocks[-MOVE_FEEDBACK_BLOCK_MAX:]]
    return json.dumps(
        {
            "message": "goal_completed" if success else "goal_finished",
            "robot": tb,
            "x": x,
            "y": y,
            "z": z,
            "success": success,
            "feedback_count": len(feedback_blocks),
            "feedback_tail": feedback_tail,
            "feedback_metrics_last": last_metrics,
            "output_chars": len(stdout),
            "stderr": _clip(stderr, 400),
        }
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Robot MCP server.")
    parser.add_argument("--host", default=os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FASTMCP_PORT", "8000")))
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="sse")
