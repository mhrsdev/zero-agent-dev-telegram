"""Zero v2 core modules — Phase 1 foundational primitives.

This package contains the non-negotiable building blocks every other module
depends on. Per ADR 0003, :mod:`zero.core.scope` is the most important file in
the entire project: nothing persistent may be written without a fully-validated
``Scope`` instance.
"""
from __future__ import annotations

from zero.core.scope import Mode, Scope, ScopeError, ScopeKeyError

__all__ = ["Mode", "Scope", "ScopeError", "ScopeKeyError"]
