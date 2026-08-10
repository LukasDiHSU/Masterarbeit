
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from .agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class MasterAgent(BaseAgent):
    """The single point of coordination. Only this agent may delegate work;
    robot agents never talk to each other, they only answer the master."""

    def __init__(self, *, bus: BusClient | None = None):
        self.bus = bus
        self._active_thread_id = "default"

        super().__init__(
            AgentSpec(
                name="master",
                description="Coordinates the four robot agents and delegates work automatically.",
                system_prompt=(
                    "You are the master coordinator for robot_tb1..robot_tb4 (centralized architecture).\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Answer the human user.\n"
                    "- ask_robot_tb1..tb4: SEND a message to one robot and wait for its reply.\n"
                    "- ask_all_robots: SEND the same message to every robot and wait for all replies.\n"
                    "- ask_selected_robots_parallel: SEND the same message to a subset of robots.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- Robots cannot send messages to each other; only you can delegate.\n"
                    "- You have no station/nav MCP tools yourself — robots do the physical work.\n"
                    "- Do not ask the user which tool to call when the request already implies it.\n"
                    "\n"
                    "WORDING: say you SEND a message. Do not say broadcast.\n"
                    "STYLE: keep every message as short but precise as possible. "
                    "After tool replies, synthesize one short answer. Never hallucinate values."
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

    def _ask_robot(self, tb_id: TB_ID, message: str) -> str:
        if self.bus is None:
            raise RuntimeError("BusClient is not attached to MasterAgent.")
        return self.bus.ask(
            to=f"robot_{tb_id}",
            text=message,
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
            """Send the same task or question to tb1..tb4 in parallel and wait for all replies as JSON."""
            robots: tuple[TB_ID, ...] = ("tb1", "tb2", "tb3", "tb4")
            replies: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=len(robots)) as pool:
                future_to_tb = {
                    pool.submit(self._ask_robot, tb_id, message): tb_id
                    for tb_id in robots
                }
                for fut in as_completed(future_to_tb):
                    tb_id = future_to_tb[fut]
                    try:
                        replies[tb_id] = fut.result()
                    except Exception as e:
                        replies[tb_id] = json.dumps(
                            {
                                "error": "ask_robot_failed",
                                "robot": tb_id,
                                "message": str(e),
                            }
                        )
            return json.dumps(replies, ensure_ascii=False, indent=2)

        @tool
        def ask_selected_robots_parallel(robots_json: str, message: str) -> str:
            """Send the same task or question to selected robots in parallel and wait for all replies as JSON.

            Args:
                robots_json: JSON array of robot IDs, e.g. ["tb1","tb3"].
                message: Message/task to send to each selected robot.
            """
            try:
                raw = json.loads(robots_json)
            except Exception as e:
                return json.dumps(
                    {
                        "error": "invalid_robots_json",
                        "message": "robots_json must be a JSON array like [\"tb1\",\"tb3\"]",
                        "detail": str(e),
                    }
                )
            if not isinstance(raw, list):
                return json.dumps(
                    {
                        "error": "invalid_robots_json",
                        "message": "robots_json must be a JSON array like [\"tb1\",\"tb3\"]",
                    }
                )

            allowed: set[str] = {"tb1", "tb2", "tb3", "tb4"}
            ordered_unique: list[TB_ID] = []
            seen: set[str] = set()
            invalid: list[str] = []
            for r in raw:
                s = str(r).strip().lower()
                if s in allowed:
                    if s not in seen:
                        ordered_unique.append(s)  # type: ignore[arg-type]
                        seen.add(s)
                else:
                    invalid.append(str(r))

            if invalid:
                return json.dumps(
                    {
                        "error": "invalid_robot_ids",
                        "invalid": invalid,
                        "allowed": ["tb1", "tb2", "tb3", "tb4"],
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
                future_to_tb = {
                    pool.submit(self._ask_robot, tb_id, message): tb_id
                    for tb_id in ordered_unique
                }
                for fut in as_completed(future_to_tb):
                    tb_id = future_to_tb[fut]
                    try:
                        replies[tb_id] = fut.result()
                    except Exception as e:
                        replies[tb_id] = json.dumps(
                            {
                                "error": "ask_robot_failed",
                                "robot": tb_id,
                                "message": str(e),
                            }
                        )

            return json.dumps(replies, ensure_ascii=False, indent=2)

        return [
            ask_robot_tb1,
            ask_robot_tb2,
            ask_robot_tb3,
            ask_robot_tb4,
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
        agent.run_persistent_chat(thread_id="default")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
