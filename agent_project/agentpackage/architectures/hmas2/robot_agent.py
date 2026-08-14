from __future__ import annotations

import argparse
import threading
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import TB_IDS, nav_id_for_tb, robot_peer_name
from ...instructions import hmas2_robot
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
                system_prompt=hmas2_robot(name=name, nav_id=self.nav_id),
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
        request_id = msg.get("request_id")

        print(f"\n[{src} -> {my_name}] {text}")

        def _job() -> None:
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
                request_id=request_id,
            )

        threading.Thread(
            target=_job, daemon=True, name=f"{my_name}-handle-{request_id or 'req'}"
        ).start()

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
