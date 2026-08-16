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
# Max map-frame distance from station center for pickup_box / drop_box.
# Approach poses from rank_stations_by_distance sit ~1.2 m off the pad.
MANIP_RADIUS_M = float(os.environ.get("MCP_MANIP_RADIUS_M", "1.8"))
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
CAMERA_STILL_MAX_AGE_SEC = float(os.environ.get("MCP_CAMERA_STILL_MAX_AGE_SEC", "8"))

# Protect station/box inventory when concurrent tool calls run in threads.
_STATE_LOCK = threading.Lock()
_TF_CACHE: dict[tuple[str, str], tuple[float, float, float, float, float]] = {}
_TF_MISS_UNTIL: dict[tuple[str, str], float] = {}
_TF_CACHE_LOCK = threading.Lock()

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
# Each station holds at most ONE box.
#   Occupied/pickable: box_id set, available=True  → pickup OK, drop FAILS
#   Empty:             box_id null, available=False → drop OK, pickup FAILS
# Robots also hold at most one box (see pickup_box / drop_box).

# Initial box layout (edit per difficulty). Coords here are fallback only —
# when items/{AGENT_WORLD}.json exists, poses come from that file and these
# box_id / available / last_box_id fields are merged onto matching station ids.
_DEFAULT_STATIONS = [
    {"id": "station_A", "name": "Station A", "x": -5.0, "y": -5.0, "box_id": "box_1", "available": True, "last_box_id": "box_1"},
    {"id": "station_B", "name": "Station B", "x": -5.0, "y": 5.0, "box_id": "box_2", "available": True, "last_box_id": "box_2"},
    {"id": "station_C", "name": "Station C", "x": 5.0, "y": 5.0, "box_id": "box_3", "available": True, "last_box_id": "box_3"},
    {"id": "station_D", "name": "Station D", "x": 5.0, "y": -5.0, "box_id": "box_4", "available": True, "last_box_id": "box_4"},
]

STATIONS: list[dict] = deepcopy(_DEFAULT_STATIONS)
# robot_id -> box_id currently held (or None)
HELD_BY: dict[str, str | None] = {}
# World id whose items/*.json currently backs STATIONS (list_stations / get_station).
ACTIVE_WORLD_ID: str = DEFAULT_WORLD_ID

# --- Shared event log --------------------------------------------------------
EVENTS: list[dict] = []


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
    """Return an error JSON and always append a matching MCP event for peers.

    Prefer ``payload={...}`` when forwarding a dict that may already contain
    ``robot_id`` / ``error``. Explicit ``error`` always wins. ``extra`` overlays
    ``payload`` for individual fields.
    """
    clean: dict[str, Any] = {}
    if payload:
        clean.update(payload)
    clean.update(extra)
    clean.pop("error", None)
    event = _emit_event(event_type, error=error, **clean)
    body = {"error": error, **clean, "event": event}
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

    # Unique short alias: "east" → station_east, "gap" → gap, "north" → north cache id
    matches: list[str] = []
    for station in STATIONS:
        sid = str(station["id"])
        sid_l = sid.lower()
        name_l = str(station.get("name") or "").lower()
        name_compact = re.sub(r"\s+", "_", name_l)
        if sid_l == compact or sid_l.endswith("_" + compact):
            matches.append(sid)
        elif compact in name_l.split() or name_compact == compact or name_compact.endswith(
            "_" + compact
        ):
            matches.append(sid)
    uniq = list(dict.fromkeys(matches))
    if len(uniq) == 1:
        return uniq[0]
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


def _quaternion_to_yaw(z: float, w: float, x: float = 0.0, y: float = 0.0) -> float:
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


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
    """Map items.json landmark ids to inventory ids.

    Accepts every landmark in the active world file (station_A, station_west,
    gap, item_1 / caches, crossing, …) — not only station_A..D.
    """
    s = (raw_id or "").strip()
    if not s:
        return None
    low = s.lower().replace("-", "_").replace(" ", "_")
    low = re.sub(r"_+", "_", low).strip("_")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", low):
        return None
    m = re.fullmatch(r"station_([a-d])", low)
    if m:
        return f"station_{m.group(1).upper()}"
    return low


def _safe_world_id(world_id: str) -> str | None:
    wid = (world_id or "").strip()
    if not wid or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]*", wid):
        return None
    return wid


