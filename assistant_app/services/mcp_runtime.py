from __future__ import annotations

import asyncio
from collections.abc import Collection
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from assistant_app.core.config import Settings


class MCPRuntimeError(RuntimeError):
    """A safe, user-facing failure raised by an MCP service call."""


def _text_content(result: Any, max_chars: int) -> str:
    if bool(getattr(result, "isError", False)):
        raise MCPRuntimeError("MCP 工具返回失败")
    parts = [
        str(block.text)
        for block in getattr(result, "content", [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    text = "\n".join(parts).strip()
    if not text:
        raise MCPRuntimeError("MCP 工具没有返回可用文本")
    if len(text) > max_chars:
        return f"{text[:max_chars]}\n\n[内容过长，已安全截断]"
    return text


async def call_mcp_tool(
    settings: Settings,
    server_url: str,
    tool_name: str,
    arguments: dict[str, object],
    allowed_tools: Collection[str],
) -> str:
    """Call one explicitly allowlisted MCP tool over Streamable HTTP."""

    if not settings.mcp_enabled:
        raise MCPRuntimeError("MCP 能力当前未启用")
    if tool_name not in allowed_tools:
        raise MCPRuntimeError("MCP 工具不在允许列表中")

    async def invoke() -> str:
        async with streamable_http_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return _text_content(result, settings.mcp_max_result_chars)

    try:
        async with asyncio.timeout(settings.mcp_timeout_seconds):
            return await invoke()
    except TimeoutError as exc:
        raise MCPRuntimeError("MCP 文档解析超时") from exc
    except MCPRuntimeError:
        raise
    except Exception as exc:
        raise MCPRuntimeError(f"MCP 服务不可用（{type(exc).__name__}）") from exc


async def list_mcp_tools(settings: Settings, server_url: str) -> list[str]:
    """Return tool names for a configured MCP server without exposing schemas."""

    if not settings.mcp_enabled:
        return []

    async def inspect() -> list[str]:
        async with streamable_http_client(server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return sorted(str(tool.name) for tool in result.tools)

    try:
        async with asyncio.timeout(min(settings.mcp_timeout_seconds, 8.0)):
            return await inspect()
    except Exception:
        return []
