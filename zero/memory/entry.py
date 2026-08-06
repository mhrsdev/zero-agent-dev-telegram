"""Zero v2 memory entry — Phase 6 data model."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from zero.core.scope import Scope

__all__ = [
    "RETRIEVAL_PRIORITY",
    "MemoryEntry",
    "MemoryKind",
    "MemorySource",
]


class MemoryKind(StrEnum):
    """Memory record kinds.

    Retrieval priority (T-6.5 acceptance): higher priority wins.
        fact > decision > semantic > episodic > preference > scratch
    """

    FACT = "fact"              # approved, immutable, project-wide
    DECISION = "decision"      # approved, immutable, project-wide
    SEMANTIC = "semantic"      # general knowledge
    EPISODIC = "episodic"      # event-based memory
    PREFERENCE = "preference"  # user/group preferences
    SCRATCH = "scratch"        # short-term, expires


# Lower number = higher priority in retrieval.
RETRIEVAL_PRIORITY: dict[MemoryKind, int] = {
    MemoryKind.FACT: 0,
    MemoryKind.DECISION: 1,
    MemoryKind.SEMANTIC: 2,
    MemoryKind.EPISODIC: 3,
    MemoryKind.PREFERENCE: 4,
    MemoryKind.SCRATCH: 5,
}


@dataclass(frozen=True, slots=True)
class MemorySource:
    """Origin of a memory record (always required — ADR 0005 §2 rule 1)."""

    type: str  # "message", "import", "agent", "document", "manual"
    ref: str   # e.g. "v1:mem_01H..." or "msg_01H..." or "doc:path/to/file"

    def to_dict(self) -> dict[str, str]:
        return {"type": self.type, "ref": self.ref}


@dataclass(slots=True)
class MemoryEntry:
    """A single memory record.

    Invariants enforced at storage layer (DB constraints):
        1. ``mode`` field non-nullable, matches scope
        2. ``scope_key`` non-nullable, matches scope.retrieval_key()
        3. For kind ∈ {fact, decision}: ``approved_by`` is non-nullable
        4. For kind=scratch: ``expires_at`` is set
        5. ``source`` is always set (never empty)
        6. NORMAL mode forbids kind ∈ {fact, decision} (T-4.11)
    """

    scope: Scope
    kind: MemoryKind
    content: str
    source: MemorySource
    created_by: str  # user_id or agent_def_id

    # Optional, but required for fact/decision
    approved_by: str | None = None

    # For scratch kind — auto-expiry
    expires_at: datetime | None = None

    # Invalidation (Fact can be invalidated but history preserved)
    invalidated_at: datetime | None = None
    invalidated_by: str | None = None
    invalidation_reason: str | None = None

    id: str = field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:16]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Optional topic_id for scratch memory tied to a specific Topic
    topic_id: int | None = None

    def __post_init__(self) -> None:
        # Validate Fact Promotion invariant (T-6.4 acceptance).
        if self.kind in (MemoryKind.FACT, MemoryKind.DECISION) and self.approved_by is None:
            raise ValueError(
                f"memory entry kind={self.kind.value!r} requires approved_by — "
                "no code path may create a Fact without explicit approval"
            )
        # NORMAL mode forbids fact/decision (T-4.11).
        if self.scope.is_normal() and self.kind in (MemoryKind.FACT, MemoryKind.DECISION):
            raise ValueError(
                f"NORMAL mode forbids kind={self.kind.value!r} — "
                "facts/decisions require Project scope (DEVELOPMENT mode)"
            )
        # Scratch requires expiry.
        if self.kind is MemoryKind.SCRATCH and self.expires_at is None:
            # Default 30 days from now.
            self.expires_at = datetime.now(UTC) + timedelta(days=30)
        # Source must not be empty.
        if not self.source.type or not self.source.ref:
            raise ValueError("MemorySource.type and .ref must both be non-empty")

    @property
    def is_valid(self) -> bool:
        """True if not invalidated and not expired."""
        if self.invalidated_at is not None:
            return False
        if self.expires_at is not None and datetime.now(UTC) > self.expires_at:
            return False
        return True

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "scope": self.scope.retrieval_key(),
            "mode": self.scope.mode.value,
            "content_chars": len(self.content),
            "approved_by": self.approved_by,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "invalidated": self.invalidated_at is not None,
        }
