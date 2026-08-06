"""Zero v2 mode isolation tests — ADR T-4.21.

Comprehensive test suite for all three mode-pair leakage boundaries
plus intra-mode (two ``normal`` Topics, two Projects).

Mandatory, in CI.

This module provides reusable verification functions — actual pytest tests
live in ``tests/adversarial/test_mode_isolation.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from zero.core.scope import Scope
from zero.memory.store import MemoryStore

__all__ = [
    "ModeIsolationResult",
    "ModeLeakageError",
    "verify_mode_isolation",
]


class ModeLeakageError(AssertionError):
    """Raised when mode isolation is violated."""


@dataclass(frozen=True, slots=True)
class ModeIsolationResult:
    """Result of mode isolation verification."""

    passed: bool
    violated: str | None = None
    detail: str | None = None


def verify_mode_isolation(store: MemoryStore) -> ModeIsolationResult:
    """Verify that no mode pair leaks memory across boundaries.

    Tests six scope-leak boundaries (per ADR 0006 §80):
        1. personal → normal
        2. personal → dev
        3. normal → normal-other (different group+topic)
        4. normal → dev
        5. dev → dev-other (different project)
        6. dev → personal-nonmember
    """
    # Set up test scopes.
    personal_scope = Scope.personal(user_id="usr_test_personal").with_default_memory_scope()
    normal_a = Scope.normal(group_id="grp_a", topic_id=100).with_default_memory_scope()
    normal_b = Scope.normal(group_id="grp_b", topic_id=200).with_default_memory_scope()
    dev_a = Scope.development(
        org_id="org_test_a",
        workspace_id="ws_test_a",
        project_id="prj_a",
        group_id="grp_dev_a",
        topic_id=300,
    ).with_default_memory_scope()
    dev_b = Scope.development(
        org_id="org_test_b",
        workspace_id="ws_test_b",
        project_id="prj_b",
        group_id="grp_dev_b",
        topic_id=400,
    ).with_default_memory_scope()

    # Populate with test entries.
    from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource  # noqa: PLC0415

    personal_entry = MemoryEntry(
        scope=personal_scope, kind=MemoryKind.SEMANTIC,
        content="personal secret", source=MemorySource(type="test", ref="t1"),
        created_by="usr_test_personal",
    )
    normal_a_entry = MemoryEntry(
        scope=normal_a, kind=MemoryKind.SEMANTIC,
        content="normal A secret", source=MemorySource(type="test", ref="t2"),
        created_by="usr_test",
    )
    dev_a_entry = MemoryEntry(
        scope=dev_a, kind=MemoryKind.SEMANTIC,
        content="dev A secret", source=MemorySource(type="test", ref="t3"),
        created_by="usr_test",
    )
    store.store(personal_entry)
    store.store(normal_a_entry)
    store.store(dev_a_entry)

    # Boundary 1: personal → normal
    result = store.retrieve(normal_a, query="personal secret")
    if any("personal secret" in r.entry.content for r in result):
        return ModeIsolationResult(
            passed=False,
            violated="personal → normal",
            detail="personal memory leaked into normal scope retrieval",
        )

    # Boundary 2: personal → dev
    result = store.retrieve(dev_a, query="personal secret")
    if any("personal secret" in r.entry.content for r in result):
        return ModeIsolationResult(
            passed=False,
            violated="personal → dev",
            detail="personal memory leaked into dev scope retrieval",
        )

    # Boundary 3: normal → normal-other
    result = store.retrieve(normal_b, query="normal A secret")
    if any("normal A secret" in r.entry.content for r in result):
        return ModeIsolationResult(
            passed=False,
            violated="normal → normal-other",
            detail="normal group A memory leaked into normal group B",
        )

    # Boundary 4: normal → dev
    result = store.retrieve(dev_a, query="normal A secret")
    if any("normal A secret" in r.entry.content for r in result):
        return ModeIsolationResult(
            passed=False,
            violated="normal → dev",
            detail="normal group A memory leaked into dev scope",
        )

    # Boundary 5: dev → dev-other
    result = store.retrieve(dev_b, query="dev A secret")
    if any("dev A secret" in r.entry.content for r in result):
        return ModeIsolationResult(
            passed=False,
            violated="dev → dev-other",
            detail="dev project A memory leaked into dev project B",
        )

    # Boundary 6: dev → personal-nonmember
    # Personal scope retrieval must not return dev entries.
    result = store.retrieve(personal_scope, query="dev A secret")
    if any("dev A secret" in r.entry.content for r in result):
        return ModeIsolationResult(
            passed=False,
            violated="dev → personal",
            detail="dev memory leaked into personal scope",
        )

    return ModeIsolationResult(passed=True)
