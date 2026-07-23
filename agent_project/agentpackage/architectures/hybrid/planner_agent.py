
from __future__ import annotations

import json
from typing import Literal

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_HOST,
    PLANNER_NAME,
    TB_IDS,
    build_peer_table,
    robot_peer_name,
)
from ..centralized.agent_bus import DEFAULT_HOST, DEFAULT_PORT
from .phase_bus import PhasedBus

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class PlannerAgent(BaseAgent):
    """Phase 1: acts exactly like the centralized master -- it is the only
    agent the human talks to and the only one that can delegate, routed
    through the broker.

    Phase 2: after it has given the robots their first instructions, it
    calls ``activate_decentralized_phase`` once. From then on every robot
    (and the planner itself) is reachable directly over a peer mesh, so
    robots can keep collaborating without routing every message back
    through the planner.
    """

    def __init__(self, *, bus: PhasedBus, mesh_host: str = DEFAULT_MESH_HOST, mesh_base_port: int = DEFAULT_MESH_BASE_PORT):
        self.bus = bus
        self._active_thread_id = "default"
        self._mesh_host = mesh_host
        self._mesh_base_port = mesh_base_port

        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description="Plans the mission, gives first instructions, then hands off to a decentralized robot mesh.",
                system_prompt=(
                    "You are the planning coordinator for a fleet of four robots: robot_tb1..robot_tb4. "
                    "PHASE 1 (centralized): gather the user's goal, think through a short plan, and give each "
                    "relevant robot its first instruction with ask_robot_tbN (or ask_all_robots for a fleet-wide "
                    "instruction). Do this yourself; do not ask the user which tool to call. "
                    "PHASE 2 (decentralized handoff): once you have delegated the FIRST round of instructions "
                    "and every robot involved has acknowledged, call activate_decentralized_phase EXACTLY ONCE. "
                    "This lets every robot talk directly to every other robot (and to you) for the rest of the "
                    "mission, without funnelling every message through you. After activating, you are just one "
                    "more peer: only step back in if the user asks a new fleet-wide question or something needs "
                    "re-planning. Keep answers short and precise. Never hallucinate values: if data is "
                    "unavailable, say so clearly."
                ),
            ),
            architecture="hybrid",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"

    def _ask_robot(self, tb_id: TB_ID, message: str) -> str:
        return self.bus.ask(
            robot_peer_name(tb_id),
            message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{tb_id}",
        )

    def _retrieve_tools(self) -> list:
        @tool
        def ask_robot_tb1(message: str) -> str:
            """Send a task or question to robot tb1 and return its reply."""
            return self._ask_robot("tb1", message)

        @tool
        def ask_robot_tb2(message: str) -> str:
            """Send a task or question to robot tb2 and return its reply."""
            return self._ask_robot("tb2", message)

        @tool
        def ask_robot_tb3(message: str) -> str:
            """Send a task or question to robot tb3 and return its reply."""
            return self._ask_robot("tb3", message)

        @tool
        def ask_robot_tb4(message: str) -> str:
            """Send a task or question to robot tb4 and return its reply."""
            return self._ask_robot("tb4", message)

        @tool
        def ask_all_robots(message: str) -> str:
            """Send the same task or question to tb1..tb4 and wait for all replies as JSON."""
            replies = self.bus.ask_many(
                [robot_peer_name(tb) for tb in TB_IDS],
                message,
                thread_id=f"{self.thread_key(self._active_thread_id)}->all",
            )
            return json.dumps(replies, ensure_ascii=False, indent=2)

        @tool
        def activate_decentralized_phase() -> str:
            """Call this exactly once, after you have delegated the first round of
            instructions to the relevant robots. It hands out a peer address table
            so every robot (and you) can talk directly to any other robot for the
            rest of the mission, without going through you for every message."""
            if self.bus.mesh_active:
                return json.dumps({"status": "already_active"})
            peer_table = build_peer_table(
                TB_IDS,
                host=self._mesh_host,
                base_port=self._mesh_base_port,
                include_planner=True,
            )
            self.bus.broadcast_activate_mesh(peer_table)
            return json.dumps(
                {
                    "status": "decentralized_phase_activated",
                    "peers": sorted(peer_table.keys()),
                    "message": "Robots can now coordinate directly with each other.",
                }
            )

        return [
            ask_robot_tb1,
            ask_robot_tb2,
            ask_robot_tb3,
            ask_robot_tb4,
            ask_all_robots,
            activate_decentralized_phase,
        ]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive planner chat for the hybrid architecture (centralized planning -> decentralized execution)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mesh-host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--mesh-base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    args = parser.parse_args()

    bus = PhasedBus(PLANNER_NAME, broker_host=args.host, broker_port=args.port)

    def handle_message(msg: dict) -> None:
        if msg.get("type") == "error":
            print(f"\n[broker error] {msg.get('text', '')}")

    bus.on_message(handle_message)

    try:
        agent = PlannerAgent(bus=bus, mesh_host=args.mesh_host, mesh_base_port=args.mesh_base_port)
        agent.run_persistent_chat(thread_id="default")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
