"""Zero v2 memory system — Phase 6.

Scope-bounded retrieval, Fact Promotion, Personal/Group/Project memory layers.

Per ADR 0003 + T-6.1 + T-6.4 + T-6.5:
    - Every memory record has non-nullable ``mode`` and scope-key columns
    - Storage layer signature requires scope (cannot retrieve without it)
    - No code path creates a Fact without ``approved_by``
    - Personal memory NEVER retrieved in DEVELOPMENT mode (under any condition)
    - Fact > Decision > Semantic > Episodic > Scratch in retrieval priority
"""
from __future__ import annotations

from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource
from zero.memory.fact_promotion import FactPromoter, FactPromotionError
from zero.memory.store import MemoryRetrievalResult, MemoryStore

__all__ = [
    "FactPromoter",
    "FactPromotionError",
    "MemoryEntry",
    "MemoryKind",
    "MemoryRetrievalResult",
    "MemorySource",
    "MemoryStore",
]
