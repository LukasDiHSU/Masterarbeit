
from __future__ import annotations

import itertools
import json
import socket
import socketserver
import threading
import time
from typing import Any, BinaryIO, Callable

from ...config import DEFAULT_POOL_HOST, DEFAULT_POOL_PORT, POOL_TURN_ORDER
from ...monitor import report_messages

DEFAULT_HOST = DEFAULT_POOL_HOST
DEFAULT_PORT = DEFAULT_POOL_PORT


def _send_line(wfile: BinaryIO, lock: threading.Lock, obj: dict[str, Any]) -> None:
    with lock:
        wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        wfile.flush()


# Process-wide state: one shared, append-only log and one set of live
# subscribers. Every accepted post is broadcast to every subscriber
# (including the poster) -- there is no "to" field and no per-recipient
# routing anywhere in this file.
_log: list[dict[str, Any]] = []
_log_lock = threading.Lock()
_seq_counter = itertools.count(1)
_subscribers: dict[int, tuple[BinaryIO, threading.Lock]] = {}
_subscribers_lock = threading.Lock()
_next_subscriber_id = itertools.count(1)

# Cumulative count of ACCEPTED posts per poster name, reported to the usage
# monitor -- rejected (out-of-turn / post-round-end) attempts don't count,
# since they never actually reached anyone.
_messages_by_agent: dict[str, int] = {}
_messages_by_agent_lock = threading.Lock()

# Turn-based round-robin state. Enforced here (not just by prompting) so two
# agents can never both believe it is their turn: the server is the single
# source of truth for whose turn it is, even though *what* to say is still
# entirely up to each agent's own reasoning.
_turn_order: list[str] = list(POOL_TURN_ORDER)
_turn_lock = threading.Lock()
_turn_index = 0
_round_active = True
_consecutive_end_votes = 0


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

        # Replay full history so a late joiner (human or agent) has the same
        # view of the world as everyone already connected.
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
            }
        _send_line(self.wfile, sub_lock, turn_state)

        try:
            for raw in self.rfile:
                envelope = json.loads(raw.decode("utf-8"))
                if envelope.get("type") != "post":
                    continue
                self._handle_post(name, envelope, self.wfile, sub_lock)
        finally:
            with _subscribers_lock:
                _subscribers.pop(sub_id, None)

    def _handle_post(
        self,
        name: str,
        envelope: dict[str, Any],
        wfile: BinaryIO,
        sub_lock: threading.Lock,
    ) -> None:
        global _turn_index, _round_active, _consecutive_end_votes

        is_turn_taker = name in _turn_order
        end_vote = bool(envelope.get("end_vote", False))

        with _turn_lock:
            if is_turn_taker:
                expected = _current_turn_locked()
                if not _round_active:
                    _send_line(
                        wfile,
                        sub_lock,
                        {
                            "type": "error",
                            "text": "round_ended: wait for a new user message to start a new round",
                        },
                    )
                    return
                if name != expected:
                    _send_line(
                        wfile,
                        sub_lock,
                        {"type": "error", "text": f"not_your_turn: it is currently {expected!r}'s turn"},
                    )
                    return
            else:
                # A message from outside the turn order (typically the human
                # user) always (re)starts a fresh round at the front.
                _turn_index = 0
                _round_active = True
                _consecutive_end_votes = 0

            message = {
                "seq": next(_seq_counter),
                "from": name,
                "text": str(envelope.get("text", "")),
                "thread_id": envelope.get("thread_id", "pool"),
                "end_vote": end_vote,
                "timestamp": time.time(),
            }
            with _log_lock:
                _log.append(message)
            with _messages_by_agent_lock:
                _messages_by_agent[name] = _messages_by_agent.get(name, 0) + 1
                count = _messages_by_agent[name]
            report_messages(agent=name, architecture="shared_pool", count=count)
            _broadcast({"type": "message", **message})

            if is_turn_taker:
                _consecutive_end_votes = _consecutive_end_votes + 1 if end_vote else 0
                if _consecutive_end_votes >= len(_turn_order):
                    _round_active = False
                    _broadcast({"type": "round_end", "reason": "all_agents_agreed_to_end"})
                else:
                    _turn_index = (_turn_index + 1) % len(_turn_order)
                    _broadcast({"type": "turn", "name": _current_turn_locked(), "round_active": True})
            else:
                _broadcast({"type": "turn", "name": _current_turn_locked(), "round_active": True})


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
        print(f"Turn order: {order_desc}")
        server.serve_forever()


class PoolClient:
    """A connection to the shared pool. Every accepted ``post`` is broadcast
    to every connected client, including the sender, and every new
    connection is replayed the full history before live messages start
    flowing. Used by both robot agents and the plain human CLI
    (``pool_cli.py``) -- there is nothing agent-specific about this
    transport.

    The pool enforces a round-robin turn order server-side: a post from an
    agent whose turn it is not is rejected with a private ``error`` message
    (never added to the shared log); a post from outside the turn order
    (e.g. the human user) always starts a fresh round. ``on_turn`` tells you
    whose turn it currently is; ``on_round_end`` fires once every
    turn-taker in a row has posted with ``end_vote=True``.
    """

    def __init__(self, name: str, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.name = name
        self.sock = socket.create_connection((host, port))
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self._send_lock = threading.Lock()
        self._message_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._turn_handlers: list[Callable[[str | None], None]] = []
        self._round_end_handlers: list[Callable[[dict[str, Any]], None]] = []
        self._error_handlers: list[Callable[[str], None]] = []
        self.history: list[dict[str, Any]] = []
        self.current_turn: str | None = None
        self.round_active: bool = True
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
                for handler in self._turn_handlers:
                    handler(self.current_turn)
                continue
            if etype == "round_end":
                self.round_active = False
                for handler in self._round_end_handlers:
                    handler(envelope)
                continue
            if etype == "error":
                for handler in self._error_handlers:
                    handler(str(envelope.get("text", "")))
                continue

    def on_message(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Called for EVERY accepted message, including this client's own --
        filter on ``msg["from"]`` yourself if you want to ignore your own posts."""
        self._message_handlers.append(handler)

    def on_turn(self, handler: Callable[[str | None], None]) -> None:
        """Called with the name of whoever's turn it now is, every time the
        turn advances (including right after connecting)."""
        self._turn_handlers.append(handler)

    def on_round_end(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Called once every turn-taker in a row has posted with end_vote=True."""
        self._round_end_handlers.append(handler)

    def on_error(self, handler: Callable[[str], None]) -> None:
        """Called when one of OUR posts was rejected (not our turn / round ended)."""
        self._error_handlers.append(handler)

    def wait_for_history(self, timeout: float = 5.0) -> None:
        self._history_ready.wait(timeout)

    def post(self, text: str, *, thread_id: str = "pool", end_vote: bool = False) -> None:
        self._send_json({"type": "post", "text": text, "thread_id": thread_id, "end_vote": end_vote})

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
        help="Comma-separated agent names, e.g. robot_tb1,robot_tb2,robot_tb3,robot_tb4. "
        "Defaults to the fleet order from config.py. An empty string removes server-side "
        "enforcement entirely, but pool_agent.py only speaks when told it's its turn, so "
        "agents will stay silent unless you also change how they react.",
    )
    args = parser.parse_args()
    turn_order = None
    if args.turn_order is not None:
        turn_order = [n.strip() for n in args.turn_order.split(",") if n.strip()]
    run_pool_server(args.host, args.port, turn_order=turn_order)


if __name__ == "__main__":
    main()
