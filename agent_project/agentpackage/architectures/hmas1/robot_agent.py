from __future__ import annotations

import argparse
from functools import cached_property
from typing import Literal

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import TB_IDS, TB_TO_ROBOT_ID, nav_id_for_tb, robot_peer_name
from ...mcp_client import load_mcp_tools_safe
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class HMAS1RobotAgent(BaseAgent):
    """Local HMAS-1 agent: joins turn-based dialogue after the central primer,
    may output EXECUTE, then carries out its action when dispatched.
    """

    def __init__(self, tb_id: TB_ID):
        self.tb_id = tb_id
        self.nav_id = nav_id_for_tb(tb_id)
        rid = TB_TO_ROBOT_ID[tb_id]

        super().__init__(
            AgentSpec(
                name=robot_peer_name(tb_id),
                description=(
                    f"HMAS-1 local agent for {tb_id}: turn-based dialogue, then execute."
                ),
                system_prompt=(
                    f"You are {robot_peer_name(tb_id)} (nav id {self.nav_id}, fleet id {rid}) "
                    "in the HMAS-1 architecture.\n"
                    "\n"
                    "ROLE SPLIT:\n"
                    "- The central planner ONLY sends ONE initial priming plan (to everyone or "
                    "to one robot). It does not ask for confirmation, does not send follow-up "
                    "plans, and does not join your discussion.\n"
                    "- After that plan, robots discuss in fixed turn order (system-driven; "
                    "there is no free mesh / ask_peer). On each of your turns you see the "
                    "initial plan plus all prior robot comments.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- On a dialogue turn: refine the plan from YOUR perspective. Use MCP "
                    "tools for local checks (list_available_boxes, get_station, get_events, "
                    "whiteboard) if needed. Keep comments as short but precise as possible.\n"
                    "- When the group should act, start your reply with EXECUTE, then one "
                    "action line per participant, e.g.:\n"
                    "  EXECUTE\n"
                    f"  {robot_peer_name(tb_id)}: ...\n"
                    "  robot_tb2: ...\n"
                    "- On a later EXECUTE APPROVED message: carry out YOUR action with MCP: "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    "pickup_box/drop_box with that robot_id, etc.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- Do not wait for the planner to confirm anything; it already finished "
                    "its job after the one initial plan.\n"
                    "- You cannot message other robots directly; turn-taking is orchestrated "
                    "for you.\n"
                    "- Do not run navigate/pickup during discussion turns — wait for "
                    "EXECUTE APPROVED.\n"
                    "- Do not invent a wholly different mission; refine the initial plan.\n"
                    "\n"
                    "WORDING: say you SEND or receive a message. Do not say broadcast.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="HMAS-1",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one HMAS-1 robot agent (turn-based dialogue participant)."
    )
    parser.add_argument("--tb-id", choices=list(TB_IDS), required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--thread-id", default="local")
    args = parser.parse_args()

    my_name = robot_peer_name(args.tb_id)
    robot = HMAS1RobotAgent(args.tb_id)
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

    print(f"{my_name} is online (HMAS-1). Turn-based dialogue after central priming.")
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
