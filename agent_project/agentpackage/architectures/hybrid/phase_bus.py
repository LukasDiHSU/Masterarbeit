
from __future__ import annotations

from typing import Any, Callable

from ..centralized.agent_bus import BusClient, DEFAULT_HOST, DEFAULT_PORT
from ..decentralized.mesh_bus import MeshNode, PeerTable

ACTIVATE_MESH = "activate_mesh"


class PhasedBus:
    """Starts in centralized (star, via broker) mode. When an
    ``activate_mesh`` control message is received -- or triggered locally by
    the planner -- it boots a :class:`MeshNode` and transparently starts
    routing traffic to any peer it knows about directly, decentralized-style,
    while keeping the star connection alive as a fallback / control channel.
    """

    def __init__(self, name: str, *, broker_host: str = DEFAULT_HOST, broker_port: int = DEFAULT_PORT):
        self.name = name
        self._handlers: list[Callable[[dict[str, Any]], None]] = []
        self.star = BusClient(name, host=broker_host, port=broker_port, architecture="hybrid")
        self.mesh: MeshNode | None = None
        self.star.on_message(self._on_star_message)

    @property
    def mesh_active(self) -> bool:
        return self.mesh is not None

    def on_message(self, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers.append(handler)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        for handler in self._handlers:
            handler(msg)

    def _on_star_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == ACTIVATE_MESH:
            peers = {n: (h, int(p)) for n, (h, p) in msg["peers"].items()}
            self.activate_mesh(peers)
            return
        self._dispatch(msg)

    def activate_mesh(self, peer_table: PeerTable) -> None:
        """Boot this node's own mesh listener and connect it to every peer
        in ``peer_table`` (which must include an entry for ``self.name``)."""
        if self.mesh is not None:
            return
        peers = dict(peer_table)
        my_host, my_port = peers.pop(self.name)
        self.mesh = MeshNode(self.name, my_host, my_port, peers, architecture="hybrid")
        self.mesh.on_message(self._dispatch)

    def broadcast_activate_mesh(self, peer_table: PeerTable) -> None:
        """Planner-only: tell every other agent in ``peer_table`` to switch
        to decentralized mesh mode, then switch itself too."""
        serializable = {n: list(hp) for n, hp in peer_table.items()}
        for peer_name in peer_table:
            if peer_name == self.name:
                continue
            self.star.send(type=ACTIVATE_MESH, to=peer_name, peers=serializable)
        self.activate_mesh(peer_table)

    def ask(self, to: str, text: str, *, thread_id: str = "default", timeout: float = 600.0) -> str:
        if self.mesh is not None and to in self.mesh.peers:
            return self.mesh.ask(to, text, thread_id=thread_id, timeout=timeout)
        return self.star.ask(to, text, thread_id=thread_id, timeout=timeout)

    def ask_many(self, peer_names: list[str], text: str, **kw: Any) -> dict[str, str]:
        if self.mesh is not None and all(p in self.mesh.peers for p in peer_names):
            return self.mesh.ask_many(peer_names, text, **kw)
        replies: dict[str, str] = {}
        for peer in peer_names:
            replies[peer] = self.ask(peer, text, **kw)
        return replies

    def send(self, **msg: Any) -> None:
        to = msg.get("to")
        if self.mesh is not None and to in self.mesh.peers:
            rest = {k: v for k, v in msg.items() if k != "to"}
            self.mesh.send(to, **rest)
            return
        self.star.send(**msg)

    def reply(self, original_msg: dict[str, Any], text: str) -> None:
        src = str(original_msg.get("from"))
        if self.mesh is not None and src in self.mesh.peers:
            self.mesh.reply(original_msg, text)
            return
        self.star.send(
            type="agent_reply",
            to=src,
            text=text,
            thread_id=original_msg.get("thread_id"),
            request_id=original_msg.get("request_id"),
        )

    def close(self) -> None:
        self.star.close()
        if self.mesh is not None:
            self.mesh.close()
