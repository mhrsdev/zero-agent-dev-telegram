"""Integration repository — integration reviews and merge proposals.

Per ``zero-project-isolation-evidence``: all queries filter by
``project_id`` before any row is loaded.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.integration import (
    CombinedTestResult,
    ConflictClassification,
    ConflictDetail,
    ImpactEntry,
    IntegrationReview,
    IntegrationReviewId,
    IntegrationReviewNotFoundError,
    IntegrationReviewState,
    MergeProposal,
    MergeProposalId,
    MergeProposalNotFoundError,
    MergeProposalState,
)
from zero.persistence.connection import Database


def _row_to_review(row: sqlite3.Row) -> IntegrationReview:
    source_task_ids = tuple(
        TaskId(tid) for tid in json.loads(row["source_task_ids"])
    )
    impact_set = tuple(
        ImpactEntry(
            file_path=e["file_path"],
            change_type=e["change_type"],  # type: ignore[arg-type]
            is_contract=e.get("is_contract", False),
        )
        for e in json.loads(row["impact_set"])
    )
    touched_contracts = tuple(json.loads(row["touched_contracts"]))
    conflict_details = tuple(
        ConflictDetail(
            conflict_type=c["conflict_type"],  # type: ignore[arg-type]
            description=c["description"],
            source_tasks=tuple(c.get("source_tasks", [])),
        )
        for c in json.loads(row["conflict_details"])
    )
    return IntegrationReview(
        id=IntegrationReviewId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        source_task_ids=source_task_ids,
        impact_set=impact_set,
        touched_contracts=touched_contracts,
        combined_test_result=row["combined_test_result"],  # type: ignore[arg-type]
        conflict_classification=row["conflict_classification"],  # type: ignore[arg-type]
        conflict_details=conflict_details,
        state=row["state"],  # type: ignore[arg-type]
        integration_worktree_id=row["integration_worktree_id"],
        reviewed_by=UserId(row["reviewed_by"]) if row["reviewed_by"] else None,
        redacted_summary=row["redacted_summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_proposal(row: sqlite3.Row) -> MergeProposal:
    return MergeProposal(
        id=MergeProposalId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        integration_review_id=IntegrationReviewId(
            row["integration_review_id"]
        ),
        execution_id=ExecutionId(row["execution_id"]),
        source_tasks=tuple(
            TaskId(tid) for tid in json.loads(row["source_tasks"])
        ),
        source_diffs=tuple(json.loads(row["source_diffs"])),
        checks_passed=bool(row["checks_passed"]),
        risks=tuple(json.loads(row["risks"])),
        state=row["state"],  # type: ignore[arg-type]
        approved_by=UserId(row["approved_by"]) if row["approved_by"] else None,
        merged_at=row["merged_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class IntegrationRepository:
    """Database-backed integration review and merge proposal repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Integration reviews
    # ------------------------------------------------------------------

    def insert_review(
        self, review: IntegrationReview, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO integration_reviews "
                "(id, project_id, execution_id, source_task_ids, "
                "impact_set, touched_contracts, combined_test_result, "
                "conflict_classification, conflict_details, state, "
                "integration_worktree_id, reviewed_by, redacted_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review.id.value,
                    review.project_id.value,
                    review.execution_id.value,
                    json.dumps([t.value for t in review.source_task_ids]),
                    json.dumps([
                        {
                            "file_path": e.file_path,
                            "change_type": e.change_type,
                            "is_contract": e.is_contract,
                        }
                        for e in review.impact_set
                    ]),
                    json.dumps(list(review.touched_contracts)),
                    review.combined_test_result,
                    review.conflict_classification,
                    json.dumps([
                        {
                            "conflict_type": c.conflict_type,
                            "description": c.description,
                            "source_tasks": list(c.source_tasks),
                        }
                        for c in review.conflict_details
                    ]),
                    review.state,
                    review.integration_worktree_id,
                    review.reviewed_by.value if review.reviewed_by else None,
                    review.redacted_summary,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_review(
        self, project_id: ProjectId, review_id: IntegrationReviewId
    ) -> IntegrationReview:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, source_task_ids, "
            "impact_set, touched_contracts, combined_test_result, "
            "conflict_classification, conflict_details, state, "
            "integration_worktree_id, reviewed_by, redacted_summary, "
            "created_at, updated_at FROM integration_reviews "
            "WHERE id = ? AND project_id = ?",
            (review_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise IntegrationReviewNotFoundError(
                f"Integration review {review_id} not found in project "
                f"{project_id}"
            )
        return _row_to_review(row)

    def list_reviews_for_execution(
        self, execution_id: ExecutionId
    ) -> list[IntegrationReview]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, source_task_ids, "
            "impact_set, touched_contracts, combined_test_result, "
            "conflict_classification, conflict_details, state, "
            "integration_worktree_id, reviewed_by, redacted_summary, "
            "created_at, updated_at FROM integration_reviews "
            "WHERE execution_id = ? ORDER BY created_at ASC",
            (execution_id.value,),
        )
        return [_row_to_review(row) for row in cursor.fetchall()]

    def update_review(
        self,
        review_id: IntegrationReviewId,
        *,
        combined_test_result: CombinedTestResult | None = None,
        conflict_classification: ConflictClassification | None = None,
        conflict_details: tuple[ConflictDetail, ...] | None = None,
        state: IntegrationReviewState | None = None,
        reviewed_by: UserId | None = None,
        redacted_summary: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        sets: list[str] = []
        params: list = []
        if combined_test_result is not None:
            sets.append("combined_test_result = ?")
            params.append(combined_test_result)
        if conflict_classification is not None:
            sets.append("conflict_classification = ?")
            params.append(conflict_classification)
        if conflict_details is not None:
            sets.append("conflict_details = ?")
            params.append(json.dumps([
                {
                    "conflict_type": c.conflict_type,
                    "description": c.description,
                    "source_tasks": list(c.source_tasks),
                }
                for c in conflict_details
            ]))
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if reviewed_by is not None:
            sets.append("reviewed_by = ?")
            params.append(reviewed_by.value)
        if redacted_summary is not None:
            sets.append("redacted_summary = ?")
            params.append(redacted_summary)
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        if not sets:
            return
        sql = f"UPDATE integration_reviews SET {', '.join(sets)} WHERE id = ?"
        params.append(review_id.value)
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise IntegrationReviewNotFoundError(
                f"Integration review {review_id} not found"
            )
        if commit:
            conn.commit()

    # ------------------------------------------------------------------
    # Merge proposals
    # ------------------------------------------------------------------

    def insert_proposal(
        self, proposal: MergeProposal, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO merge_proposals "
                "(id, project_id, integration_review_id, execution_id, "
                "source_tasks, source_diffs, checks_passed, risks, state, "
                "approved_by, merged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.id.value,
                    proposal.project_id.value,
                    proposal.integration_review_id.value,
                    proposal.execution_id.value,
                    json.dumps([t.value for t in proposal.source_tasks]),
                    json.dumps(list(proposal.source_diffs)),
                    1 if proposal.checks_passed else 0,
                    json.dumps(list(proposal.risks)),
                    proposal.state,
                    proposal.approved_by.value if proposal.approved_by else None,
                    proposal.merged_at,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_proposal(
        self, project_id: ProjectId, proposal_id: MergeProposalId
    ) -> MergeProposal:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, integration_review_id, execution_id, "
            "source_tasks, source_diffs, checks_passed, risks, state, "
            "approved_by, merged_at, created_at, updated_at "
            "FROM merge_proposals WHERE id = ? AND project_id = ?",
            (proposal_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise MergeProposalNotFoundError(
                f"Merge proposal {proposal_id} not found in project "
                f"{project_id}"
            )
        return _row_to_proposal(row)

    def list_proposals_for_execution(
        self, execution_id: ExecutionId
    ) -> list[MergeProposal]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, integration_review_id, execution_id, "
            "source_tasks, source_diffs, checks_passed, risks, state, "
            "approved_by, merged_at, created_at, updated_at "
            "FROM merge_proposals WHERE execution_id = ? "
            "ORDER BY created_at ASC",
            (execution_id.value,),
        )
        return [_row_to_proposal(row) for row in cursor.fetchall()]

    def update_proposal_state(
        self,
        proposal_id: MergeProposalId,
        new_state: MergeProposalState,
        *,
        approved_by: UserId | None = None,
        merged_at: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        sets: list[str] = ["state = ?"]
        params: list = [new_state]
        if approved_by is not None:
            sets.append("approved_by = ?")
            params.append(approved_by.value)
        if merged_at is not None:
            sets.append("merged_at = ?")
            params.append(merged_at)
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        sql = f"UPDATE merge_proposals SET {', '.join(sets)} WHERE id = ?"
        params.append(proposal_id.value)
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise MergeProposalNotFoundError(
                f"Merge proposal {proposal_id} not found"
            )
        if commit:
            conn.commit()