def _stations_from_world_items(world_id: str) -> list[dict] | None:
    """Build STATIONS inventory from *all* landmarks in items/{world_id}.json."""
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
        if item.get("scoring_only"):
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
        entry: dict[str, Any] = {
            "id": sid,
            "name": name.title() if name.lower().startswith("station") else name,
            "x": x,
            "y": y,
            "box_id": None,
            "available": False,
            "last_box_id": None,
        }
        if "yaw" in item:
            try:
                entry["yaw"] = float(item["yaw"])
            except (TypeError, ValueError):
                pass
        if item.get("look"):
            entry["look"] = True
        # Optional box fields in items.json override defaults.
        if "box_id" in item:
            entry["box_id"] = item.get("box_id")
            entry["available"] = bool(item.get("available", entry["box_id"] is not None))
            entry["last_box_id"] = item.get("last_box_id", entry["box_id"])
        if item.get("note"):
            entry["note"] = str(item["note"])
        stations.append(entry)

    if not stations:
        return None

    # For classic A–D pads, merge built-in box layout when items omit box fields.
    defaults_by_id = {s["id"]: s for s in _DEFAULT_STATIONS}
    for station in stations:
        if station.get("box_id") is not None or station.get("available"):
            continue
        default = defaults_by_id.get(station["id"])
        if default is None:
            continue
        station["box_id"] = default.get("box_id")
        station["available"] = bool(default.get("available"))
        station["last_box_id"] = default.get("last_box_id")
    return stations


def _load_stations_for_world(world_id: str | None = None) -> list[dict]:
    wid = (world_id or "").strip() or DEFAULT_WORLD_ID
    loaded = _stations_from_world_items(wid)
    return loaded if loaded else deepcopy(_DEFAULT_STATIONS)


def _apply_stations_for_world(world_id: str | None = None) -> str:
    """Reload STATIONS/HELD_BY from items/{world}.json. Returns active world id."""
    global ACTIVE_WORLD_ID
    wid = (world_id or "").strip() or DEFAULT_WORLD_ID
    safe = _safe_world_id(wid) or DEFAULT_WORLD_ID
    STATIONS.clear()
    STATIONS.extend(_load_stations_for_world(safe))
    HELD_BY.clear()
    ACTIVE_WORLD_ID = safe
    return ACTIVE_WORLD_ID


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
                items = [
                    it
                    for it in raw
                    if not (isinstance(it, dict) and it.get("scoring_only"))
                ]
                for it in items:
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
            "Use item x/y as map-frame landmarks. Look poses (look=true) are scan "
            "viewpoints, not object goals — drive there then sense. Scoring object "
            "xyz are omitted. Occupancy grid is in map.yaml/pgm."
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
def set_world(world_id: str) -> str:
    """Set the active world and reload landmarks into list_stations / get_station.

    Loads poses (and optional box fields) from worlds/items/{world_id}.json.
    Use this (or get_map_info with the same world_id) so inventory matches the map.
    """
    wid = _safe_world_id(world_id)
    if wid is None:
        return json.dumps(
            {
                "error": "invalid_world_id",
                "world_id": world_id,
                "hint": "Use an id from list_worlds (e.g. stations, bottleneck).",
            },
            indent=2,
        )
    path = _worlds_root() / "items" / f"{wid}.json"
    if not path.is_file() and wid != DEFAULT_WORLD_ID:
        # Still apply (may fall back to built-in A–D) but warn.
        active = _apply_stations_for_world(wid)
        return json.dumps(
            {
                "success": True,
                "warning": "items_json_missing",
                "world_id": active,
                "items_json": str(path),
                "stations": STATIONS,
                "hint": "No items file; inventory may be built-in defaults.",
            },
            indent=2,
        )
    active = _apply_stations_for_world(wid)
    return json.dumps(
        {
            "success": True,
            "world_id": active,
            "items_json": str(path),
            "station_count": len(STATIONS),
            "stations": STATIONS,
        },
        indent=2,
    )


@mcp.tool()
def get_map_info(world_id: str = "") -> str:
    """Get map metadata and landmark items for a world (default: active / AGENT_WORLD).

    Returns Nav2 map.yaml fields (resolution, origin, …), approximate bounds from
    the .pgm size when available, and items/*.json landmarks (stations, notes).
    When world_id is given (or differs from the active world), also reloads
    list_stations / get_station from that world's items.json so map and inventory
    stay one source of truth. Does not return raw occupancy pixels or full SDF.
    """
    wid = (world_id or "").strip() or ACTIVE_WORLD_ID or DEFAULT_WORLD_ID
    if wid != ACTIVE_WORLD_ID:
        _apply_stations_for_world(wid)
    info = _build_map_info(wid)
    info["active_world_id"] = ACTIVE_WORLD_ID
    info["stations"] = STATIONS
    info["hint"] = (
        "Landmarks and list_stations share this world_id. Use item/station x/y "
        "as map-frame goals. Prefer goals slightly off pads if notes say "
        "visual-only. Occupancy grid is in map.yaml/pgm."
    )
    return json.dumps(info, indent=2)

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
    """ROS namespace for tools. Q1 specialists all share AGENT_NAV_ROBOT (default q1)."""
    robot = _normalize_robot_id(robot_id)
    if _agent_platform() == "q1":
        return _q1_nav_id()
    return robot


