"""Scrolling Agent Trace viewer (LLM text + tool usage).

Agents emit ``event=trace`` UDP packets via ``monitor.report_trace`` to
``AGENT_TRACE_MONITOR_PORT`` (default 9901). This process listens and
prints an append-only log so you can watch reasoning and tool calls live
while agents work (including during long tools like navigate_to_pose).
"""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime

from .config import DEFAULT_MONITOR_HOST, DEFAULT_TRACE_MONITOR_PORT


def _format_line(msg: dict) -> str:
    ts = float(msg.get("ts") or time.time())
    stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    agent = str(msg.get("agent", "?"))
    kind = str(msg.get("kind", "?"))
    tool = msg.get("tool")
    text = str(msg.get("text") or "").strip()

    if kind == "tool_start":
        label = f"TOOL start {tool or '?'}"
    elif kind == "tool_end":
        label = f"TOOL end {tool or '?'}"
    elif kind == "llm":
        label = "LLM"
    elif kind == "turn_end":
        label = "TURN end"
    else:
        label = kind.upper()

    if text:
        return f"[{stamp}] {agent} | {label} | {text}"
    return f"[{stamp}] {agent} | {label}"


def run_trace_monitor(
    host: str = DEFAULT_MONITOR_HOST,
    port: int = DEFAULT_TRACE_MONITOR_PORT,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"Agent Trace listening (UDP) on {host}:{port}")
    print("Showing LLM replies and tool start/end for all agents. Ctrl+C to quit.\n")

    try:
        while True:
            try:
                data, _addr = sock.recvfrom(65536)
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("event") != "trace":
                continue
            try:
                print(_format_line(msg), flush=True)
            except Exception:
                continue
    except KeyboardInterrupt:
        print()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Live scrolling log of agent LLM text and tool usage (UDP traces)."
    )
    parser.add_argument("--host", default=DEFAULT_MONITOR_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_TRACE_MONITOR_PORT)
    args = parser.parse_args()
    run_trace_monitor(args.host, args.port)


if __name__ == "__main__":
    main()
