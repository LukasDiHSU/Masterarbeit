"""HMAS-1 central planner (Chen et al., arXiv:2309.15943, Fig. 3b).

Planning runs as a deterministic outer loop over chunks. Each chunk the
central LLM proposes a short multi-step plan, the robot agents vote on it
in turn-taking dialogue (AGREE unless they see an exception), a rules-based
verifier accepts the plan on the table, and the actions are dispatched step
by step. The resulting state feeds the next chunk.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, fleet_prompt_range
from ...instructions import hmas1_planner
from ...paper_protocol import (
    MAX_DIALOGUE_ROUNDS,
    MAX_PLAN_STEPS,
    MAX_SYNTAX_RETRIES,
    TASK_COMPLETE_RE,
    Action,
    Environment,
    StepHistory,
    assignment_text,
    pad_plan,
    parse_plan_block,
    plan_assignments,
    plan_text,
    results_text,
    verify_plan,
)
from ...timing import format_elapsed, record_timing
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from .dialogue import (
    CHUNK_STEPS,
    build_central_plan_prompt,
    build_execute_dispatch,
    build_turn_prompt,
    looks_agree,
    looks_disagree,
    resolve_participants,
)


class CentralPlannerAgent(BaseAgent):
    """LLM that proposes the initial multi-step plan for one chunk."""

    def __init__(self):
        fleet = fleet_prompt_range()
        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "HMAS-1 central planner: proposes a short multi-step plan "
                    "per chunk; robots follow it unless they vote DISAGREE."
                ),
                system_prompt=hmas1_planner(
                    fleet=fleet, n=AGENT_COUNT, chunk_steps=CHUNK_STEPS
                ),
            ),
            architecture="HMAS-1",
        )


class HMAS1Session:
    """One user mission, planned chunk by chunk until done or a limit is hit."""

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
    ) -> tuple[dict[str, list[Action]] | None, list[str]]:
        plan, problems = parse_plan_block(text, participants)
        if not plan:
            return None, problems
        errors = problems + verify_plan(
            plan, participants, self.env, max_steps=CHUNK_STEPS
        )
        if errors:
            return None, errors
        padded = pad_plan(plan, participants)
        first = plan_assignments(padded)[0]
        if history.repeats_recent(assignment_text(first, participants)):
            return None, [
                "this exact first step already ran in the last two steps "
                "without changing anything — propose something different."
            ]
        return padded, []

    def _dialogue(
        self,
        *,
        task: str,
        history: StepHistory,
        participants: list[str],
        step_index: int,
        initial_plan: str,
    ) -> tuple[dict[str, list[Action]] | None, list[dict[str, str]], str]:
        """Turn-taking until every robot agrees on a verified chunk."""
        dialogue: list[dict[str, str]] = []
        syntax_feedback = ""
        agreed: set[str] = set()

        table_plan, initial_errors = self._verified_plan(
            initial_plan, participants, history
        )
        if table_plan is None:
            syntax_feedback = (
                "The central planner's proposal has problems:\n- "
                + "\n- ".join(initial_errors or ["no valid EXECUTE chunk"])
                + "\nYou cannot AGREE yet — send a corrected EXECUTE block."
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
                            and plan_text(replacement, participants)
                            == plan_text(table_plan, participants)
                        )
                        if same_as_table:
                            agreed.add(speaker)
                        else:
                            table_plan = replacement
                            agreed = {speaker}
                            initial_plan = (
                                "EXECUTE\n" + plan_text(table_plan, participants)
                            )
                        syntax_feedback = ""
                        if agreed == set(participants):
                            return table_plan, dialogue, "agreed"
                        break

                    if parse_plan_block(reply, participants)[0]:
                        retries += 1
                        if retries >= MAX_SYNTAX_RETRIES:
                            syntax_feedback = ""
                            print(
                                f"[planner] {speaker} could not produce a valid chunk "
                                f"in {MAX_SYNTAX_RETRIES} tries; moving to the next speaker."
                            )
                            break
                        syntax_feedback = (
                            "Your EXECUTE block was rejected by the plan checker:\n- "
                            + "\n- ".join(problems)
                            + "\nSend a corrected EXECUTE block."
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
                                "AGREE. Send EXECUTE with a legal chunk."
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
                            "The plan is contested. Send a corrected EXECUTE block; "
                            "do not AGREE to the old plan."
                        )
                        print(f"[planner] {speaker} voted DISAGREE")
                        break

                    syntax_feedback = ""
                    break

        return None, dialogue, "no_consensus"

    def _execute(
        self,
        assignment: dict[str, Action],
        *,
        step_index: int,
        chunk_step: int,
    ) -> dict[str, dict]:
        """Dispatch each verified action to its robot; they run in parallel."""
        results: dict[str, dict] = {}

        def _one(peer: str) -> tuple[str, dict]:
            message = build_execute_dispatch(
                assignment[peer].text(),
                step_index=step_index,
                chunk_step=chunk_step,
            )
            reply = self._ask_robot(peer, message, thread_id=f"exec-{step_index}-{chunk_step}")
            try:
                parsed = json.loads(reply)
            except (json.JSONDecodeError, TypeError):
                parsed = {"ok": False, "action": assignment[peer].text(), "error": reply}
            if not isinstance(parsed, dict):
                parsed = {"ok": False, "action": assignment[peer].text(), "error": reply}
            return peer, parsed

        with ThreadPoolExecutor(max_workers=max(1, len(assignment))) as pool:
            futures = [pool.submit(_one, peer) for peer in assignment]
            for future in as_completed(futures):
                peer, parsed = future.result()
                results[peer] = parsed
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
            print(f"\n=== planning chunk {step_index}/{MAX_PLAN_STEPS} ===")
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
                        f"no agreed chunk after {MAX_DIALOGUE_ROUNDS} dialogue "
                        f"rounds in step {step_index}"
                    ),
                    "steps_taken": step_index - 1,
                    "dialogue": dialogue,
                    "transcript": transcript,
                }

            print(f"[EXECUTE chunk {step_index}]\n{plan_text(plan, participants)}")
            interrupted = False
            for chunk_step, assignment in enumerate(plan_assignments(plan), start=1):
                actions = assignment_text(assignment, participants)
                print(f"[EXECUTE chunk {step_index} step {chunk_step}]\n{actions}")
                results = self._execute(
                    assignment, step_index=step_index, chunk_step=chunk_step
                )
                outcome_text = results_text(results)
                print(f"[result chunk {step_index} step {chunk_step}]\n{outcome_text}")
                history.add(
                    state_before if chunk_step == 1 else "(after previous action of this chunk)",
                    actions,
                    outcome_text,
                )
                transcript.append(
                    {
                        "chunk": step_index,
                        "step": chunk_step,
                        "actions": actions,
                        "result": outcome_text,
                    }
                )
                self.env.refresh()
                state_before = self.env.state_text()
                failed = [
                    peer
                    for peer, parsed in results.items()
                    if not parsed.get("ok", False)
                    and assignment[peer].verb != "wait"
                ]
                if failed:
                    print(
                        f"[planner] chunk interrupted after step {chunk_step}: "
                        f"{', '.join(failed)} failed — replanning from the new state."
                    )
                    interrupted = True
                    break

            if interrupted:
                continue

        return {
            "status": "failed_step_limit",
            "reason": f"reached the planning-chunk limit ({MAX_PLAN_STEPS})",
            "steps_taken": MAX_PLAN_STEPS,
            "transcript": transcript,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "HMAS-1 planner: multi-step chunks primed centrally, robots AGREE "
            "or DISAGREE on exceptions."
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
        f"Limits: {MAX_PLAN_STEPS} chunks, up to {CHUNK_STEPS} actions/robot, "
        f"{MAX_DIALOGUE_ROUNDS} dialogue rounds per chunk, "
        f"{MAX_SYNTAX_RETRIES} syntax retries per speaker."
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
