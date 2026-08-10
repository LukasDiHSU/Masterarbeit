from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, TB_IDS, fleet_prompt_range
from .agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


class MasterAgent(BaseAgent):
    """The single point of coordination. Only this agent may delegate work;
    robot agents never talk to each other, they only answer the master."""

    def __init__(self, *, bus: BusClient | None = None):
        self.bus = bus
        self._active_thread_id = "default"
        fleet = fleet_prompt_range()
        tb_list = ", ".join(TB_IDS)

        super().__init__(
            AgentSpec(
                name="master",
                description=f"Coordinates {AGENT_COUNT} robot agents and delegates work automatically.",
                system_prompt=(
                    f"You are the master coordinator for {fleet} (centralized architecture, "
                    f"{AGENT_COUNT} robots: {tb_list}).\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Answer the human user.\n"
                    f"- ask_robot(robot, message): SEND a message to one robot "
                    f"({tb_list}) and wait for its reply.\n"
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

    def _ask_robot(self, tb_id: str, message: str) -> str:
        if self.bus is None:
            raise RuntimeError("BusClient is not attached to MasterAgent.")
        if tb_id not in TB_IDS:
            raise ValueError(f"Unknown robot {tb_id!r}; allowed: {list(TB_IDS)}")
        return self.bus.ask(
            to=f"robot_{tb_id}",
            text=message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{tb_id}",
        )

    def _retrieve_tools(self) -> list:
        @tool
        def ask_robot(robot: str, message: str) -> str:
            """SEND a task or question to exactly one robot and return its reply.

            Args:
                robot: A fleet id like tb1, tb2, …
                message: Message/task to send.
            """
            tb = robot.strip().lower().removeprefix("robot_")
            if tb not in TB_IDS:
                return json.dumps(
                    {
                        "error": "invalid_robot_id",
                        "got": robot,
                        "allowed": list(TB_IDS),
                    }
                )
            return self._ask_robot(tb, message)

        @tool
        def ask_all_robots(message: str) -> str:
            """SEND the same task or question to every fleet robot in parallel and wait for all replies as JSON."""
            replies: dict[str, str] = {}
            with ThreadPoolExecutor(max_workers=len(TB_IDS)) as pool:
                future_to_tb = {
                    pool.submit(self._ask_robot, tb_id, message): tb_id for tb_id in TB_IDS
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
            """SEND the same task or question to selected robots in parallel and wait for all replies as JSON.

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
                        "message": 'robots_json must be a JSON array like ["tb1","tb3"]',
                        "detail": str(e),
                    }
                )
            if not isinstance(raw, list):
                return json.dumps(
                    {
                        "error": "invalid_robots_json",
                        "message": 'robots_json must be a JSON array like ["tb1","tb3"]',
                    }
                )

            allowed = set(TB_IDS)
            ordered_unique: list[str] = []
            seen: set[str] = set()
            invalid: list[str] = []
            for r in raw:
                s = str(r).strip().lower().removeprefix("robot_")
                if s in allowed:
                    if s not in seen:
                        ordered_unique.append(s)
                        seen.add(s)
                else:
                    invalid.append(str(r))

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

        return [ask_robot, ask_all_robots, ask_selected_robots_parallel]


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
