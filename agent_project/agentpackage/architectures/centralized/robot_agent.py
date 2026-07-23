
from __future__ import annotations

import argparse
from functools import cached_property
from typing import Literal

from langchain.tools import tool

from ...BaseAgents import BaseAgent, AgentSpec
from ...config import TB_TO_ROBOT_ID
from ...mcp_client import load_mcp_tools_safe
from .agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class RobotAgent(BaseAgent):
    """A worker agent with no delegation authority: it only ever reacts to
    requests coming from the master through the central broker."""

    def __init__(self, tb_id: TB_ID):
        self.tb_id = tb_id
        rid = TB_TO_ROBOT_ID[tb_id]
        super().__init__(
            AgentSpec(
                name=f"robot_{tb_id}",
                description=f"Controls robot {tb_id}.",
                system_prompt=(
                    f"You are the physical robot agent for {tb_id} (fleet id {rid}). "
                    f"You have MCP tools for ROS navigation, poses, and items. "
                    f"Prefer the tools named get_my_amcl_pose and move_me_to — "
                    f"they only affect this robot. For shared state use get_whiteboard and add_to_whiteboard. "
                    f"When using any tool that still takes a robot id, always use {tb_id} or {rid} "
                    f"for this robot only. Keep answers short and precise. "
                    f"Do not check battery status unless the user explicitly asks. "
                    f"Never hallucinate values: if data is unavailable, say so clearly."
                ),
            ),
            architecture="centralized",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        base = list(self._mcp_tools_by_name.values())
        if not base:
            return []
        by_name = {t.name: t for t in base}
        hidden = frozenset({"get_robot_amcl_pose", "move_robot"})
        shared = [t for t in base if t.name not in hidden]
        tb = self.tb_id
        rid = TB_TO_ROBOT_ID[tb]

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

        return [get_my_amcl_pose, move_me_to, *shared]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one robot agent that can answer local user input and master requests (centralized architecture)."
    )
    parser.add_argument("--tb-id", choices=["tb1", "tb2", "tb3", "tb4"], required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--thread-id", default="local")
    args = parser.parse_args()

    robot = RobotAgent(args.tb_id)
    bus = BusClient(f"robot_{args.tb_id}", host=args.host, port=args.port)

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

        print(f"\n[{src} -> robot_{args.tb_id}] {text}")
        try:
            reply = robot.invoke(text, thread_id=thread_id)
        except Exception as e:
            reply = (
                f"robot_{args.tb_id} failed to process request: {type(e).__name__}: {e}. "
                "Please retry with a shorter request or reduced context."
            )
        print(f"[robot_{args.tb_id}] {reply}")

        bus.send(
            type="agent_reply",
            to=src,
            text=reply,
            thread_id=thread_id,
            request_id=msg.get("request_id"),
        )

    bus.on_message(handle_message)

    print(f"robot_{args.tb_id} is online.")
    print("Type directly to chat with this robot locally. Use Ctrl+C to exit.\n")

    try:
        while True:
            line = input(f"robot_{args.tb_id}> ").strip()
            if not line:
                continue
            reply = robot.invoke(line, thread_id=args.thread_id)
            print(f"[robot_{args.tb_id}] {reply}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        bus.close()


if __name__ == "__main__":
    main()
