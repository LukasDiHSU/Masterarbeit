import os

DEFAULT_MODEL = os.getenv("AGENT_MODEL", "openai:gpt-5")

TB_TO_ROBOT_ID = {
    "tb1": "robot_1",
    "tb2": "robot_2",
    "tb3": "robot_3",
    "tb4": "robot_4",
}

TB_IDS = tuple(TB_TO_ROBOT_ID.keys())

# --- Centralized architecture (star broker) -------------------------------
DEFAULT_BROKER_HOST = os.getenv("AGENT_BROKER_HOST", "127.0.0.1")
DEFAULT_BROKER_PORT = int(os.getenv("AGENT_BROKER_PORT", "8765"))

# --- Decentralized / hybrid architecture (peer-to-peer mesh) --------------
# Every robot agent binds its own listening socket so it can be reached
# directly by any other peer, without going through a central broker.
DEFAULT_MESH_HOST = os.getenv("AGENT_MESH_HOST", "127.0.0.1")
DEFAULT_MESH_BASE_PORT = int(os.getenv("AGENT_MESH_BASE_PORT", "9101"))
PLANNER_NAME = "planner"
PLANNER_MESH_PORT = int(os.getenv("AGENT_PLANNER_MESH_PORT", "9100"))

# --- Shared message pool architecture (blackboard) ------------------------
# One shared broadcast log: every agent and the user connect to the same
# pool server, see every message ever posted, and can post to it directly.
DEFAULT_POOL_HOST = os.getenv("AGENT_POOL_HOST", "127.0.0.1")
DEFAULT_POOL_PORT = int(os.getenv("AGENT_POOL_PORT", "8866"))

# Fixed round-robin speaking order for the pool's turn-based mode:
# robot_tb1 -> robot_tb2 -> robot_tb3 -> robot_tb4 -> repeat. A user post
# (any name outside this order) always restarts the round at the front.
POOL_TURN_ORDER = tuple(f"robot_{tb_id}" for tb_id in TB_IDS)

# --- Usage monitor ----------------------------------------------------------
# A dedicated, separate process that every agent/transport fires small UDP
# telemetry packets at (token usage + inter-agent message counts) so you can
# watch a live dashboard instead of scrolling through each agent's own
# terminal. Reporting is fire-and-forget: nothing breaks if this isn't running.
DEFAULT_MONITOR_HOST = os.getenv("AGENT_MONITOR_HOST", "127.0.0.1")
DEFAULT_MONITOR_PORT = int(os.getenv("AGENT_MONITOR_PORT", "9900"))


def robot_peer_name(tb_id: str) -> str:
    """Canonical peer/agent name used on the bus/mesh for a given robot."""
    return f"robot_{tb_id}"


def build_peer_table(
    tb_ids: tuple[str, ...] = TB_IDS,
    *,
    host: str = DEFAULT_MESH_HOST,
    base_port: int = DEFAULT_MESH_BASE_PORT,
    include_planner: bool = False,
) -> dict[str, tuple[str, int]]:
    """Build a static ``name -> (host, port)`` table for the mesh network.

    Ports are assigned deterministically (base_port + index) so every
    process can compute the full peer table locally without discovery.
    """
    table: dict[str, tuple[str, int]] = {
        robot_peer_name(tb_id): (host, base_port + i) for i, tb_id in enumerate(tb_ids)
    }
    if include_planner:
        table[PLANNER_NAME] = (host, PLANNER_MESH_PORT)
    return table
