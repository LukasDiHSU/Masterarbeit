from __future__ import annotations

import argparse
import threading
import time

from ...config import DEFAULT_POOL_HOST, DEFAULT_POOL_PORT
from ...timing import format_elapsed, record_timing
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

    # Round timing: user post → first reply → all AGREE (execute) → DONE.
    lock = threading.Lock()
    round_t0: float | None = None
    first_reply_at: float | None = None
    first_reply_from: str | None = None
    execute_at: float | None = None

    def _elapsed_since_start() -> float | None:
        if round_t0 is None:
            return None
        return time.perf_counter() - round_t0

    def show(msg: dict) -> None:
        nonlocal first_reply_at, first_reply_from
        if msg.get("from") == args.name:
            return
        markers = []
        if msg.get("agree"):
            markers.append("AGREE")
        if msg.get("done"):
            markers.append("DONE")
        marker = f" [{', '.join(markers)}]" if markers else ""
        print(f"\n[{msg['from']}] {msg['text']}{marker}", flush=True)
        with lock:
            if round_t0 is not None and first_reply_at is None:
                first_reply_at = time.perf_counter()
                first_reply_from = str(msg.get("from") or "?")
                dt = first_reply_at - round_t0
                print(
                    f"\n*** TIME  first agent reply: {format_elapsed(dt)} "
                    f"({dt:.1f}s)  from={first_reply_from} ***\n",
                    flush=True,
                )
                record_timing(
                    "pool_first_reply",
                    dt,
                    extra=f"from={first_reply_from}",
                )

    def show_turn(name: str | None) -> None:
        elapsed = _elapsed_since_start()
        extra = f"  elapsed={format_elapsed(elapsed)}" if elapsed is not None else ""
        print(
            f"\n... it is now {name}'s turn (phase={client.phase}){extra} ...",
            flush=True,
        )

    def show_execute(envelope: dict) -> None:
        elapsed = _elapsed_since_start()
        extra = f"  elapsed={format_elapsed(elapsed)}" if elapsed is not None else ""
        agents = envelope.get("agents") or []
        who = ", ".join(str(a) for a in agents) if agents else "all agents"
        print(
            f"\n... EXECUTE (parallel) for {who}{extra} ...",
            flush=True,
        )

    def show_phase(envelope: dict) -> None:
        nonlocal execute_at
        phase = str(envelope.get("phase") or "")
        reason = envelope.get("reason", "")
        print(f"\n=== phase → {phase} ({reason}) ===", flush=True)
        with lock:
            if phase == "execute" and round_t0 is not None and execute_at is None:
                execute_at = time.perf_counter()
                dt = execute_at - round_t0
                print(
                    f"\n*** TIME  discuss→execute (all AGREE): {format_elapsed(dt)} "
                    f"({dt:.1f}s) ***\n",
                    flush=True,
                )
                record_timing("pool_all_agree", dt, extra=f"reason={reason}")
            elif phase == "discuss" and reason == "start_discussion":
                # Re-opened discussion; keep overall round_t0, clear execute mark.
                execute_at = None
                elapsed = _elapsed_since_start()
                if elapsed is not None:
                    print(
                        f"\n*** TIME  re-discuss started at {format_elapsed(elapsed)} "
                        f"into the round ***\n",
                        flush=True,
                    )
                    record_timing(
                        "pool_rediscuss",
                        elapsed,
                        extra=f"by={envelope.get('from', '?')}",
                    )

    def show_round_end(envelope: dict) -> None:
        nonlocal round_t0, first_reply_at, first_reply_from, execute_at
        who = envelope.get("from", "?")
        with lock:
            t0 = round_t0
            first_dt = (first_reply_at - t0) if (t0 is not None and first_reply_at) else None
            agree_dt = (execute_at - t0) if (t0 is not None and execute_at) else None
            total = (time.perf_counter() - t0) if t0 is not None else None
            round_t0 = None
            first_reply_at = None
            first_reply_from = None
            execute_at = None
        print(
            f"\n=== round ended: {envelope.get('reason', 'unknown')} "
            f"(by {who}) — send a message to start a new round ===",
            flush=True,
        )
        if total is not None:
            print(
                f"\n*** TIME  until DONE (system thinks done): {format_elapsed(total)} "
                f"({total:.1f}s) ***\n",
                flush=True,
            )
            extra = f"by={who}"
            if first_dt is not None:
                extra += f" first_reply={first_dt:.1f}s"
            if agree_dt is not None:
                extra += f" all_agree={agree_dt:.1f}s"
            record_timing("pool_until_done", total, extra=extra)

    client.on_message(show)
    client.on_turn(show_turn)
    client.on_execute(show_execute)
    client.on_phase(show_phase)
    client.on_round_end(show_round_end)

    print(f"Connected to the shared pool as {args.name!r}.")
    print("Type a message and press Enter to start a DISCUSS round (agents speak in order).")
    print("Agents discuss until all AGREE → ALL execute in parallel → DONE ends the round.")
    print("Agents may call start_discussion_round to replan. Ctrl+C to quit.")
    print(
        "Timing (User window + timings.log): first reply, all-AGREE, and until DONE.\n",
        flush=True,
    )

    try:
        while True:
            line = input(f"{args.name}> ").strip()
            if not line:
                continue
            with lock:
                round_t0 = time.perf_counter()
                first_reply_at = None
                first_reply_from = None
                execute_at = None
            print("\n*** TIME  round timer started ***\n", flush=True)
            client.post(line)
    except (KeyboardInterrupt, EOFError):
        # If the user aborts mid-round, still record how long it ran.
        with lock:
            t0 = round_t0
            total = (time.perf_counter() - t0) if t0 is not None else None
            round_t0 = None
        if total is not None:
            print(
                f"\n*** TIME  aborted after {format_elapsed(total)} "
                f"({total:.1f}s) — no DONE yet ***\n",
                flush=True,
            )
            record_timing("pool_aborted", total)
        print()
    finally:
        client.close()


if __name__ == "__main__":
    main()
