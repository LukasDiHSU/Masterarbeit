from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.logging import configure_logging
from mcp.server.fastmcp.utilities.types import Image as MCPImage
import asyncio
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


configure_logging("WARNING")
mcp = FastMCP("Tools")

ROS_CLI_TIMEOUT_SEC = float(os.environ.get("MCP_ROS_CLI_TIMEOUT_SEC", "5"))
NAV_TIMEOUT_SEC = float(os.environ.get("MCP_NAV_TIMEOUT_SEC", "180"))
TOOL_TEXT_MAX_CHARS = int(os.environ.get("MCP_TOOL_TEXT_MAX_CHARS", "1200"))
DEFAULT_WORLD_ID = os.environ.get("AGENT_WORLD", "open").strip() or "open"
# ATB Q1: world origin in UTM (agent_project/launch.sh TF open → utm_32n).
ATB_UTM_ORIGIN_X = float(os.environ.get("AGENT_UTM_ORIGIN_X", "458054.71"))
ATB_UTM_ORIGIN_Y = float(os.environ.get("AGENT_UTM_ORIGIN_Y", "5429310.68"))
ATB_NAV_FRAME = os.environ.get("AGENT_NAV_FRAME", "utm_32n").strip() or "utm_32n"
ATB_SET_WAYPOINTS_SRV = os.environ.get(
    "AGENT_ATB_SET_WAYPOINTS", "/behavior_control/set_waypoints"
).strip() or "/behavior_control/set_waypoints"
ATB_ODOM_TOPIC = os.environ.get("AGENT_ATB_ODOM", "/localization/odometry").strip() or (
    "/localization/odometry"
)
ATB_NAV_EVENTS_TOPIC = os.environ.get(
    "AGENT_ATB_NAV_EVENTS", "/behavior_control/nav_events"
).strip() or "/behavior_control/nav_events"
_ATB_GOAL_REACHED_RE = re.compile(
    r"goal reached|last waypoint reached|waypoint\s+\d+\s+reached",
    re.IGNORECASE,
)
_ATB_GOAL_FAIL_RE = re.compile(r"\b(abort|aborted|nav(?:igation)? failed)\b", re.IGNORECASE)
NAV_GOAL_RADIUS_M = float(os.environ.get("MCP_NAV_GOAL_RADIUS_M", "2.5"))
CAMERA_RGB_JPEG = Path(
    os.environ.get("AGENT_CAMERA_RGB_JPEG", "/tmp/q1_camera_rgb.jpg")
).expanduser()
CAMERA_SEM_JPEG = Path(
    os.environ.get("AGENT_CAMERA_SEM_JPEG", "/tmp/q1_camera_semantic.jpg")
).expanduser()
SUMMARY_ECHO_TIMEOUT_SEC = float(os.environ.get("MCP_SUMMARY_ECHO_TIMEOUT_SEC", "12"))
ODOM_ECHO_TIMEOUT_SEC = float(os.environ.get("MCP_ODOM_ECHO_TIMEOUT_SEC", "12"))
SEMANTIC_LIDAR_TOPIC = "/q1_sensor_summary/semantic_lidar"
CAMERA_STILL_MAX_AGE_SEC = float(os.environ.get("MCP_CAMERA_STILL_MAX_AGE_SEC", "8"))

_TF_CACHE: dict[tuple[str, str], tuple[float, float, float, float, float]] = {}
_TF_MISS_UNTIL: dict[tuple[str, str], float] = {}
_TF_CACHE_LOCK = threading.Lock()

try:
    import yaml as _yaml  # type: ignore
except Exception:  # pragma: no cover
    _yaml = None


def _clip(text: str, limit: int = TOOL_TEXT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


ACTIVE_WORLD_ID: str = DEFAULT_WORLD_ID

# --- Shared event log + tour progress (Environment / peers) -----------------
EVENTS: list[dict] = []
# Ordered unique stop names confirmed via confirm_stop (e.g. container, rock).
CONFIRMED_STOPS: list[str] = []
# Latest map-frame centroids from get_semantic_lidar_objects (no invented coords).
_LAST_SEEN_OBJECTS: list[dict[str, Any]] = []
# Last successful navigate_to_pose goal in map frame (pose TF fallback).
_LAST_NAV_GOAL: dict[str, Any] | None = None
_TOUR_STATE_LOCK = threading.Lock()
_LAST_NAV_LOCK = threading.Lock()


def _emit_event(event_type: str, **payload) -> dict:
    event = {"type": event_type, "ts": time.time(), **payload}
    EVENTS.append(event)
    return event


def _tool_error(
    event_type: str,
    *,
    error: str,
    payload: dict[str, Any] | None = None,
    **extra: Any,
) -> str:
    """Return an error JSON and always append a matching MCP event for peers."""
    clean: dict[str, Any] = {}
    if payload:
        clean.update(payload)
    clean.update(extra)
    clean.pop("error", None)
    event = _emit_event(event_type, error=error, **clean)
    body = {"error": error, **clean, "event": event}
    return json.dumps(body, indent=2)


def _record_confirmed_stop(canonical: str) -> None:
    with _TOUR_STATE_LOCK:
        if canonical not in CONFIRMED_STOPS:
            CONFIRMED_STOPS.append(canonical)
    _emit_event("visit_confirmed", object=canonical)


def _record_seen_objects(objects: list[Any]) -> None:
    seen: list[dict[str, Any]] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        try:
            x = float(obj["x"])
            y = float(obj["y"])
        except (KeyError, TypeError, ValueError):
            continue
        label = str(obj.get("class") or obj.get("name") or "").strip()
        entry: dict[str, Any] = {"x": round(x, 2), "y": round(y, 2)}
        if label:
            entry["class"] = label
        if obj.get("class_id") is not None:
            entry["class_id"] = obj.get("class_id")
        seen.append(entry)
    if not seen:
        return
    with _TOUR_STATE_LOCK:
        _LAST_SEEN_OBJECTS.clear()
        _LAST_SEEN_OBJECTS.extend(seen)


def _tour_progress_payload() -> dict[str, Any]:
    with _TOUR_STATE_LOCK:
        payload: dict[str, Any] = {
            "confirmed_stops": list(CONFIRMED_STOPS),
            "last_seen_objects": [dict(o) for o in _LAST_SEEN_OBJECTS],
        }
    # Pose for Environment shared state only (not an agent tool).
    if _ros2_available():
        pose = _fetch_robot_pose(_q1_nav_id())
        if pose.get("success"):
            payload["pose"] = {
                "x": float(pose["x"]),
                "y": float(pose["y"]),
                "yaw": float(pose.get("yaw") or 0.0),
                "frame_id": pose.get("frame_id") or "map",
            }
    return payload


# Visit validation for Experiment 06 — internal only, never exposed as a list tool.
VISIT_CONFIRM_RADIUS_M = float(os.environ.get("MCP_VISIT_CONFIRM_RADIUS_M", "8.0"))
_OPEN_WORLD_VISIT_TARGETS: dict[str, dict[str, float]] = {
    "rock": {"x": 10.5, "y": 10.5, "z": 0.0, "yaw": 0.0},
    "container": {"x": -10.5, "y": -10.5, "z": 0.0, "yaw": 0.0},
    "car": {"x": -10.5, "y": 10.5, "z": 0.0, "yaw": 0.0},
    "tree": {"x": 10.5, "y": -10.5, "z": 0.0, "yaw": 0.0},
}
# Sense-tool / German labels -> canonical keys in _OPEN_WORLD_VISIT_TARGETS.
_VISIT_OBJECT_ALIASES: dict[str, str] = {
    "rock": "rock",
    "stein": "rock",
    "rock_easy": "rock",
    "40": "rock",
    "container": "container",
    "container_easy": "container",
    "58": "container",
    "car": "car",
    "auto": "car",
    "car_easy": "car",
    "12": "car",
    "tree": "tree",
    "kiefer": "tree",
    "tree_easy": "tree",
    "tree_trunk": "tree",
    "tree_crown": "tree",
    "tree_root": "tree",
    "27": "tree",
    "28": "tree",
    "62": "tree",
}


def _resolve_visit_object(name: str) -> str | None:
    """Map a sense label to a canonical visit target (rock/car/container/tree)."""
    from .goose_classes import class_id_for_name, class_name

    key = (name or "").strip().lower().replace(" ", "_")
    if not key:
        return None
    if key in _VISIT_OBJECT_ALIASES:
        return _VISIT_OBJECT_ALIASES[key]
    cid = class_id_for_name(key)
    if cid is None:
        return None
    canonical = class_name(cid)
    if canonical in _OPEN_WORLD_VISIT_TARGETS:
        return canonical
    if canonical.startswith("tree_"):
        return "tree"
    return _VISIT_OBJECT_ALIASES.get(canonical)


def _object_matches_visit(obj: dict[str, Any], canonical: str) -> bool:
    label = str(obj.get("class") or "")
    if _resolve_visit_object(label) == canonical:
        return True
    cid = obj.get("class_id")
    if cid is not None and _resolve_visit_object(str(cid)) == canonical:
        return True
    return False


# --- Worlds / map metadata (repo worlds/) ------------------------------------

def _worlds_root() -> Path:
    env = os.environ.get("AGENT_WORLDS_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "worlds"



def _safe_world_id(world_id: str) -> str | None:
    wid = (world_id or "").strip()
    if not wid or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]*", wid):
        return None
    return wid


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


def _load_pgm_pixels(pgm_path: Path) -> tuple[int, int, bytearray] | None:
    """Load binary PGM (P5) pixels. Returns (width, height, pixels) or None."""
    try:
        data = pgm_path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"P5"):
        return None
    i = 2
    parts: list[str] = []
    while len(parts) < 3 and i < len(data):
        while i < len(data) and data[i] in b" \t\r\n":
            i += 1
        if i < len(data) and data[i] == ord("#"):
            nl = data.find(b"\n", i)
            i = nl + 1 if nl >= 0 else len(data)
            continue
        j = i
        while j < len(data) and data[j] not in b" \t\r\n":
            j += 1
        parts.append(data[i:j].decode("ascii"))
        i = j
    if len(parts) < 3:
        return None
    width, height = int(parts[0]), int(parts[1])
    while i < len(data) and data[i] in b" \t\r":
        i += 1
    if i < len(data) and data[i] == 10:
        i += 1
    pixels = bytearray(data[i : i + width * height])
    if len(pixels) < width * height:
        return None
    return width, height, pixels


