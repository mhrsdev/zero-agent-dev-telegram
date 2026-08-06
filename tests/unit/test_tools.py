"""Unit tests for zero.tools.registry — tool registration & dispatch."""
from __future__ import annotations

import pytest
from zero.core.scope import Scope
from zero.tools.base import ToolContext, ToolError, ToolSpec
from zero.tools.registry import ToolRegistry, ToolResult


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


@pytest.fixture
def ctx(personal_scope: Scope) -> ToolContext:
    return ToolContext(
        scope=personal_scope,
        actor_id="usr_01HALICE",
        tool_call_id="tc_test_01",
    )


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


# ---------------------------------------------------------------------- registration

class TestRegistration:
    def test_register_basic(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        registry.register(
            name="test_tool",
            spec=ToolSpec(
                name="test_tool",
                description="A test tool",
                parameters_schema={"type": "object", "properties": {}},
            ),
            handler=handler,
        )
        assert "test_tool" in registry.list_names()

    def test_register_duplicate_rejected(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        spec = ToolSpec(name="t", description="d", parameters_schema={"type": "object"})
        registry.register(name="t", spec=spec, handler=handler)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(name="t", spec=spec, handler=handler)

    def test_register_override_allowed(self, registry: ToolRegistry) -> None:
        async def handler1(args: dict, ctx: ToolContext) -> str:
            return "v1"

        async def handler2(args: dict, ctx: ToolContext) -> str:
            return "v2"

        spec = ToolSpec(name="t", description="d", parameters_schema={"type": "object"})
        registry.register(name="t", spec=spec, handler=handler1)
        registry.register(name="t", spec=spec, handler=handler2, override=True)
        # Last registration wins
        assert registry.get("t") is not None


# ---------------------------------------------------------------------- dispatch

class TestDispatch:
    @pytest.mark.asyncio
    async def test_successful_dispatch(self, registry: ToolRegistry, ctx: ToolContext) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return f"hello {args.get('name', 'world')}"

        registry.register(
            name="greet",
            spec=ToolSpec(
                name="greet", description="greet someone",
                parameters_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
            ),
            handler=handler,
        )
        result = await registry.dispatch("greet", {"name": "alice"}, ctx)
        assert isinstance(result, ToolResult)
        assert result.error is False
        assert "alice" in result.output

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, registry: ToolRegistry, ctx: ToolContext) -> None:
        result = await registry.dispatch("nonexistent", {}, ctx)
        assert result.error is True
        assert "not registered" in result.output

    @pytest.mark.asyncio
    async def test_tool_error_caught(self, registry: ToolRegistry, ctx: ToolContext) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            raise ToolError("file not found")

        registry.register(
            name="bad_tool",
            spec=ToolSpec(name="bad_tool", description="d", parameters_schema={"type": "object"}),
            handler=handler,
        )
        result = await registry.dispatch("bad_tool", {}, ctx)
        assert result.error is True
        assert "file not found" in result.output

    @pytest.mark.asyncio
    async def test_unexpected_exception_caught(self, registry: ToolRegistry, ctx: ToolContext) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            raise RuntimeError("unexpected crash")

        registry.register(
            name="crashy",
            spec=ToolSpec(name="crashy", description="d", parameters_schema={"type": "object"}),
            handler=handler,
        )
        result = await registry.dispatch("crashy", {}, ctx)
        assert result.error is True
        assert "unexpected exception" in result.output

    @pytest.mark.asyncio
    async def test_output_truncated(self, registry: ToolRegistry, ctx: ToolContext) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "x" * 100_000  # 100k chars

        registry.register(
            name="big_output",
            spec=ToolSpec(name="big_output", description="d", parameters_schema={"type": "object"}),
            handler=handler,
        )
        result = await registry.dispatch("big_output", {}, ctx)
        assert len(result.output) < 100_000
        assert "truncated" in result.output


# ---------------------------------------------------------------------- definitions

class TestDefinitions:
    def test_get_definitions_includes_schema(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        registry.register(
            name="t1",
            spec=ToolSpec(
                name="t1", description="tool 1",
                parameters_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
            ),
            handler=handler,
        )
        defs = registry.get_definitions()
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "t1"
        assert "parameters" in defs[0]["function"]

    def test_get_definitions_deferred_mode(self, registry: ToolRegistry) -> None:
        """Deferred loading: only name + description when include_schema=False."""
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        registry.register(
            name="t1",
            spec=ToolSpec(
                name="t1", description="tool 1",
                parameters_schema={"type": "object"},
            ),
            handler=handler,
        )
        defs = registry.get_definitions(include_schema=False)
        assert len(defs) == 1
        assert "parameters" not in defs[0]["function"]

    def test_get_definitions_with_allowlist(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        for name in ("t1", "t2", "t3"):
            registry.register(
                name=name,
                spec=ToolSpec(name=name, description="d", parameters_schema={"type": "object"}),
                handler=handler,
            )
        defs = registry.get_definitions(allowed=frozenset({"t1", "t3"}))
        names = [d["function"]["name"] for d in defs]
        assert names == ["t1", "t3"]

    def test_check_fn_filters_availability(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        registry.register(
            name="conditional",
            spec=ToolSpec(name="conditional", description="d", parameters_schema={"type": "object"}),
            handler=handler,
            check_fn=lambda: False,  # always unavailable
        )
        defs = registry.get_definitions()
        assert len(defs) == 0  # filtered out


# ---------------------------------------------------------------------- coerce_args

class TestCoerceArgs:
    def test_coerce_string_to_int(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        registry.register(
            name="t",
            spec=ToolSpec(
                name="t", description="d",
                parameters_schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                },
            ),
            handler=handler,
        )
        coerced = registry.coerce_args("t", {"count": "42"})
        assert coerced["count"] == 42
        assert isinstance(coerced["count"], int)

    def test_coerce_string_to_bool(self, registry: ToolRegistry) -> None:
        async def handler(args: dict, ctx: ToolContext) -> str:
            return "ok"

        registry.register(
            name="t",
            spec=ToolSpec(
                name="t", description="d",
                parameters_schema={
                    "type": "object",
                    "properties": {"flag": {"type": "boolean"}},
                },
            ),
            handler=handler,
        )
        assert registry.coerce_args("t", {"flag": "true"})["flag"] is True
        assert registry.coerce_args("t", {"flag": "false"})["flag"] is False
        assert registry.coerce_args("t", {"flag": "yes"})["flag"] is True
        assert registry.coerce_args("t", {"flag": "0"})["flag"] is False
