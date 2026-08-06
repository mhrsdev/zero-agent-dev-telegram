"""Zero v2 memory store — Phase 6 T-6.1 + T-6.5.

Scope-bounded retrieval. ``scope`` is the FIRST filter, not the last.

Storage signature:
    - ``store(entry: MemoryEntry)`` — entry.scope selects schema + table
    - ``retrieve(scope, query, ...)`` — scope is mandatory, never None

Adversarial test (T-6.1 acceptance): no parameter combination returns
records outside scope.

Personal memory NEVER retrieved in DEVELOPMENT mode (T-6.5 acceptance).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from zero.core.errors import ErrorCode, ZeroError
from zero.core.scope import Scope
from zero.memory.entry import RETRIEVAL_PRIORITY, MemoryEntry, MemoryKind

__all__ = [
    "MemoryRetrievalResult",
    "MemoryScopeViolationError",
    "MemoryStore",
]


# ---------------------------------------------------------------------- errors

class MemoryScopeViolationError(ZeroError):
    """Raised when retrieval would cross scope boundaries."""

    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(
            code=ErrorCode.VALIDATION_FAILED,
            message=message,
            internal=internal,
        )


# ---------------------------------------------------------------------- result

@dataclass(frozen=True, slots=True)
class MemoryRetrievalResult:
    """A single retrieved memory + its origin metadata."""

    entry: MemoryEntry
    score: float = 0.0  # higher = more relevant (0..1)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            **self.entry.to_log_dict(),
            "score": round(self.score, 3),
        }


# ---------------------------------------------------------------------- store

class MemoryStore:
    """In-memory memory store with scope-bounded retrieval (base class).

    For production use, prefer :class:`zero.memory.db_store.DbMemoryStore`
    which persists to ``dev_memory`` / ``normal_memory`` / ``personal_memory``
    tables per schema with TF-IDF semantic search.
    """

    def __init__(self) -> None:
        # All entries in one list; filtered by scope at retrieval time.
        # DbMemoryStore uses DB rows with scope_key column for O(1) filtering.
        self._entries: list[MemoryEntry] = []

    # ------------------------------------------------------------------ store

    def store(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a memory entry.

        Validates entry.scope is consistent (entry.scope.mode matches the
        kind constraints already validated in MemoryEntry.__post_init__).
        """
        # Re-validate scope/kind consistency (defensive — entry may have been
        # constructed before scope was set).
        if entry.scope.is_normal() and entry.kind in (MemoryKind.FACT, MemoryKind.DECISION):
            raise MemoryScopeViolationError(
                "NORMAL mode forbids fact/decision memory kinds",
                internal=f"scope={entry.scope.retrieval_key()!r} kind={entry.kind.value!r}",
            )
        if entry.scope.is_personal() and entry.kind in (MemoryKind.FACT, MemoryKind.DECISION):
            # PERSONAL facts are allowed (user can promote their own facts).
            pass

        self._entries.append(entry)
        return entry

    # ------------------------------------------------------------------ retrieve

    def retrieve(
        self,
        scope: Scope,
        query: str,
        *,
        limit: int = 10,
        max_tokens: int = 4000,
        include_kinds: frozenset[MemoryKind] | None = None,
        exclude_kinds: frozenset[MemoryKind] | None = None,
    ) -> list[MemoryRetrievalResult]:
        """Retrieve memory entries for ``scope`` matching ``query``.

        CRITICAL (T-6.5 acceptance): in DEVELOPMENT mode, NO personal record
        is ever returned, under any condition.

        Returns entries sorted by:
            1. Retrieval priority (fact > decision > semantic > ...)
            2. Relevance score (TF-IDF in DbMemoryStore, substring in base class)
            3. Recency
        """
        if limit <= 0:
            return []
        if max_tokens <= 0:
            return []

        # Scope-bound retrieval: only entries with matching scope_key.
        scope_key = scope.retrieval_key()

        # ADVERSARIAL DEFENSE: personal memory NEVER retrieved in DEVELOPMENT.
        if scope.is_development():
            # Filter out any entry whose scope is PERSONAL — no matter what.
            candidates = [
                e for e in self._entries
                if not e.scope.is_personal()
                and e.scope.retrieval_key() == scope_key
                and e.is_valid
            ]
        elif scope.is_normal():
            # NORMAL scope: only NORMAL entries for this group+topic.
            candidates = [
                e for e in self._entries
                if e.scope.is_normal()
                and e.scope.retrieval_key() == scope_key
                and e.is_valid
            ]
        elif scope.is_personal():
            # PERSONAL scope: only this user's PERSONAL entries.
            candidates = [
                e for e in self._entries
                if e.scope.is_personal()
                and e.scope.retrieval_key() == scope_key
                and e.is_valid
            ]
        else:
            return []  # unreachable

        # Filter by kind allow/deny lists.
        if include_kinds is not None:
            candidates = [e for e in candidates if e.kind in include_kinds]
        if exclude_kinds is not None:
            candidates = [e for e in candidates if e.kind not in exclude_kinds]

        # Score by substring match (DbMemoryStore uses TF-IDF semantic search).
        scored: list[MemoryRetrievalResult] = []
        query_lower = query.lower()
        for e in candidates:
            score = _score(e, query_lower)
            scored.append(MemoryRetrievalResult(entry=e, score=score))

        # Sort: priority first, then score, then recency.
        scored.sort(
            key=lambda r: (
                RETRIEVAL_PRIORITY[r.entry.kind],
                -r.score,
                -r.entry.created_at.timestamp(),
            )
        )

        # Apply token budget.
        result: list[MemoryRetrievalResult] = []
        total_chars = 0
        for r in scored:
            # Rough token estimate: 4 chars = 1 token.
            entry_tokens = len(r.entry.content) // 4
            if total_chars + entry_tokens > max_tokens:
                continue
            result.append(r)
            total_chars += entry_tokens
            if len(result) >= limit:
                break

        return result

    # ------------------------------------------------------------------ invalidate

    def invalidate(
        self,
        entry_id: str,
        *,
        invalidated_by: str,
        reason: str,
    ) -> MemoryEntry | None:
        """Invalidate a memory entry (Fact can be invalidated but history preserved)."""
        for e in self._entries:
            if e.id == entry_id:
                e.invalidated_at = datetime.now(UTC)
                e.invalidated_by = invalidated_by
                e.invalidation_reason = reason
                return e
        return None

    # ------------------------------------------------------------------ export

    def export_scope(self, scope: Scope) -> list[MemoryEntry]:
        """Export all entries for a scope (for backup / migration)."""
        scope_key = scope.retrieval_key()
        return [e for e in self._entries if e.scope.retrieval_key() == scope_key]

    def list_all(self) -> list[MemoryEntry]:
        """For debugging only — never use in retrieval paths."""
        return list(self._entries)


def _score(entry: MemoryEntry, query_lower: str) -> float:
    """Simple relevance score (0..1) — substring match. DbMemoryStore uses TF-IDF."""
    if not query_lower:
        return 0.5
    content_lower = entry.content.lower()
    if query_lower in content_lower:
        # More occurrences = higher score.
        occurrences = content_lower.count(query_lower)
        return min(1.0, 0.5 + 0.1 * occurrences)
    # Word overlap.
    query_words = set(query_lower.split())
    content_words = set(content_lower.split())
    if not query_words:
        return 0.0
    overlap = len(query_words & content_words) / len(query_words)
    return overlap * 0.5
