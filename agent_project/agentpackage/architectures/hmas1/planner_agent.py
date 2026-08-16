"""HMAS-1 central planner (Chen et al., arXiv:2309.15943, Fig. 3b).

Each round the central LLM proposes a short natural-language plan (one leg
per robot). The robots vote AGREE unless they see an exception, then each
executor carries out its own leg with MCP tools. The resulting state opens
the next round.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, fleet_prompt_range, is_q1_platform
from ...instructions import hmas1_planner, q1_hmas1_planner
from ...mcp_client import Q1_PLANNING_MAP_TOOL_NAMES, load_mcp_tools_safe
from ...paper_protocol import (
    MAX_DIALOGUE_ROUNDS,
    MAX_PLAN_STEPS,
    MAX_SYNTAX_RETRIES,
    TASK_COMPLETE_RE,
    Environment,
    StepHistory,
)
from ...timing import format_elapsed, record_timing
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from .dialogue import (
    build_central_plan_prompt,
    build_execute_dispatch,
    build_turn_prompt,
    legs_text,
    looks_agree,
    looks_disagree,
    parse_hmas1_plan,
    resolve_participants,
)

_PLANNER_MAP_TOOLS = frozenset(
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


class CentralPlannerAgent(BaseAgent):
    """LLM that proposes the initial natural-language plan for one round."""

    def __init__(self):
        fleet = fleet_prompt_range()
        prompt_fn = q1_hmas1_planner if is_q1_platform() else hmas1_planner
        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "HMAS-1 central planner: proposes a short natural-language "
                    "plan per round; robots follow it unless they vote DISAGREE."
                ),
                system_prompt=prompt_fn(fleet=fleet, n=AGENT_COUNT),
            ),
            architecture="HMAS-1",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        names = Q1_PLANNING_MAP_TOOL_NAMES if is_q1_platform() else _PLANNER_MAP_TOOLS
        return {
            t.name: t
            for t in load_mcp_tools_safe()
            if t.name in names
        }

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


class HMAS1Session:
    """One user mission, planned round by round until done or a limit is hit."""

    def __init__(self, *, bus: BusClient, planner: CentralPlannerAgent):
        self.bus = bus
        self.planner = planner
        self.env = Environment()
        self._lock = threading.Lock()

    def _ask_robot(self, peer: str, message: str, thread_id: str) -> str:
        try:
            return self.bus.ask(to=peer, text=message, thread_id=thread_id)
        except Exception as e:
            return f"(no reply from {peer}: {type(e).__name__}: {e})"

    def _central_plan(
        self,
        *,
        task: str,
        history: StepHistory,
        participants: list[str],
        step_index: int,
    ) -> str:
        prompt = build_central_plan_prompt(
            task=task,
            env=self.env,
            history=history,
            participants=participants,
            step_index=step_index,
        )
        return self.planner.invoke(prompt, thread_id=f"step-{step_index}")

    def _verified_plan(
        self,
        text: str,
        participants: list[str],
        history: StepHistory,
    ) -> tuple[dict[str, str] | None, list[str]]:
        legs, errors = parse_hmas1_plan(text, participants)
        if legs is None:
            return None, errors
        rendered = legs_text(legs, participants)
        if history.repeats_recent(rendered):
            return None, [
                "this exact plan already ran in the last two rounds "
                "without changing anything — propose something different."
            ]
        return legs, []

    def _dialogue(
        self,
        *,
        task: str,
        history: StepHistory,
        participants: list[str],
        step_index: int,
        initial_plan: str,
    ) -> tuple[dict[str, str] | None, list[dict[str, str]], str]:
        """Turn-taking until every robot agrees on a verified plan."""
        dialogue: list[dict[str, str]] = []
        syntax_feedback = ""
        agreed: set[str] = set()

        table_plan, initial_errors = self._verified_plan(
            initial_plan, participants, history
        )
        if table_plan is None:
            syntax_feedback = (
                "The central planner's proposal has problems:\n- "
                + "\n- ".join(initial_errors or ["no valid PLAN"])
                + "\nYou cannot AGREE yet — send a corrected PLAN block."
            )

        for round_idx in range(1, MAX_DIALOGUE_ROUNDS + 1):
            for speaker in participants:
                retries = 0
                while True:
                    prompt = build_turn_prompt(
                        task=task,
                        env=self.env,
                        history=history,
                        participants=participants,
                        speaker=speaker,
                        step_index=step_index,
                        round_idx=round_idx,
                        max_rounds=MAX_DIALOGUE_ROUNDS,
                        initial_plan=initial_plan,
                        dialogue=dialogue,
                        syntax_feedback=syntax_feedback,
                    )
                    reply = self._ask_robot(
                        speaker,
                        prompt,
                        thread_id=f"step-{step_index}-r{round_idx}-{retries}",
                    )
                    dialogue.append({"speaker": speaker, "text": reply})
                    print(f"[{speaker}] {reply.strip()[:300]}")

                    if TASK_COMPLETE_RE.search(reply):
                        return None, dialogue, "task_complete"

                    replacement, problems = self._verified_plan(
                        reply, participants, history
                    )
                    if replacement is not None:
                        same_as_table = (
                            table_plan is not None
                            and legs_text(replacement, participants)
                            == legs_text(table_plan, participants)
                        )
                        if same_as_table:
                            agreed.add(speaker)
                        else:
                            table_plan = replacement
                            agreed = {speaker}
                            initial_plan = "PLAN\n" + legs_text(
                                table_plan, participants
                            )
                        syntax_feedback = ""
                        if agreed == set(participants):
                            return table_plan, dialogue, "agreed"
                        break

                    if replacement is None and problems:
                        retries += 1
                        if retries >= MAX_SYNTAX_RETRIES:
                            syntax_feedback = ""
                            print(
                                f"[planner] {speaker} could not produce a valid plan "
                                f"in {MAX_SYNTAX_RETRIES} tries; moving to the next speaker."
                            )
                            break
                        syntax_feedback = (
                            "Your PLAN was rejected:\n- "
                            + "\n- ".join(problems)
                            + "\nSend a corrected PLAN block."
                        )
                        print(f"[planner] syntax check rejected {speaker}: {problems}")
                        continue

                    if looks_agree(reply):
                        if table_plan is None:
                            retries += 1
                            if retries >= MAX_SYNTAX_RETRIES:
                                syntax_feedback = ""
                                break
                            syntax_feedback = (
                                "There is no valid plan on the table yet — you cannot "
                                "AGREE. Send PLAN with a legal leg for every robot."
                            )
                            continue
                        agreed.add(speaker)
                        syntax_feedback = ""
                        if agreed == set(participants):
                            return table_plan, dialogue, "agreed"
                        break

                    if looks_disagree(reply):
                        agreed.clear()
                        syntax_feedback = (
                            f"{speaker} voted DISAGREE:\n{reply.strip()[:400]}\n"
                            "The plan is contested. Send a corrected PLAN block; "
                            "do not AGREE to the old plan."
                        )
                        print(f"[planner] {speaker} voted DISAGREE")
                        break

                    syntax_feedback = ""
                    break

        return None, dialogue, "no_consensus"

    def _execute(
        self,
        legs: dict[str, str],
        *,
        step_index: int,
    ) -> dict[str, str]:
        """Dispatch each agreed leg; robots run them in parallel with MCP."""
        results: dict[str, str] = {}

        def _one(peer: str) -> tuple[str, str]:
            message = build_execute_dispatch(legs[peer], step_index=step_index)
            reply = self._ask_robot(peer, message, thread_id=f"exec-{step_index}")
            return peer, reply

        with ThreadPoolExecutor(max_workers=max(1, len(legs))) as pool:
            futures = [pool.submit(_one, peer) for peer in legs]
            for future in as_completed(futures):
                peer, reply = future.result()
                results[peer] = reply
        return results

    def run(self, task: str, participants: list[str]) -> dict:
        history = StepHistory()
        self.env.participants = list(participants)
        transcript: list[dict] = []

        for step_index in range(1, MAX_PLAN_STEPS + 1):
            self.env.refresh()
            if self.env.last_error:
                return {
                    "status": "environment_unavailable",
                    "detail": self.env.last_error,
                    "steps_taken": step_index - 1,
                }

            state_before = self.env.state_text()
            print(f"\n=== planning round {step_index}/{MAX_PLAN_STEPS} ===")
            print(state_before)

            initial_plan = self._central_plan(
                task=task,
                history=history,
                participants=participants,
                step_index=step_index,
            )
            print(f"[planner proposal]\n{initial_plan.strip()[:800]}")
            if TASK_COMPLETE_RE.search(initial_plan):
                return {
                    "status": "success",
                    "reason": "planner reported the task is complete",
                    "steps_taken": step_index - 1,
                    "transcript": transcript,
                }

            plan, dialogue, outcome = self._dialogue(
                task=task,
                history=history,
                participants=participants,
                step_index=step_index,
                initial_plan=initial_plan,
            )
            if outcome == "task_complete":
                return {
                    "status": "success",
                    "reason": "robots reported the task is complete",
                    "steps_taken": step_index - 1,
                    "transcript": transcript,
                }
            if plan is None:
                return {
                    "status": "failed_no_consensus",
                    "reason": (
                        f"no agreed plan after {MAX_DIALOGUE_ROUNDS} dialogue "
                        f"rounds in step {step_index}"
                    ),
                    "steps_taken": step_index - 1,
                    "dialogue": dialogue,
                    "transcript": transcript,
                }

            rendered = legs_text(plan, participants)
            print(f"[EXECUTE round {step_index}]\n{rendered}")
            results = self._execute(plan, step_index=step_index)
            outcome_text = "\n".join(
                f"{peer}: {' '.join((results.get(peer) or '').split())[:240]}"
                for peer in participants
            )
            print(f"[result round {step_index}]\n{outcome_text}")
            history.add(state_before, rendered, outcome_text)
            transcript.append(
                {"round": step_index, "plan": rendered, "result": outcome_text}
            )

        return {
            "status": "failed_step_limit",
            "reason": f"reached the planning-round limit ({MAX_PLAN_STEPS})",
            "steps_taken": MAX_PLAN_STEPS,
            "transcript": transcript,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "HMAS-1 planner: natural-language legs primed centrally, robots "
            "AGREE or DISAGREE on exceptions, then execute with MCP tools."
        )
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--participants", default="all")
    args = parser.parse_args()

    bus = BusClient(PLANNER_NAME, host=args.host, port=args.port)
    planner = CentralPlannerAgent()
    session = HMAS1Session(bus=bus, planner=planner)

    participants = resolve_participants(args.participants)
    if isinstance(participants, dict):
        raise SystemExit(json.dumps(participants, indent=2))

    def handle_message(msg: dict) -> None:
        if msg.get("type") == "error":
            print(f"\n[broker error] {msg.get('text', '')}")

    bus.on_message(handle_message)

    print(f"HMAS-1 planner online. Robots: {', '.join(participants)}")
    print(
        f"Limits: {MAX_PLAN_STEPS} rounds, {MAX_DIALOGUE_ROUNDS} dialogue "
        f"rounds per plan, {MAX_SYNTAX_RETRIES} syntax retries per speaker."
    )
    print("Type the mission and press Enter. Ctrl+C to exit.\n")

    try:
        while True:
            line = input("mission> ").strip()
            if not line:
                continue
            if line.lower() in {"quit", "exit", "q"}:
                break
            t0 = time.perf_counter()
            outcome = session.run(line, list(participants))
            elapsed = time.perf_counter() - t0
            print("\n=== mission outcome ===")
            print(json.dumps(
                {k: v for k, v in outcome.items() if k != "transcript"},
                indent=2,
                ensure_ascii=False,
            ))
            print(f"--- elapsed until done: {format_elapsed(elapsed)} ({elapsed:.1f}s) ---")
            record_timing(
                "hmas1_until_done",
                elapsed,
                extra=f"status={outcome.get('status')} steps={outcome.get('steps_taken')}",
            )
            planner.print_token_usage()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        bus.close()


if __name__ == "__main__":
    main()
