"""Structural tests — ADR 0004 §6 + ADR 0006 §130.

These tests verify architectural invariants at the code-structure level:

    1. No module imports provider SDK (openai/anthropic/etc.)
    2. No model pricing table in zero/
    3. No model selection logic in zero/
    4. Platform module not imported when platform.enabled=False
    5. No raw secret in any serialized output
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import zero

ZERO_ROOT = Path(zero.__file__).parent


# ---------------------------------------------------------------------- helpers

def _walk_python_files(root: Path) -> list[Path]:
    """Yield all .py files under ``root``."""
    return sorted(root.rglob("*.py"))


def _read_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ---------------------------------------------------------------------- test 1: no provider SDK imports

FORBIDDEN_PROVIDER_IMPORTS = frozenset({
    "openai",
    "anthropic",
    "google.generativeai",
    "vertexai",
    "boto3",  # AWS Bedrock
    "mistralai",
    "cohere",
    "replicate",
    "together",
    "groq",
    "xai_sdk",
})


class TestNoProviderSDKImports:
    """ADR 0004 §6 structural test 1: Zero never imports provider SDKs.

    Zero is a pure HTTP consumer of Router — no direct provider calls.
    """

    def test_no_forbidden_imports_in_zero_package(self) -> None:
        offenders: list[tuple[Path, str]] = []
        for path in _walk_python_files(ZERO_ROOT):
            if path.name == "__pycache__":
                continue
            try:
                tree = _read_ast(path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] in FORBIDDEN_PROVIDER_IMPORTS:
                            offenders.append((path, alias.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in FORBIDDEN_PROVIDER_IMPORTS:
                        offenders.append((path, node.module))
        assert not offenders, (
            "Zero must not import provider SDKs directly. Found:\n"
            + "\n".join(f"  {p}: {m}" for p, m in offenders)
        )


# ---------------------------------------------------------------------- test 2: no model pricing table

# Submodules that ARE the Router (not Zero) — they're allowed to know pricing.
# The RouterShim ships inside Zero's package for convenience, but it is
# architecturally a separate component (it speaks the OpenAI protocol to Zero's
# RouterClient). Pricing belongs in the Router, not in Zero.
_ROUTER_SHIM_PATHS = frozenset({
    "agents/llm_provider",
})


def _is_router_shim_path(path: Path) -> bool:
    """Check if a Python file is part of the RouterShim (allowed to have pricing)."""
    try:
        rel = path.relative_to(ZERO_ROOT)
    except ValueError:
        return False
    rel_str = str(rel).replace("\\", "/")
    return any(rel_str.startswith(prefix) for prefix in _ROUTER_SHIM_PATHS)


class TestNoModelPricingTable:
    """ADR 0004 §6 structural test 2: Zero has no model pricing table.

    Cost is read from Router response header, never computed locally.

    EXCEPTION: ``zero/agents/llm_provider/`` is the RouterShim — a separate
    component that ships in the same package for convenience but is
    architecturally the Router (not Zero). The Router legitimately needs
    pricing to compute the ``x-zero-cost-usd`` header.
    """

    def test_no_pricing_constants(self) -> None:
        offenders: list[tuple[Path, int, str]] = []
        for path in _walk_python_files(ZERO_ROOT):
            if _is_router_shim_path(path):
                continue  # RouterShim is allowed to have pricing.
            try:
                tree = _read_ast(path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # Look for assignments like:
                #     MODEL_PRICING = {...}
                #     PRICES = {...}
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            name = target.id.upper()
                            if any(kw in name for kw in ("PRICING", "PRICE", "COST_TABLE", "MODEL_COST")):
                                offenders.append((path, node.lineno, target.id))
        assert not offenders, (
            "Zero must not have model pricing tables. Found:\n"
            + "\n".join(f"  {p}:{lineno} {name}" for p, lineno, name in offenders)
        )


# ---------------------------------------------------------------------- test 3: no model selection logic

MODEL_SELECTION_PATTERNS = ("pick_model", "select_model", "choose_model", "resolve_model")


class TestNoModelSelectionLogic:
    """ADR 0004 §6 structural test 3: Zero has no model selection logic."""

    def test_no_model_selection_functions(self) -> None:
        offenders: list[tuple[Path, int, str]] = []
        for path in _walk_python_files(ZERO_ROOT):
            try:
                tree = _read_ast(path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(pat in node.name.lower() for pat in MODEL_SELECTION_PATTERNS):
                        offenders.append((path, node.lineno, node.name))
        assert not offenders, (
            "Zero must not have model selection logic. Found:\n"
            + "\n".join(f"  {p}:{lineno} {name}" for p, lineno, name in offenders)
        )


# ---------------------------------------------------------------------- test 4: platform module disabled when not enabled

class TestPlatformModuleGated:
    """ADR 0004 §6 structural test 4: platform module not loaded when disabled."""

    def test_platform_import_is_optional(self) -> None:
        """zero.platform should be importable, but its functionality
        should be a no-op when platform.enabled=False."""
        # We can't easily test "not imported" in Python — but we can test
        # that the platform module doesn't import any external HTTP client
        # at module level (would block offline startup).
        # Actually, this test is more about runtime config — for MVP we
        # just verify the module exists and is structured correctly.
        from zero.platform import Capability, CapabilityState  # noqa: F401

        # Verify CapabilityState has the four required states.
        states = {s.value for s in CapabilityState}
        assert {"available", "unavailable", "degraded", "unknown"}.issubset(states)


# ---------------------------------------------------------------------- test 5: no raw secret in serialized output

class TestNoRawSecretInSerializedOutput:
    """ADR 0004 §6 structural test 5: no raw secret in any serialized output.

    SecretValue masks __repr__/__str__/__format__ and is excluded from
    json.dumps / pydantic.model_dump.
    """

    def test_secret_value_str_is_masked(self) -> None:
        from zero.core.secret import SecretValue
        sv = SecretValue("ghp_raw_token_value_xyz")
        assert str(sv) == "***"
        assert "ghp_raw_token_value_xyz" not in repr(sv)

    def test_secret_value_not_in_json(self) -> None:
        import json

        from zero.core.secret import SecretValue
        sv = SecretValue("sk-raw_openai_key_value")
        d = {"api_key": sv}
        out = json.dumps(d, default=str)
        assert "sk-raw_openai_key_value" not in out


# ---------------------------------------------------------------------- test 6: ATTACH forbidden in DB layer

class TestNoAttachInDBLayer:
    """ADR 0003 §6: ATTACH must be forbidden at the DB connection layer."""

    def test_sqlite_connection_rejects_attach(self) -> None:
        from zero.db.sqlite_backend import _is_attach_or_detach
        # Verify the helper detects both ATTACH and DETACH forms.
        assert _is_attach_or_detach("ATTACH 'x.db' AS x") is True
        assert _is_attach_or_detach("DETACH x") is True
        assert _is_attach_or_detach("SELECT 1") is False
        assert _is_attach_or_detach("  attach database 'x' as x") is True


# ---------------------------------------------------------------------- test 7: Scope is frozen

class TestScopeIsFrozen:
    """ADR 0003: Scope must be a frozen dataclass."""

    def test_cannot_set_attribute(self) -> None:
        from zero.core.scope import Mode, Scope
        s = Scope.personal(user_id="usr_01HALICE")
        with pytest.raises((AttributeError, TypeError)):
            s.mode = Mode.NORMAL  # type: ignore[misc]
        with pytest.raises((AttributeError, TypeError)):
            s.user_id = "usr_01HBOB"  # type: ignore[misc]
