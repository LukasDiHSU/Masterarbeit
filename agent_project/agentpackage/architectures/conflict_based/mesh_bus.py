
from __future__ import annotations

import json
import queue
import socket
import socketserver
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, BinaryIO, Callable

from ...monitor import report_messages

PeerTable = dict[str, tuple[str, int]]


def _send_line(wfile: BinaryIO, lock: threading.Lock, obj: dict[str, Any]) -> None:
    with lock:
        wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        wfile.flush()


class MeshHandler(socketserver.StreamRequestHandler):
    """Accepts an inbound connection from exactly one peer and registers it
    as a bidirectional link. Set dynamically per-node via a subclass."""

    node: "MeshNode"

    def handle(self) -> None:
        first = self.rfile.readline()
        if not first:
            return
        hello = json.loads(first.decode("utf-8"))
        peer_name = str(hello.get("name"))
        lock = threading.Lock()
        link = (self.wfile, lock)

        with self.node._links_lock:
            self.node._links[peer_name] = link

        try:
            for raw in self.rfile:
                msg = json.loads(raw.decode("utf-8"))
                self.node._dispatch(msg)
        finally:
            with self.node._links_lock:
                if self.node._links.get(peer_name) is link:
                    self.node._links.pop(peer_name, None)


class MeshServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class MeshNode:
    """A single peer in a full mesh network: no central broker, every peer
    can be reached directly by every other peer it knows about.

    Exactly one physical TCP connection is established per unordered pair of
    peers: the peer whose name sorts *before* the other one dials out; the
    other side accepts. Both ends then reuse that single connection for
    requests and replies in either direction.
    """

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        peers: PeerTable,
        *,
        connect_retry_delay: float = 0.5,
        architecture: str = "conflict_based",
    ):
        self.name = name
        self.host = host
        self.port = port
        self.architecture = architecture
        self.peers: PeerTable = dict(peers)

        self._links_lock = threading.Lock()
        self._links: dict[str, tuple[BinaryIO, threading.Lock]] = {}
        self._pending: dict[str, queue.Queue[str]] = {}
        self._handlers: list[Callable[[dict[str, Any]], None]] = []
        self._messages_sent = 0

        handler_cls = type("_BoundMeshHandler", (MeshHandler,), {"node": self})
        self._server = MeshServer((host, port), handler_cls)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

        for peer_name in sorted(self.peers):
            if peer_name != self.name and peer_name > self.name:
                threading.Thread(
                    target=self._dial,
                    args=(peer_name, connect_retry_delay),
                    daemon=True,
                ).start()

    def add_peer(self, name: str, host: str, port: int, *, connect_retry_delay: float = 0.5) -> None:
        """Register a peer discovered at runtime (e.g. late joiners on the mesh)."""
        self.peers[name] = (host, port)
        if name != self.name and name > self.name:
            threading.Thread(
                target=self._dial,
                args=(name, connect_retry_delay),
                daemon=True,
            ).start()

    def _dial(self, peer_name: str, retry_delay: float) -> None:
        host, port = self.peers[peer_name]
        sock: socket.socket | None = None
        while sock is None:
            try:
                sock = socket.create_connection((host, port), timeout=5)
            except OSError:
                time.sleep(retry_delay)

        # create_connection() leaves its connect-timeout set on the socket;
        # clear it so the long-lived read loop below blocks indefinitely
        # instead of raising TimeoutError as soon as a peer goes quiet.
        sock.settimeout(None)

        wfile = sock.makefile("wb")
        rfile = sock.makefile("rb")
        lock = threading.Lock()
        _send_line(wfile, lock, {"type": "hello", "name": self.name})

        with self._links_lock:
            self._links[peer_name] = (wfile, lock)

        for raw in rfile:
            msg = json.loads(raw.decode("utf-8"))
            self._dispatch(msg)

        with self._links_lock:
            if self._links.get(peer_name) == (wfile, lock):
                self._links.pop(peer_name, None)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "agent_reply":
            req_id = msg.get("request_id")
            q = self._pending.get(req_id)
            if q is not None:
                q.put(str(msg.get("text", "")))
            return
        for handler in self._handlers:
            handler(msg)

    def on_message(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers.append(handler)

    def _wait_for_link(self, peer_name: str, timeout: float = 30.0) -> tuple[BinaryIO, threading.Lock]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._links_lock:
                link = self._links.get(peer_name)
            if link is not None:
                return link
            time.sleep(0.05)
        raise TimeoutError(f"No mesh link to {peer_name!r} established within {timeout}s")

    def send(self, to: str, **msg: Any) -> None:
        wfile, lock = self._wait_for_link(to)
        _send_line(wfile, lock, {"from": self.name, "to": to, **msg})
        self._messages_sent += 1
        report_messages(agent=self.name, architecture=self.architecture, count=self._messages_sent)

    def reply(self, original_msg: dict[str, Any], text: str) -> None:
        self.send(
            str(original_msg.get("from")),
            type="agent_reply",
            text=text,
            thread_id=original_msg.get("thread_id"),
            request_id=original_msg.get("request_id"),
        )

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
        self._pending[request_id] = q
        try:
            self.send(
                to,
                type="agent_request",
                text=text,
                thread_id=thread_id,
                request_id=request_id,
            )
            return q.get(timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    def ask_many(
        self,
        peer_names: list[str],
        text: str,
        *,
        thread_id: str = "default",
        timeout: float = 600.0,
    ) -> dict[str, str]:
        """Ask several peers the same thing in parallel and wait for all replies."""
        replies: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(peer_names))) as pool:
            future_to_peer = {
                pool.submit(self.ask, peer, text, thread_id=thread_id, timeout=timeout): peer
                for peer in peer_names
            }
            for fut in as_completed(future_to_peer):
                peer = future_to_peer[fut]
                try:
                    replies[peer] = fut.result()
                except Exception as e:
                    replies[peer] = json.dumps({"error": "ask_peer_failed", "peer": peer, "message": str(e)})
        return replies

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
