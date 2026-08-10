from __future__ import annotations

import argparse
from functools import cached_property

from ...BaseAgents import BaseAgent, AgentSpec
from ...config import TB_IDS, TB_TO_ROBOT_ID, nav_id_for_tb
from ...mcp_client import load_mcp_tools_safe
from .agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


class RobotAgent(BaseAgent):
    """A worker agent with no delegation authority: it only ever reacts to
    requests coming from the master through the central broker."""

    def __init__(self, tb_id: str):
        if tb_id not in TB_IDS:
            raise ValueError(f"Unknown robot {tb_id!r}; allowed: {list(TB_IDS)}")
        self.tb_id = tb_id
        rid = TB_TO_ROBOT_ID[tb_id]
        nav_id = nav_id_for_tb(tb_id)
        super().__init__(
            AgentSpec(
                name=f"robot_{tb_id}",
                description=f"Worker for robot {tb_id}; only answers the master.",
                system_prompt=(
                    f"You are robot_{tb_id} (nav id {nav_id}, fleet id {rid}) in the CENTRALIZED architecture.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Answer messages from the master.\n"
                    f"- MCP tools: list_available_boxes, get_station, navigate_to_pose(robot_id='{nav_id}', x, y), "
                    "pickup_box/drop_box with that robot_id, get_events, whiteboard.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- You cannot send messages to other robots; only the master can delegate.\n"
                    "- You do not invent fleet-wide plans; execute what the master asks.\n"
                    "\n"
                    "WORDING: say you SEND or receive a message. Do not say broadcast.\n"
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
    parser.add_argument("--tb-id", choices=list(TB_IDS), required=True)
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