def _occupancy_paths(world_id: str) -> tuple[Path | None, Path | None]:
    root = _worlds_root()
    wid = _safe_world_id(world_id)
    if wid is None:
        return None, None
    maps_yaml = root / "maps" / f"{wid}.yaml"
    maps_pgm = root / "maps" / f"{wid}.pgm"
    extra = root / "extra_maps" / wid
    yaml_path = maps_yaml if maps_yaml.is_file() else (
        extra / "map.yaml" if (extra / "map.yaml").is_file() else None
    )
    pgm_path = maps_pgm if maps_pgm.is_file() else (
        extra / "map.pgm" if (extra / "map.pgm").is_file() else None
    )
    return yaml_path, pgm_path


def _walls_only_occupied(
    width: int,
    height: int,
    pixels: bytearray,
    *,
    origin_x: float,
    origin_y: float,
    resolution: float,
    wall_band_m: float = 2.0,
) -> set[tuple[int, int]]:
    """Occupied pixels on the outer wall ring. Interior blobs (objects) are dropped."""
    occ: list[tuple[int, int, float, float]] = []
    for row in range(height):
        base = row * width
        y = origin_y + (height - 1 - row) * resolution
        for col in range(width):
            if pixels[base + col] > 50:
                continue
            x = origin_x + col * resolution
            occ.append((row, col, x, y))
    if not occ:
        return set()
    x_min = min(p[2] for p in occ)
    x_max = max(p[2] for p in occ)
    y_min = min(p[3] for p in occ)
    y_max = max(p[3] for p in occ)
    walls: set[tuple[int, int]] = set()
    for row, col, x, y in occ:
        if (
            x <= x_min + wall_band_m
            or x >= x_max - wall_band_m
            or y <= y_min + wall_band_m
            or y >= y_max - wall_band_m
        ):
            walls.add((row, col))
    return walls


def _occupancy_ascii(
    width: int,
    height: int,
    wall_pixels: set[tuple[int, int]],
    pixels: bytearray,
    *,
    origin_x: float,
    origin_y: float,
    resolution: float,
    cell_m: float = 1.0,
) -> dict[str, Any]:
    """Downsampled ASCII occupancy. '#' wall, '.' free, '?' unknown. No objects."""
    cell_m = max(0.5, float(cell_m))
    x_min, y_min = origin_x, origin_y
    x_max = origin_x + width * resolution
    y_max = origin_y + height * resolution
    cols = min(48, max(1, int(round((x_max - x_min) / cell_m))))
    rows = min(48, max(1, int(round((y_max - y_min) / cell_m))))
    cell_w = (x_max - x_min) / cols
    cell_h = (y_max - y_min) / rows
    grid: list[str] = []
    for r in range(rows):
        y0 = y_max - (r + 1) * cell_h
        y1 = y_max - r * cell_h
        chars: list[str] = []
        for c in range(cols):
            x0 = x_min + c * cell_w
            x1 = x_min + (c + 1) * cell_w
            c0 = max(0, int((x0 - origin_x) / resolution))
            c1 = min(width - 1, int((x1 - origin_x) / resolution))
            r0 = max(0, int((y0 - origin_y) / resolution))
            r1 = min(height - 1, int((y1 - origin_y) / resolution))
            wall = False
            unknown = 0
            total = 0
            for pr in range(r0, r1 + 1):
                pgm_row = height - 1 - pr
                base = pgm_row * width
                for pc in range(c0, c1 + 1):
                    total += 1
                    if (pgm_row, pc) in wall_pixels:
                        wall = True
                        break
                    if pixels[base + pc] == 205:
                        unknown += 1
                if wall:
                    break
            if wall:
                chars.append("#")
            elif total and unknown * 2 >= total:
                chars.append("?")
            else:
                chars.append(".")
        grid.append("".join(chars))
    return {
        "cell_m": round(cell_w, 3),
        "cols": cols,
        "rows": rows,
        "x_min": round(x_min, 3),
        "y_min": round(y_min, 3),
        "x_max": round(x_max, 3),
        "y_max": round(y_max, 3),
        "legend": "# wall  . free  ? unknown. Row 0 is max y. Column 0 is min x.",
        "grid": "\n".join(grid),
    }


# --- ROS 2 CLI helpers -------------------------------------------------------

