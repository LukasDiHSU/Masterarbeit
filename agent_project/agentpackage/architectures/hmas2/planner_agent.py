from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, TB_IDS, fleet_prompt_range, is_q1_platform, peer_id_example, resolve_robot_id, robot_peer_name
from ...instructions import HMAS2_REVIEW_PREFIX, Q1_HMAS2_REVIEW_PREFIX, hmas2_planner, q1_hmas2_planner
from ...mcp_client import load_planning_mcp_tools
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT


class PlannerAgent(BaseAgent):
    """Central CMAS-style planner that must collect local AGREE/DISAGREE
    feedback before sending execute instructions.
    """

    def __init__(self, *, bus: BusClient):
        self.bus = bus
        self._active_thread_id = "default"
        fleet = fleet_prompt_range()

        prompt_fn = q1_hmas2_planner if is_q1_platform() else hmas2_planner
        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "Central HMAS-2 planner: proposes a specialist plan, collects "
                    "AGREE/DISAGREE, then sends execute messages."
                ),
                system_prompt=prompt_fn(fleet=fleet, n=AGENT_COUNT),
            ),
            architecture="HMAS-2",
        )
        self._require_mission_done = True

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"

    def _resolve_recipients(self, recipients: str) -> list[str] | dict:
        raw = recipients.strip()
        if raw.lower() in {"all", "*", "everyone", "fleet"}:
            return [robot_peer_name(rid) for rid in TB_IDS]

        tokens = [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]
        peers: list[str] = []
        unknown: list[str] = []
        for tok in tokens:
            rid = resolve_robot_id(tok)
            if rid is None:
                unknown.append(tok)
                continue
            name = robot_peer_name(rid)
            if name not in peers:
                peers.append(name)
        if unknown or not peers:
            return {
                "error": "invalid_recipients",
                "message": (
                    "Use 'all' or a comma-separated list of specialist names like "
                    f"{peer_id_example()}."
                ),
                "unknown": unknown,
                "valid": list(TB_IDS),
            }
        return peers

    def _ask_peer(self, peer: str, message: str) -> str:
        try:
            return self.bus.ask(
                to=peer,
                text=message,
                thread_id=f"{self.thread_key(self._active_thread_id)}->{peer}",
            )
        except TimeoutError as e:
            return json.dumps(
                {"error": "ask_timeout", "peer": peer, "message": str(e)}
            )

    def _ask_many(self, peers: list[str], message: str) -> dict[str, str]:
        replies: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(peers))) as pool:
            futs = {pool.submit(self._ask_peer, peer, message): peer for peer in peers}
            for fut in as_completed(futs):
                peer = futs[fut]
                try:
                    replies[peer] = fut.result()
                except Exception as e:
                    replies[peer] = json.dumps(
                        {"error": "ask_failed", "peer": peer, "message": str(e)}
                    )
        return replies

    @staticmethod
    def _summarize_feedback(replies: dict[str, str]) -> dict:
        agree: list[str] = []
        disagree: list[str] = []
        other: list[str] = []
        for peer, text in replies.items():
            head = text.strip().splitlines()[0].strip().upper() if text.strip() else ""
            if head.startswith("AGREE"):
                agree.append(peer)
            elif head.startswith("DISAGREE"):
                disagree.append(peer)
            else:
                other.append(peer)
        return {
            "all_agree": bool(replies) and not disagree and not other,
            "agree": agree,
            "disagree": disagree,
            "unclear": other,
            "replies": replies,
        }

    @cached_property
    def _map_tools(self) -> list:
        return load_planning_mcp_tools()

    def _retrieve_tools(self) -> list:
        @tool
        def collect_feedback(plan: str, recipients: str = "all") -> str:
            """SEND a PLAN REVIEW REQUEST (not execution) to the listed robots and collect AGREE/DISAGREE replies.

            Call this after drafting a plan. If any robot DISAGREEs, revise the plan and call again.
            Only after all_agree is true should you SEND execute instructions.

            Args:
                plan: Full fleet plan with per-robot role assignments in one document.
                recipients: 'all', or specialist names e.g. 'navigator' / 'navigator,lidar'.
            """
            resolved = self._resolve_recipients(recipients)
            if isinstance(resolved, dict):
                return json.dumps(resolved)
            body = (
                Q1_HMAS2_REVIEW_PREFIX if is_q1_platform() else HMAS2_REVIEW_PREFIX
            ) + plan.strip()
            replies = self._ask_many(resolved, body)
            summary = self._summarize_feedback(replies)
            summary["recipients"] = resolved
            if summary["all_agree"]:
                summary["message"] = (
                    "All recipients AGREEd. You may now SEND execute instructions."
                )
            else:
                summary["message"] = (
                    "Not all AGREEd. Revise the plan using DISAGREE reasons, then "
                    "collect_feedback again. Do not execute yet."
                )
            return json.dumps(summary, ensure_ascii=False, indent=2)

        @tool
        def ask_robot(robot: str, message: str) -> str:
            """SEND a message to exactly one robot and wait for its reply (follow-up or EXECUTE)."""
            resolved = self._resolve_recipients(robot)
            if isinstance(resolved, dict):
                return json.dumps(resolved)
            if len(resolved) != 1:
                return json.dumps(
                    {
                        "error": "ask_robot_expects_one",
                        "message": "Pass a single specialist like navigator.",
                    }
                )
            return self._ask_peer(resolved[0], message)

        @tool
        def ask_all_robots(message: str) -> str:
            """SEND the same message to every robot in parallel (typically EXECUTE after consensus)."""
            peers = [robot_peer_name(tb) for tb in TB_IDS]
            return json.dumps(self._ask_many(peers, message), ensure_ascii=False, indent=2)

        @tool
        def ask_selected_robots(robots: str, message: str) -> str:
            """SEND the same message to a subset of robots in parallel.

            Args:
                robots: Comma-separated specialist names, e.g. 'navigator,lidar,camera'.
                message: Message to send (typically EXECUTE after consensus).
            """
            resolved = self._resolve_recipients(robots)
            if isinstance(resolved, dict):
                return json.dumps(resolved)
            return json.dumps(self._ask_many(resolved, message), ensure_ascii=False, indent=2)

        @tool
        def report_mission_done(summary: str) -> str:
            """Call only after every ordered stop is confirmed. Required before any user-facing final answer.

            Args:
                summary: Short confirmation of the completed tour (classes visited, in order).
            """
            self._mission_done_this_turn = True
            return json.dumps(
                {"ok": True, "recorded": True, "summary": summary},
                ensure_ascii=False,
            )

        return [
            *self._map_tools,
            collect_feedback,
            ask_robot,
            ask_all_robots,
            ask_selected_robots,
            report_mission_done,
        ]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive HMAS-2 planner (central plan + local feedback until consensus)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    bus = BusClient(PLANNER_NAME, host=args.host, port=args.port)

    def handle_message(msg: dict) -> None:
        if msg.get("type") == "error":
            print(f"\n[broker error] {msg.get('text', '')}")

    bus.on_message(handle_message)

    try:
        agent = PlannerAgent(bus=bus)
        agent.run_persistent_chat(
            thread_id="default",
            timing_label="hmas2_until_done",
        )
    finally:
        bus.close()


if __name__ == "__main__":
    main()
