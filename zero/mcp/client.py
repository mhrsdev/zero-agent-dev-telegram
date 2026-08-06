"""MCP client — connects Zero to external MCP servers.

Per ADR T-7.5: tool output is untrusted data; malicious tools need approval.

Two transports:
    - ``StdioMcpTransport`` — spawn a subprocess, communicate via JSON-RPC
      over stdin/stdout. Per Hermes pattern: stderr redirected to a log file.
    - ``SseMcpTransport`` — connect to an HTTP/SSE MCP server.

The client exposes MCP tools to Zero's tool registry so the agent loop can
call them like any builtin tool.
"""
from __future__ import annotations

import abc
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator  # noqa: F401  # re-exported by module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zero.core.logging import get_logger
from zero.core.scope import Scope
from zero.mcp.security import McpSecurityError, scan_mcp_server_config
from zero.tools.base import ToolContext, ToolError, ToolSpec

__all__ = [
    "McpTransport",
    "StdioMcpTransport",
    "SseMcpTransport",
    "McpServerConfig",
    "McpToolDefinition",
    "McpClient",
    "McpClientError",
]

_log = get_logger("zero.mcp.client")


class McpClientError(RuntimeError):
    """Raised on MCP client errors."""


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """Configuration for an MCP server connection.

    Per Hermes pattern: ``command`` + ``args`` for stdio, or ``url`` for SSE.
    """

    name: str  # short identifier (e.g. "github", "filesystem")
    transport: str  # "stdio" | "sse"
    command: str | None = None  # for stdio
    args: list[str] = field(default_factory=list)  # for stdio
    env: dict[str, str] = field(default_factory=dict)  # for stdio
    url: str | None = None  # for sse
    headers: dict[str, str] = field(default_factory=dict)  # for sse
    # If True, all tool calls require approval before dispatch.
    require_approval: bool = True
    # If True, scan command for exfiltration/persistence patterns at spawn.
    security_scan: bool = True
    # Working directory for stdio subprocess.
    cwd: str | None = None
    # Timeout for tool calls (seconds).
    call_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.transport not in ("stdio", "sse"):
            raise ValueError(f"transport must be 'stdio' or 'sse', got {self.transport!r}")
        if self.transport == "stdio" and self.command is None:
            raise ValueError("stdio transport requires 'command'")
        if self.transport == "sse" and self.url is None:
            raise ValueError("sse transport requires 'url'")


@dataclass(frozen=True, slots=True)
class McpToolDefinition:
    """A single MCP tool exposed by a server."""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema

    def to_tool_spec(self, *, server_name: str) -> ToolSpec:
        """Convert to a Zero ToolSpec for the tool registry."""
        # Prefix the tool name with the server name to avoid collisions.
        full_name = f"mcp_{server_name}_{self.name}"
        return ToolSpec(
            name=full_name,
            description=f"[MCP/{server_name}] {self.description}",
            parameters_schema=self.input_schema,
            approval_level="standard",  # MCP tools always require standard approval
            untrusted_output=True,  # tool output is untrusted data
        )


# ---------------------------------------------------------------------- transport

