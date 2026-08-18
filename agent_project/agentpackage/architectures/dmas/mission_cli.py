"""Human console and round driver for DMAS.

The console owns no opinion: it fetches one world snapshot per round so every
robot argues from the same state, passes the turn along a rotating order,
checks proposals against the rules, and dispatches the agreed legs. All content
— the plan, the objections, the decision to finish — comes from the robots.
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_CLI_PORT,
    DEFAULT_MESH_HOST,
    DMAS_TURN_ORDER,
    MESH_CLI_NAME,
    TB_IDS,
    build_peer_table,
    robot_peer_name,
)
from ...paper_protocol import Environment
from ...timing import format_elapsed, record_timing
from ..conflict_based.mesh_bus import MeshNode
from .protocol import (
    ARCHITECTURE,
    CONTINUE,
    DONE,
    EXECUTE,
    EXECUTE_TIMEOUT,
    INVALID,
    MAX_ROUNDS,
    MAX_SYNTAX_RETRIES,
    MAX_TURNS_PER_ROUND,
    PLAN,
    TURN_TIMEOUT,
    Consensus,
    RoundRecord,
    Turn,
    build_turn_prompt,
    execute_message,
    looks_idle,
    parse_turn,
    turn_message,
    verify_plan,
)


class Session:
    """One mission, run round by round until the fleet finishes or gives up."""

    def __init__(self, mesh: MeshNode, participants: list[str]):
        self.mesh = mesh
        self.participants = list(participants)
        self.env = Environment(self.participants)
        self.history: list[RoundRecord] = []

    # --- one turn -------------------------------------------------------
    def _ask_turn(
        self,
        speaker: str,
        *,
        round_index: int,
        turn_index: int,
        mission: str,
        state_text: str,
        dialogue: list[dict[str, str]],
        consensus: Consensus,
    ) -> Turn:
        feedback = ""
        for attempt in range(MAX_SYNTAX_RETRIES + 1):
            prompt = build_turn_prompt(
                speaker=speaker,
                mission=mission,
                participants=self.participants,
                state_text=state_text,
                history=self.history,
                dialogue=dialogue,
                consensus=consensus,
                round_index=round_index,
                feedback=feedback,
            )
            try:
                reply = self.mesh.ask(
                    speaker,
                    turn_message(
                        round_index=round_index, turn_index=turn_index, prompt=prompt
                    ),
                    thread_id=f"round-{round_index}",
                    timeout=TURN_TIMEOUT,
                )
            except Exception as e:
                print(f"  [{speaker}] unreachable: {type(e).__name__}: {e}")
                return Turn(kind=INVALID, raw="")

            turn = parse_turn(reply, self.participants)
            if turn.kind == INVALID:
                feedback = (
                    "No PLAN / AGREE / FINISHED block was found in your answer. "
                    "Answer with one of the three blocks and nothing else."
                )
            elif turn.kind == PLAN:
                problems = verify_plan(turn.legs, self.participants, env=self.env)
                if not problems:
                    return turn
                feedback = "Rejected because " + "; ".join(problems)
            else:
                return turn
            print(f"  [{speaker}] retry {attempt + 1}: {feedback}")
        return Turn(kind=INVALID, raw="")

    # --- one round ------------------------------------------------------
    def _discuss(
        self, *, round_index: int, mission: str, state_text: str
    ) -> tuple[str, Consensus]:
        consensus = Consensus(self.participants)
        dialogue: list[dict[str, str]] = []
        count = len(self.participants)
        # Rotate who opens the round so the same robot does not always propose.
        offset = (round_index - 1) % count
        order = self.participants[offset:] + self.participants[:offset]

        for turn_index in range(1, MAX_TURNS_PER_ROUND + 1):
            speaker = order[(turn_index - 1) % count]
            turn = self._ask_turn(
                speaker,
                round_index=round_index,
                turn_index=turn_index,
                mission=mission,
                state_text=state_text,
                dialogue=dialogue,
                consensus=consensus,
            )
            if turn.kind == INVALID:
                print(f"  turn {turn_index}: {speaker} gave no usable answer — skipped")
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
            outcome = self._apply(consensus, speaker, turn, turn_index)
            if outcome != CONTINUE:
                return outcome, consensus
        if consensus.proposal:
            print(
                f"  no full agreement after {MAX_TURNS_PER_ROUND} turns — "
                f"executing last plan (from {consensus.proposer})"
            )
            return EXECUTE, consensus
        print(f"  no plan proposed after {MAX_TURNS_PER_ROUND} turns")
        return CONTINUE, consensus

    def _apply(
        self, consensus: Consensus, speaker: str, turn: Turn, turn_index: int
    ) -> str:
        outcome = consensus.apply(speaker, turn)
        detail = ""
        if turn.kind == PLAN:
            detail = " -> " + "; ".join(
                f"{robot.split('_')[-1]}: {leg}" for robot, leg in turn.legs.items()
            )
        elif outcome == CONTINUE and consensus.proposal:
            detail = f" ({len(consensus.agreed)}/{len(self.participants)} back the plan)"
        print(f"  turn {turn_index}: {speaker} {turn.summary()}{detail}")
        return outcome

    # --- execution ------------------------------------------------------
    def _execute(self, legs: dict[str, str], round_index: int) -> dict[str, str]:
        working = {r: leg for r, leg in legs.items() if not looks_idle(leg)}
        results = {
            r: "waited, no action" for r in legs if r not in working
        }
        if not working:
            return results

        print(f"\n  executing round {round_index}: {', '.join(working)}")

        def run(robot: str, leg: str) -> str:
            try:
                return self.mesh.ask(
                    robot,
                    execute_message(round_index=round_index, leg=leg),
                    thread_id=f"exec-{round_index}",
                    timeout=EXECUTE_TIMEOUT,
                )
            except Exception as e:
                return f"execution failed: {type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=len(working)) as pool:
            futures = {pool.submit(run, r, leg): r for r, leg in working.items()}
            for future, robot in futures.items():
                results[robot] = future.result()
        for robot in legs:
            print(f"  [{robot}] {results.get(robot, '')}")
        return {r: results.get(r, "") for r in legs}

    # --- mission --------------------------------------------------------
    def run(self, mission: str) -> tuple[bool, str]:
        for round_index in range(1, MAX_ROUNDS + 1):
            self.env.refresh()
            state_before = self.env.state_text()
            print(f"\n--- round {round_index} ---")

            outcome, consensus = self._discuss(
                round_index=round_index, mission=mission, state_text=state_before
            )
            if outcome == DONE:
                return True, f"every robot reported FINISHED after {round_index - 1} executed round(s)"
            if outcome != EXECUTE:
                return False, f"no plan proposed in round {round_index}"

            backing = len(consensus.agreed)
            fleet = len(self.participants)
            if backing == fleet:
                print(f"  agreed (proposed by {consensus.proposer})")
            else:
                print(
                    f"  executing last plan from {consensus.proposer} "
                    f"({backing}/{fleet} had agreed)"
                )
            legs = dict(consensus.proposal)
            results = self._execute(legs, round_index)

            self.env.refresh()
            self.history.append(
                RoundRecord(
                    index=round_index,
                    legs=legs,
                    results=results,
                    state_changed=self.env.state_text() != state_before,
                )
            )
            if not self.history[-1].state_changed:
                print("  ! the world did not change in this round")
        return False, f"round limit reached ({MAX_ROUNDS})"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Mission console for DMAS. The fleet agrees on a plan, executes one "
            "short leg each, and meets again until every robot says FINISHED."
        )
    )
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    parser.add_argument("--cli-port", type=int, default=DEFAULT_MESH_CLI_PORT)
    parser.add_argument("--name", default=MESH_CLI_NAME)
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    participants = [robot_peer_name(rid) for rid in DMAS_TURN_ORDER]
    mesh = MeshNode(
        args.name, args.host, args.cli_port, peer_table, architecture=ARCHITECTURE
    )

    print(f"Mission console online as {args.name!r} at {args.host}:{args.cli_port}.")
    print(f"Fleet: {', '.join(participants)}")
    print(
        f"Protocol: discuss until all agree (or after {MAX_TURNS_PER_ROUND} turns "
        f"the last plan is executed) -> one short leg each -> discuss again "
        f"(at most {MAX_ROUNDS} rounds). Mission ends when every robot says FINISHED."
    )
    print("Paste a mission prompt and press Enter. Type quit / exit to leave.\n")

    try:
        while True:
            line = input("mission> ").strip()
            if not line:
                continue
            if line.lower() in {"quit", "exit", "q", "/quit", "/exit"}:
                break

            for peer in participants:
                mesh._wait_for_link(peer, timeout=60.0)

            print("... fleet discussion started (timer running) ...", flush=True)
            t0 = time.perf_counter()
            success, detail = Session(mesh, participants).run(line)
            elapsed = time.perf_counter() - t0

            print(f"\n*** {'MISSION DONE' if success else 'MISSION FAILED'}: {detail} ***")
            print(
                f"*** TIME  until the fleet finished: {format_elapsed(elapsed)} "
                f"({elapsed:.1f}s) ***\n",
                flush=True,
            )
            record_timing(
                "dmas_until_done",
                elapsed,
                extra=f"result={'success' if success else 'failed'} {detail}",
            )
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
