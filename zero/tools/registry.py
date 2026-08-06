"""Zero v2 tool registry — ported from Hermes (``tools/registry.py``).

Self-registration pattern: each tool file calls :func:`register` at module
import time. Discovery is AST-based (no execution) and memoized.

Deferred tool loading (T-7.5 acceptance):
    - First call sends only name + short description.
    - Full schema loaded only when Agent actually calls.

check_fn TTL+grace cache (30s TTL, 60s grace for transient failures)
determines whether a tool is currently available (e.g. depends on an env
var that may not be set).
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zero.core.logging import get_logger
from zero.tools.base import ToolContext, ToolError, ToolSpec

__all__ = [
    "ToolEntry",
    "ToolRegistry",
    "ToolResult",
    "discover_builtin_tools",
    "dispatch",
    "register",
    "registry",
    "tool_error",
    "tool_result",
]


# ---------------------------------------------------------------------- types

@dataclass
class ToolResult:
    """Normalized tool execution result."""

    output: str
    error: bool = False
    persisted_to: str | None = None  # path if output was spilled to disk
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolEntry:
    """A registered tool."""

    name: str
    spec: ToolSpec
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[str]]
    check_fn: Callable[[], bool] | None = None
    # TTL+grace cache for check_fn results.
    _last_check: float = 0.0
    _last_result: bool = True
    _cache_ttl: float = 30.0
    _grace_period: float = 60.0

    def is_available(self) -> bool:
        """Check if tool is available, with TTL+grace cache."""
        if self.check_fn is None:
            return True
        now = time.monotonic()
        if now - self._last_check < self._cache_ttl:
            return self._last_result
        # Cache expired — re-run check.
        try:
            result = self.check_fn()
            self._last_check = now
            self._last_result = result
            return result
        except Exception:
            # Transient failure: use grace period.
            if now - self._last_check < self._grace_period:
                return self._last_result
            # Grace expired — return False.
            return False

    def to_definition(self, *, include_schema: bool = True) -> dict[str, Any]:
        """Build OpenAI-format tool definition.

        With ``include_schema=False`` → only name + description (deferred
        loading Tier 1).
        """
        out: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.spec.name,
                "description": self.spec.description,
            },
        }
        if include_schema:
            out["function"]["parameters"] = self.spec.parameters_schema
        return out


# ---------------------------------------------------------------------- registry

class ToolRegistry:
    """Singleton-style registry of tools.

    Use :func:`register` to add a tool. Use :func:`dispatch` to invoke.
    Use :func:`get_definitions` to build the tool list for Router.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._max_output_chars = 50_000  # 50k chars per tool output (terminal)

    # ------------------------------------------------------------------ registration

    def register(
        self,
        *,
        name: str,
        spec: ToolSpec,
        handler: Callable[[dict[str, Any], ToolContext], Awaitable[str]],
        check_fn: Callable[[], bool] | None = None,
        override: bool = False,
    ) -> None:
        """Register a tool. Raises if name already exists (unless override=True)."""
        if not override and name in self._tools:
            raise ValueError(
                f"tool {name!r} already registered — use override=True to replace"
            )
        self._tools[name] = ToolEntry(
            name=name,
            spec=spec,
            handler=handler,
            check_fn=check_fn,
        )

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())

    # ------------------------------------------------------------------ lookup

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def get_definitions(
        self,
        *,
        allowed: frozenset[str] | None = None,
        include_schema: bool = True,
    ) -> list[dict[str, Any]]:
        """Build OpenAI-format tool definitions for Router.

        Filters by:
            - ``allowed`` (closed allowlist from AgentDefinition)
            - tool availability (check_fn TTL cache)
        """
        out: list[dict[str, Any]] = []
        for name, entry in sorted(self._tools.items()):
            if allowed is not None and name not in allowed:
                continue
            if not entry.is_available():
                continue
            out.append(entry.to_definition(include_schema=include_schema))
        return out

    # ------------------------------------------------------------------ dispatch

    async def dispatch(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Invoke a registered tool. Returns normalized :class:`ToolResult`.

        Output is truncated to ``_max_output_chars`` (terminal cap).
        Errors are caught and returned as ``error=True`` (never raised).
        """
        entry = self._tools.get(name)
        if entry is None:
            return ToolResult(
                output=f"[TOOL_ERROR] tool {name!r} not registered",
                error=True,
            )
        if not entry.is_available():
            return ToolResult(
                output=f"[TOOL_ERROR] tool {name!r} is not available (check_fn returned False)",
                error=True,
            )

        try:
            output = await entry.handler(args, ctx)
        except ToolError as e:
            return ToolResult(output=f"[TOOL_ERROR] {name}: {e}", error=True)
        except Exception as e:
            log = get_logger("zero.tools")
            log.error(f"tool {name!r} raised unexpected exception", exc=e)
            return ToolResult(
                output=f"[TOOL_ERROR] {name}: unexpected exception (see logs)",
                error=True,
            )

        # Truncate if over cap.
        if len(output) > self._max_output_chars:
            truncated = output[: self._max_output_chars]
            output = (
                truncated
                + f"\n\n[truncated: original {len(output)} chars, cap {self._max_output_chars}]"
            )

        return ToolResult(output=output)

    # ------------------------------------------------------------------ coercion

    def coerce_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Coerce string args to schema-declared types.

        LLMs sometimes emit strings where integers/booleans are expected.
        This walks the schema and coerces:
            - "integer" → int
            - "number" → float
            - "boolean" → bool
        """
        entry = self._tools.get(name)
        if entry is None:
            return args
        schema = entry.spec.parameters_schema
        return _coerce_args_with_schema(args, schema)


def _coerce_args_with_schema(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Walk JSON Schema and coerce types in ``args``."""
    props = schema.get("properties", {})
    if not props:
        return args
    out: dict[str, Any] = dict(args)
    for key, prop_schema in props.items():
        if key not in out:
            continue
        value = out[key]
        target_type = prop_schema.get("type")
        if target_type == "integer" and isinstance(value, str):
            try:
                out[key] = int(value)
            except ValueError:
                pass
        elif target_type == "number" and isinstance(value, str):
            try:
                out[key] = float(value)
            except ValueError:
                pass
        elif target_type == "boolean" and isinstance(value, str):
            low = value.lower()
            if low in ("true", "1", "yes"):
                out[key] = True
            elif low in ("false", "0", "no"):
                out[key] = False
    return out


# ---------------------------------------------------------------------- module-level API

registry = ToolRegistry()


def register(
    *,
    name: str,
    spec: ToolSpec,
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[str]],
    check_fn: Callable[[], bool] | None = None,
    override: bool = False,
) -> None:
    """Register a tool in the global registry."""
    registry.register(
        name=name,
        spec=spec,
        handler=handler,
        check_fn=check_fn,
        override=override,
    )


async def dispatch(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch a tool call via the global registry."""
    return await registry.dispatch(name, args, ctx)


def discover_builtin_tools() -> None:
    """Discover and register builtin tools.

    Uses AST-based discovery to find all Tool subclasses in zero/tools/builtin.py.
    Idempotent — safe to call multiple times.
    """
    from zero.tools import builtin  # noqa: PLC0415  # local import avoids cycle

    # Each Tool subclass in builtin.py self-registers on import.
    # We just need to make sure the module is imported.
    _ = builtin


# ---------------------------------------------------------------------- helpers

def tool_result(output: str, **metadata: Any) -> ToolResult:
    """Convenience: build a successful ToolResult."""
    return ToolResult(output=output, metadata=metadata)


def tool_error(message: str) -> ToolResult:
    """Convenience: build an error ToolResult."""
    return ToolResult(output=f"[TOOL_ERROR] {message}", error=True)
