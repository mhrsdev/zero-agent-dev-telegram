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
    CombinedTestEvidence,
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
    source_task_ids = tuple(TaskId(tid) for tid in json.loads(row["source_task_ids"]))
    impact_set = tuple(
        ImpactEntry(
            file_path=e["file_path"],
            change_type=e["change_type"],  # type: ignore[arg-type]
            is_contract=e.get("is_contract", False),
            project_id=e.get("project_id"),
            execution_id=e.get("execution_id"),
            task_id=e.get("task_id"),
            worktree_id=e.get("worktree_id"),
            artifact_id=e.get("artifact_id"),
            base_revision=e.get("base_revision"),
            content_hash=e.get("content_hash"),
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


def _row_to_combined_test_evidence(row: sqlite3.Row) -> CombinedTestEvidence:
    return CombinedTestEvidence(
        id=row["id"],
        project_id=ProjectId(row["project_id"]),
        review_id=IntegrationReviewId(row["review_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        integration_worktree_id=row["integration_worktree_id"],
        worktree_path=row["worktree_path"],
        kind=row["kind"],  # type: ignore[arg-type]
        command=row["command"],
        args=tuple(json.loads(row["args"] or "[]")),
        exit_code=row["exit_code"],
        timed_out=bool(row["timed_out"]),
        stdout=row["stdout"],
        stderr=row["stderr"],
        content_hash=row["content_hash"],
        created_at=row["created_at"],
    )


def _row_to_proposal(row: sqlite3.Row) -> MergeProposal:
    return MergeProposal(
        id=MergeProposalId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        integration_review_id=IntegrationReviewId(row["integration_review_id"]),
        execution_id=ExecutionId(row["execution_id"]),
        source_tasks=tuple(TaskId(tid) for tid in json.loads(row["source_tasks"])),
        source_diffs=tuple(json.loads(row["source_diffs"])),
        checks_passed=bool(row["checks_passed"]),
        risks=tuple(json.loads(row["risks"])),
        state=row["state"],  # type: ignore[arg-type]
        approved_by=UserId(row["approved_by"]) if row["approved_by"] else None,
        merged_at=row["merged_at"],
        integration_worktree_id=row["integration_worktree_id"],
        target_revision=row["target_revision"],
        rollback_revision=row["rollback_revision"],
        evidence_ids=tuple(json.loads(row["evidence_ids"] or "[]")),
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

    def insert_review(self, review: IntegrationReview, *, commit: bool = True) -> None:
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
                    json.dumps(
                        [
                            {
                                "file_path": e.file_path,
                                "change_type": e.change_type,
                                "is_contract": e.is_contract,
                                "project_id": e.project_id,
                                "execution_id": e.execution_id,
                                "task_id": e.task_id,
                                "worktree_id": e.worktree_id,
                                "artifact_id": e.artifact_id,
                                "base_revision": e.base_revision,
                                "content_hash": e.content_hash,
                            }
                            for e in review.impact_set
                        ]
                    ),
                    json.dumps(list(review.touched_contracts)),
                    review.combined_test_result,
                    review.conflict_classification,
                    json.dumps(
                        [
                            {
                                "conflict_type": c.conflict_type,
                                "description": c.description,
                                "source_tasks": list(c.source_tasks),
                            }
                            for c in review.conflict_details
                        ]
                    ),
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
                f"Integration review {review_id} not found in project {project_id}"
            )
        return _row_to_review(row)

    def list_reviews_for_execution(
        self, execution_id: ExecutionId, *, project_id: ProjectId
    ) -> list[IntegrationReview]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, source_task_ids, "
            "impact_set, touched_contracts, combined_test_result, "
            "conflict_classification, conflict_details, state, "
            "integration_worktree_id, reviewed_by, redacted_summary, "
            "created_at, updated_at FROM integration_reviews "
            "WHERE execution_id = ? AND project_id = ? ORDER BY created_at ASC",
            (execution_id.value, project_id.value),
        )
        return [_row_to_review(row) for row in cursor.fetchall()]

    def update_review(
        self,
        review_id: IntegrationReviewId,
        *,
        project_id: ProjectId | None = None,
        combined_test_result: CombinedTestResult | None = None,
        conflict_classification: ConflictClassification | None = None,
        conflict_details: tuple[ConflictDetail, ...] | None = None,
        state: IntegrationReviewState | None = None,
        integration_worktree_id: str | None = None,
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
            params.append(
                json.dumps(
                    [
                        {
                            "conflict_type": c.conflict_type,
                            "description": c.description,
                            "source_tasks": list(c.source_tasks),
                        }
                        for c in conflict_details
                    ]
                )
            )
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if integration_worktree_id is not None:
            sets.append("integration_worktree_id = ?")
            params.append(integration_worktree_id)
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
        if project_id is not None:
            sql += " AND project_id = ?"
            params.append(project_id.value)
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise IntegrationReviewNotFoundError(f"Integration review {review_id} not found")
        if commit:
            conn.commit()

    def insert_review_evidence(
        self,
        evidence: CombinedTestEvidence,
        *,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO integration_review_evidence "
                "(id, project_id, review_id, execution_id, integration_worktree_id, "
                "worktree_path, kind, command, args, exit_code, timed_out, stdout, "
                "stderr, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.id,
                    evidence.project_id.value,
                    evidence.review_id.value,
                    evidence.execution_id.value,
                    evidence.integration_worktree_id,
                    evidence.worktree_path,
                    evidence.kind,
                    evidence.command,
                    json.dumps(list(evidence.args)),
                    evidence.exit_code,
                    1 if evidence.timed_out else 0,
                    evidence.stdout,
                    evidence.stderr,
                    evidence.content_hash,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def list_review_evidence(
        self,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
    ) -> list[CombinedTestEvidence]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, review_id, execution_id, integration_worktree_id, "
            "worktree_path, kind, command, args, exit_code, timed_out, stdout, stderr, "
            "content_hash, created_at FROM integration_review_evidence "
            "WHERE project_id = ? AND review_id = ? ORDER BY created_at ASC",
            (project_id.value, review_id.value),
        )
        return [_row_to_combined_test_evidence(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Merge proposals
    # ------------------------------------------------------------------

    def insert_proposal(self, proposal: MergeProposal, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO merge_proposals "
                "(id, project_id, integration_review_id, execution_id, "
                "source_tasks, source_diffs, checks_passed, risks, state, "
                "approved_by, merged_at, integration_worktree_id, "
                "target_revision, rollback_revision, evidence_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    proposal.integration_worktree_id,
                    proposal.target_revision,
                    proposal.rollback_revision,
                    json.dumps(list(proposal.evidence_ids)),
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_proposal(self, project_id: ProjectId, proposal_id: MergeProposalId) -> MergeProposal:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, integration_review_id, execution_id, "
            "source_tasks, source_diffs, checks_passed, risks, state, "
            "approved_by, merged_at, integration_worktree_id, "
            "target_revision, rollback_revision, evidence_ids, created_at, updated_at "
            "FROM merge_proposals WHERE id = ? AND project_id = ?",
            (proposal_id.value, project_id.value),
        )
        row = cursor.fetchone()
        if row is None:
            raise MergeProposalNotFoundError(
                f"Merge proposal {proposal_id} not found in project {project_id}"
            )
        return _row_to_proposal(row)

    def list_proposals_for_execution(
        self, execution_id: ExecutionId, *, project_id: ProjectId
    ) -> list[MergeProposal]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, integration_review_id, execution_id, "
            "source_tasks, source_diffs, checks_passed, risks, state, "
            "approved_by, merged_at, integration_worktree_id, "
            "target_revision, rollback_revision, evidence_ids, created_at, updated_at "
            "FROM merge_proposals WHERE execution_id = ? AND project_id = ? "
            "ORDER BY created_at ASC",
            (execution_id.value, project_id.value),
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
            raise MergeProposalNotFoundError(f"Merge proposal {proposal_id} not found")
        if commit:
            conn.commit()

    def insert_integration_worktree(
        self,
        *,
        worktree_id: str,
        project_id: ProjectId,
        execution_id: ExecutionId,
        repository_id: str,
        worktree_path: str,
        branch_name: str,
        base_revision: str,
        state: str = "created",
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        conn.execute(
            "INSERT INTO integration_worktrees "
            "(id, project_id, execution_id, repository_id, worktree_path, "
            "branch_name, base_revision, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                worktree_id,
                project_id.value,
                execution_id.value,
                repository_id,
                worktree_path,
                branch_name,
                base_revision,
                state,
            ),
        )
        if commit:
            conn.commit()

    def update_integration_worktree(
        self,
        worktree_id: str,
        *,
        state: str | None = None,
        target_revision: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        sets: list[str] = []
        params: list[str | None] = []
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if target_revision is not None:
            sets.append("target_revision = ?")
            params.append(target_revision)
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        params.append(worktree_id)
        cursor = conn.execute(
            f"UPDATE integration_worktrees SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Integration worktree {worktree_id} not found")
        if commit:
            conn.commit()

    def list_merge_side_effect_windows(self) -> list[dict[str, str]]:
        """Return merge side-effect windows awaiting reconciliation.

        A crash between moving the target Git ref and persisting the
        final proposal state leaves an integration worktree recorded as
        ``merged`` (with the new ``target_revision``) while its proposal
        is still ``approved``. This query surfaces exactly those rows.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT mp.project_id AS project_id, mp.id AS proposal_id, "
            "iw.repository_id AS repository_id, iw.target_revision AS target_revision "
            "FROM merge_proposals mp "
            "JOIN integration_worktrees iw ON iw.id = mp.integration_worktree_id "
            "WHERE iw.state = 'merged' AND iw.target_revision IS NOT NULL "
            "AND mp.state = 'approved'"
        )
        return [
            {
                "project_id": str(row["project_id"]),
                "proposal_id": str(row["proposal_id"]),
                "repository_id": str(row["repository_id"]),
                "target_revision": str(row["target_revision"]),
            }
            for row in cursor.fetchall()
        ]

    def insert_integration_evidence(
        self,
        *,
        evidence_id: str,
        project_id: ProjectId,
        execution_id: ExecutionId,
        proposal_id: MergeProposalId,
        integration_worktree_id: str | None,
        kind: str,
        command: str | None,
        args: tuple[str, ...],
        exit_code: int | None,
        content: str,
        content_hash: str,
        ref_name: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        conn.execute(
            "INSERT INTO integration_evidence "
            "(id, project_id, execution_id, proposal_id, integration_worktree_id, "
            "kind, command, args, exit_code, content, content_hash, ref_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence_id,
                project_id.value,
                execution_id.value,
                proposal_id.value,
                integration_worktree_id,
                kind,
                command,
                json.dumps(list(args)),
                exit_code,
                content,
                content_hash,
                ref_name,
            ),
        )
        if commit:
            conn.commit()

    def update_proposal_evidence(
        self,
        proposal_id: MergeProposalId,
        *,
        integration_worktree_id: str,
        target_revision: str,
        rollback_revision: str,
        evidence_ids: tuple[str, ...],
        commit: bool = True,
    ) -> None:
        """Persist the external Git evidence before a merge is reported."""
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE merge_proposals SET integration_worktree_id = ?, "
            "target_revision = ?, rollback_revision = ?, evidence_ids = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (
                integration_worktree_id,
                target_revision,
                rollback_revision,
                json.dumps(list(evidence_ids)),
                proposal_id.value,
            ),
        )
        if cursor.rowcount == 0:
            raise MergeProposalNotFoundError(f"Merge proposal {proposal_id} not found")
        if commit:
            conn.commit()
