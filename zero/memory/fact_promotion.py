"""Zero v2 Fact Promotion — ADR T-6.4.

The Fact Promotion engine enforces the central security invariant:

    **No code path creates a Fact without ``approved_by``.**

This module provides the only sanctioned path to create a Fact or Decision
memory entry. Every promotion is audited.

Promotion requires ``promote_fact`` permission (Maintainer+).
Approver is recorded. Fact can be invalidated but history preserved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zero.core.audit import ActorType, AuditEntry, AuditResult
from zero.core.permissions import PermissionContext, require
from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource

__all__ = [
    "FactPromoter",
    "FactPromotionError",
    "promote_to_decision",
    "promote_to_fact",
]


class FactPromotionError(Exception):
    """Raised when fact promotion fails."""


@dataclass(slots=True)
class FactPromoter:
    """Promotes a memory entry to Fact or Decision.

    Usage:
        >>> promoter = FactPromoter(memory_store=store)
        >>> fact = await promoter.promote(
        ...     source_entry=semantic_entry,
        ...     approved_by="usr_01H...",
        ...     permission_ctx=ctx,
        ... )
    """

    memory_store: Any  # MemoryStore — avoid circular import

    async def promote(
        self,
        *,
        source_entry: MemoryEntry,
        approved_by: str,
        permission_ctx: PermissionContext,
        note: str | None = None,
        target_kind: MemoryKind = MemoryKind.FACT,
    ) -> MemoryEntry:
        """Promote ``source_entry`` to a Fact or Decision.

        Steps:
            1. Verify permission_ctx has ``promote_fact`` permission.
            2. Verify target_kind is fact or decision.
            3. Verify source_entry.scope is DEVELOPMENT (facts are project-scoped).
            4. Create new MemoryEntry with kind=target_kind, approved_by set.
            5. Invalidate source_entry (it has been promoted).
            6. Audit the promotion.

        Returns the new Fact/Decision entry.
        """
        # Step 1: permission check.
        require("memory.promote_fact", permission_ctx)

        # Step 2: kind check.
        if target_kind not in (MemoryKind.FACT, MemoryKind.DECISION):
            raise FactPromotionError(
                f"target_kind must be fact or decision, got {target_kind!r}"
            )

        # Step 3: scope check — facts only in DEVELOPMENT.
        if not source_entry.scope.is_development():
            raise FactPromotionError(
                f"facts/decisions require DEVELOPMENT scope; got {source_entry.scope.mode.value!r}"
            )

        # Step 4: create new entry.
        promoted = MemoryEntry(
            scope=source_entry.scope,
            kind=target_kind,
            content=source_entry.content,
            source=MemorySource(
                type="promotion",
                ref=f"promoted_from:{source_entry.id}",
            ),
            created_by=source_entry.created_by,
            approved_by=approved_by,  # THE critical field
        )
        self.memory_store.store(promoted)

        # Step 5: invalidate source.
        self.memory_store.invalidate(
            source_entry.id,
            invalidated_by=approved_by,
            reason=f"promoted to {target_kind.value}: {promoted.id}",
        )

        # Step 6: audit.
        from zero.core.audit import audit  # noqa: PLC0415

        await audit().log(AuditEntry(
            actor_type=ActorType.HUMAN,
            actor_id=approved_by,
            action="memory.promote",
            scope=source_entry.scope,
            result=AuditResult.SUCCESS,
            target_type="memory",
            target_id=promoted.id,
            before={"source_id": source_entry.id, "source_kind": source_entry.kind.value},
            after={"promoted_id": promoted.id, "target_kind": target_kind.value},
            note=note,
        ))

        return promoted


# ---------------------------------------------------------------------- module-level convenience

_global_promoter: FactPromoter | None = None


def set_fact_promoter(promoter: FactPromoter) -> None:
    global _global_promoter
    _global_promoter = promoter


async def promote_to_fact(
    *,
    source_entry: MemoryEntry,
    approved_by: str,
    permission_ctx: PermissionContext,
) -> MemoryEntry:
    """Module-level convenience for fact promotion."""
    if _global_promoter is None:
        raise FactPromotionError("FactPromoter not initialized — call set_fact_promoter()")
    return await _global_promoter.promote(
        source_entry=source_entry,
        approved_by=approved_by,
        permission_ctx=permission_ctx,
        target_kind=MemoryKind.FACT,
    )


async def promote_to_decision(
    *,
    source_entry: MemoryEntry,
    approved_by: str,
    permission_ctx: PermissionContext,
) -> MemoryEntry:
    """Module-level convenience for decision promotion."""
    if _global_promoter is None:
        raise FactPromotionError("FactPromoter not initialized — call set_fact_promoter()")
    return await _global_promoter.promote(
        source_entry=source_entry,
        approved_by=approved_by,
        permission_ctx=permission_ctx,
        target_kind=MemoryKind.DECISION,
    )
