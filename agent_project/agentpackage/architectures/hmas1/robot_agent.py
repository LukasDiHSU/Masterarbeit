from __future__ import annotations

import argparse
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, TB_IDS, nav_id_for_tb, robot_peer_name
from ...mcp_client import load_mcp_tools_safe
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


class HMAS1RobotAgent(BaseAgent):
    """Local HMAS-1 agent: joins turn-based dialogue after the central primer,
    may output EXECUTE, then carries out its action when dispatched.
    """

    def __init__(self, robot_id: str):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        name = robot_peer_name(robot_id)
        example_peers = "\n".join(f"  {robot_peer_name(t)}: ..." for t in TB_IDS[: min(2, len(TB_IDS))])

        super().__init__(
            AgentSpec(
                name=name,
                description=(
                    f"HMAS-1 local agent for {name}: turn-based dialogue, then execute."
                ),
                system_prompt=(
                    f"You are {name} in the HMAS-1 architecture ({AGENT_COUNT} robots).\n"
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
                    "- On a dialogue turn: refine the plan from YOUR perspective. Prefer few "
                    "or zero MCP tools (list_available_boxes, get_station if needed). "
                    "Do not call get_events (conflict-based only). Keep comments short.\n"
                    "- When the group should act, start your reply with EXECUTE, then one "
                    "action line per participant, e.g.:\n"
                    "  EXECUTE\n"
                    f"{example_peers}\n"
                    "- On a later EXECUTE APPROVED message: carry out YOUR action with MCP: "
                    f"rank_stations_by_distance if needed, get_robot_pose, distance_to_station, "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    f"drive_distance(robot_id='{self.nav_id}', distance_m, direction_deg), "
                    "get_peer_distances, pickup_box/drop_box with that robot_id, etc. "
                    "Act with few tools: gather once, then navigate. "
                    "Only after nav fails: get_peer_distances and/or drive_distance, then retry.\n"
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
                    "FLEET: Other robots share this map. Navigate first; on failure use "
                    "get_peer_distances / drive_distance to clear peers, then retry.\n"
                    "TOOLS: If the same tool with the same arguments fails twice, do not call "
                    "it a third time — change the goal/approach or report failure.\n"
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
    robot = HMAS1RobotAgent(args.robot_id)
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
