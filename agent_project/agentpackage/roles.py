"""Q1 specialist roles: one agent per sensor (plus navigator) on a single robot."""

from __future__ import annotations

from typing import Any, Iterable

from .config import Q1_SPECIALIST_IDS, is_q1_platform

Q1_ROLE_MCP_TOOLS: dict[str, frozenset[str]] = {
    "navigator": frozenset(
        {
            "get_robot_pose",
            "navigate_to_pose",
            "get_occupancy_map",
            "drive_distance",
            "rotate_by",
            "drive_forward",
        }
    ),
    "lidar": frozenset({"get_lidar_snapshot", "get_semantic_lidar_objects"}),
    "camera": frozenset(
        {
            "get_semantic_camera_classes",
            "get_camera_image",
            "get_semantic_camera_image",
        }
    ),
}

Q1_CONFLICT_MCP_TOOLS = frozenset(
    {
        "get_events",
        "clear_events",
        "emit_conflict",
        "emit_detection_conflict",
    }
)


def q1_role_tools(role: str, *, extra: Iterable[str] = ()) -> frozenset[str]:
    base = Q1_ROLE_MCP_TOOLS.get(role, frozenset())
    return frozenset(base | frozenset(extra))


def filter_mcp_tools_for_agent(
    tools: list[Any],
    robot_id: str,
    *,
    extra: Iterable[str] = (),
    blocked: Iterable[str] = (),
) -> list[Any]:
    """Keep remroc filters as-is; on Q1 restrict each specialist to its sensors."""
    blocked_set = frozenset(blocked)
    if not is_q1_platform():
        return [t for t in tools if getattr(t, "name", "") not in blocked_set]
    allowed = q1_role_tools(robot_id, extra=extra)
    return [
        t
        for t in tools
        if getattr(t, "name", "") in allowed and getattr(t, "name", "") not in blocked_set
    ]


def is_q1_specialist(robot_id: str) -> bool:
    return robot_id in Q1_SPECIALIST_IDS
