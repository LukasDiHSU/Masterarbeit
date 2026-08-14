from __future__ import annotations

import argparse
import threading
from functools import cached_property

from ...BaseAgents import BaseAgent, AgentSpec
from ...config import TB_IDS, nav_id_for_tb, robot_peer_name
from ...instructions import centralized_robot
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
                system_prompt=centralized_robot(name=name, nav_id=self.nav_id),
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
