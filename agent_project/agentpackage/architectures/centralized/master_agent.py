from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, TB_IDS, fleet_prompt_range, resolve_robot_id, robot_peer_name
from ...instructions import centralized_master
from ...mcp_client import load_planning_mcp_tools
from .agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


class MasterAgent(BaseAgent):
    """The single point of coordination. Only this agent may delegate work;
    robot agents never talk to each other, they only answer the master."""

    def __init__(self, *, bus: BusClient | None = None):
        self.bus = bus
        self._active_thread_id = "default"
        fleet = fleet_prompt_range()
        robot_list = ", ".join(TB_IDS)

        super().__init__(
            AgentSpec(
                name="master",
                description=f"Coordinates {AGENT_COUNT} robot agents and delegates work automatically.",
                system_prompt=centralized_master(
                    fleet=fleet, n=AGENT_COUNT, robot_list=robot_list
                ),
            ),
            architecture="centralized",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"

    def _ask_robot(self, robot_id: str, message: str) -> str:
        if self.bus is None:
            raise RuntimeError("BusClient is not attached to MasterAgent.")
        peer = robot_peer_name(robot_id)
        try:
            return self.bus.ask(
                to=peer,
                text=message,
                thread_id=f"{self.thread_key(self._active_thread_id)}->{peer}",
            )
        except TimeoutError as e:
            return json.dumps(
                {
                    "error": "ask_timeout",
                    "robot": robot_id,
                    "message": str(e),
                }
            )

    @cached_property
    def _map_tools(self) -> list:
        return load_planning_mcp_tools()

    def _retrieve_tools(self) -> list:
        @tool
        def ask_robot(robot: str, message: str) -> str:
            """SEND a task or question to exactly one robot and return its reply.

            Args:
                robot: A remroc id like SmallDeliveryRobot_0, SmallDeliveryRobot_1, …
                message: Message/task to send.
            """
            rid = resolve_robot_id(robot)
            if rid is None:
                return json.dumps(
                    {
                        "error": "invalid_robot_id",
                        "got": robot,
                        "allowed": list(TB_IDS),
                    }
                )
            return self._ask_robot(rid, message)

        @tool
        def ask_all_robots(message: str) -> str:
            """SEND the same task or question to every fleet robot in parallel and wait for all replies as JSON."""
            replies: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=len(TB_IDS)) as pool:
                future_to_id = {
                    pool.submit(self._ask_robot, rid, message): rid for rid in TB_IDS
                }
                for fut in as_completed(future_to_id):
                    rid = future_to_id[fut]
                    try:
                        replies[rid] = fut.result()
                    except Exception as e:
                        replies[rid] = json.dumps(
                            {
                                "error": "ask_robot_failed",
                                "robot": rid,
                                "message": str(e),
                            }
                        )
            return json.dumps(replies, ensure_ascii=False, indent=2)

        @tool
        def ask_selected_robots_parallel(robots_json: str, message: str) -> str:
            """SEND the same task or question to selected robots in parallel and wait for all replies as JSON.

            Args:
                robots_json: JSON array of robot IDs, e.g. ["SmallDeliveryRobot_0","SmallDeliveryRobot_2"].
                message: Message/task to send to each selected robot.
            """
            try:
                raw = json.loads(robots_json)
            except Exception as e:
                return json.dumps(
                    {
                        "error": "invalid_robots_json",
                        "message": (
                            'robots_json must be a JSON array like '
                            '["SmallDeliveryRobot_0","SmallDeliveryRobot_2"]'
                        ),
                        "detail": str(e),
                    }
                )
            if not isinstance(raw, list):
                return json.dumps(
                    {
                        "error": "invalid_robots_json",
                        "message": (
                            'robots_json must be a JSON array like '
                            '["SmallDeliveryRobot_0","SmallDeliveryRobot_2"]'
                        ),
                    }
                )

            ordered_unique: list[str] = []
            seen: set[str] = set()
            invalid: list[str] = []
            for r in raw:
                rid = resolve_robot_id(str(r))
                if rid is None:
                    invalid.append(str(r))
                elif rid not in seen:
                    ordered_unique.append(rid)
                    seen.add(rid)

            if invalid:
                return json.dumps(
                    {
                        "error": "invalid_robot_ids",
                        "invalid": invalid,
                        "allowed": list(TB_IDS),
                    }
                )
            if not ordered_unique:
                return json.dumps(
                    {
                        "error": "empty_robot_selection",
                        "message": "Select at least one robot ID in robots_json.",
                    }
                )

            replies: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=len(ordered_unique)) as pool:
                future_to_id = {
                    pool.submit(self._ask_robot, rid, message): rid
                    for rid in ordered_unique
                }
                for fut in as_completed(future_to_id):
                    rid = future_to_id[fut]
                    try:
                        replies[rid] = fut.result()
                    except Exception as e:
                        replies[rid] = json.dumps(
                            {
                                "error": "ask_robot_failed",
                                "robot": rid,
                                "message": str(e),
                            }
                        )

            return json.dumps(replies, ensure_ascii=False, indent=2)

        return [
            *self._map_tools,
            ask_robot,
            ask_all_robots,
            ask_selected_robots_parallel,
        ]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive master agent chat with automatic robot delegation (centralized architecture)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    bus = BusClient("master", host=args.host, port=args.port)

    def handle_message(msg: dict) -> None:
        if msg.get("type") == "error":
            print(f"\n[broker error] {msg.get('text', '')}")

    bus.on_message(handle_message)

    try:
        agent = MasterAgent(bus=bus)
        agent.run_persistent_chat(
            thread_id="default",
            timing_label="centralized_until_done",
        )
    finally:
        bus.close()


if __name__ == "__main__":
    main()
