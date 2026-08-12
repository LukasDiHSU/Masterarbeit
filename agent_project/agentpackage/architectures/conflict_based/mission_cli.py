from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_CLI_PORT,
    DEFAULT_MESH_HOST,
    MESH_CLI_NAME,
    TB_IDS,
    build_peer_table,
    nav_id_for_tb,
    robot_peer_name,
)
from ...timing import format_elapsed, record_timing
from .mesh_bus import MeshNode


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Mission CLI for conflict-based peers. "
            "Paste a prompt; it is sent to ALL robots at once as solo missions."
        )
    )
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    parser.add_argument("--cli-port", type=int, default=DEFAULT_MESH_CLI_PORT)
    parser.add_argument("--name", default=MESH_CLI_NAME)
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    peers = [robot_peer_name(rid) for rid in TB_IDS]
    mesh = MeshNode(args.name, args.host, args.cli_port, peer_table)

    print(f"Mission CLI online as {args.name!r} at {args.host}:{args.cli_port}.")
    print(f"Peers (all get every prompt): {', '.join(peers)}")
    print("Paste a mission prompt and press Enter — sent to every robot at once.")
    print("Type quit / exit to leave. Ctrl+C also quits.\n")

    def _send_one(peer: str, text: str) -> tuple[str, str, float]:
        rid = peer  # peer name == robot id
        nav = nav_id_for_tb(rid)
        mission = (
            f"SOLO MISSION (work alone; negotiate only if an event opens):\n{text}\n"
            f"Use robot_id '{nav}' for navigate_to_pose / pickup_box / drop_box.\n"
            f"Other robots received the same prompt and work in parallel.\n"
            f"Each robot holds at most ONE box; drop before picking another.\n"
            f"Do not chat on the whiteboard — peer talk is negotiate_with after a conflict.\n"
            f"BEFORE you finish: call report_done_and_confirm(summary=...) to tell peers "
            f"what you did and get AGREE/DISAGREE that the fleet task is finished. "
            f"Only end as done if all_agree is true."
        )
        t0 = time.perf_counter()
        try:
            mesh._wait_for_link(peer, timeout=60.0)
            reply = mesh.ask(peer, mission, thread_id="mission")
            return peer, reply, time.perf_counter() - t0
        except Exception as e:
            return peer, f"ERROR: {e}", time.perf_counter() - t0

    try:
        while True:
            line = input("mission> ").strip()
            if not line:
                continue
            if line.lower() in {"quit", "exit", "q", "/quit", "/exit"}:
                break

            print(
                f"... sending prompt to {len(peers)} peers in parallel "
                f"(timer started) ...",
                flush=True,
            )
            t0 = time.perf_counter()
            results: dict[str, tuple[str, float]] = {}
            with ThreadPoolExecutor(max_workers=max(1, len(peers))) as pool:
                futs = {
                    pool.submit(_send_one, peer, line): peer for peer in peers
                }
                for fut in as_completed(futs):
                    peer, reply, elapsed = fut.result()
                    results[peer] = (reply, elapsed)
                    print(
                        f"\n[{peer}] done in {format_elapsed(elapsed)} ({elapsed:.1f}s)",
                        flush=True,
                    )
                    print(f"[{peer}] {reply}", flush=True)

            total = time.perf_counter() - t0
            print(
                f"\n*** TIME  until all peers replied: {format_elapsed(total)} "
                f"({total:.1f}s) ***\n",
                flush=True,
            )
            extra = " ".join(
                f"{p}={results[p][1]:.1f}s" for p in peers if p in results
            )
            record_timing("conflict_until_done", total, extra=extra)
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
