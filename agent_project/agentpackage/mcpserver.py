from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import configure_logging
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any


configure_logging("WARNING")
mcp = FastMCP("Tools")

ROS_CLI_TIMEOUT_SEC = float(os.environ.get("MCP_ROS_CLI_TIMEOUT_SEC", "5"))
NAV_TIMEOUT_SEC = float(os.environ.get("MCP_NAV_TIMEOUT_SEC", "180"))
TOOL_TEXT_MAX_CHARS = int(os.environ.get("MCP_TOOL_TEXT_MAX_CHARS", "1200"))
# Peer within this distance of self or goal counts as "in the way" after Nav2 fails.
NAV_BLOCKER_RADIUS_M = float(os.environ.get("MCP_NAV_BLOCKER_RADIUS_M", "2.0"))
DEFAULT_WORLD_ID = os.environ.get("AGENT_WORLD", "stations").strip() or "stations"

# Protect station/box inventory when concurrent tool calls run in threads.
_STATE_LOCK = threading.Lock()

_ROBOT_STATE_RE = re.compile(r"^/(SmallDeliveryRobot_\d+)/robot_state$")
_FLOAT_RE = re.compile(
    r"(?:^|\n)\s*(x|y|z|w|angle_min|angle_increment|range_min|range_max):\s*"
    r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(?:\n|$)"
)
_RANGES_RE = re.compile(r"ranges:\s*\[([^\]]*)\]", re.DOTALL)

try:
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover
    _yaml = None


