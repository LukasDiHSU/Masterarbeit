from __future__ import annotations

import argparse
import threading
from functools import cached_property

from ...BaseAgents import BaseAgent, AgentSpec
from ...config import TB_IDS, STATION_CAPACITY_RULE, nav_id_for_tb, robot_peer_name
from ...mcp_client import load_mcp_tools_safe
from .agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


class RobotAgent(BaseAgent):
    """A worker agent with no delegation authority: it only ever reacts to
    requests coming from the master through the central broker."""

    def __init__(self, robot_id: str):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        name = robot_peer_name(robot_id)
        super().__init__(
            AgentSpec(
                name=name,
                description=f"Worker for {name}; only answers the master.",
                system_prompt=(
                    f"You are {name} in the CENTRALIZED architecture.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Answer messages from the master.\n"
                    f"- MCP tools: list_worlds, get_map_info, list_available_boxes, get_station, "
                    f"rank_stations_by_distance(robot_id='{self.nav_id}'), get_robot_pose, "
                    f"distance_to_station, get_laser_snapshot, get_peer_distances, "
                    f"drive_distance(robot_id='{self.nav_id}', distance_m, direction_deg), "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    "pickup_box/drop_box with that robot_id, whiteboard.\n"
                    "- Station ids are station_A..station_D (short A/B/C/D also work). "
                    "Prefer navigate_xy from rank_stations_by_distance (slightly off the pad).\n"
                    f"- {STATION_CAPACITY_RULE} "
                    "If unsure before drop_box, call get_station.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- You cannot send messages to other robots; only the master can delegate.\n"
                    "- You do not invent fleet-wide plans; execute what the master asks.\n"
                    "\n"
                    "ACTION (keep tool use minimal):\n"
                    "- For 'go to farthest/nearest station': call rank_stations_by_distance ONCE, "
                    "then navigate_to_pose immediately. Do not call get_peer_distances first.\n"
                    "- Do not call get_all_robot_poses / list_stations / get_robot_pose repeatedly "
                    "for the same task. One gather → act → report.\n"
                    "- Only if navigate_to_pose fails: call get_peer_distances and/or "
                    "drive_distance (e.g. 1 m at ±90 deg) to clear a peer, then retry navigate.\n"
                    "\n"
                    "WORDING: say you SEND or receive a message. Do not say broadcast.\n"
                    "FLEET: Other robots share this map. Do not probe peers before navigating; "
                    "nav failure is usually another robot — then use get_peer_distances / drive_distance.\n"
                    "TOOLS: If the same tool with the same arguments fails twice, do not call "
                    "it a third time — change the goal/approach or report failure.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="centralized",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one robot agent that can answer local user input and master requests (centralized architecture)."
    )
    parser.add_argument(
        "--robot-id",
        "--tb-id",
        dest="robot_id",
        choices=list(TB_IDS),
        required=True,
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--thread-id", default="local")
    args = parser.parse_args()

    name = robot_peer_name(args.robot_id)
    robot = RobotAgent(args.robot_id)
    bus = BusClient(name, host=args.host, port=args.port)

    def handle_message(msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "error":
            print(f"\n[broker error] {msg.get('text', '')}")
            return

        if msg_type != "agent_request":
            return

        src = str(msg.get("from", "?"))
        text = str(msg.get("text", ""))
        thread_id = str(msg.get("thread_id", src))
        request_id = msg.get("request_id")

        print(f"\n[{src} -> {name}] {text}")

        # Off the recv thread so the bus stays responsive during long MCP/nav work.
        def _job() -> None:
            try:
                reply = robot.invoke(text, thread_id=thread_id)
            except Exception as e:
                reply = (
                    f"{name} failed to process request: {type(e).__name__}: {e}. "
                    "Please retry with a shorter request or reduced context."
                )
            print(f"[{name}] {reply}")
            bus.send(
                type="agent_reply",
                to=src,
                text=reply,
                thread_id=thread_id,
                request_id=request_id,
            )

        threading.Thread(
            target=_job, daemon=True, name=f"{name}-handle-{request_id or 'req'}"
        ).start()

    bus.on_message(handle_message)

    print(f"{name} is online.")
    print("Type directly to chat with this robot locally. Use Ctrl+C to exit.\n")

    try:
        while True:
            line = input(f"{name}> ").strip()
            if not line:
                continue
            reply = robot.invoke(line, thread_id=args.thread_id)
            print(f"[{name}] {reply}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        bus.close()


if __name__ == "__main__":
    main()