def _agent_platform() -> str:
    return (os.environ.get("AGENT_PLATFORM", "q1").strip().lower() or "q1")


def _use_atb_nav() -> bool:
    return _agent_platform() == "q1"


def _ros2_available() -> bool:
    if shutil.which("ros2") is not None:
        return True
    return _use_atb_nav() and Path("/opt/ros/jazzy/setup.bash").is_file()


def _ros2_command(argv: list[str], *, line_buffered: bool = False) -> list[str]:
    """ros2 argv, sourcing ROS/ATB when Q1 tools need iosb_nav_msgs."""
    ros2_argv = ["ros2", *argv]
    if line_buffered and shutil.which("stdbuf"):
        ros2_argv = ["stdbuf", "-oL", *ros2_argv]
    if not _use_atb_nav():
        return ros2_argv
    inner = " ".join(shlex.quote(p) for p in ros2_argv)
    script = (
        "set +u; "
        "[ -f /opt/ros/jazzy/setup.bash ] && source /opt/ros/jazzy/setup.bash; "
        "[ -f /opt/atb_hsu/setup.bash ] && source /opt/atb_hsu/setup.bash; "
        "set -u; " + inner
    )
    return ["bash", "-lc", script]


def _normalize_robot_id(robot_id: str) -> str | None:
    robot = (robot_id or "").strip().lstrip("/")
    return robot or None


def _q1_nav_id() -> str:
    return (os.environ.get("AGENT_NAV_ROBOT", "q1").strip() or "q1")


def _physical_robot(robot_id: str) -> str | None:
    """ROS namespace. All Q1 specialists share AGENT_NAV_ROBOT (default q1)."""
    if not _normalize_robot_id(robot_id):
        return None
    return _q1_nav_id()


def _map_to_utm(x: float, y: float) -> tuple[float, float]:
    return float(x) + ATB_UTM_ORIGIN_X, float(y) + ATB_UTM_ORIGIN_Y


def _utm_to_map(x: float, y: float) -> tuple[float, float]:
    return float(x) - ATB_UTM_ORIGIN_X, float(y) - ATB_UTM_ORIGIN_Y


def _looks_like_utm(x: float, y: float, frame_id: str = "") -> bool:
    """True when x/y are absolute UTM easting/northing, not open-map coords."""
    del frame_id  # header frame_id may say utm_32n even for map-local values
    return abs(float(x)) > 10000.0 or abs(float(y)) > 10000.0


def _quaternion_to_yaw(z: float, w: float, x: float = 0.0, y: float = 0.0) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _parse_tf_echo(text: str) -> tuple[float, float, float, float] | None:
    """Return (tx, ty, cos_yaw, sin_yaw) from `tf2_echo` stdout."""
    trans = None
    quat = None
    for m in re.finditer(
        r"Translation:\s*\[\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\]",
        text,
    ):
        trans = (float(m.group(1)), float(m.group(2)))
    for m in re.finditer(
        r"Quaternion:\s*\[\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\]",
        text,
    ):
        qx, qy, qz, qw = (float(m.group(i)) for i in range(1, 5))
        yaw = _quaternion_to_yaw(qz, qw, qx, qy)
        quat = (math.cos(yaw), math.sin(yaw))
    if trans is None:
        return None
    c, s = quat if quat is not None else (1.0, 0.0)
    return trans[0], trans[1], c, s


def _map_tf_parents() -> list[str]:
    """Parent frames to try for map-metre poses (same order as summarizer)."""
    frames: list[str] = []
    for raw in (
        os.environ.get("AGENT_MAP_FRAME", "").strip(),
        os.environ.get("AGENT_WORLD", "").strip(),
        "open",
        "map",
        "utm_32n",
        "utm",
    ):
        if raw and raw not in frames:
            frames.append(raw)
    return frames


def _tf_is_utm_parent(parent: str) -> bool:
    return (parent or "").strip().lower() in {"utm_32n", "utm"}


def _lookup_tf_2d(target: str, source: str) -> tuple[float, float, float, float] | None:
    """Pose of ``source`` in ``target`` as (tx, ty, cos_yaw, sin_yaw)."""
    src = (source or "").strip().strip("/")
    dst = (target or "").strip().strip("/")
    if not src or not dst:
        return None
    if src == dst:
        return 0.0, 0.0, 1.0, 0.0
    now = time.monotonic()
    key = (dst, src)
    with _TF_CACHE_LOCK:
        cached = _TF_CACHE.get(key)
        if cached and cached[4] > now:
            return cached[0], cached[1], cached[2], cached[3]
        miss_until = _TF_MISS_UNTIL.get(key, 0.0)
        if miss_until > now:
            return None
    # tf2_echo streams forever; keep partial stdout. open/local often needs >2s.
    result = _run_ros2(["run", "tf2_ros", "tf2_echo", dst, src], timeout=8.0)
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    parsed = _parse_tf_echo(text)
    with _TF_CACHE_LOCK:
        if parsed is None:
            _TF_MISS_UNTIL[key] = now + 0.5
            return None
        _TF_CACHE[key] = (*parsed, now + 5.0)
        _TF_MISS_UNTIL.pop(key, None)
    return parsed


def _apply_tf_xy(
    x: float, y: float, yaw: float, tf: tuple[float, float, float, float]
) -> tuple[float, float, float]:
    tx, ty, c, s = tf
    return c * x - s * y + tx, s * x + c * y + ty, yaw + math.atan2(s, c)


def _transform_xy_to_map(
    x: float, y: float, yaw: float, source_frame: str
) -> tuple[float, float, float] | None:
    """Apply TF source→map/open/utm (utm subtracted to open metres)."""
    src = (source_frame or "").strip().strip("/")
    if not src:
        return None
    for parent in _map_tf_parents():
        tf = _lookup_tf_2d(parent, src)
        if tf is None:
            continue
        mx, my, myaw = _apply_tf_xy(x, y, yaw, tf)
        if _tf_is_utm_parent(parent):
            mx, my = _utm_to_map(mx, my)
        return mx, my, myaw
    return None


def _pose_from_tf_map(child_frame: str) -> dict[str, float] | None:
    """Robot pose in open/map from TF (preferred over raw local odom)."""
    child = (child_frame or "").strip().strip("/") or "local"
    for parent in _map_tf_parents():
        tf = _lookup_tf_2d(parent, child)
        if tf is None:
            continue
        tx, ty, c, s = tf
        if _tf_is_utm_parent(parent):
            tx, ty = _utm_to_map(tx, ty)
        return {
            "x": float(tx),
            "y": float(ty),
            "yaw": math.atan2(s, c),
            "frame_id": "map",
        }
    return None


