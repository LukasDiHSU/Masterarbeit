"""One DMAS robot: it argues its own case and carries out its own leg.

Two LLMs per robot, so the usage monitor separates coordination from work:
``SmallDeliveryRobot_i:planner`` speaks in the discussion and may inspect the
map (stations, poses, held boxes) but cannot drive; ``SmallDeliveryRobot_i``
executes the agreed leg with the MCP robot tools.

The robot is a pure request/reply peer on the mesh. It never asks anybody
anything, which is what makes the protocol deadlock-free.
"""

from __future__ import annotations

import argparse
import threading
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    AGENT_COUNT,
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_HOST,
    TB_IDS,
    build_peer_table,
    is_q1_platform,
    nav_id_for_tb,
    robot_peer_name,
)
from ...instructions import dmas_discussion, dmas_executor, q1_hmas1_executor, q1_hmas1_robot
from ...mcp_client import load_mcp_tools_safe
from ...roles import filter_mcp_tools_for_agent
from ..conflict_based.mesh_bus import MeshNode
from .protocol import (
    ARCHITECTURE,
    EXECUTE_PREFIX,
    TURN_PREFIX,
    build_execution_prompt,
    parse_message,
)

# Event tooling belongs to conflict_based; world switching would break a run.
_EXECUTOR_BLOCKED_TOOLS = frozenset(
    {
        "get_events",
        "clear_events",
        "emit_conflict",
        "set_world",
        "reset_stations",
        "list_worlds",
    }
)

# Read-only map/inventory tools for the discussion LLM. No navigate / pickup /
# drop / set_world — nobody may drive while the fleet is still negotiating.
_DISCUSSION_MAP_TOOLS = frozenset(
    {
        "list_stations",
        "get_look_poses",
        "list_available_boxes",
        "get_station",
        "get_held_boxes",
        "get_all_robot_poses",
        "get_robot_pose",
        "get_map_info",
        "rank_stations_by_distance",
        "distance_to_station",
    }
)


class DiscussionAgent(BaseAgent):
    """Speaks for this robot in the fleet discussion.

    May look up stations, poses and boxes so legs use real map coordinates.
    Has no drive or inventory-mutation tools.
    """

    def __init__(self, peer_name: str, nav_id: str, robot_id: str = ""):
        self.robot_id = robot_id
        prompt = (
            q1_hmas1_robot(name=peer_name, n=AGENT_COUNT, nav_id=nav_id)
            if is_q1_platform()
            else dmas_discussion(name=peer_name, n=AGENT_COUNT, nav_id=nav_id)
        )
        super().__init__(
            AgentSpec(
                name=f"{peer_name}:planner",
                description=f"DMAS discussion agent of {peer_name}.",
                system_prompt=prompt,
            ),
            architecture=ARCHITECTURE,
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        tools = load_mcp_tools_safe()
        if is_q1_platform() and self.robot_id:
            return {t.name: t for t in filter_mcp_tools_for_agent(tools, self.robot_id)}
        return {t.name: t for t in tools if t.name in _DISCUSSION_MAP_TOOLS}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


class ExecutorAgent(BaseAgent):
    """Carries out this robot's agreed leg with the MCP tools."""

    def __init__(self, peer_name: str, nav_id: str, robot_id: str = ""):
        self.robot_id = robot_id
        prompt = (
            q1_hmas1_executor(name=peer_name, nav_id=nav_id)
            if is_q1_platform()
            else dmas_executor(name=peer_name, nav_id=nav_id)
        )
        super().__init__(
            AgentSpec(
                name=peer_name,
                description=f"DMAS executor of {peer_name}.",
                system_prompt=prompt,
            ),
            architecture=ARCHITECTURE,
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        tools = [
            t for t in load_mcp_tools_safe() if t.name not in _EXECUTOR_BLOCKED_TOOLS
        ]
        if is_q1_platform() and self.robot_id:
            return {t.name: t for t in filter_mcp_tools_for_agent(tools, self.robot_id)}
        return {t.name: t for t in tools}

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


class DMASRobot:
    def __init__(self, robot_id: str):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.name = robot_peer_name(robot_id)
        self.discussion = DiscussionAgent(self.name, self.nav_id, self.robot_id)
        self.executor = ExecutorAgent(self.name, self.nav_id, self.robot_id)
        # A robot can only do one physical thing at a time.
        self._exec_lock = threading.Lock()

    def handle(self, text: str) -> str:
        parsed = parse_message(text)
        if parsed is None:
            return (
                f"{self.name} ignored a message it does not understand. Expected "
                f"{TURN_PREFIX} or {EXECUTE_PREFIX}."
            )
        kind, payload = parsed
        if kind == TURN_PREFIX:
            return self.take_turn(payload)
        return self.run_leg(payload)

    def take_turn(self, payload: dict) -> str:
        round_index = int(payload.get("round", 0))
        turn_index = int(payload.get("turn", 0))
        prompt = str(payload.get("prompt", ""))
        # The prompt carries the whole round, so every turn starts from a clean
        # thread instead of seeing its own earlier turns twice.
        return self.discussion.invoke(prompt, thread_id=f"r{round_index}-t{turn_index}")

    def run_leg(self, payload: dict) -> str:
        round_index = int(payload.get("round", 0))
        leg = str(payload.get("leg", ""))
        with self._exec_lock:
            prompt = build_execution_prompt(
                speaker=self.name,
                nav_id=self.nav_id,
                leg=leg,
                round_index=round_index,
            )
            return self.executor.invoke(prompt, thread_id=f"exec-r{round_index}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run one DMAS robot: takes its turn in the fleet discussion and "
            "executes its agreed leg."
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
    robot = DMASRobot(args.robot_id)

    def serve(msg: dict) -> None:
        text = str(msg.get("text", ""))
        label = "turn" if text.startswith(TURN_PREFIX) else "leg"
        print(f"\n[{my_name}] {label} requested")
        try:
            reply = robot.handle(text)
        except Exception as e:
            reply = f"{my_name} failed on this {label}: {type(e).__name__}: {e}"
        print(f"[{my_name}] {reply}")
        mesh.reply(msg, reply)

    def handle_message(msg: dict) -> None:
        if msg.get("type") != "agent_request":
            return
        # Keep the link's reader thread free while the LLM works.
        threading.Thread(target=serve, args=(msg,), daemon=True).start()

    mesh.on_message(handle_message)

    print(f"{my_name} online at {my_host}:{my_port} (nav id {robot.nav_id}).")
    print("DMAS robot: discusses each round, then executes only its own leg.")
    print("Waiting for the fleet. Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
