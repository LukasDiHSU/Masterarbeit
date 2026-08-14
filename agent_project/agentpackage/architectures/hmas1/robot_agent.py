"""HMAS-1 local robot agent (Chen et al., arXiv:2309.15943, Fig. 1).

The agent has two jobs. During dialogue it follows the central planner's
multi-step plan (AGREE) unless it sees an exception, in which case it votes
DISAGREE and may send a corrected EXECUTE. During execution it receives one
verified symbolic action and runs it through pre-defined primitives -- no LLM
call, so the agreed plan is carried out exactly as agreed.
"""

from __future__ import annotations

import argparse
import json
import threading

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, TB_IDS, nav_id_for_tb, robot_peer_name
from ...instructions import hmas1_robot
from ...monitor import report_trace
from ...paper_protocol import ACTION_SYNTAX, execute_action, parse_action
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from .dialogue import EXECUTE_DISPATCH_PREFIX


class HMAS1RobotAgent(BaseAgent):
    """Local agent: dialogue partner first, deterministic executor second."""

    def __init__(self, robot_id: str, *, bus: BusClient | None = None):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.bus = bus
        name = robot_peer_name(robot_id)

        super().__init__(
            AgentSpec(
                name=name,
                description=f"HMAS-1 local agent for {name}.",
                system_prompt=hmas1_robot(
                    name=name, n=AGENT_COUNT, action_syntax=ACTION_SYNTAX
                ),
            ),
            architecture="HMAS-1",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        text = (message or "").strip()
        if text.startswith(EXECUTE_DISPATCH_PREFIX):
            return self._run_assigned_action(text)
        return super().invoke(message, thread_id=thread_id)

    def _run_assigned_action(self, message: str) -> str:
        name = robot_peer_name(self.robot_id)
        body = message[len(EXECUTE_DISPATCH_PREFIX) :].strip()
        action = parse_action(body, name)
        if action is None:
            return json.dumps(
                {"ok": False, "error": "unparsable_action", "received": body[:160]}
            )
        report_trace(
            agent=name,
            architecture="HMAS-1",
            kind="tool_start",
            text=action.text(),
            tool="execute_action",
        )
        result = execute_action(action)
        report_trace(
            agent=name,
            architecture="HMAS-1",
            kind="tool_end",
            text=json.dumps(result, default=str),
            tool="execute_action",
        )
        return json.dumps(result, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one HMAS-1 robot agent.")
    parser.add_argument(
        "--robot-id",
        "--tb-id",
        dest="robot_id",
        choices=list(TB_IDS),
        required=True,
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    my_name = robot_peer_name(args.robot_id)
    bus = BusClient(my_name, host=args.host, port=args.port)
    robot = HMAS1RobotAgent(args.robot_id, bus=bus)

    def handle_message(msg: dict) -> None:
        if msg.get("type") == "error":
            print(f"\n[broker error] {msg.get('text', '')}")
            return
        if msg.get("type") != "agent_request":
            return

        src = str(msg.get("from", "?"))
        text = str(msg.get("text", ""))
        thread_id = str(msg.get("thread_id", src))
        request_id = msg.get("request_id")
        kind = "execute" if text.strip().startswith(EXECUTE_DISPATCH_PREFIX) else "dialogue"
        print(f"\n[{src} -> {my_name}] {kind} request")

        def _job() -> None:
            try:
                reply = robot.invoke(text, thread_id=thread_id)
            except Exception as e:
                reply = f"{my_name} failed to process request: {type(e).__name__}: {e}"
            print(f"[{my_name}] {reply.strip()[:400]}")
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

    print(f"{my_name} online (HMAS-1). Waiting for planning dialogue and action dispatch.")
    print("Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        bus.close()


if __name__ == "__main__":
    main()