def _map_to_utm(x: float, y: float) -> tuple[float, float]:
    return float(x) + ATB_UTM_ORIGIN_X, float(y) + ATB_UTM_ORIGIN_Y


def _utm_to_map(x: float, y: float) -> tuple[float, float]:
    return float(x) - ATB_UTM_ORIGIN_X, float(y) - ATB_UTM_ORIGIN_Y


def _looks_like_utm(x: float, y: float, frame_id: str = "") -> bool:
    frame = (frame_id or "").lower()
    if "utm" in frame:
        return True
    return abs(float(x)) > 10000.0 or abs(float(y)) > 10000.0


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
    result = _run_ros2(["run", "tf2_ros", "tf2_echo", dst, src], timeout=2.0)
    text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    parsed = _parse_tf_echo(text)
    with _TF_CACHE_LOCK:
        if parsed is None:
            _TF_MISS_UNTIL[key] = now + 5.0
            return None
        _TF_CACHE[key] = (*parsed, now + 3.0)
        _TF_MISS_UNTIL.pop(key, None)
    return parsed


def _apply_tf_xy(
    x: float, y: float, yaw: float, tf: tuple[float, float, float, float]
) -> tuple[float, float, float]:
    tx, ty, c, s = tf
    return c * x - s * y + tx, s * x + c * y + ty, yaw + math.atan2(s, c)


def _pose_to_map_frame(pose: dict[str, float], *, source: str) -> dict[str, float]:
    """Keep MCP x/y in the Gazebo/open map frame used by items.json."""
    x, y = float(pose["x"]), float(pose["y"])
    yaw = float(pose.get("yaw") or 0.0)
    frame = str(pose.get("frame_id") or "")
    if _looks_like_utm(x, y, frame):
        x, y = _utm_to_map(x, y)
        frame = "map"
    else:
        src = (frame or "").strip()
        if src and src.lower() not in {"map", "open", "world"}:
            tf = _lookup_tf_2d("open", src) or _lookup_tf_2d("map", src)
            if tf is not None:
                x, y, yaw = _apply_tf_xy(x, y, yaw, tf)
                frame = "map"
        else:
            frame = frame or "map"
    out = dict(pose)
    out["x"] = x
    out["y"] = y
    out["yaw"] = yaw
    out["frame_id"] = frame
    out["source"] = source
    return out


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
            _ros2_command(argv),
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
    if _agent_platform() == "q1":
        return {"robots": [_q1_nav_id()], "source": "q1_platform"}
    return {"robots": fallback, "source": "config_fallback", "note": "no_robot_state_topics"}


def _header_frame_id(parsed: Any, text: str) -> str:
    if isinstance(parsed, dict):
        header = parsed.get("header")
        if isinstance(header, dict) and header.get("frame_id"):
            return str(header["frame_id"]).strip().strip("'\"")
    m = re.search(r"(?m)^\s*frame_id:\s*[\"']?([A-Za-z0-9_/\-]+)[\"']?\s*$", text)
    return m.group(1) if m else ""


def _fetch_q1_pose(robot: str) -> dict[str, Any]:
    echo = _echo_topic_once(ATB_ODOM_TOPIC, best_effort=True)
    if echo.get("ok"):
        pose = _parse_odometry_message(echo["stdout"], echo.get("parsed"))
        if pose is not None:
            pose["frame_id"] = _header_frame_id(echo.get("parsed"), echo.get("stdout") or "")
            mapped = _pose_to_map_frame(pose, source=ATB_ODOM_TOPIC)
            mapped["success"] = True
            mapped["robot_id"] = robot
            return mapped
    return {
        "success": False,
        "error": echo.get("error") or "pose_unavailable",
        "robot_id": robot,
        "topic": ATB_ODOM_TOPIC,
        "stderr": _clip(str(echo.get("stderr", "")), 200),
    }


def _fetch_robot_pose(robot: str) -> dict[str, Any]:
    """Map-frame pose. Q1 uses ATB odometry only (no remroc topic fallbacks)."""
    robot = _physical_robot(robot) or robot
    if _use_atb_nav():
        return _fetch_q1_pose(robot)
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
    odom = _echo_topic_once(f"/{robot}/odom")
    if odom.get("ok"):
        pose = _parse_odometry_message(odom["stdout"], odom.get("parsed"))
        if pose is not None:
            return {
                "success": True,
                "robot_id": robot,
                "source": "odom",
                **pose,
            }
    err = (
        primary.get("error")
        or amcl.get("error")
        or "pose_unavailable"
    )
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


# --- Stations / boxes --------------------------------------------------------

