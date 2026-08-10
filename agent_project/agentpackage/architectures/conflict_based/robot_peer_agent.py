from __future__ import annotations

import argparse
import json
import threading
import time
from functools import cached_property
from typing import Any

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_HOST,
    TB_IDS,
    build_peer_table,
    nav_id_for_tb,
    peer_name_for_robot_id,
    robot_peer_name,
)
from ...mcp_client import load_mcp_tools_safe
from .event_gate import format_event_prompt, parse_mcp_json, participants_for_event
from .mesh_bus import MeshNode


class RobotPeerAgent(BaseAgent):
    """Conflict-based peer.

    Default mode is solo: plan/move with MCP tools only. Peer communication
    (``negotiate_with``) unlocks only while an event involves this robot, and
    only toward the event's participant subset.
    """

    def __init__(self, robot_id: str, *, mesh: MeshNode, peer_names: list[str]):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.mesh = mesh
        self.peer_names = peer_names
        self._active_thread_id = "default"
        self._gate_lock = threading.Lock()
        self._allowed_peers: set[str] | None = None
        self._active_event: dict[str, Any] | None = None
        name = robot_peer_name(robot_id)

        super().__init__(
            AgentSpec(
                name=name,
                description=f"Event-triggered peer for {name}. Solo by default.",
                system_prompt=(
                    f"You are {name} in the CONFLICT-BASED architecture.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Work alone with MCP tools: list_worlds, get_map_info, list_available_boxes, get_station, "
                    f"rank_stations_by_distance(robot_id='{self.nav_id}'), get_robot_pose, "
                    "distance_to_station, get_laser_snapshot, get_peer_distances, "
                    f"drive_distance(robot_id='{self.nav_id}', distance_m, direction_deg), "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    "pickup_box/drop_box with that robot_id, "
                    "get_events, whiteboard.\n"
                    "- Station ids are station_A..station_D (short A/B/C/D also work).\n"
                    "- ACTION: rank_stations_by_distance once → navigate_to_pose. "
                    "Do not call get_peer_distances before navigating. "
                    "Only after nav fails: get_peer_distances and/or drive_distance, then retry.\n"
                    "- When negotiation is open for an event: negotiate_with to SEND a message to listed peers only; "
                    "then end_negotiation.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- No continuous group discussion. Do not send messages to peers unless negotiation is open.\n"
                    "- negotiate_with fails outside an event — keep working alone.\n"
                    "- There is no master; do not wait for one.\n"
                    "\n"
                    "WORDING: say you SEND a message. Do not say broadcast.\n"
                    "FLEET: Other robots share this map. Navigate first; on failure use "
                    "get_peer_distances / drive_distance to clear peers, then retry.\n"
                    "TOOLS: If the same tool with the same arguments fails twice, do not call "
                    "it a third time — change the goal/approach or report failure.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="conflict_based",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"

    def open_negotiation(self, allowed_peers: list[str], event: dict[str, Any] | None = None) -> None:
        with self._gate_lock:
            self._allowed_peers = {p for p in allowed_peers if p != self.spec.name}
            self._active_event = event

    def close_negotiation(self) -> None:
        with self._gate_lock:
            self._allowed_peers = None
            self._active_event = None

    def allow_incoming_from(self, peer_name: str) -> None:
        """Unlock a one-peer reply path when another robot starts negotiating with us."""
        with self._gate_lock:
            if self._allowed_peers is None:
                self._allowed_peers = set()
            self._allowed_peers.add(peer_name)

    def _ask_peer(self, peer_name: str, message: str) -> str:
        return self.mesh.ask(
            peer_name,
            message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{peer_name}",
        )

    def _call_mcp_tool(self, name: str, args: dict[str, Any]) -> Any:
        t = self._mcp_tools_by_name.get(name)
        if t is None:
            return None
        try:
            raw = t.invoke(args)
        except Exception:
            try:
                import asyncio

                raw = asyncio.run(t.ainvoke(args))
            except Exception as e:
                return {"error": str(e)}
        return parse_mcp_json(raw)

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        # Hide raw get_events from the LLM a bit? Keep it — useful. No ask_all.
        local_tools = list(self._mcp_tools_by_name.values())

        @tool
        def negotiate_with(peer_name: str, message: str) -> str:
            """SEND a message to one peer involved in the current event. Only while negotiation is open.

            Args:
                peer_name: e.g. SmallDeliveryRobot_1
                message: status / proposal (who yields, who proceeds)
            """
            with self._gate_lock:
                allowed = self._allowed_peers
            if allowed is None:
                return json.dumps(
                    {
                        "error": "negotiation_not_active",
                        "message": "No event opened negotiation. Keep working alone.",
                    }
                )
            if peer_name not in allowed:
                return json.dumps(
                    {
                        "error": "peer_not_in_event",
                        "peer_name": peer_name,
                        "allowed_peers": sorted(allowed),
                    }
                )
            return self._ask_peer(peer_name, message)

        @tool
        def end_negotiation() -> str:
            """Close the event-triggered negotiation window and return to solo work."""
            self.close_negotiation()
            return "negotiation_closed"

        return [*local_tools, negotiate_with, end_negotiation]

    def handle_event(self, event: dict[str, Any], held_by: dict[str, Any] | None = None) -> str | None:
        """If this peer is involved, open the gate and run one negotiation turn."""
        peers = participants_for_event(event, held_by=held_by)
        if self.spec.name not in peers:
            return None
        # nav_aborted with only myself → solo recovery prompt, no peer tools needed
        others = [p for p in peers if p != self.spec.name]
        self.open_negotiation(others, event=event)
        prompt = format_event_prompt(event, self.spec.name, peers)
        try:
            return self.invoke(prompt, thread_id=f"event-{event.get('type')}-{event.get('ts')}")
        finally:
            # Leave gate open if model forgot end_negotiation but still mid-talk;
            # auto-close after the turn for nav_aborted solo / when no others.
            if not others:
                self.close_negotiation()


def _poll_events_loop(robot: RobotPeerAgent, stop: threading.Event, interval: float = 1.0) -> None:
    next_index = 0
    while not stop.wait(interval):
        payload = robot._call_mcp_tool("get_events", {"since_index": next_index})
        if not isinstance(payload, dict):
            continue
        events = payload.get("events") or []
        next_index = int(payload.get("next_index", next_index))
        if not events:
            continue
        held = robot._call_mcp_tool("get_held_boxes", {})
        held_by = held if isinstance(held, dict) else {}
        for event in events:
            if not isinstance(event, dict):
                continue
            print(f"\n[event] {json.dumps(event, ensure_ascii=False)}")
            reply = robot.handle_event(event, held_by=held_by)
            if reply is not None:
                print(f"[{robot.spec.name} event-response] {reply}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Conflict-based peer: works alone by default; "
            "opens negotiation only when MCP events involve this robot."
        )
    )
    parser.add_argument(
        "--robot-id",
        "--tb-id",
        dest="robot_id",
        choices=list(TB_IDS),
        required=True,
    )
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    parser.add_argument("--thread-id", default="local")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    my_name = robot_peer_name(args.robot_id)
    my_host, my_port = peer_table[my_name]
    peers_without_self = {n: hp for n, hp in peer_table.items() if n != my_name}

    mesh = MeshNode(my_name, my_host, my_port, peers_without_self)
    robot = RobotPeerAgent(args.robot_id, mesh=mesh, peer_names=list(peer_table.keys()))

    def handle_message(msg: dict) -> None:
        if msg.get("type") != "agent_request":
            return
        src = str(msg.get("from", "?"))
        text = str(msg.get("text", ""))
        thread_id = str(msg.get("thread_id", src))

        # Incoming peer negotiation unlocks reply to that peer only.
        if peer_name_for_robot_id(src) is not None:
            robot.allow_incoming_from(src)

        print(f"\n[{src} -> {my_name}] {text}")
        try:
            reply_text = robot.invoke(text, thread_id=thread_id)
        except Exception as e:
            reply_text = (
                f"{my_name} failed to process request: {type(e).__name__}: {e}. "
                "Please retry with a shorter request or reduced context."
            )
        print(f"[{my_name}] {reply_text}")
        mesh.reply(msg, reply_text)

    mesh.on_message(handle_message)

    stop = threading.Event()
    poller = threading.Thread(
        target=_poll_events_loop,
        args=(robot, stop, args.poll_interval),
        daemon=True,
    )
    poller.start()

    print(f"{my_name} online at {my_host}:{my_port} (nav id {robot.nav_id}).")
    print("Mode: solo by default; negotiation opens only on relevant MCP events.")
    print("Type a local mission for this robot only. Ctrl+C to exit.\n")

    try:
        while True:
            line = input(f"{my_name}> ").strip()
            if not line:
                continue
            # Solo mission — no fleet broadcast.
            mission = (
                f"SOLO MISSION (no peer chat unless an event opens negotiation):\n{line}\n"
                f"Use robot_id '{robot.nav_id}' for navigate_to_pose / pickup_box / drop_box."
            )
            reply = robot.invoke(mission, thread_id=args.thread_id)
            print(f"[{my_name}] {reply}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        stop.set()
        mesh.close()


if __name__ == "__main__":
    main()
