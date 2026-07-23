
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
    TB_IDS,
    TB_TO_ROBOT_ID,
    build_peer_table,
    robot_peer_name,
)
from ...mcp_client import load_mcp_tools_safe
from .mesh_bus import MeshNode

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class RobotPeerAgent(BaseAgent):
    """A fully symmetric peer: it has the same local robot-control tools as
    every other architecture's robot agent, plus delegation tools to reach
    ANY other peer directly. There is no master -- every peer decides for
    itself when to act locally and when to ask a neighbor for help."""

    def __init__(self, tb_id: TB_ID, *, mesh: MeshNode, peer_names: list[str]):
        self.tb_id = tb_id
        self.mesh = mesh
        self.peer_names = peer_names
        self._active_thread_id = "default"
        rid = TB_TO_ROBOT_ID[tb_id]

        super().__init__(
            AgentSpec(
                name=robot_peer_name(tb_id),
                description=f"Autonomous peer agent for robot {tb_id}. No central coordinator.",
                system_prompt=(
                    f"You are the autonomous peer agent for robot {tb_id} (fleet id {rid}) in a "
                    "DECENTRALIZED fleet: there is no master and no central coordinator. You are "
                    "one of several equal peers who can all talk to each other directly. "
                    "Handle requests about your own robot yourself using your local tools "
                    "(get_my_amcl_pose, move_me_to, whiteboard, items). If a request concerns another "
                    "robot, or requires comparing/coordinating with other robots, use ask_peer to talk "
                    "to that specific peer directly, or ask_all_peers to fan out to every other peer in "
                    "parallel and wait for all replies. Decide for yourself which peer(s) to involve -- "
                    "do not wait to be told. After gathering replies, synthesize one concise answer. "
                    "Keep answers short and precise. Do not check battery status unless explicitly asked. "
                    "Never hallucinate values: if data is unavailable, say so clearly."
                ),
            ),
            architecture="decentralized",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"

    def _ask_peer(self, peer_name: str, message: str) -> str:
        return self.mesh.ask(
            peer_name,
            message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{peer_name}",
        )

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
        others = [p for p in self.peer_names if p != self.spec.name]

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
            """Send a task or question directly to another peer robot and return its reply.

            Args:
                peer_name: The peer's bus name, e.g. "robot_tb2" (must be one of the other peers).
                message: The task or question to send.
            """
            if peer_name not in others:
                return json.dumps(
                    {
                        "error": "unknown_peer",
                        "message": f"{peer_name!r} is not a known peer.",
                        "known_peers": others,
                    }
                )
            return self._ask_peer(peer_name, message)

        @tool
        def ask_all_peers(message: str) -> str:
            """Send the same task or question to every other peer in parallel and wait for all replies as JSON."""
            replies = self.mesh.ask_many(
                others,
                message,
                thread_id=f"{self.thread_key(self._active_thread_id)}->all",
            )
            return json.dumps(replies, ensure_ascii=False, indent=2)

        return [*local_tools, ask_peer, ask_all_peers]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one autonomous peer agent in the decentralized (mesh) architecture. "
            "Every peer can be talked to directly and can talk to every other peer directly."
        )
    )
    parser.add_argument("--tb-id", choices=list(TB_IDS), required=True)
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    parser.add_argument("--thread-id", default="local")
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    my_name = robot_peer_name(args.tb_id)
    my_host, my_port = peer_table[my_name]
    peers_without_self = {n: hp for n, hp in peer_table.items() if n != my_name}

    mesh = MeshNode(my_name, my_host, my_port, peers_without_self)
    robot = RobotPeerAgent(args.tb_id, mesh=mesh, peer_names=list(peer_table.keys()))

    def handle_message(msg: dict) -> None:
        if msg.get("type") != "agent_request":
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
        mesh.reply(msg, reply_text)

    mesh.on_message(handle_message)

    print(f"{my_name} is online at {my_host}:{my_port} (mesh peers: {sorted(peers_without_self)}).")
    print("Type directly to chat with this peer locally. Use Ctrl+C to exit.\n")

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
        mesh.close()


if __name__ == "__main__":
    main()