@mcp.tool()
def list_stations() -> str:
    """List landmarks/stations for the active world (see set_world / get_map_info).

    Source: worlds/items/{active_world_id}.json (same as get_map_info items).
    Capacity: each pad holds at most ONE box when box tools are used.
    - Occupied / pickable: box_id set and available=true → pickup_box OK, drop_box FAILS.
    - Empty: box_id is null and available=false → drop_box OK, pickup_box FAILS.
    Use this (or get_station) before dropping so you never target an occupied pad.
    Non-pad landmarks (gap, caches, …) appear here for navigation coordinates.
    """
    return json.dumps(
        {
            "world_id": ACTIVE_WORLD_ID,
            "count": len(STATIONS),
            "stations": STATIONS,
        },
        indent=2,
    )


@mcp.tool()
def get_look_poses() -> str:
    """Named scan viewpoints if the world file marks landmarks with look=true.

    The Q1 semantic tour does not use this: object x/y come from
    get_semantic_lidar_objects. These poses are planted map pads, not detections.
    """
    poses = [
        {
            "id": s["id"],
            "name": s.get("name", s["id"]),
            "x": s["x"],
            "y": s["y"],
            "yaw": float(s.get("yaw", 0.0)),
        }
        for s in STATIONS
        if s.get("look")
    ]
    return json.dumps(
        {
            "success": True,
            "world_id": ACTIVE_WORLD_ID,
            "count": len(poses),
            "look_poses": poses,
            "hint": (
                "Planted scan pads from items.json, not sensor detections. "
                "Q1 tour agents should ignore this tool and use "
                "get_semantic_lidar_objects for object x/y."
            ),
        },
        indent=2,
    )


@mcp.tool()
def get_occupancy_map(world_id: str = "") -> str:
    """Load this world's occupancy map (map.yaml + .pgm) as walls and free space.

    Returns Nav2 yaml fields (resolution, origin, …) and a compact ASCII grid.
    Interior object footprints are stripped: this is the arena outline only,
    not object locations. Use free cells (.) as explore poses. Object x/y still
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


@mcp.tool()
def list_available_boxes() -> str:
    """List stations that currently have a pickable box (available=true and box_id set).

    These stations are OCCUPIED — you cannot drop another box there until the
    existing box is picked up. Empty destinations are NOT listed here; use
    list_stations / get_station (box_id null, available=false) for drop targets.
    """
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
    """Get one station by id, including occupancy (box_id / available).

    Capacity: one box per station.
    - Occupied: box_id set, available=true → can pickup, cannot drop.
    - Empty: box_id null, available=false → can drop, cannot pickup.
    Call this before drop_box if unsure whether the destination is free.
    Accepts station_A / A / 'Station A'. Unknown ids emit station_not_found.
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


def _ensure_near_station(
    robot: str,
    station: dict,
    *,
    tool: str,
) -> str | None:
    """Return an error JSON if the robot is too far (or pose unavailable); else None.

    Must run *outside* ``_STATE_LOCK`` — pose fetch uses ROS CLI and can block.
    """
    if not _ros2_available():
        return _tool_error(
            "pose_failed",
            error="ros2_not_found",
            tool=tool,
            robot_id=robot,
            station_id=station.get("id"),
            hint="ROS 2 required to verify proximity before pickup/drop.",
        )
    pose = _fetch_robot_pose(robot)
    if not pose.get("success"):
        return _tool_error(
            "pose_failed",
            error=str(pose.get("error") or "pose_unavailable"),
            tool=tool,
            robot_id=robot,
            station_id=station.get("id"),
            hint="Cannot verify proximity; retry get_robot_pose then navigate closer.",
        )
    dx = float(pose["x"]) - float(station["x"])
    dy = float(pose["y"]) - float(station["y"])
    dist = math.hypot(dx, dy)
    if dist <= MANIP_RADIUS_M:
        return None
    sid = station.get("id")
    return _tool_error(
        "too_far_from_station",
        error="too_far_from_station",
        tool=tool,
        robot_id=robot,
        station_id=sid,
        distance_m=round(dist, 3),
        max_distance_m=MANIP_RADIUS_M,
        robot_xy={"x": pose["x"], "y": pose["y"]},
        station_xy={"x": station["x"], "y": station["y"]},
        message=(
            f"{robot} is {dist:.2f} m from {sid} "
            f"(need ≤ {MANIP_RADIUS_M} m). Navigate closer before {tool}."
        ),
        hint=(
            "Call rank_stations_by_distance or get_station, then navigate_to_pose "
            "to the station's navigate_xy / pad, then retry pickup_box/drop_box."
        ),
    )


def _pickup_box_sync(robot_id: str, station_id: str) -> str:
    robot = _normalize_robot_id(robot_id) or (robot_id or "").strip()
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

    near_err = _ensure_near_station(robot, station, tool="pickup_box")
    if near_err is not None:
        return near_err

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
                "station_id": station["id"],
                "box_id": box_id,
                "distance_m_ok": True,
                "max_distance_m": MANIP_RADIUS_M,
                "message": f"{robot} picked up {box_id} from {station['id']}",
            }
        )


