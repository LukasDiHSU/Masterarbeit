from __future__ import annotations

import argparse
import json
import threading
from functools import cached_property

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    DEFAULT_POOL_HOST,
    DEFAULT_POOL_PORT,
    POOL_TURN_ORDER,
    TB_IDS,
    nav_id_for_tb,
    robot_peer_name,
)
from ...mcp_client import load_mcp_tools_safe
from .message_pool import PoolClient, message_says_done


class PoolAgent(BaseAgent):
    """Robot agent on the shared pool.

    Discussion phase: turn-based posts until every agent AGREEs → server starts
    execute and wakes ALL agents at once so they can work in parallel.
    """

    def __init__(self, robot_id: str, *, pool: PoolClient):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.pool = pool
        self.posted_this_turn = False
        self._execute_lock = threading.Lock()
        self._executing = False
        name = robot_peer_name(robot_id)
        order_desc = " -> ".join(POOL_TURN_ORDER)

        super().__init__(
            AgentSpec(
                name=name,
                description=f"Pool agent for {name}. Discuss→AGREE→parallel execute.",
                system_prompt=(
                    f"You are {name} in the SHARED POOL architecture.\n"
                    "\n"
                    "PHASES (server-enforced):\n"
                    "1) DISCUSS — turn-based talk in the pool until EVERY turn-taker's "
                    "latest post starts with AGREE. Do NOT navigate/pickup/drop here. "
                    "Reply AGREE: … or DISAGREE: … (with a short plan refinement). "
                    "In AGREE, state YOUR concrete role (which box/station).\n"
                    "2) EXECUTE — after unanimous AGREE the server wakes ALL agents "
                    "together. Work YOUR agreed role with MCP in parallel with peers "
                    "(do not wait for others' turns). Then post_to_pool once with status.\n"
                    "3) When the whole user goal is finished, include uppercase DONE in "
                    "your post so the round ends.\n"
                    "4) If you need another planning discussion, call "
                    "start_discussion_round(reason=...) (reopens discuss→AGREE→execute).\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    f"- Discuss (your turn only): AGREE/DISAGREE via post_to_pool once.\n"
                    f"- Execute (all at once): MCP — "
                    f"rank_stations_by_distance(robot_id='{self.nav_id}'), "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    f"drive_distance(robot_id='{self.nav_id}', distance_m, direction_deg), "
                    "stations/boxes, get_peer_distances, whiteboard, … — then "
                    "post_to_pool EXACTLY ONCE.\n"
                    f"- Discuss speaking order: {order_desc} (then repeats). "
                    "A user message restarts discuss.\n"
                    "- read_pool if you need more history.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- No private addressing / ask_peer — only the shared pool.\n"
                    "- Do not post when it is not your discuss turn.\n"
                    "- Never call post_to_pool more than once per discuss/execute slot.\n"
                    "- Do not use DONE during discuss to skip agreement — DONE only ends "
                    "the round in execute phase.\n"
                    "\n"
                    "WORDING: say you SEND / post a message to the pool. Do not say broadcast.\n"
                    "FLEET: Other robots share this map and may navigate at the same time. "
                    "In execute: navigate first; on failure use get_peer_distances / "
                    "drive_distance, then retry.\n"
                    "TOOLS: If the same tool with the same arguments fails twice, do not call "
                    "it a third time — change the goal/approach or report failure.\n"
                    "STYLE: keep every message as short but precise as possible. Never hallucinate values."
                ),
            ),
            architecture="shared_pool",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        local_tools = list(self._mcp_tools_by_name.values())

        @tool
        def post_to_pool(message: str) -> str:
            """SEND your contribution for this turn to the shared pool (everyone will see it).

            Discuss phase: start with AGREE: or DISAGREE:.
            Execute phase: status update; include DONE when the whole mission is finished.
            """
            self.pool.post(message, thread_id="pool")
            self.posted_this_turn = True
            if message_says_done(message):
                return "posted (DONE — ends round only in execute phase)"
            return "posted"

        @tool
        def read_pool(limit: int = 20) -> str:
            """Read the last `limit` messages from the shared pool (oldest first)."""
            return json.dumps(self.pool.recent(limit), ensure_ascii=False, indent=2)

        @tool
        def start_discussion_round(reason: str) -> str:
            """Reopen a pool discussion round (until all AGREE again), then execute.

            Use when execute is blocked and the fleet needs to replan together.

            Args:
                reason: Why a new discussion is needed.
            """
            self.pool.start_discussion((reason or "").strip() or "replan needed")
            return "discussion_round_requested"

        return [*local_tools, post_to_pool, read_pool, start_discussion_round]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one robot agent on the shared pool (discuss→AGREE→parallel execute)."
    )
    parser.add_argument(
        "--robot-id",
        "--tb-id",
        dest="robot_id",
        choices=list(TB_IDS),
        required=True,
    )
    parser.add_argument("--host", default=DEFAULT_POOL_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_POOL_PORT)
    args = parser.parse_args()

    my_name = robot_peer_name(args.robot_id)
    pool = PoolClient(my_name, host=args.host, port=args.port)
    pool.wait_for_history(timeout=5.0)
    robot = PoolAgent(args.robot_id, pool=pool)

    def handle_message(msg: dict) -> None:
        markers = []
        if msg.get("agree"):
            markers.append("AGREE")
        if msg.get("done"):
            markers.append("DONE")
        marker = f" [{', '.join(markers)}]" if markers else ""
        print(f"\n[pool #{msg.get('seq')}] {msg.get('from')}: {msg.get('text')}{marker}")

    def handle_phase(envelope: dict) -> None:
        print(
            f"\n=== phase → {envelope.get('phase')} "
            f"({envelope.get('reason', '')}) ==="
        )

    def _run_with_prompt(phase: str, prompt: str) -> None:
        robot.posted_this_turn = False
        try:
            reply = robot.invoke(prompt, thread_id="pool")
        except Exception as e:
            reply = f"{my_name} failed ({phase}): {type(e).__name__}: {e}"
        print(f"[{my_name} internal] {reply}")
        if not robot.posted_this_turn:
            print(f"[{my_name}] did not call post_to_pool; posting automatically.")
            pool.post(reply, thread_id="pool")

    def handle_turn(name: str | None) -> None:
        # Discuss only — execute is started via on_execute for all agents.
        if name != my_name:
            return
        if pool.phase != "discuss":
            return
        transcript = (
            "\n".join(f"{m['from']}: {m['text']}" for m in pool.history)
            or "(pool is empty so far)"
        )
        prompt = (
            "It is now YOUR turn in the shared pool DISCUSS phase. "
            "Conversation so far (oldest first):\n\n"
            f"{transcript}\n\n"
            "Discuss / refine the plan. Do NOT navigate or move boxes yet. "
            "Call post_to_pool exactly once. "
            "If you accept the current plan (incl. peers' posts), start with AGREE: "
            "and summarize YOUR role. If not, start with DISAGREE: and propose a change. "
            "Discussion ends only when every agent's latest post AGREEs — then everyone "
            "executes their role in parallel."
        )

        def _job() -> None:
            _run_with_prompt("discuss", prompt)

        threading.Thread(target=_job, daemon=True, name=f"{my_name}-discuss").start()

    def handle_execute(_envelope: dict) -> None:
        with robot._execute_lock:
            if robot._executing:
                return
            robot._executing = True

        def _job() -> None:
            try:
                transcript = (
                    "\n".join(f"{m['from']}: {m['text']}" for m in pool.history)
                    or "(pool is empty so far)"
                )
                prompt = (
                    "EXECUTE phase — the fleet AGREEd. The agreed plan is in the pool "
                    "(oldest first):\n\n"
                    f"{transcript}\n\n"
                    "ALL agents received this at the same time — work YOUR agreed role "
                    "NOW with MCP tools in parallel with peers (do not wait for turns). "
                    "When your part (or the whole goal) is done, call post_to_pool exactly "
                    "once with a short status. If the whole user goal is finished, include "
                    "uppercase DONE. If the fleet needs to replan, call "
                    "start_discussion_round(reason=...) instead of posting DONE."
                )
                _run_with_prompt("execute", prompt)
            finally:
                with robot._execute_lock:
                    robot._executing = False

        threading.Thread(target=_job, daemon=True, name=f"{my_name}-execute").start()

    def handle_round_end(envelope: dict) -> None:
        who = envelope.get("from", "?")
        print(
            f"\n=== round ended: {envelope.get('reason', 'unknown')} "
            f"(by {who}) — waiting for a new user message or start_discussion_round ==="
        )

    def handle_error(text: str) -> None:
        print(f"\n[pool rejected our post] {text}")

    pool.on_message(handle_message)
    pool.on_turn(handle_turn)
    pool.on_execute(handle_execute)
    pool.on_phase(handle_phase)
    pool.on_round_end(handle_round_end)
    pool.on_error(handle_error)

    print(f"{my_name} is online, watching the shared pool at {args.host}:{args.port}.")
    print(f"Discuss turn order: {' -> '.join(POOL_TURN_ORDER)}")
    print(
        "Discuss until all AGREE → ALL execute in parallel → DONE ends round; "
        "start_discussion_round replans."
    )
    print("This agent speaks on its discuss turn; execute wakes everyone. Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
