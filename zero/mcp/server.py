"""MCP server — exposes Zero's tools to external MCP clients.

Useful when you want Claude Desktop, Cursor, or another AI agent to use
Zero's scoped memory, audit log, or approval workflow.

Per ADR 0004: Zero owns Telegram/Session/Memory/Agent. Exposing them via
MCP lets external clients benefit from Zero's scope enforcement.

Run as a stdio subprocess:
    zero mcp serve

Then in Claude Desktop's config:
    {
      "mcpServers": {
        "zero": {
          "command": "zero",
          "args": ["mcp", "serve"]
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zero.core.logging import get_logger
from zero.core.scope import Scope
from zero.tools.base import ToolContext

__all__ = [
    "McpServerTool",
    "McpServer",
    "serve_stdio",
    "PROTOCOL_VERSION",
]

_log = get_logger("zero.mcp.server")

PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True, slots=True)
class McpServerTool:
    """A tool exposed by Zero's MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[str]]


class McpServer:
    """MCP server that exposes Zero's tools to external clients.

    Usage:
        >>> server = McpServer()
        >>> server.register_tool(McpServerTool(
        ...     name="memory_search",
        ...     description="Search Zero memory in a scope",
        ...     input_schema={...},
        ...     handler=my_handler,
        ... ))
        >>> await server.run_stdio()
    """

    def __init__(self, *, server_name: str = "zero-v2", version: str = "0.1.0") -> None:
        self._server_name = server_name
        self._version = version
        self._tools: dict[str, McpServerTool] = {}

    def register_tool(self, tool: McpServerTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        """Return OpenAI/MCP-format tool list."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        scope: Scope,
        actor_id: str = "mcp_external",
    ) -> str:
        """Invoke a registered tool. Raises if tool not found."""
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"tool {name!r} not registered")
        ctx = ToolContext(
            scope=scope,
            actor_id=actor_id,
            tool_call_id=f"mcp_{name}_{id(arguments)}",
        )
        return await tool.handler(arguments, ctx)

    async def run_stdio(self) -> None:
        """Run the MCP server over stdio (JSON-RPC)."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
            lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()),
            sys.stdout,
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, None, asyncio.get_event_loop())

        _log.info(f"MCP server {self._server_name!r} running on stdio")

        buffer = b""
        while True:
            try:
                chunk = await reader.read(4096)
            except (asyncio.CancelledError, ConnectionError):
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    _log.warning(f"invalid JSON-RPC line: {e}")
                    continue
                response = await self._handle_message(msg)
                if response is not None:
                    out = (json.dumps(response) + "\n").encode("utf-8")
                    writer.write(out)
                    await writer.drain()

    async def _handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC message. Returns response or None (for notifications)."""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": self._server_name,
                            "version": self._version,
                        },
                    },
                }
            if method == "notifications/initialized":
                # Notification — no response.
                return None
            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": self.list_tools()},
                }
            if method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                # Build a default scope (PERSONAL, external MCP client).
                # External MCP clients don't have a Telegram chat context.
                scope = Scope.personal(user_id="usr_mcp_external").with_default_memory_scope()
                try:
                    result_text = await self.call_tool(
                        tool_name, arguments, scope=scope,
                    )
                except Exception as e:
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {e}"}],
                            "isError": True,
                        },
                    }
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": result_text}],
                        "isError": False,
                    },
                }
            # Unknown method.
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"method not found: {method!r}",
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": f"internal error: {e}"},
            }


async def serve_stdio(server: McpServer) -> None:
    """Run an McpServer over stdio. Convenience function."""
    await server.run_stdio()
