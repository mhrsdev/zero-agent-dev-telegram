"""Text-protocol tool calling for gateways WITHOUT native function calling.

Live-run root cause (2026-08-30): the operator's gateway
(api.justwoker.icu) accepts the OpenAI ``tools`` parameter and then
SILENTLY DROPS it — every streamed and non-streamed response carries
``tool_calls: []`` and the model answers by HALLUCINATING search
results in plain text ("I'll search for ... Here are the search
results"). Verified against the gateway directly on BOTH the
OpenAI ``/chat/completions`` and the Anthropic ``/v1/messages``
protocol: no ``tool_calls`` / no ``tool_use`` block ever comes back.

Native function calling is therefore UNAVAILABLE on this deployment.
The honest alternatives were "tools never work" or a real text
protocol. This module implements the text protocol:

- the model receives the tool surface in the SYSTEM message with a
  strict call format;
- a tool call is a fenced block in the assistant text:
    ```tool_call
    {"tool": "web_search", "arguments": {"query": "..."}}
    ```
  (a bare JSON object with a ``tool`` key also parses — models drift);
- the runtime parses the block, executes through the NORMAL
  ToolService boundary (grants, redaction, audit unchanged), feeds the
  result back as a user-role ``tool_result`` block, and continues the
  loop — bounded exactly like the native loop;
- the marker text is stripped from anything presented to users.

The protocol is opt-in per provider/model through the capability probe
(``ProviderService.tool_call_support``): native tools keep working
unchanged when a gateway supports them.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The system-prompt instruction block appended when the text protocol
#: is active. Deliberately explicit: models follow exact formats better
#: than prose descriptions of formats.
PROTOCOL_INSTRUCTIONS = (
    "## Tool calling protocol\n"
    "You can call tools. To call ONE tool, end your reply with exactly "
    "this fenced block and nothing after it:\n"
    "```tool_call\n"
    '{"tool": "<tool name>", "arguments": {<json arguments>}}\n'
    "```\n"
    "After each call you will receive a ```tool_result``` block as a "
    "user message. Then continue: call another tool the same way, or "
    "write the final answer WITHOUT any tool_call block. Never claim a "
    "tool result you did not receive. Never fabricate search results, "
    "file contents, or command output."
)

_FENCED_RE = re.compile(
    r"```tool_call\s*\n?(?P<json>.*?)\s*```",
    re.DOTALL,
)
_FENCED_GENERIC_RE = re.compile(
    r"```(?:json)?\s*(?P<json>\{[^`]*?\"tool\"\s*:[^`]*?\})\s*```",
    re.DOTALL,
)
_BARE_RE = re.compile(
    r"(?P<json>\{\s*\"tool\"\s*:\s*\"[^\"\n]+?\"[^{}]*?(?:\"arguments\"\s*:\s*\{.*?\})?\s*\})",
    re.DOTALL,
)
_MAX_RESULT_CHARS = 16_000


def render_text_tool_instructions(declarations: list[dict[str, Any]]) -> str:
    """Render the tool surface for the system message.

    ``declarations`` are canonical declaration dicts (``name``,
    ``description``, ``parameters``).
    """
    lines = [PROTOCOL_INSTRUCTIONS, "", "### Available tools"]
    for declaration in declarations:
        name = declaration.get("name") or "?"
        description = (declaration.get("description") or "").strip()
        if description:
            lines.append(f"- {name}: {description}")
        else:
            lines.append(f"- {name}")
        parameters = declaration.get("parameters")
        if isinstance(parameters, dict) and parameters.get("properties"):
            schema = json.dumps(parameters, ensure_ascii=False)
            if len(schema) > 800:
                schema = schema[:797] + "..."
            lines.append(f"  arguments schema: {schema}")
    return "\n".join(lines)


def parse_tool_call(text: str) -> dict[str, Any] | None:
    """Extract ONE tool call from assistant text, or None.

    Precedence: fenced ``tool_call`` block → generic fenced JSON with a
    ``tool`` key → bare JSON object with a ``tool`` key. Malformed JSON
    inside a marker yields a structured error payload so the model can
    re-issue the call (never a crash).
    """
    source = str(text or "")
    candidate: str | None = None
    for pattern in (_FENCED_RE, _FENCED_GENERIC_RE):
        match = pattern.search(source)
        if match:
            candidate = match.group("json")
            break
    if candidate is None:
        match = _BARE_RE.search(source)
        if match:
            candidate = match.group("json")
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return {
            "tool": None,
            "arguments": {},
            "error": "tool_call block is not valid JSON; re-issue the call",
        }
    if not isinstance(parsed, dict):
        return {
            "tool": None,
            "arguments": {},
            "error": "tool_call block must be a JSON object",
        }
    name = parsed.get("tool") or parsed.get("name")
    if not name or not isinstance(name, str):
        return {
            "tool": None,
            "arguments": {},
            "error": "tool_call block lacks a 'tool' name string",
        }
    arguments = parsed.get("arguments")
    if arguments is None and "query" in parsed:
        # Tolerate flat calls: {"tool": "web_search", "query": "..."}.
        arguments = {
            key: value for key, value in parsed.items() if key not in ("tool", "name")
        }
    if not isinstance(arguments, dict):
        arguments = {}
    return {"tool": name, "arguments": arguments, "error": None}


def strip_tool_call_markers(text: str) -> str:
    """Remove any tool_call block from text shown to users."""
    source = str(text or "")
    source = _FENCED_RE.sub("", source)
    source = _FENCED_GENERIC_RE.sub("", source)
    return source.strip()


def render_tool_result_message(tool_name: str, result_text: str) -> str:
    """The user-role block that carries one tool result back to the model."""
    payload = str(result_text or "")
    if len(payload) > _MAX_RESULT_CHARS:
        payload = payload[:_MAX_RESULT_CHARS] + "\n...(truncated)"
    return (
        "```tool_result\n"
        + json.dumps({"tool": tool_name}, ensure_ascii=False)
        + "\n"
        + payload
        + "\n```"
    )


def render_tool_error_message(tool_name: str | None, error: str) -> str:
    """The user-role block carrying a malformed-call error."""
    name = tool_name or "(unknown)"
    return (
        "```tool_result\n"
        + json.dumps(
            {"tool": name, "error": str(error)[:400]}, ensure_ascii=False
        )
        + "\n```"
    )


__all__ = [
    "PROTOCOL_INSTRUCTIONS",
    "parse_tool_call",
    "render_text_tool_instructions",
    "render_tool_error_message",
    "render_tool_result_message",
    "strip_tool_call_markers",
]
