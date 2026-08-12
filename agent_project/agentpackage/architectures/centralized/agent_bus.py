
from __future__ import annotations

import json
import queue
import socket
import socketserver
import threading
import uuid
from typing import Any, BinaryIO, Callable

from ...config import DEFAULT_BROKER_HOST, DEFAULT_BROKER_PORT
from ...monitor import report_messages

DEFAULT_HOST = DEFAULT_BROKER_HOST
DEFAULT_PORT = DEFAULT_BROKER_PORT


def _send_line(wfile: BinaryIO, obj: dict[str, Any]) -> None:
    wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
    wfile.flush()


_clients: dict[str, tuple[BinaryIO, threading.Lock]] = {}
_clients_lock = threading.Lock()


class BrokerHandler(socketserver.StreamRequestHandler):
    """Every agent connects here first. The broker is the single point that
    knows about every other agent and forwards messages by name -- this is
    the defining trait of the centralized/star topology."""

    agent_name: str | None = None

    def handle(self) -> None:
        first = self.rfile.readline()
        if not first:
            return

        hello = json.loads(first.decode("utf-8"))
        if hello.get("type") != "register":
            return

        self.agent_name = str(hello["name"])

        with _clients_lock:
            _clients[self.agent_name] = (self.wfile, threading.Lock())

        try:
            for raw in self.rfile:
                msg = json.loads(raw.decode("utf-8"))
                target = msg.get("to")
                if not target:
                    continue

                with _clients_lock:
                    dst = _clients.get(str(target))

                if dst is None:
                    # Echo request_id so the caller's BusClient.ask can unblock.
                    _send_line(
                        self.wfile,
                        {
                            "type": "error",
                            "from": "broker",
                            "to": str(target),
                            "text": f"{target!r} is not connected",
                            "request_id": msg.get("request_id"),
                        },
                    )
                    continue

                dst_wfile, dst_lock = dst
                with dst_lock:
                    _send_line(dst_wfile, msg)

        finally:
            if self.agent_name is not None:
                with _clients_lock:
                    _clients.pop(self.agent_name, None)


class ThreadedBroker(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_broker(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    with ThreadedBroker((host, port), BrokerHandler) as server:
        print(f"Broker listening on {host}:{port}")
        server.serve_forever()


class BusClient:
    """Client-side handle used by every agent to talk to the broker."""

    def __init__(
        self,
        name: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        architecture: str = "centralized",
    ):
        self.name = name
        self.architecture = architecture
        self.sock = socket.create_connection((host, port))
        self.reader = self.sock.makefile("r", encoding="utf-8")
        self._send_lock = threading.Lock()
        self._handlers: list[Callable[[dict[str, Any]], None]] = []
        self._pending: dict[str, queue.Queue[str]] = {}
        self._pending_lock = threading.Lock()
        self._messages_sent = 0
        self._closed = False

        self.send(type="register", name=self.name)

        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _send_json(self, obj: dict[str, Any]) -> None:
        with self._send_lock:
            self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))

    def _complete_pending(self, request_id: Any, text: str) -> bool:
        if not request_id:
            return False
        with self._pending_lock:
            q = self._pending.get(str(request_id))
        if q is None:
            return False
        try:
            q.put_nowait(text)
        except queue.Full:
            pass
        return True

    def _recv_loop(self) -> None:
        try:
            for line in self.reader:
                msg = json.loads(line)

                if msg.get("type") == "error":
                    req_id = msg.get("request_id")
                    err_text = str(msg.get("text", "broker error"))
                    # Unblock ask() immediately when the peer is missing.
                    self._complete_pending(
                        req_id,
                        json.dumps(
                            {
                                "error": "broker_error",
                                "message": err_text,
                                "to": msg.get("to"),
                            }
                        ),
                    )
                    for handler in self._handlers:
                        handler(msg)
                    continue

                req_id = msg.get("request_id")
                if msg.get("type") == "agent_reply" and req_id is not None:
                    if self._complete_pending(req_id, str(msg.get("text", ""))):
                        continue

                for handler in self._handlers:
                    handler(msg)
        except (ValueError, OSError) as e:
            # Socket/reader closed (bus.close or peer disconnect).
            if not self._closed:
                print(f"[bus {self.name}] recv loop ended: {type(e).__name__}: {e}")
        finally:
            # Fail any waiters so tools don't hang until timeout.
            with self._pending_lock:
                pending = list(self._pending.items())
            for req_id, q in pending:
                try:
                    q.put_nowait(
                        json.dumps(
                            {
                                "error": "bus_disconnected",
                                "message": "Bus connection closed while waiting for a reply.",
                            }
                        )
                    )
                except queue.Full:
                    pass

    def on_message(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers.append(handler)

    def send(self, **msg: Any) -> None:
        if msg.get("type") == "register":
            self._send_json(msg)
            return
        self._send_json({"from": self.name, **msg})
        self._messages_sent += 1
        report_messages(agent=self.name, architecture=self.architecture, count=self._messages_sent)

    def ask(
        self,
        to: str,
        text: str,
        *,
        thread_id: str = "default",
        timeout: float = 600.0,
    ) -> str:
        request_id = uuid.uuid4().hex
        q: queue.Queue[str] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = q
        try:
            self.send(
                type="agent_request",
                to=to,
                text=text,
                thread_id=thread_id,
                request_id=request_id,
            )
            try:
                return q.get(timeout=timeout)
            except queue.Empty as e:
                raise TimeoutError(
                    f"No reply from {to!r} within {timeout:.0f}s "
                    "(peer offline, crashed, busy, or broker dropped the message)."
                ) from e
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def send_async_request(
        self,
        to: str,
        text: str,
        *,
        thread_id: str = "default",
    ) -> str:
        """Send a request without waiting for a reply and return the request id."""
        request_id = uuid.uuid4().hex
        self.send(
            type="agent_request",
            to=to,
            text=text,
            thread_id=thread_id,
            request_id=request_id,
        )
        return request_id

    def close(self) -> None:
        self._closed = True
        try:
            self.reader.close()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
