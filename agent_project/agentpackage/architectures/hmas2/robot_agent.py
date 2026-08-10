from __future__ import annotations

import argparse
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import TB_IDS, nav_id_for_tb, robot_peer_name
from ...mcp_client import load_mcp_tools_safe
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT

# Event/sensing tools used by conflict-based (or invite over-sensing). HMAS-2
# robots should not call these.
_HMAS2_BLOCKED_TOOLS = frozenset(
    {
        "get_events",
        "clear_events",
        "emit_conflict",
        "get_all_robot_poses",
        "get_laser_snapshot",
        "list_worlds",
        "get_map_info",
        "add_to_whiteboard",
        "get_whiteboard",
    }
)


class HMAS2RobotAgent(BaseAgent):
    """Local HMAS-2 agent: reviews the central plan (AGREE/DISAGREE), then
    executes only after the planner sends an EXECUTE message. No peer tools.
    """

    def __init__(self, robot_id: str):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        name = robot_peer_name(robot_id)

        super().__init__(
            AgentSpec(
                name=name,
                description=(
                    f"HMAS-2 local agent for {name}: reviews central plans, then executes."
                ),
                system_prompt=(
                    f"You are {name} in the HMAS-2 architecture.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Answer messages from the central planner only.\n"
                    "- PLAN REVIEW REQUEST: reply with exactly one line:\n"
                    "  AGREE: <short reason>\n"
                    "  or\n"
                    "  DISAGREE: <what is wrong / safer alternative>\n"
                    "  Prefer ZERO tools. If your role is 'go to farthest/nearest station' "
                    f"you may call rank_stations_by_distance(robot_id='{self.nav_id}') ONCE, "
                    "then AGREE with the station + navigate_xy. Do NOT execute during review.\n"
                    "- EXECUTE navigate (coords given): call navigate_to_pose immediately "
                    f"with robot_id='{self.nav_id}' and those x/y. No pre-sensing.\n"
                    "- EXECUTE farthest/nearest without coords: rank_stations_by_distance ONCE, "
                    "then navigate_to_pose to navigate_xy.\n"
                    "- EXECUTE HOLD / idle / wait: reply HOLDING in one short line. Call NO tools.\n"
                    "- Only if navigate_to_pose fails: use the tool's blocking_robot message, "
                    f"optionally drive_distance(robot_id='{self.nav_id}', ...), then retry navigate. "
                    "Call get_peer_distances only if the nav failure did not name a blocker.\n"
                    "- pickup_box / drop_box only when EXECUTE says so.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- No peer messaging; only the planner coordinates.\n"
                    "- Do not invent a fleet-wide plan. Review or execute only YOUR part.\n"
                    "- Do not call get_events (that is for conflict-based only).\n"
                    "- Do not call get_all_robot_poses, get_laser_snapshot, list_stations, "
                    "or get_robot_pose unless EXECUTE explicitly needs a single pose check "
                    "(almost never — prefer rank_stations_by_distance / navigate).\n"
                    "- Do not start navigating during PLAN REVIEW.\n"
                    "\n"
                    "WORDING: say you SEND or receive a message. Do not say broadcast.\n"
                    "FLEET: Navigate first; on failure clear peers with drive_distance, then retry.\n"
                    "TOOLS: If the same tool with the same arguments fails twice, do not call "
                    "it a third time — change the goal/approach or report failure.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="HMAS-2",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {
            t.name: t
            for t in load_mcp_tools_safe()
            if t.name not in _HMAS2_BLOCKED_TOOLS
        }

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one HMAS-2 robot agent (local review + execute via central planner)."
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
    robot = HMAS2RobotAgent(args.robot_id)
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
