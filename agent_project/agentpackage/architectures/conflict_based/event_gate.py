"""Helpers for conflict-based coordination."""

from __future__ import annotations

import json
from typing import Any

from ...config import peer_name_for_robot_id


def peer_names_from_robot_ids(robot_ids: list[str]) -> list[str]:
    peers: list[str] = []
    for rid in robot_ids:
        peer = peer_name_for_robot_id(rid)
        if peer and peer not in peers:
            peers.append(peer)
    return peers


def participants_for_event(event: dict[str, Any], held_by: dict[str, Any] | None = None) -> list[str]:
    """Return mesh peer names that should open negotiation for this event."""
    etype = str(event.get("type", ""))
    held_by = held_by or {}

    if etype == "conflict":
        raw = event.get("participants") or []
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.split(",") if p.strip()]
        return peer_names_from_robot_ids([str(x) for x in raw])

    if etype == "box_missing":
        ids = [str(event.get("robot_id", ""))]
        expected = event.get("expected_box")
        if expected:
            for rid, box in held_by.items():
                if box == expected:
                    ids.append(str(rid))
        return peer_names_from_robot_ids([i for i in ids if i])

    if etype == "station_occupied":
        ids = [str(event.get("robot_id", ""))]
        # Anyone currently holding nothing at that station is unknown; include
        # holder of the occupying box if known.
        occupying = event.get("box_id")
        if occupying:
            for rid, box in held_by.items():
                if box == occupying:
                    ids.append(str(rid))
        return peer_names_from_robot_ids([i for i in ids if i])

    if etype in {"nav_aborted", "nav_failed", "tool_failed"}:
        ids = [str(event.get("robot_id", ""))]
        blocker = event.get("blocking_robot")
        if blocker:
            ids.append(str(blocker))
        # Generic tool_failed may list involved peers.
        extra = event.get("participants") or event.get("peers") or []
        if isinstance(extra, str):
            extra = [p.strip() for p in extra.split(",") if p.strip()]
        ids.extend(str(x) for x in extra)
        return peer_names_from_robot_ids([i for i in ids if i])

    if etype in {
        "robot_already_holding",
        "robot_not_holding",
        "station_not_found",
        "drive_failed",
        "pose_failed",
        "laser_failed",
    }:
        return peer_names_from_robot_ids([str(event.get("robot_id", ""))])

    return []


def parse_mcp_json(raw: str | Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        # langchain-mcp-adapters hands back content blocks, e.g.
        # [{"type": "text", "text": "{...}", "id": "lc_..."}] — unwrap them
        # before deciding this is a plain JSON array.
        texts: list[str] = []
        for block in raw:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts:
            parsed = parse_mcp_json("\n".join(texts))
            if parsed is not None:
                return parsed
        return raw
    text = str(raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def format_event_prompt(event: dict[str, Any], my_peer: str, allowed_peers: list[str]) -> str:
    others = [p for p in allowed_peers if p != my_peer]
    return (
        "EVENT-TRIGGERED NEGOTIATION OPEN.\n"
        f"Event: {json.dumps(event, ensure_ascii=False)}\n"
        f"You ({my_peer}) may SEND messages ONLY to: {others or '(none — resolve alone)'}.\n"
        "Do not contact any other robot. Do not start unrelated tasks.\n"
        "1) Talk FIRST: if peers are listed, use negotiate_with to exchange status and "
        "agree who yields / who proceeds.\n"
        "2) When resolved, call end_negotiation.\n"
        "Keep replies as short but precise as possible."
    )