def _pose_to_map_frame(pose: dict[str, float], *, source: str) -> dict[str, float]:
    """Keep MCP x/y in the Gazebo/open map frame used by items.json.

    ATB ``/localization/odometry`` is always in the ``local`` frame (often a
    ~-90° yaw from ``open``). The header may say ``local`` or wrongly ``map``;
    always transform via ``open←local`` for that topic.
    """
    x, y = float(pose["x"]), float(pose["y"])
    yaw = float(pose.get("yaw") or 0.0)
    frame = str(pose.get("frame_id") or "").strip().strip("/")
    if _looks_like_utm(x, y, frame):
        x, y = _utm_to_map(x, y)
        frame = "map"
    else:
        odom_src = (source or "").strip()
        is_atb_odom = (
            odom_src == ATB_ODOM_TOPIC
            or odom_src.endswith("/localization/odometry")
            or "localization/odometry" in odom_src
        )
        if is_atb_odom:
            # Ignore lying header: ATB odom values are always local metres.
            mapped = _transform_xy_to_map(x, y, yaw, "local")
            if mapped is not None:
                x, y, yaw = mapped
                frame = "map"
            else:
                frame = "local"
        elif frame and frame.lower() not in {"map", "open", "world"}:
            mapped = _transform_xy_to_map(x, y, yaw, frame)
            if mapped is not None:
                x, y, yaw = mapped
                frame = "map"
        if frame.lower() not in {"map", "open", "world"}:
            direct = (
                _pose_from_tf_map("local")
                or _pose_from_tf_map("base_link")
                or _pose_from_tf_map("base_footprint")
            )
            if direct is not None:
                x, y, yaw = (
                    float(direct["x"]),
                    float(direct["y"]),
                    float(direct.get("yaw") or yaw),
                )
                frame = "map"
            else:
                frame = frame or "unknown"
        else:
            frame = "map"
    out = dict(pose)
    out["x"] = x
    out["y"] = y
    out["yaw"] = yaw
    out["frame_id"] = frame
    out["source"] = source
    return out


def _record_nav_goal(x: float, y: float, yaw: float = 0.0) -> None:
    with _LAST_NAV_LOCK:
        global _LAST_NAV_GOAL
        _LAST_NAV_GOAL = {
            "x": float(x),
            "y": float(y),
            "yaw": float(yaw),
            "t": time.time(),
            "frame_id": "map",
        }


def _last_nav_goal_pose(*, max_age_sec: float = 180.0) -> dict[str, Any] | None:
    with _LAST_NAV_LOCK:
        goal = dict(_LAST_NAV_GOAL) if _LAST_NAV_GOAL else None
    if not goal:
        return None
    if time.time() - float(goal.get("t") or 0.0) > max_age_sec:
        return None
    return {
        "x": float(goal["x"]),
        "y": float(goal["y"]),
        "yaw": float(goal.get("yaw") or 0.0),
        "frame_id": "map",
        "success": True,
        "source": "last_nav_goal",
        "robot_id": _q1_nav_id(),
    }


