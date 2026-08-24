"""HMAS-1 local robot: votes once on the central plan, or talks as DMAS.

During the original plan, the discussion LLM answers AGREE or DISAGREE on
the whole plan. Unanimous AGREE executes that plan. After each original
work STEP the executor reports STEP_OK or STEP_FAILED; STEP_FAILED drops
the plan. DISAGREE / a new PLAN at vote time also drops it; later turns
use the DMAS huddle + PLAN / AGREE / FINISHED protocol.
"""

from __future__ import annotations

import argparse
import threading
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, TB_IDS, is_q1_platform, nav_id_for_tb, robot_peer_name
from ...instructions import (
    fleet_huddle,
    hmas1_executor,
    hmas1_robot,
    q1_fleet_huddle,
    q1_hmas1_executor,
    q1_hmas1_robot,
)
from ...mcp_client import load_mcp_tools_safe
from ...roles import filter_mcp_tools_for_agent
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from .dialogue import (
    EXECUTE_DISPATCH_PREFIX,
    HUDDLE_TURN_PREFIX,
    STEP_CHECK_PREFIX,
    build_execution_prompt,
)

_EXECUTOR_BLOCKED_TOOLS = frozenset(
    {
        "get_events",
        "clear_events",
        "emit_conflict",
    }
)


class HuddleAgent(BaseAgent):
    """PMAS huddle turns: no tools, so the model cannot dump a snapshot."""

    def __init__(self, peer_name: str):
        prompt = (
            q1_fleet_huddle(name=peer_name, n=AGENT_COUNT)
            if is_q1_platform()
            else fleet_huddle(name=peer_name, n=AGENT_COUNT)
        )
        super().__init__(
            AgentSpec(
                name=f"{peer_name}:huddle",
                description=f"HMAS-1 huddle speaker of {peer_name}.",
                system_prompt=prompt,
            ),
            architecture="HMAS-1",
        )

    def _retrieve_tools(self):
        return []


class DiscussionAgent(BaseAgent):
    def __init__(self, peer_name: str, nav_id: str, robot_id: str):
        self.robot_id = robot_id
        prompt = (
            q1_hmas1_robot(name=peer_name, n=AGENT_COUNT, nav_id=nav_id)
            if is_q1_platform()
            else hmas1_robot(name=peer_name, n=AGENT_COUNT, nav_id=nav_id)
        )
        super().__init__(
            AgentSpec(
                name=f"{peer_name}:planner",
                description=f"HMAS-1 discussion agent of {peer_name}.",
                system_prompt=prompt,
            ),
            architecture="HMAS-1",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        tools = load_mcp_tools_safe()
        return {t.name: t for t in filter_mcp_tools_for_agent(tools, self.robot_id)}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


class ExecutorAgent(BaseAgent):
    def __init__(self, peer_name: str, nav_id: str, robot_id: str):
        self.robot_id = robot_id
        prompt = (
            q1_hmas1_executor(name=peer_name, nav_id=nav_id)
            if is_q1_platform()
            else hmas1_executor(name=peer_name, nav_id=nav_id)
        )
        super().__init__(
            AgentSpec(
                name=peer_name,
                description=f"HMAS-1 executor of {peer_name}.",
                system_prompt=prompt,
            ),
            architecture="HMAS-1",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        tools = [
            t for t in load_mcp_tools_safe() if t.name not in _EXECUTOR_BLOCKED_TOOLS
        ]
        if is_q1_platform():
            return {t.name: t for t in filter_mcp_tools_for_agent(tools, self.robot_id)}
        return {t.name: t for t in tools}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


class HMAS1RobotAgent:
    def __init__(self, robot_id: str, *, bus: BusClient | None = None):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.bus = bus
        self.name = robot_peer_name(robot_id)
        self.discussion = DiscussionAgent(self.name, self.nav_id, self.robot_id)
        self.huddle = HuddleAgent(self.name)
        self.executor = ExecutorAgent(self.name, self.nav_id, self.robot_id)
        self._exec_lock = threading.Lock()
        self._round = 0

    def invoke(self, message: str, thread_id: str = "default") -> str:
        text = (message or "").strip()
        if text.startswith(EXECUTE_DISPATCH_PREFIX):
            return self._run_leg(text)
        if text.startswith(STEP_CHECK_PREFIX):
            return self._run_step_check(text)
        if text.startswith(HUDDLE_TURN_PREFIX):
            body = text[len(HUDDLE_TURN_PREFIX) :].strip()
            return self.huddle.invoke(body, thread_id=thread_id)
        return self.discussion.invoke(text, thread_id=thread_id)

    def _run_leg(self, message: str) -> str:
        body = message[len(EXECUTE_DISPATCH_PREFIX) :].strip()
        leg = body.split("\n(", 1)[0].strip()
        self._round += 1
        with self._exec_lock:
            prompt = build_execution_prompt(
                speaker=self.name,
                nav_id=self.nav_id,
                leg=leg,
                round_index=self._round,
            )
            return self.executor.invoke(prompt, thread_id=f"exec-r{self._round}")

    def _run_step_check(self, message: str) -> str:
        body = message[len(STEP_CHECK_PREFIX) :].strip()
        with self._exec_lock:
            return self.executor.invoke(
                body, thread_id=f"step-check-r{self._round}"
            )


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
        stripped = text.strip()
        kind = (
            "execute"
            if stripped.startswith(EXECUTE_DISPATCH_PREFIX)
            else "step-check"
            if stripped.startswith(STEP_CHECK_PREFIX)
            else "huddle"
            if stripped.startswith(HUDDLE_TURN_PREFIX)
            else "dialogue"
        )
        print(f"\n[{src} -> {my_name}] {kind} request")

        def _job() -> None:
            try:
                reply = robot.invoke(text, thread_id=thread_id)
            except Exception as e:
                reply = f"{my_name} failed to process request: {type(e).__name__}: {e}"
            print(f"[{my_name}] {reply.strip()[:400]}", flush=True)
            try:
                bus.send(
                    type="agent_reply",
                    to=src,
                    text=reply,
                    thread_id=thread_id,
                    request_id=request_id,
                )
            except Exception as e:
                print(
                    f"[{my_name}] failed to send reply: {type(e).__name__}: {e}",
                    flush=True,
                )

        threading.Thread(
            target=_job, daemon=True, name=f"{my_name}-handle-{request_id or 'req'}"
        ).start()

    bus.on_message(handle_message)

    print(
        f"{my_name} online (HMAS-1). Waiting for plan vote, DMAS huddle, "
        "STEP check, and leg dispatch."
    )
    print("Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        bus.close()


if __name__ == "__main__":
    main()
