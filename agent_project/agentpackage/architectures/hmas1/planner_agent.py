from __future__ import annotations

import json
from typing import Literal

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import PLANNER_NAME, TB_IDS, robot_peer_name
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from .dialogue import (
    DEFAULT_MAX_ROUNDS,
    build_turn_prompt,
    looks_like_execute,
    parse_execute_actions,
    resolve_participants,
)

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class PlannerAgent(BaseAgent):
    """Paper HMAS-1: central LLM primes with exactly one initial plan, then
    robots run turn-based dialogue until EXECUTE. The planner does not
    confirm, re-plan mid-dialogue, or send sequential per-robot plans.
    """

    def __init__(self, *, bus: BusClient):
        self.bus = bus
        self._active_thread_id = "default"
        self._plan_sent_this_turn = False

        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "HMAS-1 planner: sends exactly one priming plan (to all or to one robot), "
                    "then robots discuss turn-based until EXECUTE."
                ),
                system_prompt=(
                    "You are the central planner for robot_tb1..robot_tb4 (HMAS-1 architecture).\n"
                    "\n"
                    "YOUR ONLY JOB: draft ONE short initial plan and SEND it once. That plan "
                    "primes the robots. After that, robots discuss among themselves in turn "
                    "order (no mesh peer-chat; the system runs the turns). You do not join "
                    "their discussion. You do not need any confirmation that they received "
                    "the plan.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Talk to the human user and write ONE short initial plan (put every "
                    "role assignment inside that single document).\n"
                    "- propose_and_discuss(plan, participants): call this EXACTLY ONCE per "
                    "user goal. participants must be 'all' (everyone) OR a single robot "
                    "like 'tb1' — never several separate calls, never one robot after another, "
                    "never a different plan text per robot.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- Do not wait for acknowledgements or ask robots to confirm the plan.\n"
                    "- Do not call propose_and_discuss more than once for the same user goal.\n"
                    "- Do not send plan A to tb1 and then plan B to tb2 (or any sequence of "
                    "plans). One call, one plan text, recipients='all' OR one robot.\n"
                    "- Do not invent a different plan per robot.\n"
                    "- No MCP station/nav tools; robots act after their dialogue reaches EXECUTE.\n"
                    "- Do not ask the user which tool to call.\n"
                    "- Do not try to speak in the robot turn-taking round yourself.\n"
                    "\n"
                    "WORDING: say you SEND the plan. Do not say broadcast.\n"
                    "\n"
                    "WORKFLOW:\n"
                    "1) Write one initial plan.\n"
                    "2) Call propose_and_discuss once (participants='all' or e.g. 'tb1').\n"
                    "3) Report the returned dialogue / EXECUTE outcome to the user.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="HMAS-1",
        )

    def invoke(self, message: str, thread_id: str = "default") -> str:
        self._active_thread_id = thread_id
        self._plan_sent_this_turn = False
        try:
            return super().invoke(message, thread_id=thread_id)
        finally:
            self._active_thread_id = "default"
            self._plan_sent_this_turn = False

    def _ask_peer(self, peer: str, message: str) -> str:
        return self.bus.ask(
            to=peer,
            text=message,
            thread_id=f"{self.thread_key(self._active_thread_id)}->{peer}",
        )

    def _run_turn_dialogue(
        self,
        plan: str,
        participants: list[str],
        max_rounds: int,
    ) -> dict:
        history: list[dict[str, str]] = []
        execute_text: str | None = None
        execute_speaker: str | None = None

        for round_idx in range(1, max_rounds + 1):
            for speaker in participants:
                prompt = build_turn_prompt(
                    initial_plan=plan,
                    participants=participants,
                    history=history,
                    speaker=speaker,
                    round_idx=round_idx,
                    max_rounds=max_rounds,
                )
                try:
                    reply = self._ask_peer(speaker, prompt)
                except Exception as e:
                    reply = json.dumps(
                        {"error": "ask_failed", "peer": speaker, "message": str(e)}
                    )
                history.append({"speaker": speaker, "text": reply})
                if looks_like_execute(reply):
                    execute_text = reply
                    execute_speaker = speaker
                    break
            if execute_text is not None:
                break

        result: dict = {
            "participants": participants,
            "turn_order": " -> ".join(participants),
            "max_rounds": max_rounds,
            "turns": history,
            "turn_count": len(history),
            "initial_plan": plan.strip(),
        }

        if execute_text is None:
            result["status"] = "no_execute"
            result["message"] = (
                "Dialogue ended without EXECUTE. Try a clearer initial plan or higher max_rounds."
            )
            return result

        actions = parse_execute_actions(execute_text, participants)
        dispatch: dict[str, str] = {}
        for peer in participants:
            action = actions.get(peer, "").strip()
            body = (
                "EXECUTE APPROVED (from turn-based HMAS-1 dialogue).\n"
                f"Decided by: {execute_speaker}\n"
                f"Your action: {action or '(see full EXECUTE block)'}\n\n"
                f"Full EXECUTE block:\n{execute_text.strip()}\n\n"
                "Carry out YOUR part now with your MCP tools. Keep the reply as short but precise as possible."
            )
            try:
                dispatch[peer] = self._ask_peer(peer, body)
            except Exception as e:
                dispatch[peer] = json.dumps(
                    {"error": "execute_dispatch_failed", "peer": peer, "message": str(e)}
                )

        result.update(
            {
                "status": "execute",
                "execute_speaker": execute_speaker,
                "execute_text": execute_text,
                "parsed_actions": actions,
                "execute_replies": dispatch,
                "message": "Dialogue reached EXECUTE; actions were dispatched to participants.",
            }
        )
        return result

    def _retrieve_tools(self) -> list:
        @tool
        def propose_and_discuss(
            plan: str,
            participants: str = "all",
            max_rounds: int = DEFAULT_MAX_ROUNDS,
        ) -> str:
            """SEND exactly one priming plan, then run robot turn-based dialogue until EXECUTE.

            Call this once per user goal. Do not wait for confirmation. Do not call again
            with another plan for the same goal.

            Args:
                plan: The single initial fleet plan (all role assignments in this one text).
                participants: 'all' (everyone) OR one robot like 'tb1' — not a list of many
                    robots, and not multiple sequential calls.
                max_rounds: Max full passes through the participant order (default 3).
            """
            if self._plan_sent_this_turn:
                return json.dumps(
                    {
                        "error": "plan_already_sent",
                        "message": (
                            "You already sent one plan for this user goal. "
                            "Do not send another. Report the previous outcome to the user."
                        ),
                    }
                )
            resolved = resolve_participants(participants)
            if isinstance(resolved, dict):
                return json.dumps(resolved)
            # Enforce: everyone OR exactly one agent (no multi-robot subset / sequential feel).
            if len(resolved) not in (1, len(TB_IDS)):
                return json.dumps(
                    {
                        "error": "invalid_participants",
                        "message": (
                            "participants must be 'all' (every robot) or a single robot "
                            "like 'tb1'. Do not pass several robots; do not send plans "
                            "one after another."
                        ),
                        "got": resolved,
                    }
                )
            rounds = max(1, min(int(max_rounds), 10))
            self._plan_sent_this_turn = True
            outcome = self._run_turn_dialogue(plan.strip(), resolved, rounds)
            return json.dumps(outcome, ensure_ascii=False, indent=2)

        return [propose_and_discuss]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive HMAS-1 planner (central prime + turn-based robot dialogue)."
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
