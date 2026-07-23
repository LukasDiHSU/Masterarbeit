
from __future__ import annotations

import argparse

from ...config import DEFAULT_POOL_HOST, DEFAULT_POOL_PORT
from .message_pool import PoolClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Plain human console for the shared message pool -- no LLM involved. "
            "Everything you type is broadcast to every agent (and everyone else "
            "watching the pool) exactly like an agent's post_to_pool call."
        )
    )
    parser.add_argument("--name", default="user")
    parser.add_argument("--host", default=DEFAULT_POOL_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_POOL_PORT)
    args = parser.parse_args()

    client = PoolClient(args.name, host=args.host, port=args.port)
    client.wait_for_history(timeout=3.0)

    if client.history:
        print(f"--- pool history ({len(client.history)} messages) ---")
        for msg in client.history:
            print(f"[{msg['from']}] {msg['text']}")
        print("--- end history ---\n")

    def show(msg: dict) -> None:
        if msg.get("from") == args.name:
            return
        marker = " [END VOTE]" if msg.get("end_vote") else ""
        print(f"\n[{msg['from']}] {msg['text']}{marker}")

    def show_turn(name: str | None) -> None:
        print(f"\n... it is now {name}'s turn ...")

    def show_round_end(envelope: dict) -> None:
        print(f"\n=== round ended: {envelope.get('reason', 'unknown')} — send a message to start a new round ===")

    client.on_message(show)
    client.on_turn(show_turn)
    client.on_round_end(show_round_end)

    print(f"Connected to the shared pool as {args.name!r}.")
    print("Type a message and press Enter to broadcast it directly to every agent -- this starts a new")
    print("turn-based round beginning with the first agent in the fixed speaking order. Ctrl+C to quit.\n")

    try:
        while True:
            line = input(f"{args.name}> ").strip()
            if not line:
                continue
            client.post(line)
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        client.close()


if __name__ == "__main__":
    main()
