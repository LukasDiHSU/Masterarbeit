import os
import re

DEFAULT_MODEL = os.getenv("AGENT_MODEL", "openai:gpt-5")

# Number of working robots (SmallDeliveryRobot_0 .. _N-1). Leaders/planners are separate.
ALLOWED_AGENT_COUNTS = (2, 4, 6, 8)


def _parse_agent_count() -> int:
    raw = os.getenv("AGENT_COUNT", "4").strip()
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(
            f"AGENT_COUNT must be one of {ALLOWED_AGENT_COUNTS}, got {raw!r}"
        ) from e
    if n not in ALLOWED_AGENT_COUNTS:
        raise ValueError(
            f"AGENT_COUNT must be one of {ALLOWED_AGENT_COUNTS}, got {n}"
        )
    return n


AGENT_COUNT = _parse_agent_count()

# Canonical remroc / Nav2 namespaces — also used as agent/peer names on the bus.
ROBOT_IDS: tuple[str, ...] = tuple(
    f"SmallDeliveryRobot_{i}" for i in range(AGENT_COUNT)
)
# Back-compat alias for older imports (same values as ROBOT_IDS).
TB_IDS = ROBOT_IDS

# Optional per-robot Nav override (defaults to the id itself).
TB_TO_NAV_ID: dict[str, str] = {
    rid: os.getenv(f"AGENT_NAV_{rid}", rid) for rid in ROBOT_IDS
}
# Legacy env keys AGENT_NAV_TB1.. still work.
for _i, _rid in enumerate(ROBOT_IDS, start=1):
    _legacy = os.getenv(f"AGENT_NAV_TB{_i}")
    if _legacy:
        TB_TO_NAV_ID[_rid] = _legacy

NAV_ID_TO_TB = {nav: rid for rid, nav in TB_TO_NAV_ID.items()}

# Deprecated display alias (identity); kept so older imports keep working.
TB_TO_ROBOT_ID: dict[str, str] = {rid: rid for rid in ROBOT_IDS}

# --- Centralized architecture (star broker) -------------------------------
DEFAULT_BROKER_HOST = os.getenv("AGENT_BROKER_HOST", "127.0.0.1")
DEFAULT_BROKER_PORT = int(os.getenv("AGENT_BROKER_PORT", "8765"))

# --- Conflict-based architecture (peer-to-peer mesh) ---------------------
DEFAULT_MESH_HOST = os.getenv("AGENT_MESH_HOST", "127.0.0.1")
DEFAULT_MESH_BASE_PORT = int(os.getenv("AGENT_MESH_BASE_PORT", "9101"))
PLANNER_NAME = "planner"
PLANNER_MESH_PORT = int(os.getenv("AGENT_PLANNER_MESH_PORT", "9100"))
MESH_CLI_NAME = "CLI"  # Must sort *before* SmallDeliveryRobot_* so CLI dials peers
DEFAULT_MESH_CLI_PORT = int(os.getenv("AGENT_MESH_CLI_PORT", "9099"))

# --- Shared message pool architecture (blackboard) ------------------------
DEFAULT_POOL_HOST = os.getenv("AGENT_POOL_HOST", "127.0.0.1")
DEFAULT_POOL_PORT = int(os.getenv("AGENT_POOL_PORT", "8866"))

# Fixed round-robin speaking order (SmallDeliveryRobot_0 -> … -> _N-1 -> repeat).
POOL_TURN_ORDER = tuple(ROBOT_IDS)

# Shared inventory rule for all architecture system prompts (stations/boxes).
STATION_CAPACITY_RULE = (
    "STATION CAPACITY: Each station holds at most ONE box. "
    "Empty = box_id is null and available=false. "
    "Never drop_box on an occupied station (box_id set or available=true); "
    "check get_station / list_stations first. "
    "For opposing swaps (e.g. A↔C), pick up from both ends (or stage) so "
    "destinations are empty before dropping. "
    "Robots also hold at most ONE box; drop before picking another."
)

# --- Usage monitor ----------------------------------------------------------
DEFAULT_MONITOR_HOST = os.getenv("AGENT_MONITOR_HOST", "127.0.0.1")
DEFAULT_MONITOR_PORT = int(os.getenv("AGENT_MONITOR_PORT", "9900"))
# Scrolling LLM/tool trace window (separate from the usage table).
DEFAULT_TRACE_MONITOR_PORT = int(os.getenv("AGENT_TRACE_MONITOR_PORT", "9901"))

_TB_RE = re.compile(r"^tb(\d+)$", re.IGNORECASE)


def resolve_robot_id(token: str) -> str | None:
    """Map a user/LLM token to a canonical SmallDeliveryRobot_* id."""
    s = token.strip()
    if not s:
        return None
    if s in ROBOT_IDS:
        return s
    low = s.lower()
    for rid in ROBOT_IDS:
        if rid.lower() == low:
            return rid
    # Optional legacy peer prefix robot_<id>
    if low.startswith("robot_"):
        return resolve_robot_id(s[6:])
    m = _TB_RE.match(s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= AGENT_COUNT:
            return f"SmallDeliveryRobot_{n - 1}"
        return None
    if s.isdigit():
        n = int(s)
        if 0 <= n < AGENT_COUNT:
            return f"SmallDeliveryRobot_{n}"
    return None


def robot_peer_name(robot_id: str) -> str:
    """Canonical peer/agent name (same as remroc / Nav2 namespace)."""
    resolved = resolve_robot_id(robot_id)
    if resolved is None:
        raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(ROBOT_IDS)}")
    return resolved


def fleet_prompt_range() -> str:
    """Human-readable range like SmallDeliveryRobot_0..SmallDeliveryRobot_3."""
    if not ROBOT_IDS:
        return "(no robots)"
    if len(ROBOT_IDS) == 1:
        return ROBOT_IDS[0]
    return f"{ROBOT_IDS[0]}..{ROBOT_IDS[-1]}"


def nav_id_for_tb(robot_id: str) -> str:
    """Nav2 / MCP robot_id for a fleet robot id."""
    rid = resolve_robot_id(robot_id)
    if rid is None:
        raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(ROBOT_IDS)}")
    return TB_TO_NAV_ID[rid]


def peer_name_for_robot_id(robot_id: str) -> str | None:
    """Map an MCP/Nav robot_id (or peer / legacy tb id) to mesh peer name."""
    return resolve_robot_id(robot_id)


def build_peer_table(
    robot_ids: tuple[str, ...] = ROBOT_IDS,
    *,
    host: str = DEFAULT_MESH_HOST,
    base_port: int = DEFAULT_MESH_BASE_PORT,
    include_planner: bool = False,
    include_cli: bool = False,
) -> dict[str, tuple[str, int]]:
    """Build a static ``name -> (host, port)`` table for the mesh network.

    Ports are assigned deterministically (base_port + index) so every
    process can compute the full peer table locally without discovery.
    """
    table: dict[str, tuple[str, int]] = {
        robot_peer_name(rid): (host, base_port + i) for i, rid in enumerate(robot_ids)
    }
    if include_planner:
        table[PLANNER_NAME] = (host, PLANNER_MESH_PORT)
    if include_cli:
        table[MESH_CLI_NAME] = (host, DEFAULT_MESH_CLI_PORT)
    return table