@mcp.tool()
async def pickup_box(robot_id: str, station_id: str) -> str:
    """Pick up the box at a station for this robot.

    Robot must be within MCP_MANIP_RADIUS_M of the station (map frame). Navigate
    to the pad / navigate_xy first; remote teleports fail with too_far_from_station.
    On failure (too far, missing box, already holding), emits an MCP event so
    conflict-based peers can open negotiation.
    """
    return await asyncio.to_thread(_pickup_box_sync, robot_id, station_id)


def _drop_box_sync(robot_id: str, station_id: str) -> str:
    robot = _normalize_robot_id(robot_id) or (robot_id or "").strip()
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

    near_err = _ensure_near_station(robot, station, tool="drop_box")
    if near_err is not None:
        return near_err

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

        occupying = station.get("box_id")
        if occupying or station.get("available"):
            sid = station.get("id") or station_id
            return _tool_error(
                "station_occupied",
                error="station_occupied",
                tool="drop_box",
                robot_id=robot,
                station_id=station_id,
                box_id=occupying,
                message=(
                    f"Station {sid} already has {occupying or 'a box'}; cannot drop. "
                    "Each station holds only ONE box. Free the pad with pickup_box "
                    "or drop on an empty station (box_id=null, available=false)."
                ),
                hint=(
                    "Call get_station / list_stations. Drop only on empty pads. "
                    "For swaps, pick up from destinations first so they are empty."
                ),
            )

        station["box_id"] = box_id
        station["last_box_id"] = box_id
        station["available"] = True
        HELD_BY[robot] = None
        return json.dumps(
            {
                "success": True,
                "robot_id": robot,
                "station_id": station["id"],
                "box_id": box_id,
                "distance_m_ok": True,
                "max_distance_m": MANIP_RADIUS_M,
                "message": f"{robot} dropped {box_id} at {station['id']}",
            }
        )


@mcp.tool()
async def drop_box(robot_id: str, station_id: str) -> str:
    """Drop the box this robot is holding onto a station. Station MUST be empty.

    Robot must be within MCP_MANIP_RADIUS_M of the station (map frame). Navigate
    first; remote teleports fail with too_far_from_station.
    Capacity: each station holds at most ONE box. Drop only when box_id is null
    and available=false (verify with get_station / list_stations first).
    If the pad already has a box, this fails with station_occupied — free it
    with pickup_box or choose another empty station. For swaps (A↔C), pick both
    sources before dropping so destinations are clear.
    On failure emits an MCP event.
    """
    return await asyncio.to_thread(_drop_box_sync, robot_id, station_id)


