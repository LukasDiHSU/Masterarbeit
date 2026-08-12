from __future__ import annotations

import itertools
import json
import re
import socket
import socketserver
import threading
import time
from typing import Any, BinaryIO, Callable

from ...config import DEFAULT_POOL_HOST, DEFAULT_POOL_PORT, POOL_TURN_ORDER
from ...monitor import report_messages

DEFAULT_HOST = DEFAULT_POOL_HOST
DEFAULT_PORT = DEFAULT_POOL_PORT

# Mission / execute-phase completion token (uppercase DONE).
_DONE_RE = re.compile(r"(?:^|\s)DONE(?:\s|$|[.!,;:])")
_AGREE_RE = re.compile(r"^\s*AGREE\b", re.IGNORECASE | re.MULTILINE)


def message_says_done(text: str) -> bool:
    """True if the post signals mission/execute completion (token DONE)."""
    return bool(_DONE_RE.search(text or ""))


def message_says_agree(text: str) -> bool:
    """True if the post starts with AGREE (discussion consensus vote)."""
    return bool(_AGREE_RE.search(text or ""))


def _send_line(wfile: BinaryIO, lock: threading.Lock, obj: dict[str, Any]) -> None:
    with lock:
        wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        wfile.flush()


_log: list[dict[str, Any]] = []
_log_lock = threading.Lock()
_seq_counter = itertools.count(1)
_subscribers: dict[int, tuple[BinaryIO, threading.Lock]] = {}
_subscribers_lock = threading.Lock()
_next_subscriber_id = itertools.count(1)

_messages_by_agent: dict[str, int] = {}
_messages_by_agent_lock = threading.Lock()

_turn_order: list[str] = list(POOL_TURN_ORDER)
_turn_lock = threading.Lock()
_turn_index = 0
_round_active = True
# discuss → unanimous AGREE → execute → DONE ends round; start_discussion → discuss
_phase: str = "discuss"


def _broadcast(obj: dict[str, Any]) -> None:
    with _subscribers_lock:
        targets = list(_subscribers.values())
    for wfile, lock in targets:
        try:
            _send_line(wfile, lock, obj)
        except OSError:
            pass


def _current_turn_locked() -> str | None:
    return _turn_order[_turn_index] if _turn_order else None


def _latest_turn_taker_texts_locked() -> dict[str, str]:
    """Most recent post text from each turn-order agent (scan log newest-first)."""
    latest: dict[str, str] = {}
    needed = set(_turn_order)
    for message in reversed(_log):
        name = str(message.get("from", ""))
        if name in needed and name not in latest:
            latest[name] = str(message.get("text", ""))
            if len(latest) >= len(needed):
                break
    return latest


def _all_turn_takers_agree_locked() -> bool:
    if not _turn_order:
        return False
    latest = _latest_turn_taker_texts_locked()
    if len(latest) < len(_turn_order):
        return False
    return all(message_says_agree(latest[name]) for name in _turn_order)


def _broadcast_turn_locked() -> None:
    _broadcast(
        {
            "type": "turn",
            "name": _current_turn_locked(),
            "round_active": _round_active,
            "phase": _phase,
        }
    )


def _enter_execute_phase_locked() -> None:
    """Enter execute: wake ALL agents at once (parallel work, no turn order)."""
    global _phase, _turn_index
    _phase = "execute"
    _turn_index = 0
    _broadcast(
        {
            "type": "phase",
            "phase": "execute",
            "reason": "all_agree",
            "message": "All agents AGREEd — execute phase started (parallel).",
        }
    )
    # Not a single-agent turn: every turn-taker should start MCP work now.
    _broadcast(
        {
            "type": "execute",
            "phase": "execute",
            "reason": "all_agree",
            "agents": list(_turn_order),
            "message": "Agreed plan is in the pool — execute YOUR role in parallel.",
        }
    )


def _enter_discuss_phase_locked(*, reason: str, by: str) -> None:
    global _phase, _turn_index, _round_active
    _phase = "discuss"
    _turn_index = 0
    _round_active = True
    _broadcast(
        {
            "type": "phase",
            "phase": "discuss",
            "reason": reason,
            "from": by,
            "message": "Discussion round started — AGREE to reach execute.",
        }
    )
    _broadcast_turn_locked()


class PoolHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        first = self.rfile.readline()
        if not first:
            return
        hello = json.loads(first.decode("utf-8"))
        name = str(hello.get("name", "anonymous"))

        sub_lock = threading.Lock()
        sub_id = next(_next_subscriber_id)
        with _subscribers_lock:
            _subscribers[sub_id] = (self.wfile, sub_lock)

        with _log_lock:
            backlog = list(_log)
        for message in backlog:
            _send_line(self.wfile, sub_lock, {"type": "message", **message})
        _send_line(self.wfile, sub_lock, {"type": "history_end"})
        with _turn_lock:
            turn_state = {
                "type": "turn",
                "name": _current_turn_locked(),
                "round_active": _round_active,
                "phase": _phase,
            }
        _send_line(self.wfile, sub_lock, turn_state)

        try:
            for raw in self.rfile:
                envelope = json.loads(raw.decode("utf-8"))
                etype = envelope.get("type")
                if etype == "control":
                    self._handle_control(name, envelope, self.wfile, sub_lock)
                elif etype == "post":
                    self._handle_post(name, envelope, self.wfile, sub_lock)
        finally:
            with _subscribers_lock:
                _subscribers.pop(sub_id, None)

    def _handle_control(
        self,
        name: str,
        envelope: dict[str, Any],
        wfile: BinaryIO,
        sub_lock: threading.Lock,
    ) -> None:
        action = str(envelope.get("action", "")).strip().lower()
        if action != "start_discussion":
            _send_line(
                wfile,
                sub_lock,
                {"type": "error", "text": f"unknown_control: {action!r}"},
            )
            return
        reason = str(envelope.get("text", "")).strip() or "agent_requested"
        with _turn_lock:
            with _log_lock:
                note = {
                    "seq": next(_seq_counter),
                    "from": name,
                    "text": f"[start_discussion] {reason}",
                    "thread_id": envelope.get("thread_id", "pool"),
                    "done": False,
                    "agree": False,
                    "timestamp": time.time(),
                    "meta": "start_discussion",
                }
                _log.append(note)
            _broadcast({"type": "message", **note})
            _enter_discuss_phase_locked(reason="start_discussion", by=name)
        _send_line(
            wfile,
            sub_lock,
            {"type": "ok", "text": "discussion_started"},
        )

    def _handle_post(
        self,
        name: str,
        envelope: dict[str, Any],
        wfile: BinaryIO,
        sub_lock: threading.Lock,
    ) -> None:
        global _turn_index, _round_active, _phase

        is_turn_taker = name in _turn_order
        text = str(envelope.get("text", ""))
        done = message_says_done(text)
        agree = message_says_agree(text)

        with _turn_lock:
            if is_turn_taker:
                if not _round_active:
                    _send_line(
                        wfile,
                        sub_lock,
                        {
                            "type": "error",
                            "text": (
                                "round_ended: wait for a new user message or "
                                "start_discussion_round"
                            ),
                        },
                    )
                    return
                # Discuss is turn-based; execute is free-for-all (parallel).
                if _phase == "discuss":
                    expected = _current_turn_locked()
                    if name != expected:
                        _send_line(
                            wfile,
                            sub_lock,
                            {
                                "type": "error",
                                "text": (
                                    f"not_your_turn: it is currently {expected!r}'s turn"
                                ),
                            },
                        )
                        return
            else:
                # Human (or non-turn agent): restart a fresh discussion round.
                _turn_index = 0
                _round_active = True
                _phase = "discuss"

            message = {
                "seq": next(_seq_counter),
                "from": name,
                "text": text,
                "thread_id": envelope.get("thread_id", "pool"),
                "done": done,
                "agree": agree,
                "phase": _phase,
                "timestamp": time.time(),
            }
            with _log_lock:
                _log.append(message)
            with _messages_by_agent_lock:
                _messages_by_agent[name] = _messages_by_agent.get(name, 0) + 1
                count = _messages_by_agent[name]
            report_messages(agent=name, architecture="shared_pool", count=count)
            _broadcast({"type": "message", **message})

            if not is_turn_taker:
                _broadcast(
                    {
                        "type": "phase",
                        "phase": "discuss",
                        "reason": "user_message",
                        "from": name,
                    }
                )
                _broadcast_turn_locked()
                return

            if _phase == "discuss":
                # DONE does not end discussion; only unanimous AGREE → execute.
                if _all_turn_takers_agree_locked():
                    _enter_execute_phase_locked()
                else:
                    _turn_index = (_turn_index + 1) % len(_turn_order)
                    _broadcast_turn_locked()
                return

            # execute phase — parallel; any agent may post status; DONE ends round.
            if done:
                _round_active = False
                _phase = "idle"
                _broadcast(
                    {
                        "type": "round_end",
                        "reason": "agent_said_done",
                        "from": name,
                        "phase": "idle",
                    }
                )


