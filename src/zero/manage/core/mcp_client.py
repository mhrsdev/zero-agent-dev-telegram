"""MCP client: Model Context Protocol servers as Zero tools (GAP 7).

Per ``docs/gap-designs/GAP-07-mcp-plugins.md``: each configured MCP
server runs as a child process speaking JSON-RPC 2.0 over stdio (the
MCP stdio transport). At startup the manager performs the
``initialize`` handshake, discovers tools via ``tools/list``, and
registers each one into :class:`ToolService` as
``mcp_<server>_<tool>`` (``[^A-Za-z0-9_]`` sanitized). Invocations flow
through the standard capability/audit/redaction pipeline unchanged.

The wire client here speaks the MCP protocol directly so the control
plane has no hard runtime dependency on any particular SDK; servers
themselves are free to use one. Servers are operator-configured and
disabled unless ``ZERO_MCP_SERVERS`` names them explicitly:

    ZERO_MCP_SERVERS=[{"name":"filesystem","command":["npx","-y",
    "@modelcontextprotocol/server-filesystem","/tmp"],"enabled":true}]

A failing server is logged and skipped — extension loading can never
crash the application.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading

logger = logging.getLogger(__name__)

_MCP_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "zero-develop", "version": "0.1.0"}
_REQUEST_TIMEOUT_SECONDS = 30.0


def sanitize_name_component(raw: str) -> str:
    """Map anything outside [A-Za-z0-9_] to '_' (Claude Code parity)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", raw or "")


def mcp_tool_name(server: str, tool: str) -> str:
    return f"mcp_{sanitize_name_component(server)}_{sanitize_name_component(tool)}"


def parse_server_config(raw_json: str | None) -> list[dict]:
    """Parse ZERO_MCP_SERVERS JSON; malformed input disables all servers."""
    if not raw_json or not raw_json.strip():
        return []
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("ZERO_MCP_SERVERS is not valid JSON; ignoring")
        return []
    if not isinstance(parsed, list):
        logger.warning("ZERO_MCP_SERVERS must be a JSON array; ignoring")
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


class MCPServerProcess:
    """One MCP stdio server child process."""

    def __init__(self, *, name: str, command: list[str]) -> None:
        self.name = name
        self._command = [str(part) for part in command]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self.tools: list[dict] = []

    def connect(self) -> bool:
        """Spawn the process and perform the initialize handshake."""
        try:
            self._proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("MCP server %r failed to start: %s", self.name, exc)
            return False
        result = self._request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        if result is None:
            self.shutdown()
            return False
        self._notify("notifications/initialized", {})
        listing = self._request("tools/list", {})
        if not isinstance(listing, dict) or not isinstance(listing.get("tools"), list):
            logger.warning("MCP server %r returned no tool list", self.name)
            return False
        self.tools = [
            tool for tool in listing["tools"] if isinstance(tool, dict) and tool.get("name")
        ]
        return True

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Invoke one remote tool; returns joined text content."""
        result = self._request("tools/call", {"name": tool_name, "arguments": dict(arguments)})
        if not isinstance(result, dict):
            raise TypeError(f"MCP tool {tool_name!r} returned no result")
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {tool_name!r} reported an error")
        parts: list[str] = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)

    def shutdown(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------

    def _send(self, payload: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise BrokenPipeError(str(exc)) from exc

    def _read_message(self) -> dict | None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = self._proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                return message

    def _request(self, method: str, params: dict) -> dict | None:
        with self._lock:
            if self._proc is None:
                return None
            request_id = self._next_id
            self._next_id += 1
            try:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                deadline_guard = 0
                while deadline_guard < 64:
                    deadline_guard += 1
                    message = self._read_message()
                    if message is None:
                        return None
                    if message.get("id") == request_id:
                        if "error" in message:
                            logger.warning(
                                "MCP server %r error for %s: %s",
                                self.name,
                                method,
                                message["error"],
                            )
                            return None
                        return message.get("result")
            except (BrokenPipeError, OSError) as exc:
                logger.warning("MCP server %r pipe failure: %s", self.name, exc)
                return None
        return None

    def _notify(self, method: str, params: dict) -> None:
        with self._lock:
            if self._proc is None:
                return
            try:
                self._send({"jsonrpc": "2.0", "method": method, "params": params})
            except (BrokenPipeError, OSError):
                pass


class MCPManager:
    """Owns every configured MCP server process and its registrations."""

    def __init__(self) -> None:
        self.servers: dict[str, MCPServerProcess] = {}

    def load_from_env(self) -> int:
        entries = parse_server_config(os.environ.get("ZERO_MCP_SERVERS"))
        return self.load(entries)

    def load(self, entries: list[dict]) -> int:
        """Connect every enabled server; returns connected-server count."""
        registered = 0
        for entry in entries:
            name = str(entry.get("name") or "").strip()
            command = entry.get("command")
            enabled = entry.get("enabled", True)
            if not name or not isinstance(command, list) or not command:
                logger.warning("MCP server entry missing name/command; skipped")
                continue
            if not enabled:
                continue
            server = MCPServerProcess(name=name, command=command)
            if not server.connect():
                server.shutdown()
                continue
            self.servers[name] = server
            registered += 1
        return registered

    def register_tools(self, tool_service) -> list[str]:
        """Register discovered tools through the standard pipeline."""
        names: list[str] = []
        for server_name, server in sorted(self.servers.items()):
            for tool in server.tools:
                tool_name = tool.get("name") or ""
                registered_name = mcp_tool_name(server_name, str(tool_name))
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}

                def make_handler(srv: MCPServerProcess, remote: str):
                    def handler(input_data, context):
                        output = srv.call_tool(remote, dict(input_data))
                        return {"output": output[:20_000]}

                    return handler

                try:
                    tool_service.register_tool(
                        name=registered_name,
                        description=str(tool.get("description") or ""),
                        input_schema=schema,
                        output_schema={"type": "object"},
                        handler_key=f"mcp:{registered_name}",
                        handler=make_handler(server, str(tool_name)),
                        inline=True,
                    )
                    names.append(registered_name)
                except Exception as exc:  # noqa: BLE001 - never crash startup
                    logger.warning(
                        "MCP tool %r could not be registered: %s",
                        registered_name,
                        type(exc).__name__,
                    )
        return names

    def shutdown(self) -> None:
        for server in self.servers.values():
            server.shutdown()
        self.servers.clear()


_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager


__all__ = [
    "MCPManager",
    "MCPServerProcess",
    "get_mcp_manager",
    "mcp_tool_name",
    "parse_server_config",
    "sanitize_name_component",
]