@mcp.tool()
def reset_stations() -> str:
    """Reset stations and held boxes from the active world's items.json.

    Reloads items/{ACTIVE_WORLD_ID}.json (set via AGENT_WORLD, set_world, or get_map_info).
    """
    active = _apply_stations_for_world(ACTIVE_WORLD_ID)
    return json.dumps(
        {
            "success": True,
            "world_id": active,
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
    robot = _physical_robot(robot_id) or _normalize_robot_id(robot_id)
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
            payload={
                **{k: v for k, v in pose.items() if k not in ("error", "success")},
                "tool": "get_robot_pose",
                "robot_id": robot,
            },
        )
    return json.dumps(pose, indent=2)


@mcp.tool()
async def get_robot_pose(robot_id: str) -> str:
    """Get map-frame pose (x, y, yaw) for one robot.

    Q1: /localization/odometry converted from UTM into the open/map frame.
    Remroc: /{robot}/robot_state, then /{robot}/amcl_pose.
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


def _cmd_vel_topic(robot: str) -> str:
    if _use_atb_nav():
        return (os.environ.get("AGENT_CMD_VEL_TOPIC", "/cmd_vel").strip() or "/cmd_vel")
    return f"/{robot}/cmd_vel"


def _twist_yaml(linear_x: float, angular_z: float = 0.0) -> str:
    return (
        "{linear: {x: "
        f"{float(linear_x)}"
        ", y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: "
        f"{float(angular_z)}"
        "}}"
    )


def _publish_cmd_vel(robot: str, linear_x: float, angular_z: float = 0.0) -> dict[str, Any]:
    topic = _cmd_vel_topic(robot)
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
    topic = _cmd_vel_topic(robot)
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
    topic = _cmd_vel_topic(robot)
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


def _clamp_linear_speed(speed_mps: float) -> float:
    speed = abs(float(speed_mps))
    if speed < 0.05:
        return 0.05
    if speed > 0.6:
        return 0.6
    return speed


def _clamp_turn_speed(turn_speed_rps: float) -> float:
    turn_speed = abs(float(turn_speed_rps))
    if turn_speed < 0.1:
        return 0.1
    if turn_speed > 1.2:
        return 1.2
    return turn_speed


CMD_VEL_MAX_ROTATE_DEG = 360.0
CMD_VEL_MAX_JOG_M = 10.0


def _yaw_delta_deg(before: dict[str, Any], after: dict[str, Any]) -> float | None:
    if not (before.get("success") and after.get("success")):
        return None
    try:
        a = float(before["yaw"])
        b = float(after["yaw"])
    except (KeyError, TypeError, ValueError):
        return None
    delta = math.degrees(b - a)
    delta = (delta + 180.0) % 360.0 - 180.0
    return round(delta, 2)


def _drive_until_distance(
    robot: str,
    *,
    distance_m: float,
    speed_mps: float,
    rate_hz: float = 20.0,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Drive along current heading via timed cmd_vel stream, then hard-stop.

    Positive distance_m is forward, negative is reverse. Gazebo latches Twist
    commands, so we stream at a steady rate and always end with zeros.
    """
    signed = float(distance_m)
    dist = abs(signed)
    speed = abs(float(speed_mps))
    linear = speed if signed >= 0.0 else -speed
    duration = dist / max(speed, 0.05)
    if timeout_sec is not None:
        duration = min(duration, float(timeout_sec))

    start = _fetch_robot_pose(robot)
    timed = _stream_cmd_vel(
        robot,
        linear_x=linear,
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
    """Drive: Q1 uses ATB waypoints; remroc rotates then drives via cmd_vel.

    direction_deg is relative to current heading: 0=forward, 90=left, -90=right, 180=back.
    Prefer navigate_to_pose for tour goals. Failures emit a drive_failed MCP event.
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
    """Blocking drive. Q1 uses ATB waypoints; remroc uses cmd_vel."""
    robot = _physical_robot(robot_id) or _normalize_robot_id(robot_id)
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

    if _use_atb_nav():
        pose = _fetch_robot_pose(robot)
        if not pose.get("success"):
            return _tool_error(
                "drive_failed",
                error=str(pose.get("error") or "pose_unavailable"),
                tool="drive_distance",
                robot_id=robot,
            )
        if dist > CMD_VEL_MAX_JOG_M:
            dist = CMD_VEL_MAX_JOG_M
        heading = _wrap_yaw(
            float(pose.get("yaw") or 0.0) + math.radians(float(direction_deg))
        )
        x2 = float(pose["x"]) + dist * math.cos(heading)
        y2 = float(pose["y"]) + dist * math.sin(heading)
        return _navigate_atb_sync(
            robot,
            x2,
            y2,
            heading,
            use_heading=False,
            radius_m=_atb_jog_radius(dist),
            timeout_sec=min(90.0, NAV_TIMEOUT_SEC),
        )

    speed = _clamp_linear_speed(speed_mps)
    turn_speed = _clamp_turn_speed(turn_speed_rps)

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


def _require_physical_robot(robot_id: str, *, tool: str, event_type: str) -> tuple[str | None, str | None]:
    robot = _physical_robot(robot_id) or _normalize_robot_id(robot_id)
    if not robot:
        return None, _tool_error(event_type, error="missing_robot_id", tool=tool)
    if not _ros2_available():
        return None, _tool_error(
            event_type, error="ros2_not_found", tool=tool, robot_id=robot
        )
    return robot, None


def _rotate_by_sync(
    robot_id: str,
    degrees: float,
    turn_speed_rps: float = 0.5,
) -> str:
    robot, err = _require_physical_robot(
        robot_id, tool="rotate_by", event_type="rotate_failed"
    )
    if err:
        return err
    assert robot is not None

    deg = float(degrees)
    if abs(deg) < 1.0:
        return _tool_error(
            "rotate_failed",
            error="angle_too_small",
            tool="rotate_by",
            robot_id=robot,
            degrees=deg,
        )
    if abs(deg) > CMD_VEL_MAX_ROTATE_DEG:
        deg = math.copysign(CMD_VEL_MAX_ROTATE_DEG, deg)

    if _use_atb_nav():
        pose_before = _fetch_robot_pose(robot)
        if not pose_before.get("success"):
            return _tool_error(
                "rotate_failed",
                error=str(pose_before.get("error") or "pose_unavailable"),
                tool="rotate_by",
                robot_id=robot,
                requested_deg=deg,
            )
        target_yaw = _wrap_yaw(float(pose_before.get("yaw") or 0.0) + math.radians(deg))
        raw = _navigate_atb_sync(
            robot,
            float(pose_before["x"]),
            float(pose_before["y"]),
            target_yaw,
            use_heading=True,
            radius_m=0.4,
            timeout_sec=min(60.0, NAV_TIMEOUT_SEC),
            require_heading=True,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": "unparsable_nav", "raw": raw[:400]}
        pose_after = payload.get("pose") if isinstance(payload.get("pose"), dict) else {}
        yaw_delta = _yaw_delta_deg(pose_before, pose_after or {})
        extra = {
            "requested_deg": deg,
            "yaw_delta_deg": yaw_delta,
            "nav": "atb_behavior_control",
        }
        if payload.get("success"):
            payload.update(extra)
            payload["tool"] = "rotate_by"
            payload["message"] = (
                f"{robot} rotated {deg:.1f} deg via ATB waypoints "
                f"(measured {yaw_delta if yaw_delta is not None else 'unknown'} deg). "
                "Positive is left / CCW."
            )
            return json.dumps(payload, indent=2)
        return _tool_error(
            "rotate_failed",
            error=str(payload.get("error") or payload.get("reason") or "rotate_incomplete"),
            tool="rotate_by",
            robot_id=robot,
            **extra,
            reason=payload.get("reason"),
            pose=pose_after or None,
        )

    turn_speed = _clamp_turn_speed(turn_speed_rps)
    yaw_off = math.radians(deg)
    duration = abs(yaw_off) / turn_speed
    ang = turn_speed if yaw_off > 0 else -turn_speed
    pose_before = _fetch_robot_pose(robot)
    turn_res = _stream_cmd_vel(
        robot, linear_x=0.0, angular_z=ang, duration_sec=duration, rate_hz=20.0
    )
    final_stop = _stop_cmd_vel(robot)
    pose_after = _fetch_robot_pose(robot)
    ok = bool(turn_res.get("ok")) and bool(final_stop.get("ok"))
    yaw_delta = _yaw_delta_deg(pose_before, pose_after)
    if not ok:
        return _tool_error(
            "rotate_failed",
            error="rotate_incomplete",
            tool="rotate_by",
            robot_id=robot,
            requested_deg=deg,
            yaw_delta_deg=yaw_delta,
            duration_sec=round(duration, 3),
            message=f"{robot} rotate_by did not complete cleanly",
        )
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "requested_deg": deg,
            "yaw_delta_deg": yaw_delta,
            "duration_sec": round(duration, 3),
            "pose_before": pose_before if pose_before.get("success") else pose_before,
            "pose_after": pose_after if pose_after.get("success") else pose_after,
            "message": (
                f"{robot} rotated {deg:.1f} deg in place "
                f"(measured {yaw_delta if yaw_delta is not None else 'unknown'} deg). "
                "Positive is left / CCW."
            ),
        },
        indent=2,
    )


def _drive_forward_sync(
    robot_id: str,
    distance_m: float,
    speed_mps: float = 0.25,
) -> str:
    robot, err = _require_physical_robot(
        robot_id, tool="drive_forward", event_type="drive_failed"
    )
    if err:
        return err
    assert robot is not None

    signed = float(distance_m)
    if abs(signed) < 1e-3:
        return _tool_error(
            "drive_failed",
            error="distance_too_small",
            tool="drive_forward",
            robot_id=robot,
            distance_m=signed,
        )
    if abs(signed) > CMD_VEL_MAX_JOG_M:
        signed = math.copysign(CMD_VEL_MAX_JOG_M, signed)

    if _use_atb_nav():
        pose_before = _fetch_robot_pose(robot)
        if not pose_before.get("success"):
            return _tool_error(
                "drive_failed",
                error=str(pose_before.get("error") or "pose_unavailable"),
                tool="drive_forward",
                robot_id=robot,
                requested_m=signed,
            )
        yaw = float(pose_before.get("yaw") or 0.0)
        x2 = float(pose_before["x"]) + signed * math.cos(yaw)
        y2 = float(pose_before["y"]) + signed * math.sin(yaw)
        raw = _navigate_atb_sync(
            robot,
            x2,
            y2,
            yaw,
            use_heading=False,
            radius_m=_atb_jog_radius(signed),
            timeout_sec=min(90.0, NAV_TIMEOUT_SEC),
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": "unparsable_nav", "raw": raw[:400]}
        pose_after = payload.get("pose") if isinstance(payload.get("pose"), dict) else {}
        traveled = None
        if pose_before.get("success") and isinstance(pose_after, dict) and pose_after.get("success"):
            traveled = round(
                math.hypot(
                    float(pose_after["x"]) - float(pose_before["x"]),
                    float(pose_after["y"]) - float(pose_before["y"]),
                ),
                3,
            )
        extra = {
            "requested_m": signed,
            "traveled_m": traveled,
            "direction": "forward" if signed >= 0 else "reverse",
            "nav": "atb_behavior_control",
        }
        if payload.get("success"):
            payload.update(extra)
            payload["tool"] = "drive_forward"
            payload["message"] = (
                f"{robot} drove {extra['direction']} "
                f"~{traveled if traveled is not None else abs(signed)} m via ATB waypoints"
            )
            return json.dumps(payload, indent=2)
        return _tool_error(
            "drive_failed",
            error=str(payload.get("error") or payload.get("reason") or "drive_incomplete"),
            tool="drive_forward",
            robot_id=robot,
            **extra,
            reason=payload.get("reason"),
            pose=pose_after or None,
        )

    speed = _clamp_linear_speed(speed_mps)
    pose_before = _fetch_robot_pose(robot)
    drive_res = _drive_until_distance(
        robot, distance_m=signed, speed_mps=speed, rate_hz=20.0
    )
    final_stop = _stop_cmd_vel(robot)
    pose_after = _fetch_robot_pose(robot)
    ok = bool(drive_res.get("ok")) and bool(final_stop.get("ok"))
    traveled = drive_res.get("traveled_m")
    if traveled is None and pose_before.get("success") and pose_after.get("success"):
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
            tool="drive_forward",
            robot_id=robot,
            requested_m=signed,
            traveled_m=traveled,
            message=f"{robot} drive_forward did not complete cleanly",
        )
    direction = "forward" if signed >= 0 else "reverse"
    return json.dumps(
        {
            "success": True,
            "robot_id": robot,
            "requested_m": signed,
            "traveled_m": traveled,
            "direction": direction,
            "pose_before": pose_before if pose_before.get("success") else pose_before,
            "pose_after": pose_after if pose_after.get("success") else pose_after,
            "message": (
                f"{robot} drove {direction} ~{traveled if traveled is not None else abs(signed)} m "
                f"(requested {signed} m) via cmd_vel and stopped"
            ),
        },
        indent=2,
    )


@mcp.tool()
async def rotate_by(
    robot_id: str,
    degrees: float,
    turn_speed_rps: float = 0.5,
) -> str:
    """Rotate in place by degrees. Q1: ATB waypoint at current xy with new yaw.

    Do not use cmd_vel on Q1 — nothing listens, and ros2 topic pub stalls IMU/odom.
    Positive degrees = left / counter-clockwise. Negative = right / clockwise.
    Example: rotate_by(robot_id='q1', degrees=90).
    Remroc still uses open-loop cmd_vel. Failures emit rotate_failed.
    """
    return await asyncio.to_thread(
        _rotate_by_sync, robot_id, degrees, turn_speed_rps
    )


@mcp.tool()
async def drive_forward(
    robot_id: str,
    distance_m: float,
    speed_mps: float = 0.25,
) -> str:
    """Drive straight along the current heading. Q1: ATB waypoint a few metres ahead.

    Do not use cmd_vel on Q1 — nothing listens, and ros2 topic pub stalls IMU/odom.
    Positive distance_m = forward. Negative = reverse.
    Example: drive_forward(robot_id='q1', distance_m=1.5).
    Remroc still uses open-loop cmd_vel. Failures emit drive_failed.
    """
    return await asyncio.to_thread(
        _drive_forward_sync, robot_id, distance_m, speed_mps
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


# --- Navigation (Nav2 remroc, or ATB behavior_control on Q1) -----------------

def _wrap_yaw(yaw: float) -> float:
    return (float(yaw) + math.pi) % (2.0 * math.pi) - math.pi


def _yaw_error(a: float, b: float) -> float:
    return abs(_wrap_yaw(float(a) - float(b)))


def _atb_jog_radius(distance_m: float) -> float:
    """Acceptance radius small enough that a short jog actually translates."""
    dist = abs(float(distance_m))
    return max(0.15, min(0.4, dist * 0.35))


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
    and smears occupancy). ``rotate_by`` turns it on so ATB actually yaws.
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
        return json.dumps(
            {
                "success": True,
                "robot_id": robot,
                "x": x,
                "y": y,
                "yaw": yaw,
                "utm_xy": {"x": ux, "y": uy},
                "frame_id": "map",
                "nav": "atb_behavior_control",
                "status": status,
                "reason": reason,
                "pose": pose,
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
    """Blocking navigate (ATB waypoints on Q1, Nav2 otherwise)."""
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

    if _use_atb_nav():
        return _navigate_atb_sync(robot, float(x), float(y), float(yaw))

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
    """Drive Q1 to map-frame (x, y, yaw) and wait until it is close enough.

    Calls /behavior_control/set_waypoints with UTM converted from the open-world
    map frame (same origin as launch.sh). Waits until ATB publishes Goal reached
    on /behavior_control/nav_events. Use robot_id 'q1'.
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
        if not objects:
            payload["hint"] = (
                "No labeled objects in range. That is success, not a tool "
                "error. Sense again after moving. Do not invent object "
                "coordinates."
            )
        return json.dumps(payload, indent=2)

    return await asyncio.to_thread(_run)


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
