"""Human entry point into the AgentNet DMAS mesh.

A mission is handed to ``SmallDeliveryRobot_0``, which chairs turn-taking until
the fleet agrees on a short plan, executes it, meets again, and finally every
robot says FINISHED.
"""

from __future__ import annotations

import argparse
import time

from ...config import (
    AGENTNET_ENTRY_ROBOT as ENTRY_ROBOT,
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_CLI_PORT,
    DEFAULT_MESH_HOST,
    MESH_CLI_NAME,
    TB_IDS,
    build_peer_table,
    robot_peer_name,
)
from ...timing import format_elapsed, mission_timeout_guard, record_timing
from ..conflict_based.mesh_bus import MeshNode
from .protocol import ARCHITECTURE, ASK_TIMEOUT, wrap_mission


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Task CLI for AgentNet (DMAS). A mission always enters at "
            f"{robot_peer_name(ENTRY_ROBOT)}; the fleet discusses, executes a "
            "short chunk, and meets again until everyone says FINISHED."
        )
    )
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    parser.add_argument("--cli-port", type=int, default=DEFAULT_MESH_CLI_PORT)
    parser.add_argument("--name", default=MESH_CLI_NAME)
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    entry = robot_peer_name(ENTRY_ROBOT)
    mesh = MeshNode(
        args.name, args.host, args.cli_port, peer_table, architecture=ARCHITECTURE
    )

    print(f"Task CLI online as {args.name!r} at {args.host}:{args.cli_port}.")
    print(
        f"Entry: {entry} chairs DMAS (agree on a short plan → execute → meet again)."
    )
    print(f"Network: {', '.join(peer_table)}")
    print("Paste a mission prompt and press Enter. Type quit / exit to leave.\n")

    try:
        while True:
            line = input("mission> ").strip()
            if not line:
                continue
            if line.lower() in {"quit", "exit", "q", "/quit", "/exit"}:
                break

            print(f"... mission handed to {entry} (timer started) ...", flush=True)
            t0 = time.perf_counter()
            try:
                with mission_timeout_guard("agentnet_until_done"):
                    mesh._wait_for_link(entry, timeout=60.0)
                    reply = mesh.ask(
                        entry,
                        wrap_mission(line),
                        thread_id="mission",
                        timeout=ASK_TIMEOUT,
                    )
            except Exception as e:
                reply = f"ERROR: {e}"
            elapsed = time.perf_counter() - t0

            print(f"\n[{entry}] {reply}", flush=True)
            print(
                f"\n*** TIME  until the fleet reported back: "
                f"{format_elapsed(elapsed)} ({elapsed:.1f}s) ***\n",
                flush=True,
            )
            record_timing("agentnet_until_done", elapsed, extra=f"entry={entry}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