class McpTransport(abc.ABC):
    """Abstract MCP transport."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the transport (spawn subprocess or open HTTP connection)."""
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the transport cleanly."""
        ...

    @abc.abstractmethod
    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        ...

    @abc.abstractmethod
    async def is_alive(self) -> bool:
        """Check if the transport is still connected."""
        ...


# ---------------------------------------------------------------------- stdio transport

class StdioMcpTransport(McpTransport):
    """MCP transport over subprocess stdin/stdout (JSON-RPC).

    Per Hermes pattern: stderr is captured to a log file to avoid corrupting
    the TUI.
    """

    def __init__(self, config: McpServerConfig) -> None:
        if config.transport != "stdio":
            raise ValueError(f"StdioMcpTransport requires transport='stdio', got {config.transport!r}")
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_log: Path | None = None
        self._stderr_file: Any = None

    async def start(self) -> None:
        # Security scan BEFORE spawning.
        if self._config.security_scan:
            findings = scan_mcp_server_config(
                command=self._config.command or "",
                args=self._config.args,
                env=self._config.env,
            )
            if findings:
                raise McpSecurityError(
                    f"MCP server {self._config.name!r} failed security scan: "
                    + "; ".join(f"{f.pattern_id}: {f.description}" for f in findings)
                )

        # Redirect stderr to a log file (per Hermes pattern).
        log_dir = Path.home() / ".zero" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_log = log_dir / f"mcp-{self._config.name}-stderr.log"
        self._stderr_file = self._stderr_log.open("ab")

        # Spawn subprocess.
        env = dict(os.environ)
        env.update(self._config.env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._config.command or "",
                *self._config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._stderr_file,
                cwd=self._config.cwd,
                env=env,
            )
        except FileNotFoundError as e:
            self._stderr_file.close()
            raise McpClientError(
                f"MCP server command not found: {self._config.command!r} — {e}"
            ) from e
        except PermissionError as e:
            self._stderr_file.close()
            raise McpClientError(
                f"permission denied executing MCP command: {self._config.command!r} — {e}"
            ) from e

        # Start reading stdout for JSON-RPC responses.
        self._reader_task = asyncio.create_task(self._read_loop())

        # Initialize the MCP session.
        try:
            await self._initialize()
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._process is not None and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError):
                try:
                    self._process.kill()
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except (TimeoutError, ProcessLookupError, asyncio.CancelledError):
                    pass
        self._process = None
        # Close stderr log file.
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except OSError:
                pass
            self._stderr_file = None

    async def _read_loop(self) -> None:
        """Read JSON-RPC messages from subprocess stdout."""
        if self._process is None or self._process.stdout is None:
            return
        reader = self._process.stdout
        buffer = b""
        while True:
            try:
                chunk = await reader.read(4096)
            except (asyncio.CancelledError, ConnectionError):
                break
            if not chunk:
                break
            buffer += chunk
            # Parse newline-delimited JSON.
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    _log.warning(f"unreadable MCP line: {line[:200]!r}")
                    continue
                await self._handle_message(msg)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """Route a JSON-RPC response to its pending future."""
        msg_id = msg.get("id")
        if msg_id is None:
            # Notification — not handled (only request/response).
            return
        future = self._pending.pop(msg_id, None)
        if future is None:
            return
        if "error" in msg:
            future.set_exception(McpClientError(f"MCP error: {msg['error']}"))
        else:
            future.set_result(msg.get("result"))

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> Any:
        if self._process is None or self._process.stdin is None:
            raise McpClientError("transport not started")
        self._request_id += 1
        req_id = self._request_id
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        # Write message (newline-delimited).
        data = (json.dumps(msg) + "\n").encode("utf-8")
        try:
            self._process.stdin.write(data)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            self._pending.pop(req_id, None)
            raise McpClientError(f"failed to send MCP request: {e}") from e

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise McpClientError(f"MCP request {method!r} timed out after {timeout}s") from None

    async def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _initialize(self) -> None:
        """Send the MCP initialize request."""
        result = await self.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "zero-v2",
                    "version": "0.1.0",
                },
            },
            timeout=10.0,
        )
        # Send initialized notification.
        if self._process is not None and self._process.stdin is not None:
            notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            data = (json.dumps(notif) + "\n").encode("utf-8")
            try:
                self._process.stdin.write(data)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        _ = result  # ignore result


# ---------------------------------------------------------------------- SSE transport

class SseMcpTransport(McpTransport):
    """MCP transport over HTTP Server-Sent Events.

    NOTE: This is a minimal implementation. Real MCP over SSE uses a
    specific request/response pattern (POST to /messages, SSE for server
    notifications).
    """

    def __init__(self, config: McpServerConfig) -> None:
        if config.transport != "sse":
            raise ValueError(f"SseMcpTransport requires transport='sse', got {config.transport!r}")
        self._config = config
        self._client: Any = None  # httpx.AsyncClient
        self._initialized = False
        self._request_id = 0

    async def start(self) -> None:
        import httpx  # noqa: PLC0415

        if self._config.url is None:
            raise McpClientError("SSE transport requires url")
        self._client = httpx.AsyncClient(
            base_url=self._config.url,
            headers=self._config.headers,
            timeout=self._config.call_timeout_seconds,
        )
        # Initialize.
        await self.send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "zero-v2", "version": "0.1.0"},
            },
            timeout=10.0,
        )
        self._initialized = True

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 60.0,
    ) -> Any:
        if self._client is None:
            raise McpClientError("transport not started")
        self._request_id += 1
        req_id = self._request_id
        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        try:
            resp = await self._client.post("/messages", json=msg, timeout=timeout)
        except Exception as e:
            raise McpClientError(f"SSE request failed: {e}") from e
        if resp.status_code >= 400:
            raise McpClientError(f"SSE returned {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as e:
            raise McpClientError(f"invalid JSON response: {e}") from e
        if "error" in payload:
            raise McpClientError(f"MCP error: {payload['error']}")
        return payload.get("result")

    async def is_alive(self) -> bool:
        return self._client is not None and self._initialized


# ---------------------------------------------------------------------- client

class McpClient:
    """High-level MCP client — manages transport + tool discovery.

    Usage:
        >>> client = McpClient(McpServerConfig(
        ...     name="github",
        ...     transport="stdio",
        ...     command="npx",
        ...     args=["-y", "@modelcontextprotocol/server-github"],
        ...     env={"GITHUB_TOKEN": "secret://env/GITHUB_TOKEN"},  # will be resolved
        ... ))
        >>> await client.connect()
        >>> tools = await client.list_tools()
        >>> result = await client.call_tool("create_issue", {"repo": "owner/repo", ...}, ctx)
        >>> await client.disconnect()
    """

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self._transport: McpTransport | None = None
        self._tools: list[McpToolDefinition] = []
        self._connected = False

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        return self._connected and self._transport is not None and await_sync(self._transport.is_alive())

    @property
    def tools(self) -> list[McpToolDefinition]:
        return list(self._tools)

    async def connect(self) -> None:
        """Start the transport and discover tools."""
        if self._config.transport == "stdio":
            self._transport = StdioMcpTransport(self._config)
        elif self._config.transport == "sse":
            self._transport = SseMcpTransport(self._config)
        else:  # pragma: no cover  # validated in McpServerConfig
            raise ValueError(f"unknown transport: {self._config.transport}")

        await self._transport.start()
        self._connected = True

        # Discover tools.
        await self.refresh_tools()
        _log.info(
            f"MCP server {self._config.name!r} connected — {len(self._tools)} tools available",
        )

    async def disconnect(self) -> None:
        if self._transport is not None:
            await self._transport.stop()
            self._transport = None
        self._connected = False
        self._tools = []

    async def refresh_tools(self) -> list[McpToolDefinition]:
        """Re-fetch the tool list from the server."""
        if self._transport is None:
            raise McpClientError("not connected")
        result = await self._transport.send_request(
            "tools/list",
            {},
            timeout=self._config.call_timeout_seconds,
        )
        tools: list[McpToolDefinition] = []
        for t in (result or {}).get("tools", []):
            tools.append(McpToolDefinition(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
            ))
        self._tools = tools
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolContext,
    ) -> str:
        """Call an MCP tool. Returns the tool's text output.

        Per T-7.5: tool output is untrusted data (caller must quote as data).
        """
        if self._transport is None:
            raise McpClientError("not connected")
        if not any(t.name == tool_name for t in self._tools):
            raise ToolError(f"MCP tool {tool_name!r} not in server {self._config.name!r}")

        _log.info(
            f"MCP tool call: server={self._config.name!r} tool={tool_name!r} "
            f"scope={ctx.scope.retrieval_key()} actor={ctx.actor_id}",
        )

        try:
            result = await self._transport.send_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                timeout=self._config.call_timeout_seconds,
            )
        except McpClientError as e:
            raise ToolError(f"MCP call failed: {e}") from e

        # MCP result shape: {content: [{type: "text", text: "..."}, ...], isError: bool}
        if not isinstance(result, dict):
            return str(result)

        if result.get("isError"):
            content = result.get("content", [])
            err_text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            raise ToolError(f"MCP tool error: {err_text or 'unknown error'}")

        content = result.get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "\n".join(texts) if texts else ""


def await_sync(coro: Any) -> Any:
    """Best-effort synchronous check — returns False if no event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Can't await — assume alive.
            return True
        return loop.run_until_complete(coro)
    except RuntimeError:
        return False
