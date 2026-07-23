
from __future__ import annotations

import argparse
import json
import threading
from functools import cached_property
from typing import Literal

from langchain.tools import tool

from ...BaseAgents import AgentSpec, BaseAgent
from ...config import (
    DEFAULT_POOL_HOST,
    DEFAULT_POOL_PORT,
    POOL_TURN_ORDER,
    TB_TO_ROBOT_ID,
    robot_peer_name,
)
from ...mcp_client import load_mcp_tools_safe
from .message_pool import PoolClient

TB_ID = Literal["tb1", "tb2", "tb3", "tb4"]


class PoolAgent(BaseAgent):
    """A robot agent that coordinates through a shared, turn-based
    broadcast log.

    There is still no addressing and everyone sees every message, but the
    pool server enforces a fixed speaking order (see ``config.POOL_TURN_ORDER``):
    this agent only ever contributes when the pool tells it it is its turn.
    If every agent in the order votes ``agree_to_end=True`` in a row, the
    round ends and nobody speaks again until the user starts a new one.
    """

    def __init__(self, tb_id: TB_ID, *, pool: PoolClient):
        self.tb_id = tb_id
        self.pool = pool
        self.posted_this_turn = False
        rid = TB_TO_ROBOT_ID[tb_id]
        order_desc = " -> ".join(POOL_TURN_ORDER)

        super().__init__(
            AgentSpec(
                name=robot_peer_name(tb_id),
                description=f"Turn-based pool agent for robot {tb_id}. No master, no direct addressing.",
                system_prompt=(
                    f"You are the agent for robot {tb_id} (fleet id {rid}) in a DECENTRALIZED fleet that "
                    "coordinates through one shared message pool: every agent and the human user read and "
                    f"write the exact same broadcast log. Speaking happens in a fixed order: {order_desc}, "
                    "then back to the front. You will only be asked to contribute when it is YOUR turn -- "
                    "you will be shown the conversation so far when that happens. "
                    "When it is your turn: decide whether the discussion needs anything from you. If so, use "
                    "your local tools first if needed (get_my_amcl_pose, move_me_to, whiteboard, items), then "
                    "call post_to_pool EXACTLY ONCE with a short, useful contribution -- posting is the only "
                    "way anyone else finds out what you did or think. If you believe the task/discussion is "
                    "already resolved and everyone should stop, still call post_to_pool once (e.g. say why you "
                    "agree it's done) but set agree_to_end=True. Only set agree_to_end=True if you genuinely "
                    "think the whole group should stop -- if every single agent in the order does this in a "
                    "row, the round ends automatically. Never call post_to_pool more than once per turn. Do "
                    "not check battery status unless explicitly asked. Never hallucinate values: if data is "
                    "unavailable, say so clearly."
                ),
            ),
            architecture="shared_pool",
        )

    @cached_property
    def _mcp_tools_by_name(self) -> dict:
        return {t.name: t for t in load_mcp_tools_safe()}

    def _retrieve_tools(self):
        base = list(self._mcp_tools_by_name.values())
        by_name = {t.name: t for t in base}
        hidden = frozenset({"get_robot_amcl_pose", "move_robot"})
        shared = [t for t in base if t.name not in hidden]
        tb = self.tb_id
        rid = TB_TO_ROBOT_ID[tb]

        local_tools: list = []
        if by_name:
            @tool
            async def get_my_amcl_pose() -> str:
                """Get this robot's AMCL pose (ROS `ros2 topic echo /<tb>/amcl_pose --once`)."""
                t = by_name.get("get_robot_amcl_pose")
                if t is None:
                    return ""
                return str(await t.ainvoke({"robot": tb}))

            @tool
            async def move_me_to(x: float, y: float, z: float) -> str:
                """Send a Nav2 navigate_to_pose goal for this robot to map position (x, y, z)."""
                t = by_name.get("move_robot")
                if t is None:
                    return ""
                return str(await t.ainvoke({"robot_id": rid, "x": x, "y": y, "z": z}))

            local_tools = [get_my_amcl_pose, move_me_to, *shared]

        @tool
        def post_to_pool(message: str, agree_to_end: bool = False) -> str:
            """Broadcast your contribution for this turn to the shared pool.
            Every other agent and the human user will see it immediately.

            Args:
                message: Your contribution (or, if voting to end, your reasoning why).
                agree_to_end: Set True if you think the whole group should stop now.
                    If every agent votes True in a row, the round ends for everyone.
            """
            self.pool.post(message, thread_id="pool", end_vote=agree_to_end)
            self.posted_this_turn = True
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
    parser.add_argument("--tb-id", choices=["tb1", "tb2", "tb3", "tb4"], required=True)
    parser.add_argument("--host", default=DEFAULT_POOL_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_POOL_PORT)
    args = parser.parse_args()

    my_name = robot_peer_name(args.tb_id)
    pool = PoolClient(my_name, host=args.host, port=args.port)
    pool.wait_for_history(timeout=5.0)
    robot = PoolAgent(args.tb_id, pool=pool)

    def handle_message(msg: dict) -> None:
        marker = " [END VOTE]" if msg.get("end_vote") else ""
        print(f"\n[pool #{msg.get('seq')}] {msg.get('from')}: {msg.get('text')}{marker}")

    def handle_turn(name: str | None) -> None:
        if name != my_name:
            return
        transcript = "\n".join(f"{m['from']}: {m['text']}" for m in pool.history) or "(pool is empty so far)"
        prompt = (
            "It is now YOUR turn in the shared pool discussion. Conversation so far (oldest first):\n\n"
            f"{transcript}\n\n"
            "Take your turn now: act with your local tools if useful, then call post_to_pool exactly once."
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
            pool.post(reply, thread_id="pool", end_vote=False)

    def handle_round_end(envelope: dict) -> None:
        print(f"\n=== round ended: {envelope.get('reason', 'unknown')} — waiting for a new user message ===")

    def handle_error(text: str) -> None:
        print(f"\n[pool rejected our post] {text}")

    pool.on_message(handle_message)
    pool.on_turn(handle_turn)
    pool.on_round_end(handle_round_end)
    pool.on_error(handle_error)

    print(f"{my_name} is online, watching the shared pool at {args.host}:{args.port}.")
    print(f"Turn order: {' -> '.join(POOL_TURN_ORDER)}")
    print("This agent only speaks on its turn -- nothing to type here. Ctrl+C to exit.\n")

    try:
        threading.Event().wait()
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        pool.close()


if __name__ == "__main__":
    main()
