"""DB-backed memory store with semantic search — replaces in-memory MemoryStore.

Per ADR T-6.1 + T-6.5:
    - Memory entries persist to ``dev_memory`` / ``normal_memory`` / ``personal_memory``
    - Scope is the FIRST filter (enforced at SQL level via WHERE scope_key = ?)
    - Personal memory NEVER retrieved in DEVELOPMENT mode (enforced by schema isolation)
    - Fact > Decision > Semantic > Episodic > Preference > Scratch in retrieval priority
    - TF-IDF cosine similarity for semantic search (replaces substring match)
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zero.core.scope import Mode, Scope
from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource, RETRIEVAL_PRIORITY
from zero.memory.store import MemoryRetrievalResult, MemoryScopeViolationError

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbMemoryStore", "TfidfIndex"]


# ---------------------------------------------------------------------- TF-IDF index

class TfidfIndex:
    """In-memory TF-IDF index for semantic search.

    Per ADR T-6.6: semantic search via embeddings (Router) is an extension.
    We use TF-IDF which is:
        - Local (no API calls)
        - Fast (sub-millisecond for small corpora)
        - Good enough for substring + word-overlap queries

    The index is rebuilt lazily when entries are added/invalidated.
    """

    def __init__(self) -> None:
        self._docs: dict[str, list[str]] = {}  # entry_id -> tokens
        self._df: Counter[str] = Counter()  # document frequency
        self._dirty = True
        self._idf_cache: dict[str, float] = {}

    def add(self, entry_id: str, content: str) -> None:
        """Add/update a document in the index."""
        tokens = self._tokenize(content)
        # Remove old document frequency contribution.
        if entry_id in self._docs:
            old_tokens = set(self._docs[entry_id])
            for t in old_tokens:
                self._df[t] -= 1
                if self._df[t] <= 0:
                    del self._df[t]
        self._docs[entry_id] = tokens
        for t in set(tokens):
            self._df[t] += 1
        self._dirty = True

    def remove(self, entry_id: str) -> None:
        """Remove a document from the index."""
        if entry_id in self._docs:
            old_tokens = set(self._docs[entry_id])
            for t in old_tokens:
                self._df[t] -= 1
                if self._df[t] <= 0:
                    del self._df[t]
            del self._docs[entry_id]
            self._dirty = True

    def score(self, entry_id: str, query: str) -> float:
        """Compute TF-IDF cosine similarity between entry and query.

        Returns float in [0, 1]. Higher = more relevant.
        """
        if entry_id not in self._docs:
            return 0.0
        self._rebuild_idf_if_dirty()
        doc_tokens = self._docs[entry_id]
        query_tokens = self._tokenize(query)
        if not query_tokens or not doc_tokens:
            return 0.0

        # Build TF vectors.
        doc_tf = Counter(doc_tokens)
        query_tf = Counter(query_tokens)

        # Compute TF-IDF vectors.
        doc_vec: dict[str, float] = {}
        query_vec: dict[str, float] = {}
        all_terms = set(doc_tf) | set(query_tf)
        for term in all_terms:
            idf = self._idf_cache.get(term, 0.0)
            if idf == 0.0:
                continue
            if term in doc_tf:
                doc_vec[term] = doc_tf[term] / len(doc_tokens) * idf
            if term in query_tf:
                query_vec[term] = query_tf[term] / len(query_tokens) * idf

        # Cosine similarity.
        dot = sum(doc_vec.get(t, 0.0) * query_vec.get(t, 0.0) for t in all_terms)
        doc_norm = math.sqrt(sum(v * v for v in doc_vec.values()))
        query_norm = math.sqrt(sum(v * v for v in query_vec.values()))
        if doc_norm == 0 or query_norm == 0:
            return 0.0
        return dot / (doc_norm * query_norm)

    def _rebuild_idf_if_dirty(self) -> None:
        if not self._dirty:
            return
        n_docs = len(self._docs)
        if n_docs == 0:
            self._idf_cache = {}
            self._dirty = False
            return
        self._idf_cache = {
            term: math.log((n_docs + 1) / (df + 1)) + 1.0
            for term, df in self._df.items()
        }
        self._dirty = False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Tokenize text: lowercase, split on non-word chars, filter empties."""
        return [t for t in re.findall(r"\w+", text.lower()) if len(t) > 1]


# ---------------------------------------------------------------------- DbMemoryStore

