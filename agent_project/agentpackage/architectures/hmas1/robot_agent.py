from __future__ import annotations

import argparse
import json
import re
from functools import cached_property

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, TB_IDS, nav_id_for_tb, robot_peer_name
from ...mcp_client import load_mcp_tools_safe
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT

_EXECUTE_APPROVED_RE = re.compile(r"EXECUTE\s+APPROVED", re.IGNORECASE)


class HMAS1RobotAgent(BaseAgent):
    """Local HMAS-1 agent: turn-based discussion until AGREE, then execute;
    may request another discussion round via ``start_discussion_round``.
    """

    def __init__(self, robot_id: str, *, bus: BusClient | None = None):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.bus = bus
        self._in_execute = False
        name = robot_peer_name(robot_id)

        super().__init__(
            AgentSpec(
                name=name,
                description=(
                    f"HMAS-1 local agent for {name}: discuss until AGREE, then execute."
                ),
                system_prompt=(
                    f"You are {name} in the HMAS-1 architecture ({AGENT_COUNT} robots).\n"
                    "\n"
                    "ROLE SPLIT:\n"
                    "- The central planner sends ONE priming plan (or a re-discussion context).\n"
                    "- Robots then discuss in fixed turn order. Each turn you see the plan plus "
                    "all prior robot comments.\n"
                    "- Discussion ends only when EVERY robot's latest message starts with AGREE.\n"
                    "- Then you receive EXECUTE APPROVED and carry out YOUR part with MCP tools.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- On a discussion turn: refine the plan. Prefer few or zero MCP tools "
                    "(list_available_boxes, get_station if needed). Reply AGREE: … or DISAGREE: …\n"
                    "- Do not navigate/pickup/drop during discussion turns.\n"
                    "- On EXECUTE APPROVED: carry out YOUR action with MCP: "
                    f"rank_stations_by_distance if needed, get_robot_pose, distance_to_station, "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    f"drive_distance(robot_id='{self.nav_id}', distance_m, direction_deg), "
                    "get_peer_distances, pickup_box/drop_box with that robot_id, etc. "
                    "Act with few tools: gather once, then navigate. "
                    "Only after nav fails: get_peer_distances and/or drive_distance, then retry.\n"
                    "- If you need a fleet replan after/during execute problems, call "
                    "start_discussion_round(reason=...) and/or end your reply with "
                    "NEED_DISCUSSION: <reason>.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- Do not wait for the planner to confirm receipt of the plan.\n"
                    "- You cannot message other robots directly; turn-taking is orchestrated.\n"
                    "- Do not end discussion alone with EXECUTE — only AGREE/DISAGREE during "
                    "discussion. Unanimous AGREE triggers execute.\n"
                    "- Do not invent a wholly different mission; refine the given plan.\n"
                    "\n"
                    "WORDING: say you SEND or receive a message. Do not say broadcast.\n"
                    "FLEET: Other robots share this map. Navigate first; on failure use "
                    "get_peer_distances / drive_distance to clear peers, then retry.\n"
                    "TOOLS: If the same tool with the same arguments fails twice, do not call "
                    "it a third time — change the goal/approach or report failure.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="HMAS-1",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._in_execute = bool(_EXECUTE_APPROVED_RE.search(message or ""))
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._in_execute = False

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        local_tools = list(self._mcp_tools_by_name.values())

        @tool
        def start_discussion_round(reason: str, plan_update: str = "") -> str:
            """Request another HMAS-1 discussion round (until unanimous AGREE), then execute.

            Prefer this when you hit a conflict that needs fleet replan. If you are currently
            answering an EXECUTE APPROVED message, end that reply with NEED_DISCUSSION: <reason>
            (this tool reminds you); the planner starts the round after execute returns.
            When idle, this tool asks the planner to start discussion immediately.

            Args:
                reason: Why a new discussion is needed.
                plan_update: Optional updated plan fragment for the fleet.
            """
            reason = (reason or "").strip() or "replan needed"
            plan_update = (plan_update or "").strip()
            if self._in_execute or self.bus is None:
                return (
                    f"Discussion requested. End your reply with a line exactly like:\n"
                    f"NEED_DISCUSSION: {reason}\n"
                    "Finish any safe local action first; do not block waiting for peers."
                )
            payload = f"START_DISCUSSION\nreason: {reason}\nplan: {plan_update}"
            try:
                return self.bus.ask(
                    to=PLANNER_NAME,
                    text=payload,
                    thread_id=f"{self.spec.name}->discussion",
                )
            except Exception as e:
                return json.dumps(
                    {
                        "error": "start_discussion_failed",
                        "message": str(e),
                        "hint": f"End your reply with NEED_DISCUSSION: {reason}",
                    }
                )

        return [*local_tools, start_discussion_round]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one HMAS-1 robot agent (AGREE discussion → execute)."
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

    my_name = robot_peer_name(args.robot_id)
    bus = BusClient(my_name, host=args.host, port=args.port)
    robot = HMAS1RobotAgent(args.robot_id, bus=bus)

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

    print(f"{my_name} is online (HMAS-1). Discuss until AGREE, then execute.")
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
