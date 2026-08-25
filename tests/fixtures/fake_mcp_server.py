"""A minimal MCP stdio server used by GAP 7 tests.

Speaks the JSON-RPC 2.0 MCP transport: initialize handshake,
notifications/initialized, tools/list, tools/call. Exposes a single
`add` tool that returns the sum of two integers.
"""

from __future__ import annotations

import json
import sys


def respond(request: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    if request_id is None:
        return None  # notification
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-adder", "version": "1.0"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "add",
                        "description": "Add two numbers.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "a": {"type": "integer"},
                                "b": {"type": "integer"},
                            },
                            "required": ["a", "b"],
                        },
                    }
                ]
            },
        }
    if method == "tools/call":
        params = request.get("params") or {}
        args = params.get("arguments") or {}
        total = int(args.get("a", 0)) + int(args.get("b", 0))
        if int(args.get("b", 0)) < 0:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "negative b unsupported"}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"sum={total}"}],
                "isError": False,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unknown method {method!r}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = respond(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
