from __future__ import annotations

import argparse
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property
from typing import Any

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_HOST,
    TB_IDS,
    STATION_CAPACITY_RULE,
    build_peer_table,
    nav_id_for_tb,
    peer_name_for_robot_id,
    robot_peer_name,
)
from ...mcp_client import load_mcp_tools_safe
from .event_gate import format_event_prompt, parse_mcp_json, participants_for_event
from .mesh_bus import MeshNode

_COMPLETION_CHECK_RE = re.compile(r"^\s*COMPLETION\s+CHECK\b", re.IGNORECASE)


class RobotPeerAgent(BaseAgent):
    """Conflict-based peer.

    Solo by default; ``negotiate_with`` on events. Before ending a mission,
    ``report_done_and_confirm`` informs peers and collects AGREE/DISAGREE.
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
        self._status_lock = threading.Lock()
        self._my_part_done = False
        self._done_summary = ""
        self._status_note = "mission started; still working"
        self._request_depth = 0
        self._peer_says_done: set[str] = set()
        name = robot_peer_name(robot_id)
        others = [p for p in peer_names if p != name]

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
                    "pickup_box/drop_box with that robot_id, get_events.\n"
                    f"- {STATION_CAPACITY_RULE}\n"
                    "- Station ids are station_A..station_D (short A/B/C/D also work).\n"
                    "- ACTION: rank_stations_by_distance once → navigate_to_pose. "
                    "Do not call get_peer_distances before navigating. "
                    "Only after nav fails: get_peer_distances and/or drive_distance, then retry.\n"
                    "- Tool failures emit MCP events; negotiation opens when you (and any "
                    "blocking peer) are involved.\n"
                    "- On an open event: talk FIRST with negotiate_with, then optionally store "
                    "a short fact on the whiteboard (storage only), then end_negotiation.\n"
                    "- Before you end your mission reply you MUST call "
                    "report_done_and_confirm(summary=...) to tell peers what you did and ask "
                    f"whether the fleet task is finished. Peers: {', '.join(others) or '(none)'}.\n"
                    "  Only treat the round as finished if that tool returns all_agree=true. "
                    "If anyone DISAGREEs, keep working (or help) and call it again later.\n"
                    "- If a peer tells you the deliveries are already complete (boxes at the "
                    "right stations), BELIEVE them: call report_done_and_confirm, do NOT pick "
                    "boxes back up from destinations, do NOT undo finished work.\n"
                    "- Optional: set_work_status(note=...) while working so peers see progress "
                    "during completion checks.\n"
                    "\n"
                    "WHITEBOARD (storage only — NOT chat):\n"
                    "- Store durable facts only. Do NOT chat or replace negotiate_with.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- No continuous group discussion. SEND to peers only when negotiation is "
                    "open OR via report_done_and_confirm.\n"
                    "- negotiate_with fails outside an event — keep working alone.\n"
                    "- Do not chat via the whiteboard.\n"
                    "- Do not end your final mission answer without report_done_and_confirm.\n"
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
        self._request_depth += 1
        try:
            # Peer told us the fleet is done — remember that for completion checks.
            low = (message or "").lower()
            if any(
                k in low
                for k in (
                    "deliveries complete",
                    "already delivered",
                    "fleet finished",
                    "task is finished",
                    "consider the deliveries complete",
                    "confirm whether you consider the deliveries complete",
                )
            ):
                src_hint = thread_id.split("->")[0] if "->" in thread_id else ""
                with self._status_lock:
                    if src_hint:
                        self._peer_says_done.add(src_hint)
                    self._status_note = "peer reports deliveries complete"
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._request_depth = max(0, self._request_depth - 1)
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
        with self._gate_lock:
            if self._allowed_peers is None:
                self._allowed_peers = set()
            self._allowed_peers.add(peer_name)

    def completion_check_reply(self, from_peer: str, text: str) -> str:
        """Fast reply to COMPLETION CHECK (no nested mesh asks — avoids deadlock)."""
        # Only score the peer's summary — the template always says "finished my part".
        summary_match = re.search(
            r"finished my part:\s*(.+?)(?:\n|$)",
            text or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        summary_low = (summary_match.group(1) if summary_match else text or "").lower()
        peer_claims_fleet_done = any(
            k in summary_low
            for k in (
                "delivered",
                "delivering",
                "fleet finished",
                "fleet task",
                "task is finished",
                "completed both",
                "all packages",
                "deliveries complete",
                "already complete",
                "a->c",
                "b->d",
                "both deliveries",
            )
        )
        status_l = ""
        with self._status_lock:
            done = self._my_part_done
            summary = self._done_summary
            status = self._status_note
            status_l = (status or "").lower()
            if peer_claims_fleet_done:
                self._peer_says_done.add(from_peer)
            peers_done = set(self._peer_says_done)

        waiting = any(
            s in status_l
            for s in (
                "awaiting peer",
                "waiting for peer",
                "part done",
                "all packages delivered",
            )
        )

        # I already finished my part → always AGREE so the peer can close.
        if done:
            return (
                f"AGREE: fleet finished from my side. "
                f"I ({self.spec.name}) done: {summary or status or 'my part complete'}"
            )
        # Peer reports the deliveries are done (or I'm only waiting on them) → AGREE.
        # Do not keep DISAGREE-ing just because this robot never got a box.
        if peer_claims_fleet_done or waiting or from_peer in peers_done:
            return (
                f"AGREE: accepting that deliveries are complete. "
                f"I ({self.spec.name}) status was: {status or 'in progress'}"
            )
        return (
            f"DISAGREE: still working. "
            f"I ({self.spec.name}) status: {status or 'in progress'} "
            f"(asked by {from_peer})"
        )

    def _ask_peer(self, peer_name: str, message: str, *, timeout: float = 120.0) -> str:
        return self.mesh.ask(
            peer_name,
            message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{peer_name}",
            timeout=timeout,
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
        local_tools = list(self._mcp_tools_by_name.values())

        @tool
        def set_work_status(note: str) -> str:
            """Update your short status string (seen by peers during completion checks)."""
            with self._status_lock:
                self._status_note = (note or "").strip() or self._status_note
            return "status_updated"

        @tool
        def negotiate_with(peer_name: str, message: str) -> str:
            """SEND a message to one peer involved in the current event. Only while negotiation is open."""
            if self._request_depth > 1:
                return json.dumps(
                    {
                        "error": "nested_ask_forbidden",
                        "message": (
                            "You are already answering a peer. Put your reply in the "
                            "final text — do not call negotiate_with or report_done_and_confirm now."
                        ),
                    }
                )
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
            # Short timeout so mutual negotiate_with cannot hang the fleet forever.
            try:
                return self._ask_peer(peer_name, message, timeout=45.0)
            except Exception as e:
                return json.dumps(
                    {
                        "error": "negotiate_timeout_or_fail",
                        "peer_name": peer_name,
                        "message": str(e),
                    }
                )

        @tool
        def end_negotiation() -> str:
            """Close the event-triggered negotiation window and return to solo work."""
            self.close_negotiation()
            return "negotiation_closed"

        @tool
        def report_done_and_confirm(summary: str) -> str:
            """Inform all peers what you did and ask if the fleet mission is finished.

            Call BEFORE your final mission answer. Only end as finished if all_agree is true.
            """
            if self._request_depth > 1:
                return json.dumps(
                    {
                        "error": "nested_ask_forbidden",
                        "all_agree": False,
                        "message": (
                            "Cannot run completion check while answering a peer. "
                            "Finish your text reply; call report_done_and_confirm on your own turn."
                        ),
                    },
                    indent=2,
                )

            summary = (summary or "").strip() or "my assigned work"
            with self._status_lock:
                self._my_part_done = True
                self._done_summary = summary
                self._status_note = f"part done: {summary}"

            others = [p for p in self.peer_names if p != self.spec.name]
            if not others:
                return json.dumps(
                    {
                        "all_agree": True,
                        "summary": summary,
                        "message": "No peers — you may end as finished.",
                        "replies": {},
                    },
                    indent=2,
                )

            body = (
                "COMPLETION CHECK — reply with ONE line only (AGREE: or DISAGREE:), no tools:\n"
                f"From {self.spec.name}: I finished my part: {summary}\n"
                "Do you agree the overall fleet mission is finished?\n"
                "AGREE: fleet finished  OR  DISAGREE: <what remains>."
            )

            replies: dict[str, str] = {}

            def _ask(peer: str) -> tuple[str, str]:
                try:
                    return peer, self._ask_peer(peer, body, timeout=45.0)
                except Exception as e:
                    return peer, f"DISAGREE: no reply ({type(e).__name__}: {e})"

            with ThreadPoolExecutor(max_workers=max(1, len(others))) as pool:
                futs = [pool.submit(_ask, peer) for peer in others]
                for fut in as_completed(futs):
                    peer, text = fut.result()
                    replies[peer] = text

            agree: list[str] = []
            disagree: list[str] = []
            for peer, text in replies.items():
                head = (text or "").strip().splitlines()[0].strip().upper() if text else ""
                if head.startswith("AGREE"):
                    agree.append(peer)
                    with self._status_lock:
                        self._peer_says_done.add(peer)
                else:
                    disagree.append(peer)

            all_agree = bool(replies) and not disagree
            # Keep _my_part_done True so later peer completion checks can AGREE to us.
            if not all_agree:
                with self._status_lock:
                    self._status_note = (
                        f"part done, awaiting peers: {', '.join(disagree)}"
                    )

            return json.dumps(
                {
                    "all_agree": all_agree,
                    "summary_sent": summary,
                    "agree": agree,
                    "disagree": disagree,
                    "replies": replies,
                    "message": (
                        "All peers AGREEd — you may end the round as finished."
                        if all_agree
                        else (
                            "Not all AGREEd. If peers say deliveries are already done, "
                            "BELIEVE them and call report_done_and_confirm again without "
                            "undoing drops. Otherwise continue work, then retry."
                        )
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )

        return [
            *local_tools,
            set_work_status,
            negotiate_with,
            end_negotiation,
            report_done_and_confirm,
        ]

    def handle_event(self, event: dict[str, Any], held_by: dict[str, Any] | None = None) -> str | None:
        peers = participants_for_event(event, held_by=held_by)
        if self.spec.name not in peers:
            return None
        others = [p for p in peers if p != self.spec.name]
        self.open_negotiation(others, event=event)
        prompt = format_event_prompt(event, self.spec.name, peers)
        try:
            return self.invoke(prompt, thread_id=f"event-{event.get('type')}-{event.get('ts')}")
        finally:
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
            "Conflict-based peer: solo by default; negotiate on events; "
            "confirm done with peers before ending."
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

        if peer_name_for_robot_id(src) is not None:
            robot.allow_incoming_from(src)

        print(f"\n[{src} -> {my_name}] {text}")

        # Fast path: avoid nested LLM/mesh asks when several robots finish together.
        if _COMPLETION_CHECK_RE.search(text):
            reply_text = robot.completion_check_reply(src, text)
            print(f"[{my_name}] {reply_text}")
            mesh.reply(msg, reply_text)
            return

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
    print("Mode: solo by default; negotiate on events; confirm done with peers before ending.")
    print("Type a local mission for this robot only. Ctrl+C to exit.\n")

    try:
        while True:
            line = input(f"{my_name}> ").strip()
            if not line:
                continue
            mission = (
                f"SOLO MISSION (negotiate on events; before ending call "
                f"report_done_and_confirm):\n{line}\n"
                f"Use robot_id '{robot.nav_id}' for navigate_to_pose / pickup_box / drop_box.\n"
                f"{STATION_CAPACITY_RULE}"
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