def _clip(text: str, limit: int = TOOL_TEXT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


# --- Station / box inventory -------------------------------------------------
# Each station may hold at most one box. available=True means the box is still
# at the station and can be picked up.

# Fallback when items/{AGENT_WORLD}.json is missing (1.0x stations arena).
_DEFAULT_STATIONS = [
    {"id": "station_A", "name": "Station A", "x": -5.0, "y": -5.0, "box_id": "box_1", "available": True, "last_box_id": "box_1"},
    {"id": "station_B", "name": "Station B", "x": -5.0, "y": 5.0, "box_id": "box_2", "available": True, "last_box_id": "box_2"},
    {"id": "station_C", "name": "Station C", "x": 5.0, "y": 5.0, "box_id": "box_3", "available": True, "last_box_id": "box_3"},
    {"id": "station_D", "name": "Station D", "x": 5.0, "y": -5.0, "box_id": "box_4", "available": True, "last_box_id": None},
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


def _tool_error(event_type: str, *, error: str, **payload: Any) -> str:
    """Return an error JSON and always append a matching MCP event for peers."""
    event = _emit_event(event_type, error=error, **payload)
    body = {"error": error, "event": event, **payload}
    return json.dumps(body, indent=2)


def _normalize_station_id(station_id: str) -> str | None:
    """Accept station_A, station_a, 'Station A', 'A', 'C', etc. → canonical id."""
    s = (station_id or "").strip()
    if not s:
        return None
    for station in STATIONS:
        if station["id"] == s:
            return station["id"]

    low = s.lower().replace("-", " ").replace("_", " ")
    low = re.sub(r"\s+", " ", low).strip()
    compact = low.replace(" ", "_")

    for station in STATIONS:
        sid = station["id"]
        name = str(station.get("name") or "")
        if sid.lower() == compact or sid.lower() == low.replace(" ", "_"):
            return sid
        if name.lower() == low or name.lower().replace(" ", "_") == compact:
            return sid

    # Bare letter / "station X" → station_X
    letter = compact
    if letter.startswith("station_"):
        letter = letter[len("station_") :]
    elif letter.startswith("station"):
        letter = letter[len("station") :].lstrip("_")
    if len(letter) == 1 and letter.isalpha():
        candidate = f"station_{letter.upper()}"
        for station in STATIONS:
            if station["id"] == candidate:
                return candidate
    return None


def _find_station(station_id: str) -> dict | None:
    canon = _normalize_station_id(station_id)
    if canon is None:
        return None
    for station in STATIONS:
        if station["id"] == canon:
            return station
    return None


def _station_ids() -> list[str]:
    return [s["id"] for s in STATIONS]


def _yaw_to_quaternion(yaw: float) -> dict[str, float]:
    half = yaw * 0.5
    return {"x": 0.0, "y": 0.0, "z": math.sin(half), "w": math.cos(half)}


def _quaternion_to_yaw(z: float, w: float) -> float:
    return math.atan2(2.0 * z * w, 1.0 - 2.0 * z * z)


def _nav_outcome(stdout: str, stderr: str, returncode: int) -> tuple[str, str]:
    """Return (status, reason) where status is succeeded|aborted|failed."""
    combined = f"{stdout}\n{stderr}"
    # Prefer the Result: section — preamble often says "Waiting for an action server..."
    upper = combined.upper()
    result_at = upper.rfind("RESULT:")
    focus = combined[result_at:] if result_at >= 0 else combined
    focus_u = focus.upper()

    if "SUCCEEDED" in focus_u or "STATUS: 4" in focus_u:
        return "succeeded", "goal_succeeded"
    if any(tok in focus_u for tok in ("ABORTED", "CANCELED", "CANCELLED", "STATUS: 5", "STATUS: 6")):
        reason = "aborted"
        for line in focus.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith("waiting for an action server"):
                continue
            if stripped.lower() in {"result:", "result"}:
                continue
            reason = stripped
            break
        return "aborted", reason
    if returncode != 0:
        return "failed", _clip((stderr or stdout or f"returncode={returncode}").strip())
    # Goal accepted but no clear terminal status in output
    if "GOAL ACCEPTED" in upper and "SUCCEEDED" not in upper:
        return "failed", "goal_accepted_but_no_success_status"
    return "failed", "unknown_nav_result"


# --- Worlds / map metadata (repo worlds/) ------------------------------------

def _worlds_root() -> Path:
    env = os.environ.get("AGENT_WORLDS_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # agentpackage/mcpserver.py -> Masterarbeit/worlds
    master = Path(__file__).resolve().parents[2] / "worlds"
    if master.is_dir():
        return master
    remroc = Path("/home/lukas/agent_ws/src/remroc/remroc/worlds")
    return remroc if remroc.is_dir() else master


def _canonical_station_id_from_item(raw_id: str) -> str | None:
    """Map items.json ids (station_a, station_nw, …) to inventory ids."""
    s = (raw_id or "").strip()
    if not s:
        return None
    low = s.lower().replace("-", "_")
    m = re.fullmatch(r"station_([a-d])", low)
    if m:
        return f"station_{m.group(1).upper()}"
    if low.startswith("station_"):
        return low  # station_nw / station_ne / …
    return None


def _safe_world_id(world_id: str) -> str | None:
    wid = (world_id or "").strip()
    if not wid or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]*", wid):
        return None
    return wid


def _stations_from_world_items(world_id: str) -> list[dict] | None:
    """Build STATIONS inventory from items/{world_id}.json landmark entries."""
    wid = _safe_world_id(world_id)
    if wid is None:
        return None
    path = _worlds_root() / "items" / f"{wid}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, list):
        return None

    stations: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = _canonical_station_id_from_item(str(item.get("id") or ""))
        if sid is None:
            continue
        try:
            x = float(item["x"])
            y = float(item["y"])
        except Exception:
            continue
        name = str(item.get("name") or sid)
        stations.append(
            {
                "id": sid,
                "name": name.title() if name.lower().startswith("station") else name,
                "x": x,
                "y": y,
                "box_id": None,
                "available": False,
                "last_box_id": None,
            }
        )

    if not stations:
        return None

    # First three stations start with a box (matches default stations scenario).
    for i, station in enumerate(stations):
        if i < 3:
            box_id = f"box_{i + 1}"
            station["box_id"] = box_id
            station["available"] = True
            station["last_box_id"] = box_id
    return stations


def _load_stations_for_world(world_id: str | None = None) -> list[dict]:
    wid = (world_id or "").strip() or DEFAULT_WORLD_ID
    loaded = _stations_from_world_items(wid)
    return loaded if loaded else deepcopy(_DEFAULT_STATIONS)


def _apply_stations_for_world(world_id: str | None = None) -> None:
    STATIONS.clear()
    STATIONS.extend(_load_stations_for_world(world_id))
    HELD_BY.clear()


# Prefer landmarks from items/{AGENT_WORLD}.json (supports stations_1 / stations_2).
_apply_stations_for_world(DEFAULT_WORLD_ID)


def _parse_pgm_size(pgm_path: Path) -> dict[str, int] | None:
    """Read width/height from a binary/ascii PGM header without loading pixels."""
    try:
        with pgm_path.open("rb") as f:
            magic = f.readline().strip()
            if magic not in (b"P2", b"P5"):
                return None
            # Skip comments
            while True:
                line = f.readline()
                if not line:
                    return None
                line = line.strip()
                if not line or line.startswith(b"#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    return {"width": int(parts[0]), "height": int(parts[1])}
                # width on one line, height on next
                w = int(parts[0])
                h_line = f.readline()
                while h_line and (not h_line.strip() or h_line.strip().startswith(b"#")):
                    h_line = f.readline()
                if not h_line:
                    return None
                return {"width": w, "height": int(h_line.split()[0])}
    except Exception:
        return None


def _load_map_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed = _load_yaml_maybe(text)
    if isinstance(parsed, dict):
        return parsed
    # Minimal fallback without PyYAML
    meta: dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key in {"resolution", "occupied_thresh", "free_thresh"}:
            try:
                meta[key] = float(val)
            except ValueError:
                meta[key] = val
        elif key == "origin":
            nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", val)
            if len(nums) >= 2:
                meta["origin"] = [float(nums[0]), float(nums[1]), float(nums[2]) if len(nums) > 2 else 0.0]
        elif key in {"image", "mode"}:
            meta[key] = val
        elif key == "negate":
            try:
                meta[key] = int(val)
            except ValueError:
                meta[key] = val
    return meta or None


def _map_bounds(meta: dict[str, Any], pgm_path: Path | None) -> dict[str, float] | None:
    try:
        resolution = float(meta["resolution"])
        origin = meta["origin"]
        ox, oy = float(origin[0]), float(origin[1])
    except Exception:
        return None
    size = _parse_pgm_size(pgm_path) if pgm_path and pgm_path.is_file() else None
    if not size:
        return {"origin_x": ox, "origin_y": oy, "resolution": resolution}
    w, h = size["width"], size["height"]
    return {
        "origin_x": ox,
        "origin_y": oy,
        "resolution": resolution,
        "width_px": w,
        "height_px": h,
        "x_min": ox,
        "y_min": oy,
        "x_max": ox + w * resolution,
        "y_max": oy + h * resolution,
    }


def _discover_world_ids(root: Path) -> list[str]:
    names: set[str] = set()
    maps_dir = root / "maps"
    items_dir = root / "items"
    extra_dir = root / "extra_maps"
    if maps_dir.is_dir():
        for p in maps_dir.glob("*.yaml"):
            names.add(p.stem)
    if items_dir.is_dir():
        for p in items_dir.glob("*.json"):
            names.add(p.stem)
    if extra_dir.is_dir():
        for p in extra_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                names.add(p.name)
    return sorted(names)


def _build_map_info(world_id: str) -> dict[str, Any]:
    root = _worlds_root()
    wid = _safe_world_id(world_id)
    if wid is None:
        return {"error": "invalid_world_id", "world_id": world_id}

    maps_yaml = root / "maps" / f"{wid}.yaml"
    maps_pgm = root / "maps" / f"{wid}.pgm"
    items_json = root / "items" / f"{wid}.json"
    extra_dir = root / "extra_maps" / wid
    extra_yaml = extra_dir / "map.yaml"
    extra_pgm = extra_dir / "map.pgm"
    extra_items = extra_dir / "items.json"
    sdf_primary = root / "sdfs" / f"{wid}_0_0.sdf"
    extra_sdf = extra_dir / "world.sdf"

    yaml_path = maps_yaml if maps_yaml.is_file() else (extra_yaml if extra_yaml.is_file() else None)
    pgm_path = maps_pgm if maps_pgm.is_file() else (extra_pgm if extra_pgm.is_file() else None)
    items_path = items_json if items_json.is_file() else (extra_items if extra_items.is_file() else None)

    if yaml_path is None and items_path is None and pgm_path is None:
        return {
            "error": "world_not_found",
            "world_id": wid,
            "worlds_dir": str(root),
            "available": _discover_world_ids(root),
        }

    meta = _load_map_yaml(yaml_path) if yaml_path else None
    items: list[Any] | None = None
    notes: list[str] = []
    if items_path and items_path.is_file():
        try:
            raw = json.loads(items_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = raw
                for it in raw:
                    if isinstance(it, dict) and it.get("note"):
                        notes.append(str(it["note"]))
            else:
                items = [raw]
        except Exception as e:
            items = [{"error": f"items_parse_failed: {e}"}]

    info: dict[str, Any] = {
        "success": True,
        "world_id": wid,
        "worlds_dir": str(root),
        "files": {
            "map_yaml": str(yaml_path) if yaml_path else None,
            "map_pgm": str(pgm_path) if pgm_path else None,
            "items_json": str(items_path) if items_path else None,
            "sdf": str(sdf_primary) if sdf_primary.is_file() else (
                str(extra_sdf) if extra_sdf.is_file() else None
            ),
        },
        "map": meta,
        "bounds": _map_bounds(meta, pgm_path) if meta else None,
        "items": items,
        "notes": notes,
        "hint": (
            "Use item x/y as map-frame landmarks. Prefer goals slightly off station pads "
            "if notes say pads are visual-only. Occupancy grid is in map.yaml/pgm."
        ),
    }
    return info


@mcp.tool()
def list_worlds() -> str:
    """List available simulation/map world ids under the repo worlds/ directory.

    Worlds typically have maps/<id>.yaml (+ .pgm) and/or items/<id>.json with
    landmark poses. Use get_map_info(world_id) for details.
    """
    root = _worlds_root()
    if not root.is_dir():
        return json.dumps(
            {
                "error": "worlds_dir_missing",
                "worlds_dir": str(root),
                "message": "Set AGENT_WORLDS_DIR or add Masterarbeit/worlds.",
            },
            indent=2,
        )
    worlds = _discover_world_ids(root)
    return json.dumps(
        {
            "success": True,
            "worlds_dir": str(root),
            "default_world": DEFAULT_WORLD_ID,
            "worlds": worlds,
            "count": len(worlds),
        },
        indent=2,
    )


@mcp.tool()
def get_map_info(world_id: str = "") -> str:
    """Get map metadata and landmark items for a world (default: AGENT_WORLD or stations).

    Returns Nav2 map.yaml fields (resolution, origin, …), approximate bounds from
    the .pgm size when available, and items/*.json landmarks (stations, notes).
    Does not return raw occupancy pixels or full SDF meshes.
    """
    wid = (world_id or "").strip() or DEFAULT_WORLD_ID
    return json.dumps(_build_map_info(wid), indent=2)

# --- ROS 2 CLI helpers -------------------------------------------------------

def _ros2_available() -> bool:
    return shutil.which("ros2") is not None


def _normalize_robot_id(robot_id: str) -> str | None:
    robot = (robot_id or "").strip().lstrip("/")
    return robot or None


def _run_ros2(
    argv: list[str],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run a ros2 CLI command. Returns {ok, returncode, stdout, stderr, error?}."""
    if not _ros2_available():
        return {
            "ok": False,
            "error": "ros2_not_found",
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    to = ROS_CLI_TIMEOUT_SEC if timeout is None else timeout
    try:
        proc = subprocess.run(
            ["ros2", *argv],
            capture_output=True,
            text=True,
            timeout=to,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": "timeout",
            "timeout_sec": to,
            "returncode": None,
            "stdout": e.stdout if isinstance(e.stdout, str) else "",
            "stderr": e.stderr if isinstance(e.stderr, str) else "",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": "execution_failed",
            "message": str(e),
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "error": None if proc.returncode == 0 else "ros2_nonzero_exit",
    }


def _load_yaml_maybe(text: str) -> Any | None:
    if not text.strip() or _yaml is None:
        return None
    try:
        return _yaml.safe_load(text)
    except Exception:
        return None


def _echo_topic_once(topic: str, *, timeout: float | None = None) -> dict[str, Any]:
    result = _run_ros2(["topic", "echo", "--once", topic], timeout=timeout)
    if result.get("error") == "ros2_not_found":
        return result
    if not result["stdout"]:
        return {
            "ok": False,
            "error": result.get("error") or "no_message",
            "topic": topic,
            "stderr": result.get("stderr", ""),
            "stdout": "",
        }
    return {
        "ok": True,
        "topic": topic,
        "stdout": result["stdout"],
        "stderr": result.get("stderr", ""),
        "parsed": _load_yaml_maybe(result["stdout"]),
    }


def _pose_from_pose_dict(pose_msg: dict) -> dict[str, float] | None:
    try:
        pos = pose_msg["position"]
        ori = pose_msg["orientation"]
        x = float(pos["x"])
        y = float(pos["y"])
        z = float(ori.get("z", 0.0))
        w = float(ori.get("w", 1.0))
        return {"x": x, "y": y, "yaw": _quaternion_to_yaw(z, w), "frame_id": "map"}
    except Exception:
        return None


def _pose_from_echo_text(text: str) -> dict[str, float] | None:
    """Fallback field scrape when YAML parse fails."""
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    ws: list[float] = []
    for line in text.splitlines():
        m = re.match(r"\s*(x|y|z|w):\s*([-+0-9.eE]+)\s*$", line)
        if not m:
            continue
        key, val = m.group(1), float(m.group(2))
        if key == "x":
            xs.append(val)
        elif key == "y":
            ys.append(val)
        elif key == "z":
            zs.append(val)
        else:
            ws.append(val)
    if not xs or not ys:
        return None
    # Prefer first position x/y; last orientation z/w.
    z = zs[-1] if zs else 0.0
    w = ws[-1] if ws else 1.0
    return {"x": xs[0], "y": ys[0], "yaw": _quaternion_to_yaw(z, w), "frame_id": "map"}


def _parse_odometry_message(text: str, parsed: Any | None = None) -> dict[str, float] | None:
    if isinstance(parsed, dict):
        try:
            pose = _pose_from_pose_dict(parsed["pose"]["pose"])
            if pose is not None:
                return pose
        except Exception:
            pass
    return _pose_from_echo_text(text)


def _parse_amcl_message(text: str, parsed: Any | None = None) -> dict[str, float] | None:
    if isinstance(parsed, dict):
        try:
            pose = _pose_from_pose_dict(parsed["pose"]["pose"])
            if pose is not None:
                return pose
        except Exception:
            pass
    return _pose_from_echo_text(text)


def _configured_robot_ids() -> list[str]:
    try:
        from .config import ROBOT_IDS

        return list(ROBOT_IDS)
    except Exception:
        return []


def _discover_robot_ids() -> dict[str, Any]:
    """Live namespaces from topic list; fall back to config.ROBOT_IDS."""
    fallback = _configured_robot_ids()
    if not _ros2_available():
        return {
            "robots": fallback,
            "source": "config_fallback",
            "error": "ros2_not_found",
        }
    result = _run_ros2(["topic", "list"], timeout=ROS_CLI_TIMEOUT_SEC)
    if not result["ok"]:
        return {
            "robots": fallback,
            "source": "config_fallback",
            "error": result.get("error") or "topic_list_failed",
            "stderr": _clip(result.get("stderr", ""), 400),
        }
    found: list[str] = []
    for line in result["stdout"].splitlines():
        m = _ROBOT_STATE_RE.match(line.strip())
        if m:
            rid = m.group(1)
            if rid not in found:
                found.append(rid)

    def _sort_key(s: str) -> tuple:
        tail = s.rsplit("_", 1)[-1]
        return (0, int(tail)) if tail.isdigit() else (1, s)

    found.sort(key=_sort_key)
    if found:
        return {"robots": found, "source": "ros_topic_list"}
    return {"robots": fallback, "source": "config_fallback", "note": "no_robot_state_topics"}


def _fetch_robot_pose(robot: str) -> dict[str, Any]:
    """Map-frame pose from robot_state, with amcl_pose fallback."""
    primary = _echo_topic_once(f"/{robot}/robot_state")
    if primary.get("ok"):
        pose = _parse_odometry_message(primary["stdout"], primary.get("parsed"))
        if pose is not None:
            return {
                "success": True,
                "robot_id": robot,
                "source": "robot_state",
                **pose,
            }
    amcl = _echo_topic_once(f"/{robot}/amcl_pose")
    if amcl.get("ok"):
        pose = _parse_amcl_message(amcl["stdout"], amcl.get("parsed"))
        if pose is not None:
            return {
                "success": True,
                "robot_id": robot,
                "source": "amcl_pose",
                **pose,
            }
    err = primary.get("error") or amcl.get("error") or "pose_unavailable"
    return {
        "success": False,
        "error": err,
        "robot_id": robot,
        "robot_state_stderr": _clip(str(primary.get("stderr", "")), 200),
        "amcl_pose_stderr": _clip(str(amcl.get("stderr", "")), 200),
    }


def _parse_laser_snapshot(text: str, parsed: Any | None = None) -> dict[str, Any] | None:
    angle_min = None
    angle_inc = None
    ranges: list[float] = []

    if isinstance(parsed, dict):
        try:
            angle_min = float(parsed["angle_min"])
            angle_inc = float(parsed["angle_increment"])
            raw = parsed.get("ranges") or []
            ranges = []
            for r in raw:
                try:
                    ranges.append(float(r))
                except (TypeError, ValueError):
                    continue
        except Exception:
            ranges = []

    def _yaml_float(key: str) -> float | None:
        m = re.search(
            rf"(?m)^\s*{re.escape(key)}:\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$",
            text,
        )
        return float(m.group(1)) if m else None

    if angle_min is None:
        angle_min = _yaml_float("angle_min")
    if angle_inc is None:
        angle_inc = _yaml_float("angle_increment")

    if not ranges:
        m = _RANGES_RE.search(text)
        if m:
            for tok in re.split(r"[,\s]+", m.group(1).strip()):
                if not tok or tok.lower() in {"nan", ".nan", "-.nan"}:
                    continue
                try:
                    ranges.append(float(tok))
                except ValueError:
                    continue

    # ros2 topic echo usually prints: ranges:\n- 1.2\n- 1.3\n...
    if not ranges:
        in_ranges = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ranges:"):
                rest = stripped[len("ranges:") :].strip()
                in_ranges = True
                if rest.startswith("[") and "]" in rest:
                    inner = rest.strip("[]")
                    for tok in re.split(r"[,\s]+", inner):
                        if not tok or tok.lower() in {"nan", ".nan", "-.nan"}:
                            continue
                        try:
                            ranges.append(float(tok))
                        except ValueError:
                            pass
                    break
                continue
            if not in_ranges:
                continue
            if stripped.startswith("-"):
                tok = stripped[1:].strip()
                if tok.lower() in {"nan", ".nan", "-.nan", ""}:
                    continue
                try:
                    ranges.append(float(tok))
                except ValueError:
                    continue
            elif stripped and not stripped.startswith("#"):
                if re.match(r"^[A-Za-z_][\w]*:", stripped):
                    break

    if angle_min is None or angle_inc is None or not ranges:
        return None

    finite: list[tuple[int, float]] = []
    for i, r in enumerate(ranges):
        if math.isfinite(r) and r > 0.0:
            finite.append((i, r))
    if not finite:
        return {
            "min_range": None,
            "min_angle_deg": None,
            "front_min": None,
            "left_min": None,
            "right_min": None,
            "finite_count": 0,
            "range_count": len(ranges),
        }

    min_i, min_r = min(finite, key=lambda t: t[1])
    min_angle = angle_min + min_i * angle_inc

    def _sector_min(lo_deg: float, hi_deg: float) -> float | None:
        lo = math.radians(lo_deg)
        hi = math.radians(hi_deg)
        vals = [
            r
            for i, r in finite
            if lo <= (angle_min + i * angle_inc) <= hi
        ]
        return round(min(vals), 3) if vals else None

    return {
        "min_range": round(min_r, 3),
        "min_angle_deg": round(math.degrees(min_angle), 1),
        "front_min": _sector_min(-30, 30),
        "left_min": _sector_min(30, 90),
        "right_min": _sector_min(-90, -30),
        "finite_count": len(finite),
        "range_count": len(ranges),
    }


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

    Event types include box_missing and nav_aborted.
    Use len(events) + since_index as the next since_index to poll incrementally.
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
    """Get one station by id, including whether its box is still available.

    Accepts ids like station_A / station_C, or short forms A / C / 'Station C'.
    Unknown ids emit a station_not_found event.
    """
    station = _find_station(station_id)
    if station is None:
        return _tool_error(
            "station_not_found",
            error="station_not_found",
            tool="get_station",
            station_id=station_id,
            allowed=_station_ids(),
            hint="Use station_A..station_D (or short A/B/C/D).",
        )
    return json.dumps(station, indent=2)


@mcp.tool()
def get_held_boxes() -> str:
    """Return which robot currently holds which box (robot_id -> box_id or null)."""
    return json.dumps(HELD_BY, indent=2)


@mcp.tool()
def pickup_box(robot_id: str, station_id: str) -> str:
    """Pick up the box at a station for this robot.

    On failure (missing box, wrong station, already holding), emits an MCP event
    so conflict-based peers can open negotiation.
    """
    robot = robot_id.strip()
    with _STATE_LOCK:
        station = _find_station(station_id)
        if station is None:
            return _tool_error(
                "station_not_found",
                error="station_not_found",
                tool="pickup_box",
                robot_id=robot,
                station_id=station_id,
                allowed=_station_ids(),
                hint="Use station_A..station_D (or short A/B/C/D).",
            )

        if HELD_BY.get(robot):
            return _tool_error(
                "robot_already_holding",
                error="robot_already_holding",
                tool="pickup_box",
                robot_id=robot,
                station_id=station_id,
                box_id=HELD_BY[robot],
            )

        box_id = station.get("box_id")
        if not station.get("available") or not box_id:
            return _tool_error(
                "box_missing",
                error="box_missing",
                tool="pickup_box",
                station_id=station_id,
                robot_id=robot,
                expected_box=box_id or station.get("last_box_id"),
                message=f"No box available at {station_id} for {robot}",
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
    """Drop the box the robot is holding onto a station (must be empty).

    On failure (occupied station, not holding, bad id), emits an MCP event.
    """
    robot = robot_id.strip()
    with _STATE_LOCK:
        station = _find_station(station_id)
        if station is None:
            return _tool_error(
                "station_not_found",
                error="station_not_found",
                tool="drop_box",
                robot_id=robot,
                station_id=station_id,
                allowed=_station_ids(),
                hint="Use station_A..station_D (or short A/B/C/D).",
            )

        box_id = HELD_BY.get(robot)
        if not box_id:
            return _tool_error(
                "robot_not_holding",
                error="robot_not_holding",
                tool="drop_box",
                robot_id=robot,
                station_id=station_id,
            )

        if station.get("box_id") or station.get("available"):
            return _tool_error(
                "station_occupied",
                error="station_occupied",
                tool="drop_box",
                robot_id=robot,
                station_id=station_id,
                box_id=station.get("box_id"),
                message=f"Station {station_id} already has a box; cannot drop.",
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
    """Reset stations and held boxes from items/{AGENT_WORLD}.json (or built-in defaults)."""
    _apply_stations_for_world(DEFAULT_WORLD_ID)
    return json.dumps(
        {
            "success": True,
            "world_id": DEFAULT_WORLD_ID,
            "stations": STATIONS,
        },
        indent=2,
    )


# --- Remroc / ROS 2 awareness ------------------------------------------------

@mcp.tool()
def list_robots() -> str:
    """List remroc robot namespaces (SmallDeliveryRobot_*).

    Prefers live ROS topic discovery (`/*/robot_state`); falls back to
    configured AGENT_COUNT fleet ids when ROS is unavailable.
    """
    return json.dumps(_discover_robot_ids(), indent=2)


def _get_robot_pose_sync(robot_id: str) -> str:
    robot = _normalize_robot_id(robot_id)
    if not robot:
        return _tool_error(
            "pose_failed",
            error="missing_robot_id",
            tool="get_robot_pose",
        )
    if not _ros2_available():
        return _tool_error(
            "pose_failed",
            error="ros2_not_found",
            tool="get_robot_pose",
            robot_id=robot,
        )
    pose = _fetch_robot_pose(robot)
    if not pose.get("success"):
        return _tool_error(
            "pose_failed",
            error=str(pose.get("error") or "pose_unavailable"),
            tool="get_robot_pose",
            robot_id=robot,
            **{k: v for k, v in pose.items() if k != "error"},
        )
    return json.dumps(pose, indent=2)


@mcp.tool()
async def get_robot_pose(robot_id: str) -> str:
    """Get map-frame pose (x, y, yaw) for one robot from /{robot}/robot_state.

    Falls back to /{robot}/amcl_pose if robot_state is unavailable.
    Failures emit a pose_failed MCP event.
    Async so ROS topic echoes do not block other robots' tools.
    """
    return await asyncio.to_thread(_get_robot_pose_sync, robot_id)


def _get_all_robot_poses_sync() -> str:
    discovery = _discover_robot_ids()
    robots = discovery.get("robots") or []
    poses = []
    for rid in robots:
        poses.append(_fetch_robot_pose(rid))
    return json.dumps(
        {
            "discovery": {k: discovery[k] for k in discovery if k != "robots"},
            "robots": robots,
            "poses": poses,
        },
        indent=2,
    )


@mcp.tool()
async def get_all_robot_poses() -> str:
    """Get map-frame poses for every discovered (or configured) remroc robot."""
    return await asyncio.to_thread(_get_all_robot_poses_sync)


def _distance_to_station_sync(robot_id: str, station_id: str) -> str:
    robot = _normalize_robot_id(robot_id)
    if not robot:
        return _tool_error(
            "pose_failed",
            error="missing_robot_id",
            tool="distance_to_station",
        )
    station = _find_station(station_id)
    if station is None:
        return _tool_error(
            "station_not_found",
            error="station_not_found",
            tool="distance_to_station",
            robot_id=robot,
            station_id=station_id,
            allowed=_station_ids(),
            hint="Use station_A..station_D (or short A/B/C/D).",
        )
    if not _ros2_available():
        return _tool_error(
            "pose_failed",
            error="ros2_not_found",
            tool="distance_to_station",
            robot_id=robot,
            station_id=station_id,
        )
    pose = _fetch_robot_pose(robot)
    if not pose.get("success"):
        return _tool_error(
            "pose_failed",
            error=str(pose.get("error") or "pose_unavailable"),
            tool="distance_to_station",
            robot_id=robot,
            station_id=station_id,
        )
    dx = float(pose["x"]) - float(station["x"])
    dy = float(pose["y"]) - float(station["y"])
    dist = math.hypot(dx, dy)
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "station_id": station["id"],
            "distance_m": round(dist, 3),
            "robot_xy": {"x": pose["x"], "y": pose["y"]},
            "station_xy": {"x": station["x"], "y": station["y"]},
        },
        indent=2,
    )


@mcp.tool()
async def distance_to_station(robot_id: str, station_id: str) -> str:
    """Euclidean distance in map frame from the robot's current pose to a station.

    station_id accepts station_A / A / 'Station A' (same aliases as get_station).
    Failures emit an MCP event.
    """
    return await asyncio.to_thread(_distance_to_station_sync, robot_id, station_id)


def _approach_pose(station: dict, robot_x: float, robot_y: float, offset_m: float = 1.2) -> dict[str, float]:
    """Point off the station pad toward the robot (pads visual-only; keep clear for r≈0.6)."""
    sx = float(station["x"])
    sy = float(station["y"])
    dx = float(robot_x) - sx
    dy = float(robot_y) - sy
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return {"x": sx + offset_m, "y": sy, "yaw": 0.0}
    ux, uy = dx / dist, dy / dist
    return {
        "x": round(sx + offset_m * ux, 3),
        "y": round(sy + offset_m * uy, 3),
        "yaw": 0.0,
    }


def _rank_stations_by_distance_sync(robot_id: str) -> str:
    robot = _normalize_robot_id(robot_id)
    if not robot:
        return json.dumps({"error": "missing_robot_id"})
    if not _ros2_available():
        return json.dumps({"error": "ros2_not_found", "robot_id": robot})
    pose = _fetch_robot_pose(robot)
    if not pose.get("success"):
        return json.dumps(pose, indent=2)

    rx, ry = float(pose["x"]), float(pose["y"])
    ranked: list[dict[str, Any]] = []
    for station in STATIONS:
        dist = math.hypot(rx - float(station["x"]), ry - float(station["y"]))
        approach = _approach_pose(station, rx, ry)
        ranked.append(
            {
                "station_id": station["id"],
                "name": station.get("name"),
                "distance_m": round(dist, 3),
                "station_xy": {"x": station["x"], "y": station["y"]},
                "navigate_xy": approach,
                "box_id": station.get("box_id"),
                "available": station.get("available"),
            }
        )
    ranked.sort(key=lambda r: r["distance_m"], reverse=True)
    farthest = ranked[0] if ranked else None
    nearest = ranked[-1] if ranked else None
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "robot_pose": {"x": rx, "y": ry, "yaw": pose["yaw"], "source": pose.get("source")},
            "farthest": farthest,
            "nearest": nearest,
            "stations_farthest_first": ranked,
            "next_step": (
                "Call navigate_to_pose with farthest.navigate_xy (or your chosen "
                "station's navigate_xy). Do not call get_peer_distances unless "
                "navigation fails. Do not re-rank unless navigation fails."
            ),
        },
        indent=2,
    )


@mcp.tool()
async def rank_stations_by_distance(robot_id: str) -> str:
    """One-shot: read pose, rank all stations by distance, suggest nav goals.

    Prefer this over calling get_robot_pose + list_stations + distance_to_station
    repeatedly. Returns farthest/nearest and approach poses slightly off each pad.
    Then call navigate_to_pose to the chosen approach x/y. Only if navigation fails,
    use get_peer_distances and/or drive_distance to clear other robots.
    """
    return await asyncio.to_thread(_rank_stations_by_distance_sync, robot_id)


def _fleet_robot_ids(include: str | None = None) -> list[str]:
    configured = _configured_robot_ids()
    discovered = _discover_robot_ids().get("robots") or []
    fleet: list[str] = []
    for rid in list(configured) + list(discovered):
        if rid not in fleet:
            fleet.append(rid)
    if include and include not in fleet:
        fleet.append(include)
    return fleet


def _compute_peer_distances(robot: str) -> dict[str, Any]:
    """Live distances from robot to every other fleet member."""
    if not _ros2_available():
        return {"error": "ros2_not_found", "robot_id": robot}

    self_pose = _fetch_robot_pose(robot)
    if not self_pose.get("success"):
        return self_pose

    sx, sy = float(self_pose["x"]), float(self_pose["y"])
    peers: list[dict[str, Any]] = []
    for rid in _fleet_robot_ids(include=robot):
        if rid == robot:
            continue
        other = _fetch_robot_pose(rid)
        if not other.get("success"):
            peers.append({"robot_id": rid, "error": other.get("error")})
            continue
        dist = math.hypot(float(other["x"]) - sx, float(other["y"]) - sy)
        peers.append(
            {
                "robot_id": rid,
                "distance_m": round(dist, 3),
                "x": other["x"],
                "y": other["y"],
                "yaw": other["yaw"],
            }
        )
    peers.sort(key=lambda p: p.get("distance_m", 1e9))
    closest = next((p for p in peers if "distance_m" in p), None)
    return {
        "success": True,
        "robot_id": robot,
        "self": {"x": sx, "y": sy, "yaw": self_pose["yaw"]},
        "peers_closest_first": peers,
        "closest_peer": closest,
        "peer_count": len(peers),
    }


def _nav_blocker_diagnosis(
    robot: str,
    goal_x: float,
    goal_y: float,
    *,
    radius_m: float | None = None,
) -> dict[str, Any]:
    """After Nav2 fails, detect which peer is likely blocking (near self or goal)."""
    radius = float(NAV_BLOCKER_RADIUS_M if radius_m is None else radius_m)
    peers_info = _compute_peer_distances(robot)
    if not peers_info.get("success"):
        return {
            "blocker_check": "unavailable",
            "blocker_error": peers_info.get("error") or peers_info,
            "message": (
                f"Navigation failed for {robot}, but peer positions could not be checked."
            ),
        }

    nearby: list[dict[str, Any]] = []
    for peer in peers_info.get("peers_closest_first") or []:
        if "distance_m" not in peer:
            continue
        dist_self = float(peer["distance_m"])
        dist_goal = math.hypot(
            float(peer["x"]) - float(goal_x), float(peer["y"]) - float(goal_y)
        )
        near_self = dist_self <= radius
        near_goal = dist_goal <= radius
        if not (near_self or near_goal):
            continue
        where = (
            "near_self_and_goal"
            if near_self and near_goal
            else ("near_self" if near_self else "near_goal")
        )
        nearby.append(
            {
                "robot_id": peer["robot_id"],
                "distance_to_self_m": round(dist_self, 3),
                "distance_to_goal_m": round(dist_goal, 3),
                "where": where,
                "x": peer["x"],
                "y": peer["y"],
                "yaw": peer["yaw"],
            }
        )

    nearby.sort(key=lambda p: (p["distance_to_self_m"], p["distance_to_goal_m"]))
    closest = peers_info.get("closest_peer")
    if not nearby:
        msg = (
            f"Navigation failed for {robot}. No other robot within {radius} m of "
            f"this robot or the goal"
        )
        if closest and "distance_m" in closest:
            msg += (
                f" (closest peer {closest['robot_id']} is "
                f"{closest['distance_m']} m away)."
            )
        else:
            msg += "."
        return {
            "blocker_check": "no_nearby_robot",
            "blocker_radius_m": radius,
            "blocking_robot": None,
            "nearby_robots": [],
            "closest_peer": closest,
            "message": msg,
            "recovery": (
                "Retry navigate_to_pose, or call drive_distance then retry. "
                "Optionally call get_peer_distances for a full fleet snapshot."
            ),
        }

    blocker = nearby[0]
    bid = blocker["robot_id"]
    if blocker["where"] == "near_goal":
        cause = (
            f"Navigation failed because {bid} is in the way near the goal "
            f"({blocker['distance_to_goal_m']} m from goal, "
            f"{blocker['distance_to_self_m']} m from {robot})."
        )
    elif blocker["where"] == "near_self_and_goal":
        cause = (
            f"Navigation failed because {bid} is in the way "
            f"({blocker['distance_to_self_m']} m from {robot}, "
            f"{blocker['distance_to_goal_m']} m from goal)."
        )
    else:
        cause = (
            f"Navigation failed because {bid} is in the way "
            f"({blocker['distance_to_self_m']} m from {robot})."
        )

    return {
        "blocker_check": "robot_in_the_way",
        "blocker_radius_m": radius,
        "blocking_robot": bid,
        "blocking_robot_detail": blocker,
        "nearby_robots": nearby,
        "closest_peer": closest,
        "message": cause,
        "recovery": (
            f"Call drive_distance to move aside of {bid} "
            f"(e.g. 1.0 m at 90 or -90 deg), then retry navigate_to_pose. "
            f"Or wait for {bid} to move."
        ),
    }


@mcp.tool()
async def get_peer_distances(robot_id: str) -> str:
    """Distances from this robot to EVERY other fleet robot (map frame).

    Use ONLY after navigate_to_pose fails (peers are the usual cause). Returns all
    peers sorted closest-first. Do not call this before the first navigation attempt.
    """
    return await asyncio.to_thread(_get_peer_distances_sync, robot_id)


def _get_peer_distances_sync(robot_id: str) -> str:
    robot = _normalize_robot_id(robot_id)
    if not robot:
        return json.dumps({"error": "missing_robot_id"})
    info = _compute_peer_distances(robot)
    if not info.get("success"):
        return json.dumps(info, indent=2)
    info["hint"] = (
        "If closest_peer is nearby, call drive_distance to move aside "
        "(e.g. 1 m at ±90 deg), then retry navigate_to_pose."
    )
    return json.dumps(info, indent=2)


def _twist_yaml(linear_x: float, angular_z: float = 0.0) -> str:
    return (
        "{linear: {x: "
        f"{float(linear_x)}"
        ", y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: "
        f"{float(angular_z)}"
        "}}"
    )


def _publish_cmd_vel(robot: str, linear_x: float, angular_z: float = 0.0) -> dict[str, Any]:
    topic = f"/{robot}/cmd_vel"
    return _run_ros2(
        [
            "topic",
            "pub",
            "--once",
            topic,
            "geometry_msgs/msg/Twist",
            _twist_yaml(linear_x, angular_z),
        ],
        timeout=ROS_CLI_TIMEOUT_SEC,
    )


def _stop_cmd_vel(robot: str, *, duration_sec: float = 0.6, rate_hz: float = 20.0) -> dict[str, Any]:
    """Gazebo latches the last Twist — stream zeros so motion actually stops."""
    if not _ros2_available():
        return {"ok": False, "error": "ros2_not_found"}
    topic = f"/{robot}/cmd_vel"
    rate = max(5, int(round(rate_hz)))
    duration_sec = max(0.2, float(duration_sec))
    proc = subprocess.Popen(
        [
            "ros2",
            "topic",
            "pub",
            "-r",
            str(rate),
            topic,
            "geometry_msgs/msg/Twist",
            _twist_yaml(0.0, 0.0),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    try:
        proc.wait(timeout=duration_sec)
        return {"ok": proc.returncode in (0, None), "mode": "zero_rate_pub"}
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        # One final --once zero after killing the rate publisher.
        once = _publish_cmd_vel(robot, 0.0, 0.0)
        return {
            "ok": bool(once.get("ok")),
            "mode": "zero_rate_pub",
            "final_once": once,
        }
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": "execution_failed", "message": str(e)}


def _stream_cmd_vel(
    robot: str,
    *,
    linear_x: float,
    angular_z: float,
    duration_sec: float,
    rate_hz: float = 20.0,
) -> dict[str, Any]:
    """Publish cmd_vel at a steady rate for duration_sec, then hard-stop."""
    duration_sec = max(0.0, float(duration_sec))
    topic = f"/{robot}/cmd_vel"
    rate = max(5, int(round(rate_hz)))
    stream: dict[str, Any] = {"ok": True, "mode": "rate_pub"}

    if duration_sec > 0.0:
        if not _ros2_available():
            return {"ok": False, "error": "ros2_not_found"}
        cmd = [
            "ros2",
            "topic",
            "pub",
            "-r",
            str(rate),
            topic,
            "geometry_msgs/msg/Twist",
            _twist_yaml(linear_x, angular_z),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=duration_sec)
            # Publisher exited early (unusual); treat as soft failure only if non-zero.
            stream["ok"] = proc.returncode in (0, None)
            stream["returncode"] = proc.returncode
        except subprocess.TimeoutExpired:
            # Expected: we only wanted to stream for duration_sec.
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
            stream["ok"] = True
            stream["stopped"] = "timeout"
        except Exception as e:
            proc.kill()
            stream = {"ok": False, "error": "execution_failed", "message": str(e)}

    stop = _stop_cmd_vel(robot)
    return {
        "ok": bool(stream.get("ok")) and bool(stop.get("ok")),
        "stream": stream,
        "stop": stop,
    }


def _drive_until_distance(
    robot: str,
    *,
    distance_m: float,
    speed_mps: float,
    rate_hz: float = 20.0,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Drive forward for distance_m via timed cmd_vel stream, then hard-stop.

    Gazebo latches Twist commands, so we stream at a steady rate and always end
    with a zero-velocity burst. Pose is sampled only for reporting.
    """
    dist = abs(float(distance_m))
    speed = abs(float(speed_mps))
    duration = dist / max(speed, 0.05)
    if timeout_sec is not None:
        duration = min(duration, float(timeout_sec))

    start = _fetch_robot_pose(robot)
    timed = _stream_cmd_vel(
        robot,
        linear_x=speed,
        angular_z=0.0,
        duration_sec=duration,
        rate_hz=rate_hz,
    )
    # Extra stop after stream (stream already stops, but be defensive).
    stop = _stop_cmd_vel(robot, duration_sec=0.5)
    traveled = None
    end = _fetch_robot_pose(robot)
    if start.get("success") and end.get("success"):
        traveled = round(
            math.hypot(float(end["x"]) - float(start["x"]), float(end["y"]) - float(start["y"])),
            3,
        )
    return {
        "ok": bool(timed.get("ok")) and bool(stop.get("ok")),
        "pose_tracking": "timed_open_loop",
        "duration_sec": round(duration, 3),
        "traveled_m": traveled,
        "target_m": dist,
        "stream": timed,
        "stop": stop,
    }


@mcp.tool()
async def drive_distance(
    robot_id: str,
    distance_m: float,
    direction_deg: float = 0.0,
    speed_mps: float = 0.25,
    turn_speed_rps: float = 0.5,
) -> str:
    """Open-loop drive via cmd_vel: rotate by direction_deg, then drive distance_m.

    direction_deg is relative to current heading: 0=forward, 90=left, -90=right, 180=back.
    Streams /{robot}/cmd_vel at a steady rate for distance/speed seconds, then publishes
    zero twists (Gazebo latches the last Twist — a single stop is not enough).
    Use after navigate_to_pose fails to clear another robot, then retry navigation.
    distance_m must be positive; use direction_deg=180 to reverse.
    Failures emit a drive_failed MCP event.
    Async so it does not block other robots' MCP tools while cmd_vel runs.
    """
    return await asyncio.to_thread(
        _drive_distance_sync,
        robot_id,
        distance_m,
        direction_deg,
        speed_mps,
        turn_speed_rps,
    )


def _drive_distance_sync(
    robot_id: str,
    distance_m: float,
    direction_deg: float = 0.0,
    speed_mps: float = 0.25,
    turn_speed_rps: float = 0.5,
) -> str:
    """Blocking cmd_vel drive (worker thread)."""
    robot = _normalize_robot_id(robot_id)
    if not robot:
        return _tool_error(
            "drive_failed",
            error="missing_robot_id",
            tool="drive_distance",
        )
    if not _ros2_available():
        return _tool_error(
            "drive_failed",
            error="ros2_not_found",
            tool="drive_distance",
            robot_id=robot,
        )

    dist = abs(float(distance_m))
    if dist < 1e-3:
        return _tool_error(
            "drive_failed",
            error="distance_too_small",
            tool="drive_distance",
            robot_id=robot,
            distance_m=distance_m,
        )

    speed = abs(float(speed_mps))
    if speed < 0.05:
        speed = 0.05
    if speed > 0.6:
        speed = 0.6

    turn_speed = abs(float(turn_speed_rps))
    if turn_speed < 0.1:
        turn_speed = 0.1
    if turn_speed > 1.2:
        turn_speed = 1.2

    yaw_off = math.radians(float(direction_deg))
    yaw_off = (yaw_off + math.pi) % (2 * math.pi) - math.pi

    pose_before = _fetch_robot_pose(robot)
    steps: list[dict[str, Any]] = []

    if abs(yaw_off) > math.radians(5.0):
        turn_dur = abs(yaw_off) / turn_speed
        ang = turn_speed if yaw_off > 0 else -turn_speed
        turn_res = _stream_cmd_vel(
            robot, linear_x=0.0, angular_z=ang, duration_sec=turn_dur, rate_hz=20.0
        )
        steps.append(
            {
                "phase": "rotate",
                "yaw_offset_deg": float(direction_deg),
                "duration_sec": round(turn_dur, 3),
                "ok": bool(turn_res.get("ok")),
            }
        )
        if turn_res.get("error") == "ros2_not_found":
            return _tool_error(
                "drive_failed",
                error="ros2_not_found",
                tool="drive_distance",
                robot_id=robot,
                steps=steps,
            )

    drive_res = _drive_until_distance(robot, distance_m=dist, speed_mps=speed, rate_hz=20.0)
    steps.append(
        {
            "phase": "drive",
            "distance_m": dist,
            "speed_mps": speed,
            "traveled_m": drive_res.get("traveled_m"),
            "pose_tracking": drive_res.get("pose_tracking"),
            "ok": bool(drive_res.get("ok")),
        }
    )
    if drive_res.get("error") == "ros2_not_found":
        return _tool_error(
            "drive_failed",
            error="ros2_not_found",
            tool="drive_distance",
            robot_id=robot,
            steps=steps,
        )

    # Final hard stop in case Nav2 or another node left a residual twist.
    final_stop = _stop_cmd_vel(robot)
    pose_after = _fetch_robot_pose(robot)
    ok = all(s.get("ok") for s in steps) and bool(final_stop.get("ok"))
    traveled = None
    if pose_before.get("success") and pose_after.get("success"):
        traveled = round(
            math.hypot(
                float(pose_after["x"]) - float(pose_before["x"]),
                float(pose_after["y"]) - float(pose_before["y"]),
            ),
            3,
        )
    if not ok:
        return _tool_error(
            "drive_failed",
            error="drive_incomplete",
            tool="drive_distance",
            robot_id=robot,
            requested={
                "distance_m": dist,
                "direction_deg": float(direction_deg),
                "speed_mps": speed,
            },
            traveled_m=traveled,
            steps=steps,
            message=f"{robot} drive_distance did not complete cleanly",
        )
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "requested": {
                "distance_m": dist,
                "direction_deg": float(direction_deg),
                "speed_mps": speed,
            },
            "traveled_m": traveled,
            "steps": steps,
            "pose_before": pose_before if pose_before.get("success") else pose_before,
            "pose_after": pose_after if pose_after.get("success") else pose_after,
            "message": (
                f"{robot} drove ~{traveled if traveled is not None else dist} m "
                f"(requested {dist} m) via cmd_vel and stopped"
            ),
        },
        indent=2,
    )


def _get_laser_snapshot_sync(robot_id: str) -> str:
    robot = _normalize_robot_id(robot_id)
    if not robot:
        return _tool_error(
            "laser_failed",
            error="missing_robot_id",
            tool="get_laser_snapshot",
        )
    if not _ros2_available():
        return _tool_error(
            "laser_failed",
            error="ros2_not_found",
            tool="get_laser_snapshot",
            robot_id=robot,
        )
    echo = _echo_topic_once(f"/{robot}/laser_scan")
    if not echo.get("ok"):
        return _tool_error(
            "laser_failed",
            error=str(echo.get("error") or "laser_unavailable"),
            tool="get_laser_snapshot",
            robot_id=robot,
            stderr=_clip(str(echo.get("stderr", "")), 300),
        )
    snap = _parse_laser_snapshot(echo["stdout"], echo.get("parsed"))
    if snap is None:
        return _tool_error(
            "laser_failed",
            error="laser_parse_failed",
            tool="get_laser_snapshot",
            robot_id=robot,
            stdout=_clip(echo["stdout"], 400),
        )
    return json.dumps({"success": True, "robot_id": robot, **snap}, indent=2)


@mcp.tool()
async def get_laser_snapshot(robot_id: str) -> str:
    """Compact obstacle summary from /{robot}/laser_scan (not the full scan).

    Returns min range, angle of closest hit, and front/left/right sector mins.
    Failures emit a laser_failed MCP event.
    """
    return await asyncio.to_thread(_get_laser_snapshot_sync, robot_id)


# --- Navigation (ROS 2 Nav2) -------------------------------------------------

def _navigate_to_pose_sync(robot_id: str, x: float, y: float, yaw: float = 0.0) -> str:
    """Blocking Nav2 NavigateToPose (runs in a worker thread via navigate_to_pose)."""
    robot = robot_id.strip()
    if not robot:
        return json.dumps({"error": "missing_robot_id"})

    if not _ros2_available():
        event = _emit_event(
            "nav_aborted",
            robot_id=robot,
            x=x,
            y=y,
            yaw=yaw,
            reason="ros2_not_found",
        )
        return json.dumps({"error": "ros2_not_found", "event": event})

    # ros2 action send_goal expects YAML (not JSON). JSON double-quotes often
    # parse incorrectly and Nav2 then aborts on a bad/empty goal.
    # Brace nesting must match: pose.pose = {position: {...}, orientation: {...}}
    orientation = _yaw_to_quaternion(float(yaw))
    goal = (
        "{pose: {"
        "header: {frame_id: 'map'}, "
        "pose: {"
        f"position: {{x: {float(x)}, y: {float(y)}, z: 0.0}}, "
        "orientation: {"
        f"x: {float(orientation['x'])}, y: {float(orientation['y'])}, "
        f"z: {float(orientation['z'])}, w: {float(orientation['w'])}"
        "}}}}"
    )
    action_name = f"/{robot}/navigate_to_pose"
    result = _run_ros2(
        [
            "action",
            "send_goal",
            action_name,
            "nav2_msgs/action/NavigateToPose",
            goal,
        ],
        timeout=NAV_TIMEOUT_SEC,
    )

    def _fail_payload(**extra: Any) -> str:
        diagnosis = _nav_blocker_diagnosis(robot, float(x), float(y))
        event = _emit_event(
            "nav_aborted",
            robot_id=robot,
            x=x,
            y=y,
            yaw=yaw,
            reason=extra.get("nav_reason") or extra.get("reason") or extra.get("error"),
            status=extra.get("status"),
            blocking_robot=diagnosis.get("blocking_robot"),
            blocker_check=diagnosis.get("blocker_check"),
            timeout_sec=extra.get("timeout_sec"),
            returncode=extra.get("returncode"),
        )
        payload = {
            "robot_id": robot,
            "x": x,
            "y": y,
            "yaw": yaw,
            **extra,
            **diagnosis,
            "event": event,
        }
        if diagnosis.get("message"):
            payload["message"] = diagnosis["message"]
        return json.dumps(payload, indent=2)

    if result.get("error") == "timeout":
        return _fail_payload(
            error="timeout",
            tool="navigate_to_pose",
            timeout_sec=NAV_TIMEOUT_SEC,
            nav_reason="timeout",
        )
    if result.get("error") == "execution_failed":
        return _fail_payload(
            error="execution_failed",
            tool="navigate_to_pose",
            nav_reason=result.get("message"),
        )

    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    status, reason = _nav_outcome(stdout, stderr, result.get("returncode") or 0)

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

    return _fail_payload(
        error="nav_aborted" if status == "aborted" else "nav_failed",
        status=status,
        reason=reason,
        nav_reason=reason,
        returncode=result.get("returncode"),
        stderr=_clip(stderr, 400),
        stdout=_clip(stdout, 400),
    )


@mcp.tool()
async def navigate_to_pose(robot_id: str, x: float, y: float, yaw: float = 0.0) -> str:
    """Send a Nav2 NavigateToPose goal for a robot and wait for the result.

    Runs (conceptually):
      ros2 action send_goal /{robot_id}/navigate_to_pose nav2_msgs/action/NavigateToPose
      with map pose (x, y) and orientation from yaw (radians; default 0 => w=1).

    On abort/cancel/failure, checks for nearby peers and, if one is within
    MCP_NAV_BLOCKER_RADIUS_M of this robot or the goal, reports that specific
    robot as being in the way. Then use drive_distance and retry.

    Implemented as async so concurrent robots can navigate in parallel (the
    official MCP FastMCP runs sync tools on the event loop otherwise).
    """
    return await asyncio.to_thread(_navigate_to_pose_sync, robot_id, x, y, yaw)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent MCP server.")
    parser.add_argument("--host", default=os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FASTMCP_PORT", "8000")))
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="sse")
