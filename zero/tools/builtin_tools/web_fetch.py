"""WebFetchTool — fetch content from a URL through the SSRF net_guard.

All URLs pass through :func:`zero.security.net_guard.check_url` which
blocks requests to private IP ranges, localhost, cloud metadata
endpoints, etc. This prevents server-side request forgery (SSRF) attacks.

Requires standard approval because network access is high-risk.
"""
from __future__ import annotations

from typing import Any

from zero.security.net_guard import check_url
from zero.tools.base import Tool, ToolContext, ToolSpec

__all__ = ["WebFetchTool", "WEB_FETCH_SCHEMA", "register"]


WEB_FETCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "HTTPS URL to fetch"},
        "max_chars": {"type": "integer", "description": "Max chars to return", "default": 10000},
    },
    "required": ["url"],
}


class WebFetchTool(Tool):
    """Fetch a URL through the SSRF net_guard."""

    spec = ToolSpec(
        name="web_fetch",
        description="Fetch content from a URL (passes through SSRF net_guard)",
        parameters_schema=WEB_FETCH_SCHEMA,
        required_permissions=frozenset({"sandbox.exec"}),
        approval_level="standard",
    )

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> str:
        url = str(args["url"])
        max_chars = int(args.get("max_chars", 10000))

        # SSRF check — required by ADR T-8.9.
        try:
            check_url(url)
        except Exception as e:
            return f"[TOOL_ERROR] SSRF check failed: {e}"

        # Fetch with timeout.
        import httpx  # noqa: PLC0415

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                text = resp.text
        except httpx.HTTPError as e:
            return f"[TOOL_ERROR] HTTP error: {e}"

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n[truncated: {len(text) - max_chars} more chars]"
        return text


def register() -> None:
    """Register the WebFetchTool with the global tool registry."""
    from zero.tools.builtin_tools._helpers import register_tool  # noqa: PLC0415

    register_tool(WebFetchTool())
