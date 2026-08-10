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
    """A robot agent that coordinates through a shared, turn-based
    broadcast log.

    There is still no addressing and everyone sees every message, but the
    pool server enforces a fixed speaking order (see ``config.POOL_TURN_ORDER``):
    this agent only ever contributes when the pool tells it it is its turn.
    If any agent posts a message containing the word ``DONE``, the round
    ends immediately until the user starts a new one.
    """

    def __init__(self, robot_id: str, *, pool: PoolClient):
        if robot_id not in TB_IDS:
            raise ValueError(f"Unknown robot {robot_id!r}; allowed: {list(TB_IDS)}")
        self.robot_id = robot_id
        self.nav_id = nav_id_for_tb(robot_id)
        self.pool = pool
        self.posted_this_turn = False
        name = robot_peer_name(robot_id)
        order_desc = " -> ".join(POOL_TURN_ORDER)

        super().__init__(
            AgentSpec(
                name=name,
                description=f"Turn-based pool agent for {name}. No master, no direct addressing.",
                system_prompt=(
                    f"You are {name} in the SHARED POOL architecture.\n"
                    "\n"
                    "WHAT YOU CAN DO:\n"
                    f"- On your turn only: use MCP tools if needed (list_worlds/get_map_info, "
                    f"rank_stations_by_distance(robot_id='{self.nav_id}'), stations/boxes, get_robot_pose, "
                    f"distance_to_station, get_laser_snapshot, get_peer_distances, "
                    f"drive_distance(robot_id='{self.nav_id}', distance_m, direction_deg), "
                    f"navigate_to_pose(robot_id='{self.nav_id}', x, y), "
                    "events, whiteboard), "
                    "then post_to_pool EXACTLY ONCE to SEND your message into the shared pool "
                    "(everyone reads the same log).\n"
                    "- Prefer few tools: rank stations once → navigate. "
                    "Only after nav fails: get_peer_distances and/or drive_distance, then retry. "
                    "Do not re-sense poses repeatedly.\n"
                    f"- Speaking order: {order_desc} (then repeats). A user message restarts at the front.\n"
                    "- read_pool if you need more history than you were shown.\n"
                    "\n"
                    "HOW TO END THE CONVERSATION:\n"
                    "- When the user goal is finished (or there is nothing useful left to do), your pool "
                    "message MUST include the uppercase token DONE (e.g. end with a line that says DONE).\n"
                    "- As soon as ANY agent posts DONE, the round stops for everyone. Do not keep chatting "
                    "after the task is done.\n"
                    "- Do NOT write DONE while work is still in progress. Lowercase 'done' does not count.\n"
                    "\n"
                    "WHAT YOU CANNOT DO:\n"
                    "- You cannot address one robot privately; there is no ask_peer. Only the shared pool.\n"
                    "- You cannot post when it is not your turn.\n"
                    "- Never call post_to_pool more than once per turn.\n"
                    "\n"
                    "WORDING: say you SEND / post a message to the pool. Do not say broadcast.\n"
                    "FLEET: Other robots share this map. Navigate first; on failure use "
                    "get_peer_distances / drive_distance to clear peers, then retry.\n"
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

            Args:
                message: Your contribution. If the task is finished, include the uppercase
                    token DONE (e.g. end with a line that says DONE) so the conversation stops.
            """
            self.pool.post(message, thread_id="pool")
            self.posted_this_turn = True
            if message_says_done(message):
                return "posted (DONE — round will end)"
            return "posted"

        @tool
        def read_pool(limit: int = 20) -> str:
            """Read the last `limit` messages from the shared pool (oldest first),
            in case you need more backlog than what you were shown for this turn."""
            return json.dumps(self.pool.recent(limit), ensure_ascii=False, indent=2)

        return [*local_tools, post_to_pool, read_pool]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one robot agent that coordinates via the turn-based shared message pool (blackboard architecture)."
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
        marker = " [DONE]" if msg.get("done") else ""
        print(f"\n[pool #{msg.get('seq')}] {msg.get('from')}: {msg.get('text')}{marker}")

    def handle_turn(name: str | None) -> None:
        if name != my_name:
            return
        transcript = "\n".join(f"{m['from']}: {m['text']}" for m in pool.history) or "(pool is empty so far)"
        prompt = (
            "It is now YOUR turn in the shared pool discussion. Conversation so far (oldest first):\n\n"
            f"{transcript}\n\n"
            "Take your turn now: act with your local tools if useful, then call post_to_pool exactly once. "
            "Keep your post as short but precise as possible. "
            "If the user goal is already finished and nothing useful remains, your post MUST include the "
            "uppercase token DONE so the conversation stops."
        )
        robot.posted_this_turn = False
        try:
            reply = robot.invoke(prompt, thread_id="pool")
        except Exception as e:
            reply = f"{my_name} failed to take its turn: {type(e).__name__}: {e}"
        print(f"[{my_name} internal] {reply}")
        if not robot.posted_this_turn:
            # Safety net: never let the round stall just because the model
            # forgot to call the tool -- post its final answer on its behalf.
            print(f"[{my_name}] did not call post_to_pool; posting its reply automatically.")
            pool.post(reply, thread_id="pool")

    def handle_round_end(envelope: dict) -> None:
        who = envelope.get("from", "?")
        print(
            f"\n=== round ended: {envelope.get('reason', 'unknown')} "
            f"(by {who}) — waiting for a new user message ==="
        )

    def handle_error(text: str) -> None:
        print(f"\n[pool rejected our post] {text}")

    pool.on_message(handle_message)
    pool.on_turn(handle_turn)
    pool.on_round_end(handle_round_end)
    pool.on_error(handle_error)

    print(f"{my_name} is online, watching the shared pool at {args.host}:{args.port}.")
    print(f"Turn order: {' -> '.join(POOL_TURN_ORDER)}")
    print("Round ends when any agent posts DONE. This agent only speaks on its turn. Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
