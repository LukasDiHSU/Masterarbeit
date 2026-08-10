from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, TB_IDS, fleet_prompt_range, resolve_robot_id, robot_peer_name
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT

_REVIEW_PREFIX = (
    "PLAN REVIEW REQUEST — do NOT execute yet. "
    "Check only YOUR assignment. Prefer ZERO tools; reply AGREE: or DISAGREE: "
    "in one line. If you must pick farthest/nearest station, call "
    "rank_stations_by_distance once — do not sense peers, lasers, or events.\n\n"
    "FLEET PLAN:\n"
)


class PlannerAgent(BaseAgent):
    """Central CMAS-style planner that must collect local AGREE/DISAGREE
    feedback before sending execute instructions.
    """

    def __init__(self, *, bus: BusClient):
        self.bus = bus
        self._active_thread_id = "default"
        fleet = fleet_prompt_range()

        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "Central HMAS-2 planner: proposes a fleet plan, collects local "
                    "feedback, re-plans until consensus, then sends execute messages."
                ),
                system_prompt=(
                    f"You are the central planner for {fleet} (HMAS-2 architecture, "
                    f"{AGENT_COUNT} robots).\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Talk to the human user and draft ONE short fleet plan with clear "
                    "per-robot assignments inside that single document.\n"
                    "- collect_feedback(plan, recipients): SEND that plan as a REVIEW "
                    "request (not execution) to recipients='all' or e.g. "
                    "'SmallDeliveryRobot_0,SmallDeliveryRobot_2'. "
                    "Each robot replies AGREE: … or DISAGREE: …\n"
                    "- ask_robot(robot, message): SEND a follow-up or an EXECUTE instruction "
                    "to exactly one robot after consensus.\n"
                    "- ask_all_robots(message) / ask_selected_robots(robots, message): SEND "
                    "the same EXECUTE (or other) message to many robots in parallel.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- You have no MCP station/nav tools yourself; robots execute.\n"
                    "- Robots cannot message each other; only you coordinate.\n"
                    "- Do not tell robots to execute until every involved robot has AGREEd "
                    "on the current plan (or you re-planned and they AGREEd).\n"
                    "- Do not invent a different plan per robot via separate collect_feedback "
                    "calls with different texts for the same goal — put roles in one plan.\n"
                    "- Do not ask the user which tool to call.\n"
                    "\n"
                    "WORDING: say you SEND a message / SEND the plan. Do not say broadcast.\n"
                    "\n"
                    "WORKFLOW:\n"
                    "1) Draft one short fleet plan (who moves where; who holds).\n"
                    "2) collect_feedback until all recipients AGREE (on DISAGREE, revise and "
                    "collect again).\n"
                    "3) Only then SEND execute instructions, clearly marked as EXECUTE.\n"
                    "   - Moving robot: EXECUTE with concrete x/y (from their AGREE navigate_xy "
                    "if they reported it) — one ask_robot is enough.\n"
                    "   - Idle robots: either omit EXECUTE, or a one-line "
                    "'EXECUTE: HOLD. Reply HOLDING. Do not use tools.' "
                    "Do NOT ask them to laser-scan or monitor surroundings.\n"
                    "FLEET: Robots share one map; plans must avoid collisions between peers.\n"
                    "TOOLS: If the same tool/ask fails twice with the same args, do not retry "
                    "a third identical call — change the plan or report failure.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="HMAS-2",
        )

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
                    "Use 'all' or a comma-separated list like "
                    "SmallDeliveryRobot_0 or SmallDeliveryRobot_0,SmallDeliveryRobot_1."
                ),
                "unknown": unknown,
                "valid": list(TB_IDS),
            }
        return peers

    def _ask_peer(self, peer: str, message: str) -> str:
        return self.bus.ask(
            to=peer,
            text=message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{peer}",
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

    def _retrieve_tools(self) -> list:
        @tool
        def collect_feedback(plan: str, recipients: str = "all") -> str:
            """SEND a PLAN REVIEW REQUEST (not execution) to the listed robots and collect AGREE/DISAGREE replies.

            Call this after drafting a plan. If any robot DISAGREEs, revise the plan and call again.
            Only after all_agree is true should you SEND execute instructions.

            Args:
                plan: Full fleet plan with per-robot role assignments in one document.
                recipients: 'all', or e.g. 'SmallDeliveryRobot_0' / 'SmallDeliveryRobot_0,SmallDeliveryRobot_1'.
            """
            resolved = self._resolve_recipients(recipients)
            if isinstance(resolved, dict):
                return json.dumps(resolved)
            body = _REVIEW_PREFIX + plan.strip()
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
                        "message": "Pass a single robot like tb2.",
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
                robots: Comma-separated remroc ids, e.g. 'SmallDeliveryRobot_0,SmallDeliveryRobot_2'.
                message: Message to send (typically EXECUTE after consensus).
            """
            resolved = self._resolve_recipients(robots)
            if isinstance(resolved, dict):
                return json.dumps(resolved)
            return json.dumps(self._ask_many(resolved, message), ensure_ascii=False, indent=2)

        return [collect_feedback, ask_robot, ask_all_robots, ask_selected_robots]


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
        agent.run_persistent_chat(thread_id="default")
    finally:
        bus.close()


if __name__ == "__main__":
    main()
