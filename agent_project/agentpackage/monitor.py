"""Live usage dashboard shared by all four architectures.

Every agent (via ``BaseAgent``) and every transport (the centralized broker
client, the conflict-based mesh, and the shared message pool) fires small
UDP telemetry packets at this module's reporting helpers. A separate
process -- ``python -m agentpackage.monitor`` -- listens for those packets
and renders a live table: token usage and inter-agent message counts per
agent, plus fleet-wide totals, in its own terminal.

UDP is used deliberately: reporting is fire-and-forget and never blocks or
raises inside the agent/transport code, and the monitor is entirely
optional -- nothing else depends on it being up.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_MONITOR_HOST, DEFAULT_MONITOR_PORT


class MonitorReporter:
    """Fire-and-forget UDP telemetry sender."""

    def __init__(self, host: str = DEFAULT_MONITOR_HOST, port: int = DEFAULT_MONITOR_PORT):
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, **fields: Any) -> None:
        try:
            payload = json.dumps({"ts": time.time(), **fields}).encode("utf-8")
            self._sock.sendto(payload, (self.host, self.port))
        except OSError:
            pass  # monitor not running / network hiccup -- never let this break the caller


_reporter: MonitorReporter | None = None
_reporter_lock = threading.Lock()


def get_reporter() -> MonitorReporter:
    global _reporter
    with _reporter_lock:
        if _reporter is None:
            _reporter = MonitorReporter()
        return _reporter


def report_tokens(
    *,
    agent: str,
    architecture: str,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    llm_calls: int,
) -> None:
    """Report an agent's CUMULATIVE token usage so far. Sending the running
    total (not a delta) makes this robust to UDP packet loss/reordering --
    a dropped packet just means one stale-but-not-wrong update, never drift."""
    get_reporter().send(
        event="tokens",
        agent=agent,
        architecture=architecture,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        llm_calls=llm_calls,
    )


def report_messages(*, agent: str, architecture: str, count: int) -> None:
    """Report an agent's/transport's CUMULATIVE count of messages it has
    sent (asks, replies, mesh sends, or accepted pool posts) so far."""
    get_reporter().send(event="messages", agent=agent, architecture=architecture, count=count)


@dataclass
class _AgentStats:
    architecture: str = "?"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0
    messages: int = 0
    last_seen: float = field(default_factory=time.time)


def _render(stats: dict[str, _AgentStats]) -> str:
    rows = sorted(stats.items())
    totals = _AgentStats()
    for _, s in rows:
        totals.input_tokens += s.input_tokens
        totals.output_tokens += s.output_tokens
        totals.total_tokens += s.total_tokens
        totals.llm_calls += s.llm_calls
        totals.messages += s.messages

    col = "{:<16} {:<14} {:>10} {:>10} {:>9} {:>9} {:>10}"
    header = col.format("AGENT", "ARCHITECTURE", "MESSAGES", "LLM CALLS", "IN TOK", "OUT TOK", "TOTAL TOK")
    lines = ["=== Agent usage monitor === (Ctrl+C to quit)", "", header, "-" * len(header)]
    for name, s in rows:
        age = time.time() - s.last_seen
        marker = name if age < 15 else f"{name} (idle {int(age)}s)"
        lines.append(
            col.format(marker, s.architecture, s.messages, s.llm_calls, s.input_tokens, s.output_tokens, s.total_tokens)
        )
    lines.append("-" * len(header))
    lines.append(
        col.format("TOTAL", f"{len(rows)} agent(s)", totals.messages, totals.llm_calls, totals.input_tokens, totals.output_tokens, totals.total_tokens)
    )
    return "\n".join(lines)


def run_monitor_server(
    host: str = DEFAULT_MONITOR_HOST,
    port: int = DEFAULT_MONITOR_PORT,
    *,
    redraw_interval: float = 0.5,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"Usage monitor listening (UDP) on {host}:{port}")

    stats: dict[str, _AgentStats] = {}
    lock = threading.Lock()
    dirty = threading.Event()

    def receiver() -> None:
        while True:
            try:
                data, _addr = sock.recvfrom(65536)
            except OSError:
                return
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            agent = str(msg.get("agent", "?"))
            with lock:
                s = stats.setdefault(agent, _AgentStats())
                s.architecture = str(msg.get("architecture", s.architecture))
                s.last_seen = float(msg.get("ts", time.time()))
                if msg.get("event") == "tokens":
                    s.input_tokens = int(msg.get("input_tokens", s.input_tokens))
                    s.output_tokens = int(msg.get("output_tokens", s.output_tokens))
                    s.total_tokens = int(msg.get("total_tokens", s.total_tokens))
                    s.llm_calls = int(msg.get("llm_calls", s.llm_calls))
                elif msg.get("event") == "messages":
                    s.messages = int(msg.get("count", s.messages))
            dirty.set()

    threading.Thread(target=receiver, daemon=True).start()

    try:
        while True:
            dirty.wait(timeout=redraw_interval)
            dirty.clear()
            with lock:
                snapshot = {name: _AgentStats(**vars(s)) for name, s in stats.items()}
            print("\x1b[2J\x1b[H" + _render(snapshot), flush=True)
    except KeyboardInterrupt:
        print()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Live dashboard of token usage and inter-agent message counts across all agents/architectures."
    )
    parser.add_argument("--host", default=DEFAULT_MONITOR_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_MONITOR_PORT)
    args = parser.parse_args()
    run_monitor_server(args.host, args.port)


if __name__ == "__main__":
    main()
