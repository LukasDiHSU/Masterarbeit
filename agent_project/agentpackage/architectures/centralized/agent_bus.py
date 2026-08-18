
from __future__ import annotations

import json
import queue
import select
import socket
import socketserver
import threading
import uuid
from typing import Any, Callable

from ...config import DEFAULT_BROKER_HOST, DEFAULT_BROKER_PORT
from ...monitor import report_messages

DEFAULT_HOST = DEFAULT_BROKER_HOST
DEFAULT_PORT = DEFAULT_BROKER_PORT

_LINE_MAX = 8 * 1024 * 1024


def _encode_line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")


_clients: dict[str, tuple[socket.socket, threading.Lock]] = {}
_clients_lock = threading.Lock()


def _send_line(sock: socket.socket, obj: dict[str, Any]) -> None:
    sock.sendall(_encode_line(obj))


class BrokerHandler(socketserver.BaseRequestHandler):
    """Every agent connects here first. The broker is the single point that
    knows about every other agent and forwards messages by name -- this is
    the defining trait of the centralized/star topology."""

    agent_name: str | None = None

    def handle(self) -> None:
        sock: socket.socket = self.request
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        buf = b""
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > _LINE_MAX:
                    return
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw, buf = buf[:nl], buf[nl + 1 :]
                    if not raw.strip():
                        continue
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not self._on_message(sock, msg):
                        return
        finally:
            if self.agent_name is not None:
                with _clients_lock:
                    current = _clients.get(self.agent_name)
                    if current is not None and current[0] is sock:
                        _clients.pop(self.agent_name, None)

    def _on_message(self, sock: socket.socket, msg: dict[str, Any]) -> bool:
        if self.agent_name is None:
            if msg.get("type") != "register":
                return False
            self.agent_name = str(msg["name"])
            with _clients_lock:
                _clients[self.agent_name] = (sock, threading.Lock())
            return True

        target = msg.get("to")
        if not target:
            return True

        with _clients_lock:
            dst = _clients.get(str(target))

        if dst is None:
            # Echo request_id so the caller's BusClient.ask can unblock.
            try:
                _send_line(
                    sock,
                    {
                        "type": "error",
                        "from": "broker",
                        "to": str(target),
                        "text": f"{target!r} is not connected",
                        "request_id": msg.get("request_id"),
                    },
                )
            except OSError:
                return False
            return True

        dst_sock, dst_lock = dst
        try:
            with dst_lock:
                _send_line(dst_sock, msg)
        except OSError:
            pass
        return True


class ThreadedBroker(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_broker(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    with ThreadedBroker((host, port), BrokerHandler) as server:
        print(f"Broker listening on {host}:{port}")
        server.serve_forever()


class BusClient:
    """Client-side handle used by every agent to talk to the broker.

    All socket reads and writes run on one I/O thread. Worker threads (LLM
    invoke, parallel execute) only enqueue outgoing messages, so makefile /
    sendall races cannot stall a reply after the robot has already printed it.
    """

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
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._wake_r, self._wake_w = socket.socketpair()
        for side in (self._wake_r, self._wake_w):
            side.setblocking(False)
        self._outgoing: queue.Queue[dict[str, Any]] = queue.Queue()
        self._io_thread: threading.Thread | None = None
        self._handlers: list[Callable[[dict[str, Any]], None]] = []
        self._pending: dict[str, queue.Queue[str]] = {}
        self._pending_lock = threading.Lock()
        self._messages_sent = 0
        self._closed = False

        self.send(type="register", name=self.name)

        self._io_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._recv_loop, daemon=True, name=f"bus-io-{self.name}"
        )
        self._thread.start()
        if not self._io_ready.wait(timeout=5):
            raise RuntimeError(f"Bus I/O thread failed to start for {self.name!r}")

    def _wakeup(self) -> None:
        try:
            self._wake_w.send(b"\0")
        except (BlockingIOError, OSError):
            pass

    def _send_json_now(self, obj: dict[str, Any]) -> None:
        self.sock.sendall(_encode_line(obj))

    def _flush_outgoing(self) -> None:
        while True:
            try:
                obj = self._outgoing.get_nowait()
            except queue.Empty:
                return
            self._send_json_now(obj)

    def _complete_pending(self, request_id: Any, text: str) -> bool:
        key = str(request_id or "")
        if not key:
            return False
        with self._pending_lock:
            q = self._pending.get(key)
        if q is None:
            return False
        try:
            q.put_nowait(text)
        except queue.Full:
            pass
        return True

    def _dispatch_line(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            print(f"[bus {self.name}] dropped bad line: {e}")
            return

        if msg.get("type") == "error":
            req_id = msg.get("request_id")
            err_text = str(msg.get("text", "broker error"))
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
            return

        req_id = msg.get("request_id")
        if msg.get("type") == "agent_reply" and req_id is not None:
            if self._complete_pending(req_id, str(msg.get("text", ""))):
                return
            print(
                f"[bus {self.name}] unmatched reply "
                f"from={msg.get('from')!r} request_id={req_id!r}"
            )

        for handler in self._handlers:
            handler(msg)

    def _recv_loop(self) -> None:
        self._io_thread = threading.current_thread()
        self._io_ready.set()
        buf = b""
        try:
            while not self._closed:
                try:
                    self._flush_outgoing()
                except OSError as e:
                    if not self._closed:
                        print(f"[bus {self.name}] send failed: {type(e).__name__}: {e}")
                    break
                try:
                    ready, _, _ = select.select([self.sock, self._wake_r], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if self._wake_r in ready:
                    try:
                        while self._wake_r.recv(1024):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                if self.sock not in ready:
                    continue
                try:
                    chunk = self.sock.recv(65536)
                except OSError as e:
                    if not self._closed:
                        print(f"[bus {self.name}] recv failed: {type(e).__name__}: {e}")
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _LINE_MAX:
                    print(f"[bus {self.name}] incoming line exceeded {_LINE_MAX} bytes")
                    break
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line, buf = buf[:nl], buf[nl + 1 :]
                    self._dispatch_line(line)
                    try:
                        self._flush_outgoing()
                    except OSError:
                        self._closed = True
                        break
        except OSError as e:
            if not self._closed:
                print(f"[bus {self.name}] recv loop ended: {type(e).__name__}: {e}")
        finally:
            with self._pending_lock:
                pending = list(self._pending.items())
            for _req_id, q in pending:
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
            payload = dict(msg)
        else:
            payload = {"from": self.name, **msg}
            self._messages_sent += 1
            report_messages(
                agent=self.name, architecture=self.architecture, count=self._messages_sent
            )

        if self._io_thread is None or threading.current_thread() is self._io_thread:
            self._send_json_now(payload)
            return
        self._outgoing.put(payload)
        self._wakeup()

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
        self._wakeup()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        for side in (self._wake_r, self._wake_w):
            try:
                side.close()
            except OSError:
                pass
