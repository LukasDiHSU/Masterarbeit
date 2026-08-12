from __future__ import annotations

import json
import re
import threading

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, TB_IDS, STATION_CAPACITY_RULE, fleet_prompt_range, robot_peer_name
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from .dialogue import (
    DEFAULT_MAX_ROUNDS,
    agreed_plan_text,
    all_participants_agree,
    build_turn_prompt,
    parse_execute_actions,
    resolve_participants,
)

_START_DISCUSSION_RE = re.compile(
    r"^\s*START_DISCUSSION\b", re.IGNORECASE | re.MULTILINE
)


class PlannerAgent(BaseAgent):
    """Paper HMAS-1: central LLM primes with one initial plan, then robots
    discuss turn-based until every participant AGREEs; then execute is
    dispatched. Further discussion rounds use ``start_discussion_round``.
    """

    def __init__(self, *, bus: BusClient):
        self.bus = bus
        self._active_thread_id = "default"
        self._plan_sent_this_turn = False
        self._last_participants: list[str] = [robot_peer_name(rid) for rid in TB_IDS]
        self._last_plan: str = ""
        self._dialogue_lock = threading.Lock()
        fleet = fleet_prompt_range()

        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "HMAS-1 planner: one priming plan, then robots discuss until "
                    "unanimous AGREE, then execute; can reopen discussion."
                ),
                system_prompt=(
                    f"You are the central planner for {fleet} (HMAS-1 architecture, "
                    f"{AGENT_COUNT} robots).\n"
                    "\n"
                    "YOUR JOB: draft ONE short initial plan and SEND it once via "
                    "propose_and_discuss. Robots then discuss in turn order until "
                    "every robot's latest message starts with AGREE. Only then does "
                    "the system dispatch execute. You do not join their discussion.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    "- Talk to the human user and write ONE short initial plan.\n"
                    "- propose_and_discuss(plan, participants): call ONCE per new user "
                    "goal to prime and run discussion→AGREE→execute.\n"
                    "- start_discussion_round(context, participants): call when robots "
                    "(or you) need another discussion after execute — e.g. conflict or "
                    "replan. Same AGREE-then-execute flow without a brand-new mission.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- Do not wait for acknowledgements that they received the plan.\n"
                    "- Do not call propose_and_discuss more than once for the same user goal.\n"
                    "- Do not send different plan texts to different robots sequentially.\n"
                    "- No MCP station/nav tools; robots act after unanimous AGREE.\n"
                    "- Do not ask the user which tool to call.\n"
                    "- Do not speak in the robot turn-taking round yourself.\n"
                    "\n"
                    f"{STATION_CAPACITY_RULE}\n"
                    "Initial plan must only use empty drop destinations; for swaps, clear pads first.\n"
                    "\n"
                    "WORDING: say you SEND the plan. Do not say broadcast.\n"
                    "\n"
                    "WORKFLOW:\n"
                    "1) Write one initial plan.\n"
                    "2) Call propose_and_discuss once (participants='all' or one robot).\n"
                    "3) Report the AGREE / execute outcome to the user.\n"
                    "4) If a replan is needed later, call start_discussion_round.\n"
                    "FLEET: Robots share one map; the initial plan must avoid collisions.\n"
                    "TOOLS: If the same tool/ask fails twice with the same args, do not retry "
                    "a third identical call — change the plan or report failure.\n"
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

    def _dispatch_execute(
        self,
        plan: str,
        participants: list[str],
        history: list[dict[str, str]],
    ) -> dict[str, str]:
        agreed = agreed_plan_text(plan, history, participants)
        # Prefer action lines from the last AGREE messages combined.
        combined_agree = "\n".join(
            t["text"] for t in history if t.get("speaker") in participants
        )
        actions = parse_execute_actions(combined_agree, participants)
        dispatch: dict[str, str] = {}
        for peer in participants:
            action = actions.get(peer, "").strip()
            body = (
                "EXECUTE APPROVED (HMAS-1: all participants AGREEd).\n"
                f"Your action: {action or '(carry out YOUR role from the agreed plan)'}\n\n"
                f"{agreed}\n\n"
                "Carry out YOUR part now with your MCP tools. "
                "If you hit a conflict that needs fleet replan, call "
                "start_discussion_round(reason=...). "
                "Keep the reply as short but precise as possible."
            )
            try:
                dispatch[peer] = self._ask_peer(peer, body)
            except Exception as e:
                dispatch[peer] = json.dumps(
                    {"error": "execute_dispatch_failed", "peer": peer, "message": str(e)}
                )
        return dispatch

    def _run_turn_dialogue(
        self,
        plan: str,
        participants: list[str],
        max_rounds: int,
        *,
        allow_followup: bool = True,
    ) -> dict:
        history: list[dict[str, str]] = []
        self._last_plan = plan
        self._last_participants = list(participants)

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
                if all_participants_agree(history, participants):
                    break
            if all_participants_agree(history, participants):
                break

        result: dict = {
            "participants": participants,
            "turn_order": " -> ".join(participants),
            "max_rounds": max_rounds,
            "turns": history,
            "turn_count": len(history),
            "initial_plan": plan.strip(),
        }

        if not all_participants_agree(history, participants):
            result["status"] = "no_consensus"
            result["message"] = (
                "Dialogue ended without unanimous AGREE. "
                "Call start_discussion_round with a clearer context, or raise max_rounds."
            )
            return result

        dispatch = self._dispatch_execute(plan, participants, history)
        result.update(
            {
                "status": "agreed_execute",
                "agreed_plan": agreed_plan_text(plan, history, participants),
                "execute_replies": dispatch,
                "message": (
                    "All participants AGREEd; execute was dispatched to each robot."
                ),
            }
        )

        # Robots may signal NEED_DISCUSSION in execute replies (avoids bus deadlock
        # if they called start_discussion_round mid-execute).
        need_reasons: list[str] = []
        if allow_followup:
            for peer, reply in dispatch.items():
                for line in (reply or "").splitlines():
                    if re.match(r"^\s*NEED_DISCUSSION\b", line, re.IGNORECASE):
                        need_reasons.append(f"{peer}: {line.strip()}")
        if need_reasons:
            follow_plan = (
                f"{plan.strip()}\n\nRe-discussion requested after execute:\n"
                + "\n".join(need_reasons)
            )
            result["followup_discussion"] = self._run_turn_dialogue(
                follow_plan, participants, max_rounds, allow_followup=False
            )
            result["message"] += " A follow-up discussion→AGREE→execute round was run."

        return result

    def run_discussion(
        self,
        plan: str,
        participants: str = "all",
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> dict:
        """Public entry for propose_and_discuss / start_discussion_round / robot asks."""
        with self._dialogue_lock:
            resolved = resolve_participants(participants)
            if isinstance(resolved, dict):
                return resolved
            if len(resolved) not in (1, len(TB_IDS)):
                return {
                    "error": "invalid_participants",
                    "message": (
                        "participants must be 'all' (every robot) or a single robot "
                        "like 'SmallDeliveryRobot_0'."
                    ),
                    "got": resolved,
                }
            rounds = max(1, min(int(max_rounds), 10))
            return self._run_turn_dialogue(plan.strip(), resolved, rounds)

    def handle_robot_start_discussion(self, text: str) -> str:
        """Handle START_DISCUSSION requests from robots over the bus."""
        body = _START_DISCUSSION_RE.sub("", text or "", count=1).strip()
        reason = ""
        plan = self._last_plan or "Replan from current fleet state."
        for line in body.splitlines():
            low = line.strip().lower()
            if low.startswith("reason:"):
                reason = line.split(":", 1)[1].strip()
            elif low.startswith("plan:"):
                plan = line.split(":", 1)[1].strip() or plan
        if reason:
            plan = f"{plan}\n\nRe-discussion reason: {reason}"
        participants = ",".join(self._last_participants) if self._last_participants else "all"
        outcome = self.run_discussion(plan, participants=participants)
        return json.dumps(outcome, ensure_ascii=False, indent=2)

    def _retrieve_tools(self) -> list:
        @tool
        def propose_and_discuss(
            plan: str,
            participants: str = "all",
            max_rounds: int = DEFAULT_MAX_ROUNDS,
        ) -> str:
            """SEND one priming plan, then run robot discussion until unanimous AGREE, then execute.

            Call once per new user goal. For a later replan, use start_discussion_round instead.

            Args:
                plan: The single initial fleet plan (all role assignments in this one text).
                participants: 'all' OR one robot like 'SmallDeliveryRobot_0'.
                max_rounds: Max full passes through the participant order (default 5).
            """
            if self._plan_sent_this_turn:
                return json.dumps(
                    {
                        "error": "plan_already_sent",
                        "message": (
                            "You already sent one plan for this user goal. "
                            "Use start_discussion_round for a replan, or report the outcome."
                        ),
                    }
                )
            self._plan_sent_this_turn = True
            outcome = self.run_discussion(plan, participants=participants, max_rounds=max_rounds)
            return json.dumps(outcome, ensure_ascii=False, indent=2)

        @tool
        def start_discussion_round(
            context: str,
            participants: str = "all",
            max_rounds: int = DEFAULT_MAX_ROUNDS,
        ) -> str:
            """Start another HMAS-1 discussion round (until unanimous AGREE), then execute again.

            Use after the first propose_and_discuss when robots need to replan (conflict,
            blocked path, new info). Robots may also request this via their own tool.

            Args:
                context: Updated plan / reason for re-discussion (roles, constraints).
                participants: 'all' OR one robot.
                max_rounds: Max full passes (default 5).
            """
            plan = context.strip() or self._last_plan or "Replan from current fleet state."
            outcome = self.run_discussion(plan, participants=participants, max_rounds=max_rounds)
            return json.dumps(outcome, ensure_ascii=False, indent=2)

        return [propose_and_discuss, start_discussion_round]


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Interactive HMAS-1 planner (prime → AGREE discussion → execute)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    bus = BusClient(PLANNER_NAME, host=args.host, port=args.port)
    agent = PlannerAgent(bus=bus)

    def handle_message(msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "error":
            print(f"\n[broker error] {msg.get('text', '')}")
            return
        if msg_type != "agent_request":
            return
        src = str(msg.get("from", "?"))
        text = str(msg.get("text", ""))
        thread_id = str(msg.get("thread_id", src))
        if not _START_DISCUSSION_RE.search(text):
            bus.send(
                type="agent_reply",
                to=src,
                text=json.dumps(
                    {
                        "error": "unsupported_request",
                        "message": "Planner only handles START_DISCUSSION from robots.",
                    }
                ),
                thread_id=thread_id,
                request_id=msg.get("request_id"),
            )
            return
        print(f"\n[{src} -> planner] start_discussion request")
        try:
            reply = agent.handle_robot_start_discussion(text)
        except Exception as e:
            reply = json.dumps(
                {"error": "start_discussion_failed", "message": str(e)}
            )
        print(f"[planner] discussion outcome ready ({len(reply)} chars)")
        bus.send(
            type="agent_reply",
            to=src,
            text=reply,
            thread_id=thread_id,
            request_id=msg.get("request_id"),
        )

    bus.on_message(handle_message)

    try:
        agent.run_persistent_chat(
            thread_id="default",
            timing_label="hmas1_until_done",
        )
    finally:
        bus.close()


if __name__ == "__main__":
    main()