class DbMemoryStore:
    """DB-backed memory store with TF-IDF semantic search.

    Replaces the in-memory ``MemoryStore`` with persistent storage.
    Each scope's entries are stored in the corresponding schema's memory table:
        - PERSONAL → personal_memory
        - NORMAL → normal_memory
        - DEVELOPMENT → dev_memory

    The TF-IDF index is maintained in-memory for fast retrieval, but the
    source of truth is the DB. On startup, the index is rebuilt from DB rows.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._tfidf = TfidfIndex()
        # In-memory cache for entries (synced with DB).
        self._cache: dict[str, MemoryEntry] = {}
        self._loaded_scopes: set[str] = set()

    async def _ensure_scope_loaded(self, scope: Scope) -> None:
        """Lazily load entries for a scope from DB into cache + index."""
        scope_key = scope.retrieval_key()
        if scope_key in self._loaded_scopes:
            return
        # Load from DB.
        table = self._table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            rows = await conn.fetchall(
                f"SELECT id, scope_key, mode, kind, content, source, approved_by, "
                f"created_by, created_at, expires_at, invalidated_at, topic_id "
                f"FROM {table} WHERE scope_key = ? AND invalidated_at IS NULL",
                (scope_key,),
            )
            for row in rows:
                entry = self._row_to_entry(row, scope)
                self._cache[entry.id] = entry
                self._tfidf.add(entry.id, entry.content)
        self._loaded_scopes.add(scope_key)

    def _table_for_scope(self, scope: Scope) -> str:
        """Return the memory table name for this scope's schema."""
        if scope.is_personal():
            return "personal_memory"
        if scope.is_normal():
            return "normal_memory"
        return "dev_memory"

    async def store(self, entry: MemoryEntry) -> MemoryEntry:
        """Persist a memory entry to DB + index."""
        # Validate scope/kind consistency.
        if entry.scope.is_normal() and entry.kind in (MemoryKind.FACT, MemoryKind.DECISION):
            raise MemoryScopeViolationError(
                "NORMAL mode forbids fact/decision memory kinds",
                internal=f"scope={entry.scope.retrieval_key()!r} kind={entry.kind.value!r}",
            )

        table = self._table_for_scope(entry.scope)
        source_json = json.dumps({"type": entry.source.type, "ref": entry.source.ref})

        async with self._db.connection_for(entry.scope) as conn:
            # Build column list based on schema.
            if entry.scope.is_personal():
                await conn.execute(
                    f"""INSERT INTO {table}
                       (id, user_id, scope_key, mode, kind, content, source, approved_by, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.id,
                        entry.scope.user_id,
                        entry.scope.retrieval_key(),
                        entry.scope.mode.value,
                        entry.kind.value,
                        entry.content,
                        source_json,
                        entry.approved_by,
                        entry.created_at.isoformat(),
                    ),
                )
            elif entry.scope.is_normal():
                await conn.execute(
                    f"""INSERT INTO {table}
                       (id, group_id, topic_id, scope_key, mode, kind, content, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.id,
                        entry.scope.group_id,
                        entry.scope.topic_id,
                        entry.scope.retrieval_key(),
                        entry.scope.mode.value,
                        entry.kind.value,
                        entry.content,
                        source_json,
                    ),
                )
            else:  # dev
                await conn.execute(
                    f"""INSERT INTO {table}
                       (id, project_id, topic_id, scope_key, mode, kind, content, source,
                        approved_by, created_by, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.id,
                        entry.scope.project_id,
                        entry.topic_id,
                        entry.scope.retrieval_key(),
                        entry.scope.mode.value,
                        entry.kind.value,
                        entry.content,
                        source_json,
                        entry.approved_by,
                        entry.created_by,
                        entry.created_at.isoformat(),
                        entry.expires_at.isoformat() if entry.expires_at else None,
                    ),
                )

        # Update cache + index.
        self._cache[entry.id] = entry
        self._tfidf.add(entry.id, entry.content)
        self._loaded_scopes.add(entry.scope.retrieval_key())
        return entry

    async def retrieve(
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
        is ever returned, under any condition. This is enforced by:
            1. Schema isolation (dev queries go to dev.db, not personal.db)
            2. SQL WHERE clause filtering by scope_key
            3. Python-side filter on scope.mode

        Returns entries sorted by:
            1. Retrieval priority (fact > decision > semantic > ...)
            2. TF-IDF relevance score
            3. Recency
        """
        if limit <= 0 or max_tokens <= 0:
            return []

        await self._ensure_scope_loaded(scope)
        scope_key = scope.retrieval_key()

        # Filter cached entries by scope_key (matches DB WHERE clause).
        candidates: list[MemoryEntry] = []
        for entry in self._cache.values():
            if entry.scope.retrieval_key() != scope_key:
                continue
            if not entry.is_valid:
                continue
            # ADVERSARIAL DEFENSE: personal memory NEVER in dev retrieval.
            if scope.is_development() and entry.scope.is_personal():
                continue
            candidates.append(entry)

        # Filter by kind.
        if include_kinds is not None:
            candidates = [e for e in candidates if e.kind in include_kinds]
        if exclude_kinds is not None:
            candidates = [e for e in candidates if e.kind not in exclude_kinds]

        # Score with TF-IDF.
        scored: list[MemoryRetrievalResult] = []
        for entry in candidates:
            score = self._tfidf.score(entry.id, query)
            # Boost score for substring match (exact phrase relevance).
            if query.lower() in entry.content.lower():
                score = max(score, 0.7)
            # Only include results with non-zero score (when query is non-empty).
            if query.strip() and score == 0.0:
                continue
            scored.append(MemoryRetrievalResult(entry=entry, score=score))

        # Sort: priority first, then score (desc), then recency (desc).
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
            entry_tokens = len(r.entry.content) // 4
            if total_chars + entry_tokens > max_tokens:
                continue
            result.append(r)
            total_chars += entry_tokens
            if len(result) >= limit:
                break

        return result

    async def invalidate(
        self,
        entry_id: str,
        *,
        invalidated_by: str,
        reason: str,
        scope: Scope,
    ) -> MemoryEntry | None:
        """Invalidate a memory entry (Fact can be invalidated but history preserved)."""
        entry = self._cache.get(entry_id)
        if entry is None:
            return None

        table = self._table_for_scope(scope)
        now = datetime.now(UTC)
        async with self._db.connection_for(scope) as conn:
            await conn.execute(
                f"UPDATE {table} SET invalidated_at = ? WHERE id = ?",
                (now.isoformat(), entry_id),
            )

        entry.invalidated_at = now
        entry.invalidated_by = invalidated_by
        entry.invalidation_reason = reason
        self._tfidf.remove(entry_id)
        return entry

    async def export_scope(self, scope: Scope) -> list[MemoryEntry]:
        """Export all entries for a scope (for backup / migration)."""
        await self._ensure_scope_loaded(scope)
        scope_key = scope.retrieval_key()
        return [e for e in self._cache.values() if e.scope.retrieval_key() == scope_key]

    def list_all(self) -> list[MemoryEntry]:
        """For debugging only — never use in retrieval paths."""
        return list(self._cache.values())

    @staticmethod
    def _row_to_entry(row: tuple[Any, ...], scope: Scope) -> MemoryEntry:
        """Convert a DB row to MemoryEntry."""
        (
            entry_id,
            _scope_key,
            mode_str,
            kind_str,
            content,
            source_json,
            approved_by,
            created_by,
            created_at_str,
            expires_at_str,
            invalidated_at_str,
            topic_id,
        ) = row

        # Parse source (stored as JSON).
        try:
            source_data = json.loads(str(source_json)) if source_json else {}
        except (json.JSONDecodeError, TypeError):
            source_data = {"type": "unknown", "ref": "unknown"}

        entry = MemoryEntry(
            scope=scope,
            kind=MemoryKind(str(kind_str)),
            content=str(content),
            source=MemorySource(
                type=str(source_data.get("type", "unknown")),
                ref=str(source_data.get("ref", "unknown")),
            ),
            created_by=str(created_by) if created_by else "system",
            approved_by=str(approved_by) if approved_by else None,
            id=str(entry_id),
            topic_id=int(str(topic_id)) if topic_id is not None else None,
        )
        # Override auto-generated timestamps.
        if created_at_str:
            entry.created_at = datetime.fromisoformat(str(created_at_str))
        if expires_at_str:
            entry.expires_at = datetime.fromisoformat(str(expires_at_str))
        if invalidated_at_str:
            entry.invalidated_at = datetime.fromisoformat(str(invalidated_at_str))
        return entry
