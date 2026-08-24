from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

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


# Lead agents (master / planners): occupancy only. Object x/y come from sensing.
Q1_PLANNING_MAP_TOOL_NAMES = frozenset({"get_occupancy_map"})


def load_planning_mcp_tools() -> list[BaseTool]:
    """MCP tools lead agents may use to inspect the map before planning."""
    return [t for t in load_mcp_tools_safe() if t.name in Q1_PLANNING_MAP_TOOL_NAMES]


def parse_mcp_payload(raw: Any) -> Any:
    """Decode an MCP tool result into plain Python.

    Adapters return content blocks such as ``[{"type": "text", "text": "{...}"}]``;
    treating that list as the payload silently loses the result, so unwrap the
    text blocks before falling back to plain JSON decoding.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        texts: list[str] = []
        for block in raw:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
        if texts:
            parsed = parse_mcp_payload("\n".join(texts))
            if parsed is not None:
                return parsed
        return raw
    text = str(raw).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


_tools_by_name: dict[str, BaseTool] | None = None
_tools_lock = threading.Lock()


def mcp_tools_by_name(*, refresh: bool = False) -> dict[str, BaseTool]:
    """Process-wide cache of MCP tools keyed by tool name."""
    global _tools_by_name
    with _tools_lock:
        if _tools_by_name is None or refresh:
            _tools_by_name = {t.name: t for t in load_mcp_tools_safe()}
        return _tools_by_name


def call_mcp_tool(name: str, args: dict[str, Any] | None = None) -> Any:
    """Call one MCP tool synchronously from any thread and decode the result."""
    tool = mcp_tools_by_name().get(name)
    if tool is None:
        return {"error": "mcp_tool_unavailable", "tool": name}
    payload = args or {}
    try:
        raw = tool.invoke(payload)
    except NotImplementedError:
        raw = None
    except Exception as e:
        if not _looks_like_sync_unsupported(e):
            return {"error": "mcp_call_failed", "tool": name, "message": str(e)}
        raw = None
    if raw is None:
        try:
            raw = asyncio.run(tool.ainvoke(payload))
        except Exception as e:
            return {"error": "mcp_call_failed", "tool": name, "message": str(e)}
    parsed = parse_mcp_payload(raw)
    if parsed is None:
        return {"error": "mcp_unparsable_result", "tool": name, "raw": str(raw)[:400]}
    return parsed


def _looks_like_sync_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    return "async" in text or "coroutine" in text or "not support" in text