class ThreadedPool(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_pool_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    turn_order: list[str] | None = None,
) -> None:
    global _turn_order
    if turn_order is not None:
        _turn_order = list(turn_order)
    with ThreadedPool((host, port), PoolHandler) as server:
        order_desc = " -> ".join(_turn_order) if _turn_order else "(no turn order; free-for-all)"
        print(f"Shared message pool listening on {host}:{port}")
        print(f"Discuss turn order: {order_desc}")
        print(
            "Phases: discuss (turn-based until all AGREE) → "
            "execute (ALL agents in parallel until DONE); "
            "start_discussion reopens discuss."
        )
        server.serve_forever()


class PoolClient:
    """Connection to the shared pool (agents + human CLI).

    Discussion continues until every turn-taker's latest post AGREEs; then the
    server enters execute and wakes all agents in parallel. DONE ends the round
    only in execute. ``start_discussion`` reopens a discussion round.
    """

    def __init__(self, name: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.name = name
        self.sock = socket.create_connection((host, port))
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self._send_lock = threading.Lock()
        self._message_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._turn_handlers: list[Callable[[str | None], None]] = []
        self._execute_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._round_end_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._phase_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._error_handlers: list[Callable[[str], None]] = []
        self.history: list[dict[str, Any]] = []
        self.current_turn: str | None = None
        self.round_active: bool = True
        self.phase: str = "discuss"
        self._history_ready = threading.Event()

        self._send_json({"type": "hello", "name": self.name})
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _send_json(self, obj: dict[str, Any]) -> None:
        with self._send_lock:
            self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def _recv_loop(self) -> None:
        for line in self.reader:
            envelope = json.loads(line)
            etype = envelope.get("type")
            if etype == "history_end":
                self._history_ready.set()
                continue
            if etype == "message":
                message = {k: v for k, v in envelope.items() if k != "type"}
                self.history.append(message)
                for handler in self._message_handlers:
                    handler(message)
                continue
            if etype == "turn":
                self.current_turn = envelope.get("name")
                self.round_active = bool(envelope.get("round_active", True))
                if "phase" in envelope:
                    self.phase = str(envelope.get("phase") or self.phase)
                for handler in self._turn_handlers:
                    handler(self.current_turn)
                continue
            if etype == "execute":
                self.phase = "execute"
                self.round_active = True
                self.current_turn = None
                for handler in self._execute_handlers:
                    handler(envelope)
                continue
            if etype == "phase":
                self.phase = str(envelope.get("phase") or self.phase)
                self.round_active = True
                for handler in self._phase_handlers:
                    handler(envelope)
                continue
            if etype == "round_end":
                self.round_active = False
                self.phase = str(envelope.get("phase") or "idle")
                for handler in self._round_end_handlers:
                    handler(envelope)
                continue
            if etype == "error":
                for handler in self._error_handlers:
                    handler(str(envelope.get("text", "")))
                continue
            if etype == "ok":
                continue

    def on_message(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._message_handlers.append(handler)

    def on_turn(self, handler: Callable[[str | None], None]) -> None:
        self._turn_handlers.append(handler)

    def on_execute(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Called when the pool enters parallel execute (after unanimous AGREE)."""
        self._execute_handlers.append(handler)

    def on_round_end(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._round_end_handlers.append(handler)

    def on_phase(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._phase_handlers.append(handler)

    def on_error(self, handler: Callable[[str], None]) -> None:
        self._error_handlers.append(handler)

    def wait_for_history(self, timeout: float = 5.0) -> None:
        self._history_ready.wait(timeout)

    def post(self, text: str, *, thread_id: str = "pool") -> None:
        self._send_json({"type": "post", "text": text, "thread_id": thread_id})

    def start_discussion(self, reason: str = "", *, thread_id: str = "pool") -> None:
        """Ask the pool to reopen a discuss phase (until all AGREE again)."""
        self._send_json(
            {
                "type": "control",
                "action": "start_discussion",
                "text": reason,
                "thread_id": thread_id,
            }
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.history[-limit:]

    def close(self) -> None:
        try:
            self.reader.close()
        finally:
            self.sock.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the shared message pool (blackboard) server.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--turn-order",
        default=None,
        help="Comma-separated agent names. Defaults to fleet order from config.py.",
    )
    args = parser.parse_args()
    turn_order = None
    if args.turn_order is not None:
        turn_order = [n.strip() for n in args.turn_order.split(",") if n.strip()]
    run_pool_server(args.host, args.port, turn_order=turn_order)


if __name__ == "__main__":
    main()
