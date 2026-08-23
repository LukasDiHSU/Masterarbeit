"""HMAS-1 central planner (Chen et al., arXiv:2309.15943, Fig. 3b).

The central LLM proposes a full multi-step mission plan once. Each robot
votes AGREE or DISAGREE once on that whole plan (no debate). Unanimous
AGREE executes the STEPs in order. After each original work STEP, every
executor that had a real leg reports STEP_OK or STEP_FAILED; any
STEP_FAILED drops the original plan and continues as DMAS. DISAGREE or a
different PLAN at vote time also discards the original plan.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import cached_property

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import AGENT_COUNT, PLANNER_NAME, fleet_prompt_range
from ...instructions import hmas1_planner
from ...mcp_client import load_mcp_tools_safe
from ...paper_protocol import (
    MAX_PLAN_STEPS,
    MAX_SYNTAX_RETRIES,
    TASK_COMPLETE_RE,
    Environment,
    StepHistory,
)
from ...timing import format_elapsed, mission_timeout_guard, record_timing
from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from ..dmas.protocol import (
    CONTINUE,
    DONE,
    EXECUTE,
    EXECUTE_TIMEOUT,
    INVALID,
    MAX_TURNS_PER_ROUND,
    MIN_TALK_TURNS,
    AGREE,
    PLAN,
    TALK,
    TURN_TIMEOUT,
    Consensus,
    RoundRecord,
    Turn,
    build_turn_prompt as build_pmas_turn_prompt,
    looks_idle,
    parse_turn,
    verify_plan,
    coerce_talk_window,
    in_talk_window,
    talk_window_feedback,
)
from .dialogue import (
    HUDDLE_TURN_PREFIX,
    MissionPlan,
    build_central_plan_prompt,
    build_execute_dispatch,
    build_plan_vote_prompt,
    build_step_check_dispatch,
    finish_step,
    is_finish_step,
    looks_agree,
    looks_disagree,
    mission_text,
    parse_hmas1_plan,
    parse_step_check,
    resolve_participants,
    restates_original_step,
)

_PLANNER_MAP_TOOLS = frozenset(
    {
        "list_stations",
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
    """LLM that proposes a full multi-step natural-language mission plan."""

    def __init__(self):
        fleet = fleet_prompt_range()
        super().__init__(
            AgentSpec(
                name=PLANNER_NAME,
                description=(
                    "HMAS-1 central planner: proposes a full multi-step "
                    "mission plan once, ending with FINISHED. Robots vote "
                    "AGREE/DISAGREE once on that whole plan. A rejected "
                    "plan is discarded and the fleet continues as DMAS."
                ),
                system_prompt=hmas1_planner(
                    fleet=fleet, n=AGENT_COUNT, max_steps=MAX_PLAN_STEPS
                ),
            ),
            architecture="HMAS-1",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {
            t.name: t
            for t in load_mcp_tools_safe()
            if t.name in _PLANNER_MAP_TOOLS
        }

    def _retrieve_tools(self):
        return list(self._mcp_tools_by_name.values())


class HMAS1Session:
    """Central plan once, one vote, per-STEP executor check, else DMAS."""

    def __init__(self, *, bus: BusClient, planner: CentralPlannerAgent):
        self.bus = bus
        self.planner = planner
        self.env = Environment()
        self._lock = threading.Lock()

    def _ask_robot(
        self,
        peer: str,
        message: str,
        thread_id: str,
        *,
        timeout: float | None = None,
    ) -> str:
        kwargs: dict = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return self.bus.ask(to=peer, text=message, thread_id=thread_id, **kwargs)
        except Exception as e:
            return f"(no reply from {peer}: {type(e).__name__}: {e})"

    def _central_plan(
        self,
        *,
        task: str,
        history: StepHistory,
        participants: list[str],
        syntax_feedback: str = "",
    ) -> str:
        prompt = build_central_plan_prompt(
            task=task,
            env=self.env,
            history=history,
            participants=participants,
            step_index=1,
            syntax_feedback=syntax_feedback,
        )
        return self.planner.invoke(prompt, thread_id="mission-plan")

    def _propose_original(
        self,
        *,
        task: str,
        history: StepHistory,
        participants: list[str],
    ) -> tuple[MissionPlan | None, str]:
        feedback = ""
        last_raw = ""
        for attempt in range(MAX_SYNTAX_RETRIES + 1):
            last_raw = self._central_plan(
                task=task,
                history=history,
                participants=participants,
                syntax_feedback=feedback,
            )
            print(f"[planner proposal]\n{last_raw.strip()[:1200]}")
            if TASK_COMPLETE_RE.search(last_raw):
                return [finish_step(participants)], "ok"
            plan, errors = parse_hmas1_plan(
                last_raw,
                participants,
                max_steps=MAX_PLAN_STEPS,
                ensure_finish=True,
            )
            if plan is not None:
                return plan, "ok"
            feedback = (
                "Your PLAN was rejected:\n- "
                + "\n- ".join(errors or ["no valid STEP sequence"])
                + "\nSend a corrected PLAN block."
            )
            print(f"[planner] retry {attempt + 1}: {feedback}")
        return None, "invalid"

    def _classify_plan_vote(
        self,
        reply: str,
        *,
        speaker: str,
        participants: list[str],
        original: MissionPlan,
    ) -> str:
        """Return 'agree', 'pmas', or 'retry'."""
        if looks_disagree(reply):
            print(
                f"[planner] {speaker} voted DISAGREE — original plan discarded, "
                "switching to DMAS",
                flush=True,
            )
            return "pmas"
        if looks_agree(reply) or TASK_COMPLETE_RE.search(reply or ""):
            return "agree"
        replacement, problems = parse_hmas1_plan(
            reply, participants, max_steps=MAX_PLAN_STEPS
        )
        if replacement is not None:
            if restates_original_step(
                replacement,
                current=original[0] if original else {},
                remaining=original,
                original=original,
                participants=participants,
            ):
                return "agree"
            print(
                f"[planner] {speaker} proposed a different plan — "
                "original plan discarded, switching to DMAS"
            )
            return "pmas"
        if replacement is None and problems:
            return "retry"
        return "retry"

    def _vote_original_plan(
        self,
        *,
        task: str,
        history: StepHistory,
        participants: list[str],
        original: MissionPlan,
    ) -> str:
        """One AGREE/DISAGREE each, in parallel. Unanimous AGREE else DMAS."""
        print("\n=== vote on original plan (AGREE / DISAGREE) ===", flush=True)

        def _ask(speaker: str) -> tuple[str, str]:
            prompt = build_plan_vote_prompt(
                task=task,
                env=self.env,
                history=history,
                participants=participants,
                speaker=speaker,
                original=original,
            )
            reply = self._ask_robot(
                speaker,
                prompt,
                thread_id=f"vote-original-{speaker}",
                timeout=TURN_TIMEOUT,
            )
            return speaker, reply

        replies: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(participants))) as pool:
            futures = [pool.submit(_ask, speaker) for speaker in participants]
            for future in as_completed(futures):
                speaker, reply = future.result()
                replies[speaker] = reply
                print(f"[{speaker}] {reply.strip()[:300]}", flush=True)

        agreed: set[str] = set()
        retry_speakers: list[str] = []
        for speaker in participants:
            verdict = self._classify_plan_vote(
                replies.get(speaker, ""),
                speaker=speaker,
                participants=participants,
                original=original,
            )
            if verdict == "pmas":
                return "pmas"
            if verdict == "agree":
                agreed.add(speaker)
            else:
                retry_speakers.append(speaker)

        for speaker in retry_speakers:
            retries = 0
            syntax_feedback = (
                "Reply with only AGREE or DISAGREE. Do not rewrite the plan."
            )
            while retries < MAX_SYNTAX_RETRIES:
                retries += 1
                prompt = build_plan_vote_prompt(
                    task=task,
                    env=self.env,
                    history=history,
                    participants=participants,
                    speaker=speaker,
                    original=original,
                    syntax_feedback=syntax_feedback,
                )
                reply = self._ask_robot(
                    speaker,
                    prompt,
                    thread_id=f"vote-original-{speaker}-retry{retries}",
                    timeout=TURN_TIMEOUT,
                )
                print(f"[{speaker}] retry {retries}: {reply.strip()[:300]}")
                verdict = self._classify_plan_vote(
                    reply,
                    speaker=speaker,
                    participants=participants,
                    original=original,
                )
                if verdict == "pmas":
                    return "pmas"
                if verdict == "agree":
                    agreed.add(speaker)
                    break
            else:
                print(
                    f"[planner] {speaker} did not AGREE — switching to DMAS",
                    flush=True,
                )
                return "pmas"

        if agreed == set(participants):
            print("[planner] every robot AGREEd the original plan")
            return "agree"
        print("[planner] no unanimous AGREE — switching to DMAS")
        return "pmas"

    def _execute(
        self,
        legs: dict[str, str],
        *,
        step_index: int,
        n_steps: int,
    ) -> dict[str, str]:
        """Dispatch one STEP; robots run their legs in parallel with MCP."""
        results: dict[str, str] = {
            peer: "waited, no action"
            for peer, leg in legs.items()
            if looks_idle(leg)
        }
        working = {
            peer: leg for peer, leg in legs.items() if not looks_idle(leg)
        }
        if not working:
            return results

        def _one(peer: str) -> tuple[str, str]:
            message = build_execute_dispatch(
                legs[peer], step_index=step_index, n_steps=n_steps
            )
            reply = self._ask_robot(
                peer,
                message,
                thread_id=f"exec-{step_index}",
                timeout=EXECUTE_TIMEOUT,
            )
            return peer, reply

        with ThreadPoolExecutor(max_workers=max(1, len(working))) as pool:
            futures = [pool.submit(_one, peer) for peer in working]
            for future in as_completed(futures):
                peer, reply = future.result()
                results[peer] = reply
        return results

    def _check_original_step(
        self,
        legs: dict[str, str],
        results: dict[str, str],
        *,
        step_index: int,
        n_steps: int,
    ) -> str | None:
        """Ask each executor if its original-plan STEP succeeded.

        Returns a short reason if anyone reports STEP_FAILED (or an
        unreadable check), else None so the original plan continues.
        Idle/wait legs skip the check.
        """
        working = {
            peer: leg for peer, leg in legs.items() if not looks_idle(leg)
        }
        if not working:
            return None

        print(
            f"[step check] original STEP {step_index}: each executor "
            "reports STEP_OK or STEP_FAILED",
            flush=True,
        )

        def _one(peer: str) -> tuple[str, str, str]:
            message = build_step_check_dispatch(
                leg=working[peer],
                report=results.get(peer) or "",
                step_index=step_index,
                n_steps=n_steps,
            )
            reply = self._ask_robot(
                peer,
                message,
                thread_id=f"step-check-{step_index}",
                timeout=TURN_TIMEOUT,
            )
            return peer, parse_step_check(reply), reply

        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, len(working))) as pool:
            futures = [pool.submit(_one, peer) for peer in working]
            for future in as_completed(futures):
                peer, verdict, reply = future.result()
                preview = " ".join((reply or "").split())[:160]
                print(f"  [{peer}] {verdict}: {preview}", flush=True)
                if verdict != "ok":
                    failed.append(peer)

        if not failed:
            print(
                f"[step check] STEP {step_index} accomplished — continue original plan",
                flush=True,
            )
            return None
        who = ", ".join(sorted(failed))
        print(
            f"[step check] STEP {step_index} not accomplished ({who}) — switching to DMAS",
            flush=True,
        )
        return who

    def _record(
        self,
        transcript: list[dict],
        history: StepHistory,
        *,
        state_before: str,
        legs: dict[str, str],
        results: dict[str, str],
        participants: list[str],
        executed: int,
        mode: str,
    ) -> None:
        outcome_text = "\n".join(
            f"{peer}: {' '.join((results.get(peer) or '').split())[:240]}"
            for peer in participants
        )
        print(f"[result step {executed}]\n{outcome_text}")
        history.add(state_before, mission_text([legs], participants), outcome_text)
        transcript.append(
            {
                "mode": mode,
                "step": executed,
                "plan": mission_text([legs], participants),
                "result": outcome_text,
            }
        )

    def _pmas_ask_turn(
        self,
        speaker: str,
        *,
        round_index: int,
        turn_index: int,
        mission: str,
        state_text: str,
        dialogue: list[dict[str, str]],
        consensus: Consensus,
        history: list[RoundRecord],
        participants: list[str],
    ) -> Turn:
        feedback = ""
        for attempt in range(MAX_SYNTAX_RETRIES + 1):
            inner = build_pmas_turn_prompt(
                speaker=speaker,
                mission=mission,
                participants=participants,
                state_text=state_text,
                history=history,
                dialogue=dialogue,
                consensus=consensus,
                round_index=round_index,
                turn_index=turn_index,
                feedback=feedback,
            )
            if in_talk_window(turn_index):
                prompt = f"{HUDDLE_TURN_PREFIX}\n{inner}"
            else:
                prompt = (
                    "PEER MODE (DMAS). The original central mission plan is gone. "
                    "There is no central planner. "
                    "If [World State Now] already achieves the mission, answer "
                    "FINISHED — do not propose another drive.\n\n"
                    + inner
                )
            print(
                f"  asking {speaker} (DMAS r{round_index} t{turn_index}"
                f"{'' if attempt == 0 else f' retry {attempt}'})...",
                flush=True,
            )
            reply = self._ask_robot(
                speaker,
                prompt,
                thread_id=f"pmas-r{round_index}-t{turn_index}-{attempt}",
                timeout=TURN_TIMEOUT,
            )
            preview = " ".join((reply or "").split())[:160]
            print(f"  [{speaker}] {preview}", flush=True)
            turn = parse_turn(reply, participants)
            last_attempt = attempt >= MAX_SYNTAX_RETRIES
            if turn.kind == INVALID:
                feedback = (
                    "Your reply was empty. Argue in plain language, or end "
                    "with PLAN / AGREE / FINISHED."
                )
            elif in_talk_window(turn_index) and turn.kind in (PLAN, AGREE):
                feedback = talk_window_feedback(turn_index)
                if last_attempt:
                    return coerce_talk_window(turn, turn_index)
            elif turn.kind == PLAN:
                problems = verify_plan(turn.legs, participants, env=self.env)
                if not problems:
                    return turn
                feedback = "Rejected because " + "; ".join(problems)
            else:
                return turn
            print(f"  [{speaker}] DMAS retry {attempt + 1}: {feedback}")
        return Turn(kind=INVALID, raw="")

    def _pmas_run(
        self,
        *,
        task: str,
        participants: list[str],
        history: StepHistory,
        transcript: list[dict],
        executed: int,
        why: str,
    ) -> dict:
        mission = (
            task.strip()
            + "\n\n[Mode] "
            + why
            + " Plan as a peer fleet.\n"
            "[Important] The human task above includes STARTING positions, "
            "not current ones and not targets by themselves. If the goal is "
            "already true in [World State Now], every robot must answer "
            "FINISHED. Do not undo completed work. The mission ends when "
            "every robot answers FINISHED in the same discussion round."
        )
        pmas_history: list[RoundRecord] = [
            RoundRecord(
                index=int(item.get("step") or i + 1),
                legs={"prior": str(item.get("plan") or "")},
                results={"fleet": str(item.get("result") or "")},
                state_changed=True,
            )
            for i, item in enumerate(transcript)
        ]
        print(f"\n=== DMAS fallback ({why}) ===", flush=True)

        while executed < MAX_PLAN_STEPS:
            self.env.refresh()
            state_before = self.env.state_text()
            round_index = executed + 1
            print(f"\n--- DMAS round {round_index} ---", flush=True)
            consensus = Consensus(participants)
            dialogue: list[dict[str, str]] = []
            count = len(participants)
            offset = executed % count
            order = participants[offset:] + participants[:offset]
            outcome = CONTINUE

            for turn_index in range(1, MAX_TURNS_PER_ROUND + 1):
                if turn_index == MIN_TALK_TURNS + 1:
                    print("  ----- now planning -----", flush=True)
                speaker = order[(turn_index - 1) % count]
                turn = self._pmas_ask_turn(
                    speaker,
                    round_index=round_index,
                    turn_index=turn_index,
                    mission=mission,
                    state_text=state_before,
                    dialogue=dialogue,
                    consensus=consensus,
                    history=pmas_history,
                    participants=participants,
                )
                if turn.kind == INVALID:
                    print(f"  turn {turn_index}: {speaker} skipped")
                    dialogue.append(
                        {
                            "speaker": speaker,
                            "kind": "skipped",
                            "text": "(no usable answer)",
                        }
                    )
                    continue
                dialogue.append(
                    {"speaker": speaker, "kind": turn.kind, "text": turn.raw.strip()}
                )
                outcome = consensus.apply(speaker, turn)
                if turn.kind == TALK:
                    preview = " ".join((turn.raw or "").split())[:160]
                    print(f"  turn {turn_index}: {speaker}: {preview}")
                else:
                    print(f"  turn {turn_index}: {speaker} {turn.summary()}")
                if outcome != CONTINUE:
                    break
            else:
                if consensus.proposal:
                    print(
                        f"  no full agreement after {MAX_TURNS_PER_ROUND} turns — "
                        f"executing last plan (from {consensus.proposer})"
                    )
                    outcome = EXECUTE
                else:
                    return {
                        "status": "failed_no_consensus",
                        "reason": f"DMAS produced no plan in round {round_index}",
                        "steps_taken": executed,
                        "transcript": transcript,
                    }

            if outcome == DONE:
                return {
                    "status": "success",
                    "reason": (
                        "every robot reported FINISHED in DMAS after "
                        f"{executed} executed step(s)"
                    ),
                    "steps_taken": executed,
                    "transcript": transcript,
                }
            if outcome != EXECUTE or not consensus.proposal:
                return {
                    "status": "failed_no_consensus",
                    "reason": f"DMAS produced no plan in round {round_index}",
                    "steps_taken": executed,
                    "transcript": transcript,
                }

            executed += 1
            print(f"[EXECUTE DMAS step {executed}]")
            results = self._execute(
                consensus.proposal, step_index=executed, n_steps=executed
            )
            self._record(
                transcript,
                history,
                state_before=state_before,
                legs=consensus.proposal,
                results=results,
                participants=participants,
                executed=executed,
                mode="pmas",
            )
            self.env.refresh()
            pmas_history.append(
                RoundRecord(
                    index=round_index,
                    legs=dict(consensus.proposal),
                    results=results,
                    state_changed=self.env.state_text() != state_before,
                )
            )

        return {
            "status": "failed_step_limit",
            "reason": f"reached the planning-step limit ({MAX_PLAN_STEPS})",
            "steps_taken": executed,
            "transcript": transcript,
        }

    def run(self, task: str, participants: list[str]) -> dict:
        history = StepHistory()
        self.env.participants = list(participants)
        transcript: list[dict] = []
        executed = 0

        self.env.refresh()
        if self.env.last_error:
            return {
                "status": "environment_unavailable",
                "detail": self.env.last_error,
                "steps_taken": 0,
            }

        print(f"\n=== original mission plan ===\n{self.env.state_text()}")
        original, _status = self._propose_original(
            task=task, history=history, participants=participants
        )
        if original is None:
            print("[planner] no usable original plan — starting as DMAS")
            return self._pmas_run(
                task=task,
                participants=participants,
                history=history,
                transcript=transcript,
                executed=executed,
                why="the central planner did not produce a valid mission plan.",
            )

        n_steps = len(original)
        print(f"[ORIGINAL PLAN]\n{mission_text(original, participants)}")

        vote = self._vote_original_plan(
            task=task,
            history=history,
            participants=participants,
            original=original,
        )
        if vote == "pmas":
            return self._pmas_run(
                task=task,
                participants=participants,
                history=history,
                transcript=transcript,
                executed=executed,
                why="a robot rejected the original mission plan (DISAGREE or a different PLAN).",
            )

        for offset, legs in enumerate(original):
            step_index = offset + 1
            if is_finish_step(legs):
                print(
                    f"[FINISH] original plan STEP {step_index} is FINISHED — "
                    "mission complete",
                    flush=True,
                )
                return {
                    "status": "success",
                    "reason": (
                        "robots AGREEd the original plan; FINISH STEP reached "
                        f"after {executed} executed step(s)"
                    ),
                    "steps_taken": executed,
                    "transcript": transcript,
                }

            if executed >= MAX_PLAN_STEPS:
                break
            executed += 1
            state_before = self.env.state_text()
            print(f"\n[EXECUTE original STEP {step_index}/{n_steps}]")
            results = self._execute(legs, step_index=step_index, n_steps=n_steps)
            self._record(
                transcript,
                history,
                state_before=state_before,
                legs=legs,
                results=results,
                participants=participants,
                executed=executed,
                mode="original",
            )
            self.env.refresh()
            failed = self._check_original_step(
                legs, results, step_index=step_index, n_steps=n_steps
            )
            if failed:
                return self._pmas_run(
                    task=task,
                    participants=participants,
                    history=history,
                    transcript=transcript,
                    executed=executed,
                    why=(
                        f"after original STEP {step_index}, {failed} reported "
                        "STEP_FAILED (the assigned leg was not accomplished). "
                        "The original plan is dropped; continue as a peer fleet."
                    ),
                )

        return self._pmas_run(
            task=task,
            participants=participants,
            history=history,
            transcript=transcript,
            executed=executed,
            why=(
                "the original plan hit the executed-STEP limit before FINISH. "
                "Say FINISHED if the mission is done, otherwise plan the rest."
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "HMAS-1 planner: one full mission plan ending with FINISHED, "
            "one AGREE/DISAGREE vote on that plan, per-STEP executor check, "
            "DMAS peer fallback if anyone rejects a STEP or the plan."
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
        f"Limits: {MAX_PLAN_STEPS} work STEPs plus a final FINISHED STEP, "
        f"{MAX_SYNTAX_RETRIES} syntax retries. One AGREE/DISAGREE vote on "
        "the original plan; unanimous AGREE executes it. After each work "
        "STEP every executor reports STEP_OK or STEP_FAILED; STEP_FAILED "
        "drops the original plan and continues as DMAS. A DISAGREE or "
        "new PLAN on the original vote also switches to DMAS."
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
            with mission_timeout_guard("hmas1_until_done"):
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
