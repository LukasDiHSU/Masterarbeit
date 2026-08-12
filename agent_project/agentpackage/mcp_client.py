from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

ROBOT_SERVER_NAME = "robot_tools"


def build_mcp_connections() -> dict:
    url = os.getenv("AGENT_MCP_URL", "http://127.0.0.1:8000/sse").strip()
    return {
        ROBOT_SERVER_NAME: {
            "transport": "sse",
            "url": url,
        }
    }


async def load_mcp_tools_async() -> list[BaseTool]:
    client = MultiServerMCPClient(
        build_mcp_connections(),
        tool_name_prefix=False,
    )
    return await client.get_tools()


def load_mcp_tools_sync() -> list[BaseTool]:
    async def _run() -> list[BaseTool]:
        return await load_mcp_tools_async()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_run())).result()


def load_mcp_tools_safe() -> list[BaseTool]:
    try:
        return load_mcp_tools_sync()
    except Exception as e:
        logger.warning("MCP tools unavailable (%s); continuing without MCP.", e)
        return []


# Read-only world/map/station tools for lead agents (master / HMAS-2 planner).
# No navigate / pickup / drop — robots do physical work.
PLANNING_MAP_TOOL_NAMES = frozenset(
    {
        "list_worlds",
        "get_map_info",
        "list_stations",
        "list_available_boxes",
        "get_station",
        "get_held_boxes",
        "get_all_robot_poses",
    }
)


def load_planning_mcp_tools() -> list[BaseTool]:
    """MCP tools lead agents may use to inspect the map before planning."""
    return [t for t in load_mcp_tools_safe() if t.name in PLANNING_MAP_TOOL_NAMES]
