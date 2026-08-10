from __future__ import annotations

import argparse
import json

from langchain_mcp_adapters.client import MultiServerMCPClient

from ...config import (
    DEFAULT_MESH_BASE_PORT,
    DEFAULT_MESH_CLI_PORT,
    DEFAULT_MESH_HOST,
    MESH_CLI_NAME,
    TB_IDS,
    TB_TO_NAV_ID,
    build_peer_table,
    nav_id_for_tb,
    peer_name_for_robot_id,
    robot_peer_name,
)
from ...mcp_client import build_mcp_connections
from .mesh_bus import MeshNode


def _run_mcp_tool(name: str, args: dict) -> str:
    import asyncio

    async def _call() -> str:
        client = MultiServerMCPClient(build_mcp_connections(), tool_name_prefix=False)
        tools = await client.get_tools()
        by_name = {t.name: t for t in tools}
        t = by_name.get(name)
        if t is None:
            return json.dumps({"error": "tool_not_found", "tool": name})
        return str(await t.ainvoke(args))

    return asyncio.run(_call())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Mission CLI for conflict-based peers. "
            "Assign a solo mission to ONE robot, or inject a conflict event "
            "so only the involved subset opens negotiation."
        )
    )
    parser.add_argument("--host", default=DEFAULT_MESH_HOST)
    parser.add_argument("--base-port", type=int, default=DEFAULT_MESH_BASE_PORT)
    parser.add_argument("--cli-port", type=int, default=DEFAULT_MESH_CLI_PORT)
    parser.add_argument("--name", default=MESH_CLI_NAME)
    args = parser.parse_args()

    peer_table = build_peer_table(TB_IDS, host=args.host, base_port=args.base_port)
    peers = list(peer_table.keys())
    mesh = MeshNode(args.name, args.host, args.cli_port, peer_table)

    print(f"Mission CLI online as {args.name!r} at {args.host}:{args.cli_port}.")
    print(f"Waiting for links to: {', '.join(peers)} ...")
    for peer in peers:
        mesh._wait_for_link(peer, timeout=60.0)
        print(f"  linked: {peer}")
    print()
    tb_help = "|".join(TB_IDS)
    print("Commands:")
    print(f"  mission <{tb_help}> <text>   — solo mission to one peer")
    print("  conflict <tb_a>,<tb_b>[,...] [reason] — emit conflict event (MCP)")
    print("  events [since]                     — show MCP event log")
    print("  stations                           — list stations/boxes")
    print("  help                               — this text")
    print("Ctrl+C to quit.\n")

    try:
        while True:
            line = input("mission> ").strip()
            if not line:
                continue
            if line in {"help", "?"}:
                print(
                    "mission tb1 Go pick box at station_A and drop at station_D\n"
                    "conflict tb1,tb2 bottleneck approach\n"
                    "events 0\n"
                    "stations"
                )
                continue

            if line == "stations":
                print(_run_mcp_tool("list_stations", {}))
                continue

            if line.startswith("events"):
                parts = line.split()
                since = int(parts[1]) if len(parts) > 1 else 0
                print(_run_mcp_tool("get_events", {"since_index": since}))
                continue

            if line.startswith("conflict "):
                rest = line[len("conflict ") :].strip()
                if not rest:
                    print("usage: conflict tb1,tb2 [reason...]")
                    continue
                bits = rest.split(maxsplit=1)
                ids_raw = bits[0]
                reason = bits[1] if len(bits) > 1 else "conflict"
                nav_ids = []
                for tok in ids_raw.split(","):
                    tok = tok.strip()
                    if tok in TB_IDS:
                        nav_ids.append(nav_id_for_tb(tok))
                    elif tok in TB_TO_NAV_ID.values():
                        nav_ids.append(tok)
                    else:
                        peer = peer_name_for_robot_id(tok)
                        if peer and peer.startswith("robot_"):
                            tb = peer.replace("robot_", "", 1)
                            if tb in TB_IDS:
                                nav_ids.append(nav_id_for_tb(tb))
                                continue
                        print(f"unknown robot token: {tok}")
                        nav_ids = []
                        break
                if not nav_ids:
                    continue
                print(_run_mcp_tool("emit_conflict", {"robot_ids": ",".join(nav_ids), "reason": reason}))
                continue

            if line.startswith("mission "):
                rest = line[len("mission ") :].strip()
                parts = rest.split(maxsplit=1)
                if len(parts) < 2:
                    print(f"usage: mission <{'|'.join(TB_IDS)}> <text>")
                    continue
                target_tok, text = parts[0], parts[1]
                if target_tok in TB_IDS:
                    peer = robot_peer_name(target_tok)
                    nav = nav_id_for_tb(target_tok)
                else:
                    peer = peer_name_for_robot_id(target_tok)
                    nav = TB_TO_NAV_ID.get(target_tok.replace("robot_", ""), target_tok)
                    if peer is None:
                        print(f"unknown target: {target_tok}")
                        continue
                mission = (
                    f"SOLO MISSION (work alone; negotiate only if an event opens):\n{text}\n"
                    f"Use robot_id '{nav}' for navigate_to_pose / pickup_box / drop_box."
                )
                print(f"... sending solo mission to {peer} ...")
                reply = mesh.ask(peer, mission, thread_id="mission")
                print(f"[{peer}] {reply}\n")
                continue

            print("Unknown command. Type 'help'.")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
