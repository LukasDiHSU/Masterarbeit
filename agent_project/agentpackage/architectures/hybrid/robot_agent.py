
from __future__ import annotations

import argparse
import json
from functools import cached_property
from typing import Literal

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_HOST,
    PLANNER_NAME,
    TB_IDS,
    TB_TO_ROBOT_ID,
    robot_peer_name,
)
from ...mcp_client import load_mcp_tools_safe
from ..centralized.agent_bus import DEFAULT_HOST, DEFAULT_PORT
from .phase_bus import PhasedBus

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class HybridRobotAgent(BaseAgent):
    """Phase 1: behaves like a pure centralized worker -- it only reacts to
    requests from the planner via the broker, and its peer tools report
    that the decentralized phase has not started yet.

    Phase 2: once the planner activates the mesh, its peer tools start
    working and it can coordinate directly with any other robot (and the
    planner) without going back through the broker.
    """

    def __init__(self, tb_id: TB_ID, *, bus: PhasedBus):
        self.tb_id = tb_id
        self.bus = bus
        self._active_thread_id = "default"
        self._other_peers = [robot_peer_name(t) for t in TB_IDS if t != tb_id] + [PLANNER_NAME]
        rid = TB_TO_ROBOT_ID[tb_id]

        super().__init__(
            AgentSpec(
                name=robot_peer_name(tb_id),
                description=f"Hybrid agent for robot {tb_id}: centralized worker first, mesh peer after handoff.",
                system_prompt=(
                    f"You are the hybrid agent for robot {tb_id} (fleet id {rid}). "
                    "At first you only receive instructions from the planner through the broker -- just carry "
                    "them out with your local tools (get_my_amcl_pose, move_me_to, whiteboard, items) and reply. "
                    "Once the planner has activated the decentralized phase, ask_peer and ask_all_peers will start "
                    "working: from then on you may coordinate directly with the other robots (and the planner) "
                    "instead of waiting for the planner to relay everything. If ask_peer/ask_all_peers report the "
                    "decentralized phase is not active yet, just keep working locally and wait. "
                    "Keep answers short and precise. Do not check battery status unless explicitly asked. "
                    "Never hallucinate values: if data is unavailable, say so clearly."
                ),
            ),
            architecture="hybrid",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        base = list(self._mcp_tools_by_name.values())
        by_name = {t.name: t for t in base}
        hidden = frozenset({"get_robot_amcl_pose", "move_robot"})
        shared = [t for t in base if t.name not in hidden]
        tb = self.tb_id
        rid = TB_TO_ROBOT_ID[tb]

        local_tools: list = []
        if by_name:
            @tool
            async def get_my_amcl_pose() -> str:
                """Get this robot's AMCL pose (ROS `ros2 topic echo /<tb>/amcl_pose --once`)."""
                t = by_name.get("get_robot_amcl_pose")
                if t is None:
                    return ""
                return str(await t.ainvoke({"robot": tb}))

            @tool
            async def move_me_to(x: float, y: float, z: float) -> str:
                """Send a Nav2 navigate_to_pose goal for this robot to map position (x, y, z)."""
                t = by_name.get("move_robot")
                if t is None:
                    return ""
                return str(await t.ainvoke({"robot_id": rid, "x": x, "y": y, "z": z}))

            local_tools = [get_my_amcl_pose, move_me_to, *shared]

        @tool
        def ask_peer(peer_name: str, message: str) -> str:
            """Send a task or question directly to another robot or the planner.
            Only works after the planner has activated the decentralized phase.

            Args:
                peer_name: e.g. "robot_tb2" or "planner".
                message: The task or question to send.
            """
            if not self.bus.mesh_active:
                return json.dumps(
                    {
                        "error": "decentralized_phase_not_active",
                        "message": "The planner has not activated the mesh yet. Keep working locally for now.",
                    }
                )
            if peer_name not in self._other_peers:
                return json.dumps(
                    {"error": "unknown_peer", "known_peers": self._other_peers}
                )
            return self.bus.ask(
                peer_name,
                message,
                thread_id=f"{self.thread_key(self._active_thread_id)}->{peer_name}",
            )

        @tool
        def ask_all_peers(message: str) -> str:
            """Send the same task or question to every other robot (and the planner) in
            parallel and wait for all replies. Only works after the decentralized phase
            has been activated."""
            if not self.bus.mesh_active:
                return json.dumps(
                    {
                        "error": "decentralized_phase_not_active",
                        "message": "The planner has not activated the mesh yet. Keep working locally for now.",
                    }
                )
            replies = self.bus.ask_many(
                self._other_peers,
                message,
                thread_id=f"{self.thread_key(self._active_thread_id)}->all",
            )
            return json.dumps(replies, ensure_ascii=False, indent=2)

        return [*local_tools, ask_peer, ask_all_peers]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one robot agent for the hybrid architecture (centralized first, then mesh)."
    )
    parser.add_argument("--tb-id", choices=list(TB_IDS), required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--thread-id", default="local")
    args = parser.parse_args()

    my_name = robot_peer_name(args.tb_id)
    bus = PhasedBus(my_name, broker_host=args.host, broker_port=args.port)
    robot = HybridRobotAgent(args.tb_id, bus=bus)

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

        print(f"\n[{src} -> {my_name}] {text}")
        try:
            reply_text = robot.invoke(text, thread_id=thread_id)
        except Exception as e:
            reply_text = (
                f"{my_name} failed to process request: {type(e).__name__}: {e}. "
                "Please retry with a shorter request or reduced context."
            )
        print(f"[{my_name}] {reply_text}")
        bus.reply(msg, reply_text)

    bus.on_message(handle_message)

    print(f"{my_name} is online (hybrid). Starts centralized; becomes mesh-capable once the planner activates it.")
    print("Type directly to chat with this robot locally. Use Ctrl+C to exit.\n")

    try:
        while True:
            line = input(f"{my_name}> ").strip()
            if not line:
                continue
            reply = robot.invoke(line, thread_id=args.thread_id)
            print(f"[{my_name}] {reply}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        bus.close()


if __name__ == "__main__":
    main()
