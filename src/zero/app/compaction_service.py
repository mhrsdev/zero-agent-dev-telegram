"""Compaction service — pre-flush, fit, summary validation, atomic
replacement, no-thrash protection.

Per ``zero-context-memory`` SKILL.md §"Compact with a fixed degradation
ladder":

Before compaction:
1. capture the immutable source event range;
2. persist accepted memory deltas;
3. capture typed execution state;
4. persist or reserve the full transcript artifact;
5. prepare provider-safe summarizer input.

Fit summarizer input in this order only:
1. verbatim if it fits;
2. remove oldest historical turns first;
3. truncate individually oversized tool results;
4. remove oldest current-step turns;
5. retain and truncate the newest item only as an emergency fallback.

Per ``zero-context-memory`` §"Replace context only after durable
commit": the safe commit order is:
1. canonical memory transaction committed;
2. transcript/segment artifact stored and verified;
3. typed execution snapshot stored;
4. compact summary validated;
5. provider-ready history validated;
6. active context pointer advanced atomically;
7. audit event recorded.

Per ``zero-claude-token-economics`` §"Prune deterministically before
summarizing": use a no-thrash guard: after repeated compactions without
meaningful reclaimed space, stop and surface the oversized source.

Per PLAN.md M9 invariants:
- Compaction never replaces typed execution state or durable memory.
- Omitted material remains recoverable through immutable references.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from zero.app.artifact_service import ArtifactService
from zero.domain.artifacts import CompactionThrashError
from zero.domain.context import (
    CompactionBlockerError,
    CompactionRecord,
    CompactionRecordId,
    ContextVersion,
    ContextVersionId,
    estimate_tokens,
    exceeds_threshold,
)
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_compaction_record_id,
    generate_context_version_id,
)
from zero.persistence.repositories.context_repository import (
    ContextRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


#: The no-thrash threshold: if this many consecutive compactions do not
#: reclaim at least this percentage of the context, compaction is
#: blocked with a typed blocker.
NO_THRASH_MAX_CONSECUTIVE = 3
NO_THRASH_MIN_RECLAIM_PERCENT = 10


class CompactionService:
    """Compaction lifecycle: pre-flush, fit, summary validation, durable
    commit, atomic context replacement, no-thrash protection.

    Per ``zero-context-memory`` §"Compaction lifecycle with pre-flush,
    fit, summary validation, durable commit, atomic context replacement,
    and no-thrash protection".
    """

    def __init__(
        self,
        context_repo: ContextRepository,
        artifact_service: ArtifactService,
    ) -> None:
        self._context_repo = context_repo
        self._artifact_service = artifact_service

    def should_compact(
        self,
        execution_id: ExecutionId,
        context_window: int,
        threshold_percent: int = 85,
    ) -> bool:
        """Check whether the active context exceeds the compaction
        threshold."""
        cv = self._context_repo.get_active_context_version(execution_id)
        if cv is None:
            return False
        return exceeds_threshold(
            cv.token_count, context_window, threshold_percent
        )

    def compact(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        actor_id: UserId,
        system_message: str,
        user_prefix: str,
        plan_contract: str,
        execution_snapshot: str,
        conversation_messages: list[dict[str, Any]],
        context_window: int = 200000,
        threshold_percent: int = 85,
        summary: str | None = None,
    ) -> CompactionRecord:
        """Compact the conversation into a new context version.

        This is the canonical compaction entry point. It:

        1. Checks if compaction is needed (no-thrash guard).
        2. Captures the source event range.
        3. Stores the transcript as an immutable artifact.
        4. Fits the summarizer input using the degradation ladder.
        5. Validates the summary.
        6. Creates a new context version.
        7. Atomically activates the new context version.
        8. Records the compaction record.

        Per ``zero-context-memory`` §"A crash at any point must leave
        either the old context active or a fully recoverable new
        context."
        """
        # 1. Get the source context version.
        source_cv = self._context_repo.get_active_context_version(
            execution_id
        )
        source_version = source_cv.version if source_cv else 0
        # 2. Check no-thrash guard.
        latest_compaction = (
            self._context_repo.get_latest_compaction_record(execution_id)
        )
        no_thrash_count = 0
        if latest_compaction is not None:
            # Check if the last compaction reclaimed meaningful space.
            source_tokens = source_cv.token_count if source_cv else 0
            if source_tokens > 0:
                # We don't have the target token count yet, but we can
                # check if the last compaction's source was similar.
                no_thrash_count = latest_compaction.no_thrash_count
                if (
                    latest_compaction.state == "activated"
                    and source_tokens
                    >= context_window * threshold_percent // 100
                ):
                    # The last compaction didn't help; increment.
                    no_thrash_count = latest_compaction.no_thrash_count + 1
                    if no_thrash_count >= NO_THRASH_MAX_CONSECUTIVE:
                        # Block with a typed blocker.
                        record = CompactionRecord(
                            id=CompactionRecordId(
                                generate_compaction_record_id()
                            ),
                            project_id=project_id,
                            execution_id=execution_id,
                            source_context_version=source_version,
                            target_context_version=source_version,
                            source_event_range="{}",
                            summary="[no-thrash blocked]",
                            fit_rung="no_thrash_blocked",
                            state="no_thrash_blocked",
                            no_thrash_count=no_thrash_count,
                            created_at=_now_utc_iso(),
                        )
                        self._context_repo.insert_compaction_record(record)
                        raise CompactionThrashError(
                            f"Compaction thrash detected: "
                            f"{no_thrash_count} consecutive compactions "
                            f"without meaningful reclaimed space. "
                            f"Oversized source likely."
                        )
        # 3. Capture the source event range.
        source_event_range = json.dumps({
            "message_count": len(conversation_messages),
            "first_message_role": conversation_messages[0]["role"]
            if conversation_messages
            else None,
            "last_message_role": conversation_messages[-1]["role"]
            if conversation_messages
            else None,
        })
        # 4. Store the transcript as an immutable artifact.
        transcript_text = json.dumps(
            conversation_messages, ensure_ascii=False, indent=2
        )
        transcript_artifact = self._artifact_service.store_artifact(
            project_id=project_id,
            actor_id=actor_id,
            kind="transcript",
            content=transcript_text,
            producer=f"compaction:execution:{execution_id.value}",
            provenance=json.dumps({
                "execution_id": execution_id.value,
                "source_context_version": source_version,
            }),
        )
        # 5. Fit the summarizer input using the degradation ladder.
        fit_rung, fitted_messages = self._fit_messages(
            conversation_messages,
            context_window * 30 // 100,  # 30% of context for summary input
        )
        # 6. Validate the summary.
        if summary is None:
            # Generate a simple summary from the fitted messages.
            summary = self._generate_summary(fitted_messages)
        if not self._validate_summary(summary):
            raise CompactionBlockerError(
                "Compaction summary validation failed: summary is empty "
                "or too short"
            )
        # 7. Create the compaction record (pre-activation).
        target_version = source_version + 1
        record = CompactionRecord(
            id=CompactionRecordId(generate_compaction_record_id()),
            project_id=project_id,
            execution_id=execution_id,
            source_context_version=source_version,
            target_context_version=target_version,
            source_event_range=source_event_range,
            transcript_artifact_id=transcript_artifact.id,
            summary=summary,
            fit_rung=fit_rung,
            state="pre_flush",
            no_thrash_count=no_thrash_count,
            created_at=_now_utc_iso(),
        )
        self._context_repo.insert_compaction_record(record)
        # 8. Create the new context version (not yet active).
        new_cv = ContextVersion(
            id=ContextVersionId(generate_context_version_id()),
            project_id=project_id,
            execution_id=execution_id,
            version=target_version,
            active=False,
            system_message=system_message,
            user_prefix=user_prefix,
            plan_contract=plan_contract,
            execution_snapshot=execution_snapshot,
            retrieved_context="[]",
            conversation_tail=json.dumps(fitted_messages[-3:]),
            compaction_summary=summary,
            transcript_artifact_id=transcript_artifact.id,
            token_count=estimate_tokens(system_message)
            + estimate_tokens(user_prefix)
            + estimate_tokens(plan_contract)
            + estimate_tokens(execution_snapshot)
            + estimate_tokens(summary),
            created_at=_now_utc_iso(),
        )
        self._context_repo.insert_context_version(new_cv)
        # 9. Update compaction record state.
        self._context_repo.update_compaction_state(
            record.id, "committed"
        )
        # 10. Atomically activate the new context version.
        self._context_repo.activate_context_version(
            execution_id, target_version
        )
        # 11. Update compaction record to 'activated'.
        self._context_repo.update_compaction_state(
            record.id, "activated"
        )
        return self._context_repo.get_latest_compaction_record(execution_id)

    def _fit_messages(
        self,
        messages: list[dict[str, Any]],
        budget: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Fit messages into a budget using the degradation ladder.

        Per ``zero-context-memory`` reference: the fitting order is
        fixed:
        1. verbatim if it fits;
        2. remove oldest history first while preserving current work;
        3. truncate individually oversized tool results;
        4. remove oldest current step turns;
        5. retain and hard-truncate the newest item as an emergency
           fallback.
        """
        if not messages:
            return "verbatim", []
        total_tokens = sum(
            estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in messages
        )
        if total_tokens <= budget:
            return "verbatim", list(messages)
        # Rung 2: remove oldest history.
        kept = list(messages)
        history_omitted = 0
        while len(kept) > 1 and sum(
            estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in kept
        ) > budget:
            kept.pop(0)
            history_omitted += 1
        if kept and sum(
            estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in kept
        ) <= budget:
            return "history_turn_selected", kept
        # Rung 3: truncate oversized tool results.
        for i, m in enumerate(kept):
            if m.get("role") == "tool":
                content = str(m.get("content") or "")
                if estimate_tokens(content) > budget // 2:
                    truncated = content[: budget // 4] + "\n[...truncated...]"
                    kept[i] = {**m, "content": truncated}
        if kept and sum(
            estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in kept
        ) <= budget:
            return "tool_truncated", kept
        # Rung 4: remove oldest current-step turns (keep only the last).
        if len(kept) > 1:
            kept = [kept[-1]]
        if kept and sum(
            estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in kept
        ) <= budget:
            return "step_turns_selected", kept
        # Rung 5: emergency truncation of the newest item.
        if kept:
            newest = kept[0]
            content = str(newest.get("content") or "")
            max_bytes = max(1, budget) * 4
            truncated = content.encode("utf-8")[:max_bytes].decode(
                "utf-8", errors="ignore"
            )
            kept[0] = {**newest, "content": truncated}
        return "emergency", kept

    def _generate_summary(self, messages: list[dict[str, Any]]) -> str:
        """Generate a simple summary from fitted messages.

        In a real system, this would call a provider model. For now,
        we generate a deterministic summary from the message metadata.
        """
        if not messages:
            return "[empty conversation]"
        parts: list[str] = []
        parts.append(f"Compacted conversation with {len(messages)} messages.")
        for m in messages:
            role = m.get("role", "unknown")
            content = str(m.get("content") or "")
            # Include first 100 chars of each message.
            preview = content[:100] + ("..." if len(content) > 100 else "")
            parts.append(f"  {role}: {preview}")
        return "\n".join(parts)

    def _validate_summary(self, summary: str) -> bool:
        """Validate that the summary covers the required fields.

        Per ``zero-context-memory`` §"Validate summary coverage against
        typed state": a valid summary covers the current goal, accepted
        decisions, modified artifacts, unresolved tasks, blockers,
        failures, and the next safe action.

        For Phase 5, we check that the summary is non-empty and has a
        minimum length. A real implementation would check for required
        fields.
        """
        if not summary or not summary.strip():
            return False
        return not len(summary.strip()) < 10

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_active_context(
        self, execution_id: ExecutionId
    ) -> ContextVersion | None:
        return self._context_repo.get_active_context_version(execution_id)

    def get_latest_compaction(
        self, execution_id: ExecutionId
    ) -> CompactionRecord | None:
        return self._context_repo.get_latest_compaction_record(execution_id)

    def list_compaction_records(
        self, execution_id: ExecutionId
    ) -> list[CompactionRecord]:
        return self._context_repo.list_compaction_records(execution_id)
