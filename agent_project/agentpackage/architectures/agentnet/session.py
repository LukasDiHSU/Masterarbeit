"""Turn-taking DMAS session chaired by one mesh node (usually Agent 0).

The chair is not a planner: it only asks each robot in order, checks EXECUTE
and FINISHED replies, dispatches verified actions, and opens the next meeting
from the new world state.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from ...paper_protocol import (
    FINISHED_RE,
    MAX_DIALOGUE_ROUNDS,
    MAX_PLAN_STEPS,
    MAX_SYNTAX_RETRIES,
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
from .protocol import CHUNK_STEPS, build_execute_dispatch, build_turn_prompt

AskFn = Callable[[str, str, str], str]


class DMASSession:
    def __init__(self, *, ask: AskFn, chair: str, participants: list[str]):
        self._ask = ask
        self.chair = chair
        self.participants = list(participants)
        self.env = Environment(self.participants)

    def run(self, task: str) -> dict[str, Any]:
        history = StepHistory()
        transcript: list[dict[str, Any]] = []

        for meeting in range(1, MAX_PLAN_STEPS + 1):
            self.env.refresh()
            if self.env.last_error:
                return {
                    "status": "environment_unavailable",
                    "detail": self.env.last_error,
                    "meetings": meeting - 1,
                }

            state_before = self.env.state_text()
            print(f"\n=== meeting {meeting}/{MAX_PLAN_STEPS} ===")
            print(state_before)

            plan, dialogue, outcome = self._discuss(
                task=task,
                history=history,
                meeting=meeting,
            )
            transcript.append({"meeting": meeting, "dialogue": dialogue, "outcome": outcome})

            if outcome == "finished":
                return {
                    "status": "success",
                    "reason": "every robot said FINISHED in the same discussion round",
                    "meetings": meeting,
                    "transcript": transcript,
                }
            if plan is None:
                return {
                    "status": "failed_no_consensus",
                    "reason": (
                        f"no valid plan after {MAX_DIALOGUE_ROUNDS} discussion "
                        f"rounds in meeting {meeting}"
                    ),
                    "meetings": meeting,
                    "dialogue": dialogue,
                    "transcript": transcript,
                }

            padded = pad_plan(plan, self.participants)
            print(f"[EXECUTE meeting {meeting}]\n{plan_text(padded, self.participants)}")
            for step_index, assignment in enumerate(plan_assignments(padded), start=1):
                results = self._execute(assignment, meeting=meeting, step=step_index)
                outcome_text = results_text(results)
                print(f"[result meeting {meeting} step {step_index}]\n{outcome_text}")
                history.add(
                    state_before if step_index == 1 else "(after previous step of this chunk)",
                    assignment_text(assignment, self.participants),
                    outcome_text,
                )
                self.env.refresh()
                state_before = self.env.state_text()

        return {
            "status": "failed_meeting_limit",
            "reason": f"reached the meeting limit ({MAX_PLAN_STEPS})",
            "meetings": MAX_PLAN_STEPS,
            "transcript": transcript,
        }

    def _discuss(
        self,
        *,
        task: str,
        history: StepHistory,
        meeting: int,
    ) -> tuple[dict[str, list[Action]] | None, list[dict[str, str]], str]:
        dialogue: list[dict[str, str]] = []
        syntax_feedback = ""

        for round_idx in range(1, MAX_DIALOGUE_ROUNDS + 1):
            round_finished = True
            for speaker in self.participants:
                retries = 0
                while True:
                    prompt = build_turn_prompt(
                        task=task,
                        env=self.env,
                        history=history,
                        participants=self.participants,
                        speaker=speaker,
                        meeting=meeting,
                        max_meetings=MAX_PLAN_STEPS,
                        round_idx=round_idx,
                        max_rounds=MAX_DIALOGUE_ROUNDS,
                        dialogue=dialogue,
                        syntax_feedback=syntax_feedback,
                    )
                    reply = self._ask(
                        speaker,
                        prompt,
                        f"meet-{meeting}-r{round_idx}-{speaker}-{retries}",
                    )
                    dialogue.append({"speaker": speaker, "text": reply})
                    print(f"[{speaker}] {(reply or '').strip()[:400]}")

                    plan, problems = parse_plan_block(reply, self.participants)
                    if plan:
                        errors = problems + verify_plan(
                            plan,
                            self.participants,
                            self.env,
                            max_steps=CHUNK_STEPS,
                        )
                        first = plan_assignments(pad_plan(plan, self.participants))[0]
                        if history.repeats_recent(
                            assignment_text(first, self.participants)
                        ):
                            errors.append(
                                "this exact chunk already ran in the last two steps "
                                "without changing anything — propose something different."
                            )
                        if not errors:
                            return plan, dialogue, "agreed"
                        retries += 1
                        if retries >= MAX_SYNTAX_RETRIES:
                            syntax_feedback = ""
                            round_finished = False
                            print(
                                f"[{self.chair}] {speaker} could not produce a valid plan "
                                f"in {MAX_SYNTAX_RETRIES} tries; next speaker."
                            )
                            break
                        syntax_feedback = (
                            "Your EXECUTE block was rejected by the plan checker:\n- "
                            + "\n- ".join(errors)
                            + "\nSend a corrected EXECUTE block."
                        )
                        print(f"[{self.chair}] rejected {speaker}: {errors}")
                        continue

                    syntax_feedback = ""
                    if not FINISHED_RE.search(reply or ""):
                        round_finished = False
                    break

            if round_finished and len(dialogue) >= len(self.participants):
                last = dialogue[-len(self.participants) :]
                names = [turn["speaker"] for turn in last]
                if names == self.participants and all(
                    FINISHED_RE.search(turn["text"] or "") for turn in last
                ):
                    return None, dialogue, "finished"

        return None, dialogue, "no_consensus"

    def _execute(
        self,
        assignment: dict[str, Action],
        *,
        meeting: int,
        step: int,
    ) -> dict[str, dict]:
        results: dict[str, dict] = {}

        def _one(peer: str) -> tuple[str, dict]:
            message = build_execute_dispatch(
                assignment[peer].text(), meeting=meeting, step=step
            )
            reply = self._ask(peer, message, f"exec-{meeting}-{step}-{peer}")
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
