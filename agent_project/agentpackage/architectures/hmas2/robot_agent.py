from __future__ import annotations

import argparse
from functools import cached_property
from typing import Literal

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import TB_TO_ROBOT_ID, nav_id_for_tb, robot_peer_name
from ...mcp_client import load_mcp_tools_safe
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class HMAS2RobotAgent(BaseAgent):
    """Local HMAS-2 agent: reviews the central plan (AGREE/DISAGREE), then
    executes only after the planner sends an EXECUTE message. No peer tools.
    """

    def __init__(self, tb_id: TB_ID):
        self.tb_id = tb_id
        self.nav_id = nav_id_for_tb(tb_id)
        rid = TB_TO_ROBOT_ID[tb_id]

        super().__init__(
            AgentSpec(
                name=robot_peer_name(tb_id),
                description=(
                    f"HMAS-2 local agent for {tb_id}: reviews central plans, then executes."
                ),
                system_prompt=(
                    f"You are {robot_peer_name(tb_id)} (nav id {self.nav_id}, fleet id {rid}) "
                    "in the HMAS-2 architecture.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Answer messages from the central planner.\n"
                    "- On a PLAN REVIEW REQUEST: inspect YOUR assignment only. Use MCP tools "
                    "if needed (list_available_boxes, get_station, get_events, whiteboard) to "
                    "check feasibility. Reply with exactly one line:\n"
                    "  AGREE: <short reason>\n"
                    "  or\n"
                    "  DISAGREE: <what is wrong / safer alternative>\n"
                    "  Do NOT execute during review.\n"
                    "- On an EXECUTE / approved-plan message: carry out YOUR part with MCP tools: "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    "pickup_box/drop_box with that robot_id, etc.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- You cannot send messages to other robots; only the planner coordinates.\n"
                    "- You do not invent a fleet-wide plan. Review or execute only YOUR part.\n"
                    "- Do not start executing during a PLAN REVIEW REQUEST.\n"
                    "\n"
                    "WORDING: say you SEND or receive a message. Do not say broadcast.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="HMAS-2",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one HMAS-2 robot agent (local review + execute via central planner)."
    )
    parser.add_argument("--tb-id", choices=["tb1", "tb2", "tb3", "tb4"], required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--thread-id", default="local")
    args = parser.parse_args()

    my_name = robot_peer_name(args.tb_id)
    robot = HMAS2RobotAgent(args.tb_id)
    bus = BusClient(my_name, host=args.host, port=args.port)

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
            reply = robot.invoke(text, thread_id=thread_id)
        except Exception as e:
            reply = (
                f"{my_name} failed to process request: {type(e).__name__}: {e}. "
                "Please retry with a shorter request or reduced context."
            )
        print(f"[{my_name}] {reply}")
        bus.send(
            type="agent_reply",
            to=src,
            text=reply,
            thread_id=thread_id,
            request_id=msg.get("request_id"),
        )

    bus.on_message(handle_message)

    print(f"{my_name} is online (HMAS-2). Reviews central plans, then executes when told.")
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
