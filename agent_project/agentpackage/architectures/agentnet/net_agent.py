"""One AgentNet node in the DMAS variant: discuss, then execute a short chunk.

Every robot is a peer on the mesh. The mission always enters at Agent 0, which
chairs turn-taking (it does not plan for the others). Peers answer discussion
prompts with an LLM and carry out verified symbolic actions without one.
"""

from __future__ import annotations

import argparse
import json
import threading
from typing import Any

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    AGENT_COUNT,
    AGENTNET_ENTRY_ROBOT,
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_HOST,
    TB_IDS,
    build_peer_table,
    is_q1_platform,
    nav_id_for_tb,
    robot_peer_name,
)
from ...monitor import report_trace
from ...paper_protocol import ACTION_SYNTAX, execute_action, parse_action
from ...instructions import agentnet_robot, q1_agentnet_robot
from ...mcp_client import load_mcp_tools_safe
from ...roles import filter_mcp_tools_for_agent
from ..conflict_based.mesh_bus import MeshNode
from .protocol import (
    ARCHITECTURE,
    ASK_TIMEOUT,
    CHUNK_STEPS,
    EXECUTE_DISPATCH_PREFIX,
    is_mission,
    mission_text,
)
from .session import DMASSession


class NetAgent(BaseAgent):
    """Dialogue partner during meetings; deterministic executor afterwards."""

    def __init__(self, robot_id: str):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.name = robot_peer_name(robot_id)

        prompt_fn = q1_agentnet_robot if is_q1_platform() else agentnet_robot
        super().__init__(
            AgentSpec(
                name=self.name,
                description=f"AgentNet DMAS agent for {self.name}.",
                system_prompt=prompt_fn(
                    name=self.name,
                    n=AGENT_COUNT,
                    action_syntax=ACTION_SYNTAX,
                    chunk_steps=CHUNK_STEPS,
                ),
            ),
            architecture=ARCHITECTURE,
        )

    def _retrieve_tools(self) -> list:
        if not is_q1_platform():
            return []
        return filter_mcp_tools_for_agent(load_mcp_tools_safe(), self.robot_id)

    def invoke(self, message: str, thread_id: str = "default") -> str:
        text = (message or "").strip()
        if text.startswith(EXECUTE_DISPATCH_PREFIX):
            return self._run_assigned_action(text)
        return super().invoke(message, thread_id=thread_id)

    def _run_assigned_action(self, message: str) -> str:
        body = message[len(EXECUTE_DISPATCH_PREFIX) :].strip()
        action = parse_action(body, self.name)
        if action is None:
            return json.dumps(
                {"ok": False, "error": "unparsable_action", "received": body[:160]}
            )
        report_trace(
            agent=self.name,
            architecture=ARCHITECTURE,
            kind="tool_start",
            text=action.text(),
            tool="execute_action",
        )
        result = execute_action(action)
        report_trace(
            agent=self.name,
            architecture=ARCHITECTURE,
            kind="tool_end",
            text=json.dumps(result, default=str),
            tool="execute_action",
        )
        return json.dumps(result, default=str)


class AgentNetNode:
    def __init__(self, robot_id: str, *, mesh: MeshNode, peer_names: list[str]):
        self.agent = NetAgent(robot_id)
        self.mesh = mesh
        self.name = self.agent.name
        self.peer_names = list(peer_names)
        self._mission_lock = threading.Lock()

    def handle_prompt(self, text: str, *, thread_id: str) -> str:
        return self.agent.invoke(text, thread_id=thread_id)

    def run_mission(self, task: str) -> str:
        if not self._mission_lock.acquire(blocking=False):
            return json.dumps(
                {"status": "busy", "error": "this node is already running a mission"}
            )

        def ask(peer: str, message: str, thread_id: str) -> str:
            if peer == self.name:
                return self.agent.invoke(message, thread_id=thread_id)
            return self.mesh.ask(
                peer, message, thread_id=thread_id, timeout=ASK_TIMEOUT
            )

        try:
            session = DMASSession(
                ask=ask, chair=self.name, participants=self.peer_names
            )
            outcome = session.run(task)
            return json.dumps(
                {k: v for k, v in outcome.items() if k != "transcript"},
                indent=2,
                ensure_ascii=False,
            )
        finally:
            self._mission_lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one AgentNet DMAS node: discuss a short plan with peers, "
            "execute the agreed chunk, meet again until everyone says FINISHED."
        )
    )
    parser.add_argument(
        "--robot-id",
        "--tb-id",
        dest="robot_id",
        choices=list(TB_IDS),
        required=True,
    )
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    my_name = robot_peer_name(args.robot_id)
    my_host, my_port = peer_table[my_name]
    peers_without_self = {n: hp for n, hp in peer_table.items() if n != my_name}

    mesh = MeshNode(
        my_name, my_host, my_port, peers_without_self, architecture=ARCHITECTURE
    )
    node = AgentNetNode(args.robot_id, mesh=mesh, peer_names=list(peer_table.keys()))
    entry = robot_peer_name(AGENTNET_ENTRY_ROBOT)

    def serve(msg: dict[str, Any]) -> None:
        src = str(msg.get("from", "?"))
        text = str(msg.get("text", ""))
        thread_id = str(msg.get("thread_id") or src)
        try:
            if is_mission(text) and my_name == entry:
                print(f"\n[{src} -> {my_name}] mission")
                reply = node.run_mission(mission_text(text))
            else:
                kind = (
                    "execute"
                    if text.strip().startswith(EXECUTE_DISPATCH_PREFIX)
                    else "dialogue"
                )
                print(f"\n[{src} -> {my_name}] {kind}")
                reply = node.handle_prompt(text, thread_id=thread_id)
        except Exception as e:
            reply = f"{my_name} failed: {type(e).__name__}: {e}"
        print(f"[{my_name}] {(reply or '')[:400]}")
        mesh.reply(msg, reply)

    def handle_message(msg: dict[str, Any]) -> None:
        if msg.get("type") != "agent_request":
            return
        threading.Thread(target=serve, args=(msg,), daemon=True).start()

    mesh.on_message(handle_message)

    print(f"{my_name} online at {my_host}:{my_port} (nav id {node.agent.nav_id}).")
    if my_name == entry:
        print(
            f"Chair: this node runs the DMAS loop (chunks of {CHUNK_STEPS} actions, "
            "mission ends when every robot says FINISHED in one round)."
        )
    else:
        print(f"Peer: answers discussion turns and executes agreed actions. Chair is {entry}.")
    print("Waiting. Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
