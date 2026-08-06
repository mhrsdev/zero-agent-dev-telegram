"""Zero v2 MCP (Model Context Protocol) server support.

Two roles:
    1. **MCP Client** — Zero connects to external MCP servers (stdio or SSE)
       and exposes their tools to the agent loop via the tool registry.
    2. **MCP Server** — Zero exposes its own tools to external MCP clients
       (e.g. Claude Desktop, other AI agents) so they can call Zero's
       scoped memory, audit, and approval tools.

Per ADR 0004: Zero is a tool consumer. Per T-7.5: malicious tools need
approval; tool output is untrusted data.

MCP transport per T-7.5 (token-economy-design.md §3):
    - First call: only name + short description sent to model
    - Full schema loaded only when Agent actually calls
    - Deferred tool loading reduces context bloat
"""
from __future__ import annotations

from zero.mcp.client import (
    McpClient,
    McpServerConfig,
    McpToolDefinition,
    McpTransport,
    StdioMcpTransport,
    SseMcpTransport,
)
from zero.mcp.server import McpServer, McpServerTool, serve_stdio
from zero.mcp.security import (
    McpSecurityError,
    scan_mcp_command_for_persistence,
    scan_mcp_command_for_exfiltration,
)

__all__ = [
    "McpClient",
    "McpServerConfig",
    "McpToolDefinition",
    "McpTransport",
    "StdioMcpTransport",
    "SseMcpTransport",
    "McpServer",
    "McpServerTool",
    "serve_stdio",
    "McpSecurityError",
    "scan_mcp_command_for_persistence",
    "scan_mcp_command_for_exfiltration",
]