def _last_seen_centroid(canonical: str) -> tuple[float, float] | None:
    with _TOUR_STATE_LOCK:
        objects = list(_LAST_SEEN_OBJECTS)
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if not _object_matches_visit(obj, canonical):
            continue
        try:
            return float(obj["x"]), float(obj["y"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


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

    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    try:
        proc = subprocess.run(
            _ros2_command(argv),
            capture_output=True,
            text=True,
            timeout=to,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        # Streaming tools (tf2_echo) never exit; keep partial stdout.
        stdout = _as_text(e.stdout).strip()
        stderr = _as_text(e.stderr).strip()
        return {
            "ok": bool(stdout),
            "error": None if stdout else "timeout",
            "timeout_sec": to,
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
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


def _echo_topic_once(
    topic: str,
    *,
    timeout: float | None = None,
    best_effort: bool = False,
    full_length: bool = False,
) -> dict[str, Any]:
    argv = ["topic", "echo", "--once"]
    if full_length:
        argv.append("--full-length")
    if best_effort:
        argv.extend(["--qos-reliability", "best_effort"])
    argv.append(topic)
    result = _run_ros2(argv, timeout=timeout)
    if result.get("error") == "ros2_not_found":
        return result
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    combined = f"{stdout}\n{stderr}".lower()
    if "does not appear to be published" in combined:
        return {
            "ok": False,
            "error": "topic_unavailable",
            "topic": topic,
            "stderr": stderr,
            "stdout": stdout,
        }
    if not result.get("ok"):
        return {
            "ok": False,
            "error": result.get("error") or "no_message",
            "topic": topic,
            "stderr": stderr,
            "stdout": stdout,
        }
    if not stdout.strip():
        return {
            "ok": False,
            "error": result.get("error") or "no_message",
            "topic": topic,
            "stderr": stderr,
            "stdout": "",
        }
    return {
        "ok": True,
        "topic": topic,
        "stdout": stdout,
        "stderr": stderr,
        "parsed": _load_yaml_maybe(stdout),
    }


def _echo_json_string_topic(topic: str) -> dict[str, Any]:
    """Echo a std_msgs/String topic whose data field is JSON.

    ``ros2 topic echo`` YAML may leave ``data`` as a dict, a quoted JSON
    string, or an escaped JSON string. Empty objects/classes is success.
    Retries while the summarizer is still starting or a once-echo times out.
    """
    last: dict[str, Any] | None = None
    for attempt in range(4):
        echo = _echo_topic_once(
            topic, full_length=True, timeout=SUMMARY_ECHO_TIMEOUT_SEC
        )
        if echo.get("ok"):
            payload = _parse_std_msgs_json_data(
                echo.get("parsed"), echo.get("stdout") or ""
            )
            if isinstance(payload, dict):
                err = str(payload.get("error") or "")
                waiting = (not payload.get("success")) and err.startswith("waiting_for")
                if waiting:
                    last = payload
                else:
                    payload.setdefault("topic", topic)
                    return payload
            else:
                last = {
                    "success": False,
                    "error": "summary_unparsable",
                    "topic": topic,
                    "raw": _clip(str(echo.get("stdout") or ""), 400),
                }
        else:
            last = {
                "success": False,
                "error": str(echo.get("error") or "topic_unavailable"),
                "topic": topic,
                "stderr": _clip(str(echo.get("stderr", "")), 300),
            }
        time.sleep(0.7 * (attempt + 1))
    if last is None:
        return {
            "success": False,
            "error": "topic_unavailable",
            "topic": topic,
        }
    last.setdefault("topic", topic)
    return last


def _try_json_dict(text: str) -> dict[str, Any] | None:
    s = (text or "").strip()
    if not s:
        return None
    candidates = [s]
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        candidates.append(s[1:-1].strip())
    brace = s.find("{")
    end = s.rfind("}")
    if brace >= 0 and end > brace:
        candidates.append(s[brace : end + 1])
    for blob in candidates:
        if not blob:
            continue
        try:
            out = json.loads(blob)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass
        try:
            unescaped = bytes(blob, "utf-8").decode("unicode_escape")
            out = json.loads(unescaped)
            if isinstance(out, dict):
                return out
        except Exception:
            pass
    return None


def _parse_std_msgs_json_data(parsed: Any, stdout: str) -> dict[str, Any] | None:
    """Turn a std_msgs/String echo into the JSON object published on ``data``."""
    data: Any = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("data"), (dict, str)):
            data = parsed.get("data")
        elif "success" in parsed:
            return parsed
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        out = _try_json_dict(data)
        if out is not None:
            return out
    text = (stdout or "").strip()
    if text.endswith("\n---"):
        text = text[: -len("\n---")].rstrip()
    elif text.endswith("---"):
        text = text[: -len("---")].rstrip()
    m = re.search(r"(?:^|\n)data:\s*(.*)$", text, re.DOTALL)
    if m:
        raw = m.group(1).strip()
        if raw.startswith("|") or raw.startswith(">"):
            raw = raw.split("\n", 1)[-1].strip()
        out = _try_json_dict(raw)
        if out is not None:
            return out
    return _try_json_dict(text)


def _pose_from_pose_dict(pose_msg: dict) -> dict[str, float] | None:
    try:
        pos = pose_msg["position"]
        ori = pose_msg["orientation"]
        x = float(pos["x"])
        y = float(pos["y"])
        qx = float(ori.get("x", 0.0))
        qy = float(ori.get("y", 0.0))
        qz = float(ori.get("z", 0.0))
        qw = float(ori.get("w", 1.0))
        return {
            "x": x,
            "y": y,
            "yaw": _quaternion_to_yaw(qz, qw, qx, qy),
            "frame_id": "map",
        }
    except Exception:
        return None


def _pose_from_echo_text(text: str) -> dict[str, float] | None:
    """Fallback field scrape when YAML parse fails."""
    m = re.search(
        r"pose:\s*\n(?:\s+pose:\s*\n)?\s+position:\s*\n"
        r"\s+x:\s*([-+0-9.eE]+)\s*\n"
        r"\s+y:\s*([-+0-9.eE]+)\s*\n"
        r"(?:\s+z:\s*([-+0-9.eE]+)\s*\n)?"
        r"\s+orientation:\s*\n"
        r"\s+x:\s*([-+0-9.eE]+)\s*\n"
        r"\s+y:\s*([-+0-9.eE]+)\s*\n"
        r"\s+z:\s*([-+0-9.eE]+)\s*\n"
        r"\s+w:\s*([-+0-9.eE]+)",
        text,
        re.MULTILINE,
    )
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        qx, qy, qz, qw = (float(m.group(i)) for i in range(4, 8))
        return {
            "x": x,
            "y": y,
            "yaw": _quaternion_to_yaw(qz, qw, qx, qy),
            "frame_id": "map",
        }
    xs: list[float] = []
    ys: list[float] = []
    for line in text.splitlines():
        m = re.match(r"\s*(x|y):\s*([-+0-9.eE]+)\s*$", line)
        if not m:
            continue
        key, val = m.group(1), float(m.group(2))
        if key == "x":
            xs.append(val)
        elif key == "y":
            ys.append(val)
    if not xs or not ys:
        return None
    return {"x": xs[0], "y": ys[0], "yaw": 0.0, "frame_id": "map"}


def _parse_odometry_message(text: str, parsed: Any | None = None) -> dict[str, float] | None:
    if isinstance(parsed, dict):
        candidates: list[Any] = []
        pose_block = parsed.get("pose")
        if isinstance(pose_block, dict):
            inner = pose_block.get("pose")
            if isinstance(inner, dict):
                candidates.append(inner)
            if "position" in pose_block:
                candidates.append(pose_block)
        for candidate in candidates:
            pose = _pose_from_pose_dict(candidate)
            if pose is not None:
                return pose
    return _pose_from_echo_text(text)


def _header_frame_id(parsed: Any, text: str) -> str:
    if isinstance(parsed, dict):
        header = parsed.get("header")
        if isinstance(header, dict) and header.get("frame_id"):
            return str(header["frame_id"]).strip().strip("'\"")
    m = re.search(r"(?m)^\s*frame_id:\s*[\"']?([A-Za-z0-9_/\-]+)[\"']?\s*$", text)
    return m.group(1) if m else ""


def _echo_localization_odometry() -> dict[str, Any]:
    """Read one sample from /localization/odometry (raw local-frame pose)."""
    last: dict[str, Any] = {
        "success": False,
        "error": "pose_unavailable",
        "topic": ATB_ODOM_TOPIC,
    }
    for best_effort in (False, True):
        echo = _echo_topic_once(
            ATB_ODOM_TOPIC,
            best_effort=best_effort,
            timeout=ODOM_ECHO_TIMEOUT_SEC,
        )
        if not echo.get("ok"):
            last = {
                "success": False,
                "error": echo.get("error") or "pose_unavailable",
                "topic": ATB_ODOM_TOPIC,
                "stderr": _clip(str(echo.get("stderr", "")), 200),
            }
            continue
        pose = _parse_odometry_message(echo["stdout"], echo.get("parsed"))
        if pose is None:
            last = {
                "success": False,
                "error": "pose_unparsable",
                "topic": ATB_ODOM_TOPIC,
                "stdout": _clip(str(echo.get("stdout") or ""), 200),
            }
            continue
        pose["frame_id"] = _header_frame_id(
            echo.get("parsed"), echo.get("stdout") or ""
        ) or "local"
        pose["success"] = True
        pose["topic"] = ATB_ODOM_TOPIC
        pose["source"] = ATB_ODOM_TOPIC
        return pose
    return last


def _odom_to_map_pose(odom: dict[str, Any]) -> dict[str, Any]:
    """Apply open←local to odometry x/y (never treat the TF origin as the robot)."""
    x, y = float(odom["x"]), float(odom["y"])
    yaw = float(odom.get("yaw") or 0.0)
    if _looks_like_utm(x, y, str(odom.get("frame_id") or "")):
        x, y = _utm_to_map(x, y)
        out = dict(odom)
        out.update({"x": x, "y": y, "yaw": yaw, "frame_id": "map", "success": True})
        return out
    mapped = _transform_xy_to_map(x, y, yaw, "local")
    out = dict(odom)
    if mapped is None:
        out.update(
            {
                "x": x,
                "y": y,
                "yaw": yaw,
                "frame_id": str(odom.get("frame_id") or "local"),
                "success": True,
                "map_tf": "failed",
            }
        )
        return out
    mx, my, myaw = mapped
    out.update(
        {
            "x": mx,
            "y": my,
            "yaw": myaw,
            "frame_id": "map",
            "success": True,
            "odom_raw": {
                "x": round(x, 3),
                "y": round(y, 3),
                "frame_id": odom.get("frame_id"),
            },
            "source": ATB_ODOM_TOPIC,
        }
    )
    return out


def _fetch_q1_pose(robot: str) -> dict[str, Any]:
    """Map-frame pose from /localization/odometry only (transformed via open←local)."""
    last: dict[str, Any] = {
        "success": False,
        "error": "pose_unavailable",
        "robot_id": robot,
        "topic": ATB_ODOM_TOPIC,
    }
    for attempt in range(4):
        odom = _echo_localization_odometry()
        if not odom.get("success"):
            last = dict(odom)
            last["robot_id"] = robot
            time.sleep(0.5 * (attempt + 1))
            continue
        mapped = _odom_to_map_pose(odom)
        mapped["robot_id"] = robot
        mapped["success"] = True
        return mapped
    return last


def _fetch_robot_pose(robot: str) -> dict[str, Any]:
    """Map-frame pose from ATB /localization/odometry."""
    return _fetch_q1_pose(_physical_robot(robot) or robot)


# --- Events ------------------------------------------------------------------

@mcp.tool()
def get_events(since_index: int = 0) -> str:
    """Return events from the shared event log starting at since_index (0-based).

    Event types include nav_aborted and detection_conflict.
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
    """Clear the shared event log and tour progress (for a new experiment run)."""
    EVENTS.clear()
    with _TOUR_STATE_LOCK:
        CONFIRMED_STOPS.clear()
        _LAST_SEEN_OBJECTS.clear()
    with _LAST_NAV_LOCK:
        global _LAST_NAV_GOAL
        _LAST_NAV_GOAL = None
    return "Event log cleared."


@mcp.tool()
def get_mission_progress() -> str:
    """Confirmed tour stops, last lidar-seen centroids, and Q1 pose (shared state).

    Used by the experiment Environment. Does not reveal planted landmark poses.
    """
    return json.dumps({"success": True, **_tour_progress_payload()}, indent=2)


@mcp.tool()
def emit_conflict(robot_ids: str, reason: str = "conflict", detail: str = "") -> str:
    """Manually emit a conflict event (comma-separated specialist names).

    Opens event-triggered communication between the named specialists,
    e.g. 'navigator,lidar'.
    """
    participants = [p.strip() for p in robot_ids.split(",") if p.strip()]
    if len(participants) < 2:
        return json.dumps(
            {
                "error": "need_at_least_two_robots",
                "message": "Pass comma-separated specialist names, e.g. navigator,lidar",
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


@mcp.tool()
def emit_detection_conflict(
    robot_ids: str,
    reason: str = "detection_disagreement",
    detail: str = "",
) -> str:
    """Emit a perceptual conflict between Q1 specialists (camera vs semantic lidar, etc.).

    Opens conflict-based negotiation among the named specialists. Pass at least
        two comma-separated peer names, e.g. 'camera,lidar'.
    """
    participants = [p.strip() for p in robot_ids.split(",") if p.strip()]
    if len(participants) < 2:
        return json.dumps(
            {
                "error": "need_at_least_two_robots",
                "message": "Pass comma-separated specialist names, e.g. camera,lidar",
            }
        )
    event = _emit_event(
        "detection_conflict",
        participants=participants,
        reason=reason,
        detail=detail,
        message=f"Detection conflict among {participants}: {reason}",
    )
    return json.dumps({"success": True, "event": event}, indent=2)


@mcp.tool()
def get_occupancy_map(world_id: str = "") -> str:
    """Load this world's occupancy map (map.yaml + .pgm) as walls and free space.

    Optional — call only when exploring without a centroid, after nav fails,
    or when you suspect a goal is blocked. Not required before every drive.

    Returns map.yaml fields (resolution, origin, …) and a compact ASCII grid.
    Interior object footprints are stripped: this is the arena outline only,
    not object locations. Free cells (.) can be explore poses. Object x/y still
    come only from get_semantic_lidar_objects when Q1 is close to them.
    """
    wid = (world_id or "").strip() or ACTIVE_WORLD_ID or DEFAULT_WORLD_ID
    yaml_path, pgm_path = _occupancy_paths(wid)
    if yaml_path is None or pgm_path is None:
        return json.dumps(
            {
                "success": False,
                "error": "occupancy_map_missing",
                "world_id": wid,
                "map_yaml": str(yaml_path) if yaml_path else None,
                "map_pgm": str(pgm_path) if pgm_path else None,
            },
            indent=2,
        )
    meta = _load_map_yaml(yaml_path) or {}
    loaded = _load_pgm_pixels(pgm_path)
    if loaded is None:
        return json.dumps(
            {
                "success": False,
                "error": "pgm_unreadable",
                "world_id": wid,
                "map_pgm": str(pgm_path),
            },
            indent=2,
        )
    width, height, pixels = loaded
    try:
        resolution = float(meta.get("resolution", 0.05))
        origin = meta.get("origin") or [-20.0, -20.0, 0.0]
        origin_x, origin_y = float(origin[0]), float(origin[1])
    except (TypeError, ValueError, IndexError):
        resolution, origin_x, origin_y = 0.05, -20.0, -20.0
    walls = _walls_only_occupied(
        width,
        height,
        pixels,
        origin_x=origin_x,
        origin_y=origin_y,
        resolution=resolution,
    )
    ascii_map = _occupancy_ascii(
        width,
        height,
        walls,
        pixels,
        origin_x=origin_x,
        origin_y=origin_y,
        resolution=resolution,
        cell_m=1.0,
    )
    yaml_out = {
        k: meta[k]
        for k in (
            "image",
            "resolution",
            "origin",
            "negate",
            "occupied_thresh",
            "free_thresh",
            "mode",
        )
        if k in meta
    }
    return json.dumps(
        {
            "success": True,
            "world_id": wid,
            "yaml": yaml_out,
            "files": {"map_yaml": str(yaml_path), "map_pgm": str(pgm_path)},
            "occupancy": ascii_map,
            "hint": (
                "Arena walls only — objects are not on this map. "
                "'.' cells are free; pick x/y inside them to get closer, then "
                "sense. Do not treat '#' as objects. Object classes/xyz come "
                "only from get_semantic_lidar_objects when you are close."
            ),
        },
        indent=2,
    )


# --- Navigation (ATB behavior_control) --------------------------------------

def _wrap_yaw(yaw: float) -> float:
    return (float(yaw) + math.pi) % (2.0 * math.pi) - math.pi


def _yaw_error(a: float, b: float) -> float:
    return abs(_wrap_yaw(float(a) - float(b)))


def _atb_set_waypoints_yaml(
    x: float,
    y: float,
    yaw: float,
    radius_m: float,
    *,
    use_heading: bool = False,
) -> str:
    """Waypoint in UTM.

    Tour goals keep ``use_heading`` false (forced yaw makes the Q1 lattice-turn
    and smears occupancy).
    """
    ux, uy = _map_to_utm(x, y)
    heading = "true" if use_heading else "false"
    return (
        "{waypoints: {"
        f"header: {{frame_id: '{ATB_NAV_FRAME}'}}, "
        "repeat: false, "
        "waypoints: [{"
        f"pose: {{x: {float(ux)}, y: {float(uy)}, theta: {float(yaw)}}}, "
        f"radius: {float(radius_m)}, heading_accuracy: 0.2, "
        f"use_heading: {heading}, wait_time: 0.0"
        "}]}}"
    )


def _popen_ros2(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        _ros2_command(argv, line_buffered=True),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        stdin=subprocess.DEVNULL,
        bufsize=1,
    )


def _wait_atb_nav_events(
    proc: subprocess.Popen,
    *,
    timeout_sec: float,
    min_monotonic: float = 0.0,
) -> tuple[str, str]:
    """Read a live `ros2 topic echo` of /behavior_control/nav_events until arrival."""
    hit: dict[str, str | None] = {"status": None, "reason": None}

    def _reader() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            text = line.strip()
            if not text or text == "---":
                continue
            if time.monotonic() < min_monotonic:
                continue
            if _ATB_GOAL_REACHED_RE.search(text):
                hit["status"] = "succeeded"
                hit["reason"] = text
                return
            if _ATB_GOAL_FAIL_RE.search(text):
                hit["status"] = "failed"
                hit["reason"] = text
                return

    thread = threading.Thread(target=_reader, daemon=True)
    thread.start()
    thread.join(timeout=max(1.0, float(timeout_sec)))
    if hit["status"]:
        return str(hit["status"]), str(hit["reason"] or "nav_event")
    if thread.is_alive():
        return "failed", "timeout"
    return "failed", "nav_events_ended"


def _wait_atb_goal(
    robot: str,
    x: float,
    y: float,
    yaw: float,
    *,
    radius_m: float,
    timeout_sec: float,
    nav_echo: subprocess.Popen | None = None,
    require_heading: bool = False,
    yaw_tol_rad: float = 0.25,
    event_after: float = 0.0,
) -> tuple[str, str, dict[str, Any] | None]:
    """Wait until ATB publishes Goal reached (events after set_waypoints only)."""

    def _xy_ok(pose: dict[str, Any]) -> bool:
        px, py = float(pose["x"]), float(pose["y"])
        if _looks_like_utm(px, py, str(pose.get("frame_id") or "")):
            ux, uy = _map_to_utm(x, y)
            dist = math.hypot(px - ux, py - uy)
        else:
            dist = math.hypot(px - float(x), py - float(y))
        if dist > radius_m:
            return False
        if require_heading:
            return _yaw_error(float(pose.get("yaw") or 0.0), yaw) <= yaw_tol_rad
        return True

    if nav_echo is not None:
        status, reason = _wait_atb_nav_events(
            nav_echo,
            timeout_sec=timeout_sec,
            min_monotonic=event_after,
        )
        pose = _fetch_robot_pose(robot)
        if status == "succeeded":
            return "succeeded", reason, pose if pose.get("success") else None
        if reason == "timeout":
            return "failed", "timeout", pose if pose.get("success") else None
        timeout_sec = min(timeout_sec, 20.0)
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    last_pose: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        pose = _fetch_robot_pose(robot)
        if pose.get("success"):
            last_pose = pose
            if _xy_ok(pose):
                return "succeeded", "goal_succeeded", pose
        time.sleep(0.5)
    if last_pose and last_pose.get("success"):
        px, py = float(last_pose["x"]), float(last_pose["y"])
        if _looks_like_utm(px, py, str(last_pose.get("frame_id") or "")):
            ux, uy = _map_to_utm(x, y)
            dist = math.hypot(px - ux, py - uy)
        else:
            dist = math.hypot(px - float(x), py - float(y))
        extra = ""
        if require_heading:
            extra = f"_yaw_{_yaw_error(float(last_pose.get('yaw') or 0.0), yaw):.2f}rad"
        return "failed", f"timeout_still_{dist:.2f}m_from_goal{extra}", last_pose
    return "failed", "timeout", last_pose


def _navigate_atb_sync(
    robot: str,
    x: float,
    y: float,
    yaw: float,
    *,
    use_heading: bool = False,
    radius_m: float | None = None,
    timeout_sec: float | None = None,
    require_heading: bool = False,
) -> str:
    radius = NAV_GOAL_RADIUS_M if radius_m is None else float(radius_m)
    wait_timeout = NAV_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
    goal = _atb_set_waypoints_yaml(x, y, yaw, radius, use_heading=use_heading)
    nav_echo = _popen_ros2(["topic", "echo", ATB_NAV_EVENTS_TOPIC])
    time.sleep(0.3)
    try:
        result = _run_ros2(
            [
                "service",
                "call",
                ATB_SET_WAYPOINTS_SRV,
                "iosb_nav_msgs/srv/SetWaypoints",
                goal,
            ],
            timeout=max(ROS_CLI_TIMEOUT_SEC, 20.0),
        )
        if result.get("error") == "timeout":
            return json.dumps(
                {
                    "error": "timeout",
                    "tool": "navigate_to_pose",
                    "robot_id": robot,
                    "reason": "set_waypoints_timeout",
                    "service": ATB_SET_WAYPOINTS_SRV,
                },
                indent=2,
            )
        if not result.get("ok"):
            event = _emit_event(
                "nav_aborted",
                robot_id=robot,
                x=x,
                y=y,
                yaw=yaw,
                reason=result.get("error") or "set_waypoints_failed",
            )
            return json.dumps(
                {
                    "error": "nav_failed",
                    "robot_id": robot,
                    "x": x,
                    "y": y,
                    "yaw": yaw,
                    "service": ATB_SET_WAYPOINTS_SRV,
                    "reason": result.get("error") or "set_waypoints_failed",
                    "stderr": _clip(str(result.get("stderr", "")), 400),
                    "stdout": _clip(str(result.get("stdout", "")), 400),
                    "event": event,
                    "hint": (
                        "ATB waypoint service missing or ROS/ATB not sourced. "
                        "Bring up ./launch.sh, then retry."
                    ),
                },
                indent=2,
            )

        event_after = time.monotonic()
        status, reason, pose = _wait_atb_goal(
            robot,
            x,
            y,
            yaw,
            radius_m=radius,
            timeout_sec=wait_timeout,
            nav_echo=nav_echo,
            require_heading=require_heading or use_heading,
            event_after=event_after,
        )
    finally:
        if nav_echo.poll() is None:
            nav_echo.kill()
            try:
                nav_echo.wait(timeout=2)
            except subprocess.TimeoutExpired:
                nav_echo.terminate()
    ux, uy = _map_to_utm(x, y)
    if status == "succeeded":
        _record_nav_goal(x, y, yaw)
        # Agents must trust ATB success. Live odometry can disagree with the
        # map goal (frame noise); do not expose that as a failure signal.
        return json.dumps(
            {
                "success": True,
                "reached": True,
                "robot_id": robot,
                "x": x,
                "y": y,
                "yaw": yaw,
                "utm_xy": {"x": ux, "y": uy},
                "frame_id": "map",
                "nav": "atb_behavior_control",
                "status": status,
                "reason": reason,
                "goal_tolerance_m": radius,
                "message": (
                    f"SUCCESS: waypoint reached (ATB accepts within "
                    f"±{radius:.1f} m of the goal). Report SUCCESS to the "
                    "requester. Do NOT compare any pose to the goal or call "
                    "this a FAILURE — approximate arrival is correct."
                ),
            },
            indent=2,
        )
    event = _emit_event(
        "nav_aborted",
        robot_id=robot,
        x=x,
        y=y,
        yaw=yaw,
        reason=reason,
        status=status,
    )
    return json.dumps(
        {
            "error": "nav_failed" if status != "aborted" else "nav_aborted",
            "robot_id": robot,
            "x": x,
            "y": y,
            "yaw": yaw,
            "utm_xy": {"x": ux, "y": uy},
            "status": status,
            "reason": reason,
            "pose": pose,
            "event": event,
        },
        indent=2,
    )


def _navigate_to_pose_sync(robot_id: str, x: float, y: float, yaw: float = 0.0) -> str:
    """Blocking navigate via ATB set_waypoints."""
    robot = _physical_robot(robot_id) or (robot_id or "").strip()
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

    return _navigate_atb_sync(robot, float(x), float(y), float(yaw))


@mcp.tool()
async def navigate_to_pose(robot_id: str, x: float, y: float, yaw: float = 0.0) -> str:
    """Drive Q1 to map-frame (x, y, yaw); ATB stops within goal tolerance (~2.5 m).

    Calls /behavior_control/set_waypoints with UTM converted from the open-world
    map frame (same origin as launch.sh). Waits until ATB publishes Goal reached
    on /behavior_control/nav_events. success=true / reached=true means SUCCESS —
    arrival is approximate, not spot-on. Use robot_id 'q1'.
    """
    return await asyncio.to_thread(_navigate_to_pose_sync, robot_id, x, y, yaw)


# --- Q1 compact sensor summaries (Experiment 06) -----------------------------

@mcp.tool()
async def get_lidar_snapshot(robot_id: str = "q1") -> str:
    """Compact occupancy summary from the Q1 3D lidar (not the raw cloud).

    Requires q1_sensor_summarizer. Returns min range and front/left/right/rear
    sector minima in the sensor frame.
    """
    def _run() -> str:
        payload = _echo_json_string_topic("/q1_sensor_summary/lidar")
        payload["robot_id"] = _physical_robot(robot_id) or robot_id
        if not payload.get("success"):
            return _tool_error(
                "laser_failed",
                error=str(payload.get("error") or "lidar_summary_unavailable"),
                tool="get_lidar_snapshot",
                payload=payload,
            )
        return json.dumps(payload, indent=2)

    return await asyncio.to_thread(_run)


@mcp.tool()
async def get_semantic_lidar_objects(class_name: str = "") -> str:
    """Clustered GOOSE objects from Q1 semantic lidar, with map-frame centroids.

    Semantic lidar is 360° (see fov_deg; default 360). Optional class_name
    filters (e.g. 'rock', 'barrel', 'car', or an ID). Requires
    q1_sensor_summarizer. Empty objects means nothing labeled in range — that
    is success, not a tool error. Do not invent object x/y.
    """
    def _run() -> str:
        payload = _echo_json_string_topic("/q1_sensor_summary/semantic_lidar")
        if not payload.get("success"):
            return _tool_error(
                "laser_failed",
                error=str(payload.get("error") or "semantic_lidar_unavailable"),
                tool="get_semantic_lidar_objects",
                payload=payload,
            )
        wanted = (class_name or "").strip().lower().replace(" ", "_")
        objects = list(payload.get("objects") or [])
        if wanted:
            objects = [
                o
                for o in objects
                if str(o.get("class", "")).lower() == wanted
                or str(o.get("class_id")) == wanted
            ]
            payload["filter"] = wanted
        payload["objects"] = objects
        payload["count"] = len(objects)
        if objects:
            _record_seen_objects(objects)
        if not objects:
            payload["hint"] = (
                "No labeled objects in range. That is success, not a tool "
                "error. Sense again after moving. Do not invent object "
                "coordinates."
            )
        return json.dumps(payload, indent=2)

    return await asyncio.to_thread(_run)


def _confirm_stop_sync(name: str) -> str:
    """1) object name → 2) pose from /localization/odometry → 3) compare → 4) result."""
    canonical = _resolve_visit_object(name)
    if canonical is None:
        return json.dumps(
            {
                "success": False,
                "confirmed": False,
                "error": "unknown_object",
                "message": (
                    "Unknown object label. Pass the class name from your sense "
                    "result (e.g. rock, car, container, tree_trunk)."
                ),
            },
            indent=2,
        )
    if not _ros2_available():
        return _tool_error(
            "pose_failed",
            error="ros2_not_found",
            tool="confirm_stop",
            object=canonical,
        )
    target = _OPEN_WORLD_VISIT_TARGETS[canonical]
    tx, ty = float(target["x"]), float(target["y"])

    odom = _echo_localization_odometry()
    if not odom.get("success"):
        return _tool_error(
            "pose_failed",
            error=str(odom.get("error") or "pose_unavailable"),
            tool="confirm_stop",
            object=canonical,
            topic=ATB_ODOM_TOPIC,
        )
    pose = _odom_to_map_pose(odom)
    px, py = float(pose["x"]), float(pose["y"])
    dist = math.hypot(px - tx, py - ty)
    confirmed = dist <= VISIT_CONFIRM_RADIUS_M

    if confirmed:
        _record_confirmed_stop(canonical)

    return json.dumps(
        {
            "success": True,
            "confirmed": confirmed,
            "object": canonical,
            "message": (
                f"Visit to {canonical} confirmed."
                if confirmed
                else (
                    f"Not close enough to confirm a visit to {canonical}. "
                    "Ask navigator to drive closer, then sense again."
                )
            ),
        },
        indent=2,
    )


@mcp.tool()
async def confirm_stop(name: str) -> str:
    """Confirm Q1 stopped close enough to a tour object.

    Pass only the class name (e.g. rock, container). Returns confirmed=true/false.
    """
    return await asyncio.to_thread(_confirm_stop_sync, name)


@mcp.tool()
async def get_semantic_camera_classes() -> str:
    """GOOSE class histogram for the current Q1 semantic camera frame.

    Requires q1_sensor_summarizer. Returns visible class names and pixel share,
    not 3D coordinates — use get_semantic_lidar_objects for map-frame goals.
    """
    def _run() -> str:
        payload = _echo_json_string_topic("/q1_sensor_summary/camera")
        if not payload.get("success"):
            return _tool_error(
                "laser_failed",
                error=str(payload.get("error") or "semantic_camera_unavailable"),
                tool="get_semantic_camera_classes",
                payload=payload,
            )
        classes = payload.get("classes") or payload.get("histogram") or []
        if not classes:
            payload["hint"] = (
                "No classes in the camera image. That is success, not a tool "
                "error. Camera is forward-facing and has no xyz — use "
                "get_semantic_lidar_objects for object coordinates."
            )
        return json.dumps(payload, indent=2)

    return await asyncio.to_thread(_run)


def _still_path(base: Path) -> Path | None:
    found: list[Path] = []
    seen: set[Path] = set()
    for candidate in (
        base,
        base.with_suffix(".jpg"),
        base.with_suffix(".jpeg"),
        base.with_suffix(".png"),
    ):
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if candidate.is_file() and candidate.stat().st_size > 32:
                found.append(candidate)
        except OSError:
            continue
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def _mcp_camera_image(base: Path, *, kind: str):
    deadline = time.monotonic() + 3.0
    path: Path | None = None
    age = None
    while time.monotonic() <= deadline:
        path = _still_path(base)
        if path is not None:
            age = time.time() - path.stat().st_mtime
            if age <= CAMERA_STILL_MAX_AGE_SEC:
                break
        time.sleep(0.4)
    if path is None:
        return json.dumps(
            {
                "success": False,
                "error": "camera_frame_unavailable",
                "kind": kind,
                "hint": (
                    "q1_sensor_summarizer writes this still. Restart ./launch.sh "
                    "and wait for a camera frame."
                ),
            },
            indent=2,
        )
    if age is None:
        age = time.time() - path.stat().st_mtime
    if age > CAMERA_STILL_MAX_AGE_SEC:
        return json.dumps(
            {
                "success": False,
                "error": "camera_frame_stale",
                "kind": kind,
                "path": str(path),
                "age_sec": round(age, 1),
                "hint": (
                    "RGB still is older than "
                    f"{CAMERA_STILL_MAX_AGE_SEC:.0f}s. The summarizer is not "
                    "receiving /q1_camera (QoS). Restart ./launch.sh."
                ),
            },
            indent=2,
        )
    payload = path.read_bytes()
    fmt = "jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "png"
    caption = (
        f"{kind} camera frame ({path.name}, {len(payload)} bytes). "
        "Look at this picture. Camera has no xyz — lidar "
        "get_semantic_lidar_objects is the only source of object coordinates."
    )
    return [caption, MCPImage(data=payload, format=fmt)]


@mcp.tool(structured_output=False)
def get_camera_image():
    """Current Q1 RGB camera frame as a picture for a multimodal model.

    Look at the image. Camera has no xyz — use get_semantic_lidar_objects
    (lidar specialist) for map-frame object coordinates.
    """
    return _mcp_camera_image(CAMERA_RGB_JPEG, kind="rgb")


@mcp.tool(structured_output=False)
def get_semantic_camera_image():
    """Current Q1 semantic-camera colorization as a picture (GOOSE palette).

    Look at the image. This is a visualization, not map-frame xyz.
    """
    return _mcp_camera_image(CAMERA_SEM_JPEG, kind="semantic_color")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent MCP server.")
    parser.add_argument("--host", default=os.environ.get("FASTMCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FASTMCP_PORT", "8000")))
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="sse")
