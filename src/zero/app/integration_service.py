"""Integration service — compatibility review, conflict detection, merge gates.

Per ``zero-agent-execution-lifecycle`` SKILL.md §"Integration is impact
review, not diff aesthetics":

- The Integration / Compatibility Sub Agent is another dynamic type
  with a special responsibility: determine whether independently
  correct changes remain correct together.
- Its useful input begins with: immutable base revisions, diffs and
  changed paths, touched contracts and dependencies, schema/type/API/
  config changes, test results and failure artifacts, and approved plan
  constraints.
- Broad repository reading becomes justified only when impact analysis
  points outward.

Per ``zero-planner-worker-contract`` §"Merge is a controlled product
transition": a clean Git merge proves textual compatibility. Zero
additionally needs combined tests, migration ordering, contract
compatibility, required human decisions, source task and approval
provenance, authority to merge, and recoverable target state.

Per PLAN.md M11 invariants:
- Low-risk deterministic conflicts may be resolved by policy; product
  decisions return to humans.
- Merge requires explicit authority and passing gates.
- Post-integration memory/RAG update only from accepted results.
"""

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import (
    AuditEvent,
    AuditEventId,
    AuditSource,
    redact_sensitive_text,
)
from zero.domain.execution import ExecutionId, TaskId
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_integration_evidence_id,
    generate_integration_review_id,
    generate_integration_test_evidence_id,
    generate_integration_worktree_id,
    generate_merge_proposal_id,
)
from zero.domain.integration import (
    CombinedTestEvidence,
    CombinedTestResult,
    ConflictClassification,
    ConflictDetail,
    ImpactEntry,
    IntegrationReview,
    IntegrationReviewId,
    MergeGateError,
    MergeProposal,
    MergeProposalId,
)
from zero.domain.worktrees import RepositoryId
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.execution_repository import ExecutionRepository
from zero.persistence.repositories.integration_repository import (
    IntegrationRepository,
)
from zero.persistence.repositories.worktree_repository import (
    WorktreeRepository,
)

logger = logging.getLogger(__name__)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024


def _descendant_pids(root_pid: int) -> tuple[int, ...]:
    """Return Linux child-process descendants while the root is alive."""
    seen: set[int] = set()
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        children_path = Path(f"/proc/{parent_pid}/task/{parent_pid}/children")
        try:
            child_ids = [int(value) for value in children_path.read_text().split()]
        except (OSError, ValueError):
            continue
        for child_pid in child_ids:
            if child_pid not in seen:
                seen.add(child_pid)
                pending.append(child_pid)
    return tuple(seen)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill a process group and descendants that created a new session."""
    for child_pid in _descendant_pids(process.pid):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _run_bounded_git_process(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_bytes: int,
    stdin=None,
) -> tuple[str, int, bool]:
    """Run a Git read/apply command without unbounded output buffering.

    A dedicated reader thread drains stdout so the parent never blocks
    on a full pipe; this also works on Windows where ``select()`` cannot
    wait on pipes.
    """
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdin=stdin if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    def terminate_process_group() -> None:
        _terminate_process_tree(process)

    chunks: list[bytes] = []
    total = 0

    def drain() -> None:
        nonlocal total, truncated
        assert process.stdout is not None
        while True:
            try:
                data = process.stdout.read(8192)
            except (OSError, ValueError):
                return
            if not data:
                return
            room = max_bytes + 1 - total
            if room > 0:
                chunks.append(data[:room])
                total += min(len(data), room)
            if total > max_bytes:
                truncated = True
                terminate_process_group()

    truncated = False
    reader = threading.Thread(target=drain, name="zero-bounded-git-reader", daemon=True)
    reader.start()
    try:
        reader.join(timeout=timeout)
        if reader.is_alive():
            terminate_process_group()
            reader.join(timeout=5)
            raise subprocess.TimeoutExpired(args, timeout)
        if (truncated or total > max_bytes) and process.poll() is None:
            terminate_process_group()
        process.wait(timeout=5)
    except BaseException:
        terminate_process_group()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    raw = b"".join(chunks)
    return raw[:max_bytes].decode("utf-8", errors="replace"), process.returncode, truncated


class IntegrationService:
    """Application operations for integration review and controlled merge.

    The service:
    - derives the impact set from task outputs (diffs, touched
      contracts);
    - detects conflicts (file collisions, schema/API/type/config
      conflicts);
    - classifies conflicts (none, low_risk, human_decision_required);
    - creates merge proposals with source tasks, diffs, checks, risks;
    - enforces merge gates (explicit authority, passing checks);
    - updates memory/RAG only from accepted results.
    """

    def __init__(
        self,
        integration_repo: IntegrationRepository,
        worktree_repo: WorktreeRepository,
        audit_repo: AuditRepository,
        authorization_service: AuthorizationService,
        execution_repo: ExecutionRepository | None = None,
        allowed_commands: tuple[str, ...] | frozenset[str] = (),
        max_test_timeout_seconds: int = 300,
        max_test_output_bytes: int = 64 * 1024,
    ) -> None:
        self._repo = integration_repo
        self._worktree_repo = worktree_repo
        self._audit_repo = audit_repo
        self._authz = authorization_service
        self._execution_repo = execution_repo
        self._allowed_commands = frozenset(allowed_commands)
        self._max_test_timeout_seconds = max_test_timeout_seconds
        self._max_test_output_bytes = max_test_output_bytes

    # ------------------------------------------------------------------
    # Impact-set derivation
    # ------------------------------------------------------------------

    def derive_impact_set(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        task_ids: tuple[TaskId, ...],
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> tuple[ImpactEntry, ...]:
        """Derive impact entries without discarding source provenance.

        A path is not an identity: two tasks may touch the same path and
        every contribution must survive into conflict analysis. Resolution
        is scoped by the complete project/execution/task/worktree tuple.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="execution.view_diffs",
            source=source,
        )
        impact: list[ImpactEntry] = []
        seen: set[tuple[str, str, str]] = set()
        for task_id in task_ids:
            worktree = self._resolve_worktree(
                project_id=project_id,
                execution_id=execution_id,
                task_id=task_id,
            )
            if worktree is None:
                raise MergeGateError(
                    f"No worktree bound to task {task_id.value} in execution {execution_id.value}"
                )
            artifacts = self._worktree_repo.list_artifacts_for_task_in_lineage(
                worktree.project_id,
                execution_id,
                task_id,
                worktree.id,
                kind="diff",
            )
            if not artifacts:
                raise MergeGateError(f"Task {task_id.value} has no captured diff artifact")
            for artifact in artifacts:
                for line in artifact.content.splitlines():
                    path = self._extract_path_from_diff_line(line)
                    if not path:
                        continue
                    key = (task_id.value, artifact.id.value, path)
                    if key in seen:
                        continue
                    seen.add(key)
                    if line.startswith("??"):
                        change_type = "added"
                    elif line.startswith("-") and not line.startswith("---"):
                        change_type = "deleted"
                    else:
                        change_type = "modified"
                    impact.append(
                        ImpactEntry(
                            file_path=path,
                            change_type=change_type,  # type: ignore[arg-type]
                            is_contract=self._is_contract_file(path),
                            project_id=worktree.project_id.value,
                            execution_id=execution_id.value,
                            task_id=task_id.value,
                            worktree_id=worktree.id.value,
                            artifact_id=artifact.id.value,
                            base_revision=worktree.base_revision,
                            content_hash=artifact.content_hash,
                        )
                    )
        return tuple(impact)

    def _resolve_worktree(
        self,
        *,
        project_id: ProjectId | None,
        execution_id: ExecutionId,
        task_id: TaskId,
    ):
        """Resolve and verify task lineage before loading diff content."""
        if project_id is not None:
            worktree = self._worktree_repo.get_worktree_for_task_in_execution(
                project_id, execution_id, task_id
            )
        else:
            worktree = self._worktree_repo.get_worktree_for_task(task_id)
            if worktree is not None and worktree.execution_id != execution_id:
                worktree = None
        if worktree is None:
            return None
        if self._execution_repo is not None:
            task = self._execution_repo.get_task(task_id)
            if task.project_id != worktree.project_id or task.execution_id != execution_id:
                raise MergeGateError(
                    f"Task {task_id.value} does not belong to the requested project/execution"
                )
        return worktree

    def _extract_path_from_diff_line(self, line: str) -> str | None:
        """Extract a file path from a diff or status line."""
        if line.startswith("?? "):
            return line[3:].strip()
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/"):
            return line[6:].strip()
        if line.startswith("A "):
            return line[2:].strip()
        if line.startswith("M "):
            return line[2:].strip()
        if line.startswith("D "):
            return line[2:].strip()
        return None

    def _is_contract_file(self, path: str) -> bool:
        """Heuristic: determine if a file is a contract (schema, API,
        type, config) that other tasks may depend on."""
        path_lower = path.lower().strip()
        # Strip trailing slash (git status may show "api/" for an
        # untracked directory).
        path_stripped = path_lower.rstrip("/")
        # Check for contract file extensions.
        contract_extensions = (
            ".json",
            ".yaml",
            ".yml",
            ".proto",
            ".graphql",
        )
        if path_stripped.endswith(contract_extensions):
            return True
        # Check for contract path segments (as directory or file).
        contract_segments = (
            "schema",
            "api",
            "types",
            "interfaces",
            "contracts",
            "proto",
            "graphql",
        )
        parts = path_stripped.split("/")
        for part in parts:
            if part in contract_segments:
                return True
        # Check for contract file names.
        contract_names = ("schema.sql",)
        for name in contract_names:
            if name in path_stripped:
                return True
        # Check for migration files.
        return "migration" in path_stripped

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def detect_conflicts(
        self,
        impact_set: tuple[ImpactEntry, ...],
        task_ids: tuple[TaskId, ...],
    ) -> tuple[ConflictDetail, ...]:
        """Detect conflicts between tasks' changes.

        Per PLAN.md M11: "Conflicting schema/type/API changes are
        detected."

        For Phase 6, we detect:
        - File collisions: multiple tasks modify the same file.
        - Contract conflicts: multiple tasks modify the same contract
          file.

        We do NOT detect semantic conflicts (e.g. one task renames a
        field while another adds a serializer for it). That requires
        deeper analysis and may return to humans.
        """
        conflicts: list[ConflictDetail] = []
        path_to_tasks: dict[str, list[str]] = {}
        for entry in impact_set:
            task_str = entry.task_id or (task_ids[0].value if task_ids else "unknown")
            path_to_tasks.setdefault(entry.file_path, [])
            if task_str not in path_to_tasks[entry.file_path]:
                path_to_tasks[entry.file_path].append(task_str)
        for path, tasks in path_to_tasks.items():
            entries = [entry for entry in impact_set if entry.file_path == path]
            if not any(entry.is_contract for entry in entries) and len(tasks) < 2:
                continue
            conflicts.append(
                ConflictDetail(
                    conflict_type="schema"
                    if "schema" in path
                    else "api"
                    if "api" in path
                    else "type"
                    if "type" in path
                    else "config"
                    if any(e.is_contract for e in entries)
                    else "file_collision",
                    description=(
                        f"Contract/file {path} was modified by {len(tasks)} task(s); "
                        "compatibility review required"
                    ),
                    source_tasks=tuple(tasks),
                )
            )
        return tuple(conflicts)

    # ------------------------------------------------------------------
    # Integration review
    # ------------------------------------------------------------------

    def create_review(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        source_task_ids: tuple[TaskId, ...],
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> IntegrationReview:
        """Create an integration review for multiple task outputs.

        Per PLAN.md M11: "Compatibility review contract and evidence
        format."
        """
        if not source_task_ids:
            raise ValueError("source_task_ids must not be empty")
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        if self._execution_repo is not None:
            execution = self._execution_repo.get_execution(execution_id)
            if execution.project_id != project_id:
                raise MergeGateError("Execution does not belong to project")
        impact_set = self.derive_impact_set(
            execution_id=execution_id,
            task_ids=source_task_ids,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        touched_contracts = tuple(e.file_path for e in impact_set if e.is_contract)
        # Detect conflicts.
        conflicts = self.detect_conflicts(impact_set, source_task_ids)
        # Classify.
        if not conflicts:
            classification: ConflictClassification = "none"
        elif all(c.conflict_type in ("config",) for c in conflicts):
            classification = "low_risk"
        else:
            classification = "human_decision_required"
        review = IntegrationReview(
            id=IntegrationReviewId(generate_integration_review_id()),
            project_id=project_id,
            execution_id=execution_id,
            source_task_ids=source_task_ids,
            impact_set=impact_set,
            touched_contracts=touched_contracts,
            combined_test_result="not_run",
            conflict_classification=classification,
            conflict_details=conflicts,
            state="reviewing",
            redacted_summary=(
                f"Reviewing {len(source_task_ids)} tasks; "
                f"{len(impact_set)} impacted files; "
                f"{len(conflicts)} conflicts"
            ),
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_review(review)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="integration.review",
                target_type="integration_review",
                target_id=review.id.value,
                result="success",
                redacted_summary=review.redacted_summary or "",
                correlation_id=execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return review

    def record_combined_test_result(
        self,
        *,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
        result: CombinedTestResult,
        actor_id: UserId,
        integration_worktree_id: str | None = None,
        source: AuditSource = "system",
    ) -> IntegrationReview:
        """Record the combined test result.

        Per PLAN.md M11: "Combined test/integration workspace isolated
        from task worktrees."
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        review = self._repo.get_review(project_id, review_id)
        # human_decision_required.
        new_classification = review.conflict_classification
        if result == "fail" and review.conflict_details:
            new_classification = "human_decision_required"
        self._repo.update_review(
            review_id,
            project_id=project_id,
            combined_test_result=result,
            conflict_classification=new_classification,
            state="human_decision_paused"
            if new_classification == "human_decision_required"
            else "approved"
            if result == "pass"
            else "rejected",
            integration_worktree_id=integration_worktree_id,
            reviewed_by=actor_id,
            redacted_summary=(f"Combined tests: {result}; classification: {new_classification}"),
        )
        return self._repo.get_review(project_id, review_id)

    def run_combined_tests(
        self,
        *,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
        command: str,
        args: tuple[str, ...] = (),
        actor_id: UserId,
        timeout_seconds: int = 300,
        source: AuditSource = "system",
    ) -> IntegrationReview:
        """Run the configured test command on a disposable combined workspace.

        The source task worktrees are never mutated and the target repository
        ref is never advanced.  A non-zero result is durable evidence of a
        rejected or human-paused review, not a successful scheduler tick.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        self._validate_test_command(command, args, timeout_seconds)
        review = self._repo.get_review(project_id, review_id)
        if review.state not in {"reviewing", "pending"}:
            raise MergeGateError(f"Cannot run combined tests from review state {review.state!r}")

        integration_path: Path | None = None
        target_path: Path | None = None
        worktree_id: str | None = None
        worktree_persisted = False
        target_sha: str | None = None
        exit_code: int | None = None
        timed_out = False
        stdout = ""
        stderr = ""
        evidence_kind: str = "test"
        result: CombinedTestResult = "fail"
        try:
            source_worktrees = []
            repositories = []
            for task_id in review.source_task_ids:
                worktree = self._resolve_worktree(
                    project_id=project_id,
                    execution_id=review.execution_id,
                    task_id=task_id,
                )
                if worktree is None:
                    raise MergeGateError(f"Missing source worktree for {task_id.value}")
                repository = self._worktree_repo.get_repository(project_id, worktree.repository_id)
                source_worktrees.append(worktree)
                repositories.append(repository)
            if not repositories or len({repo.id.value for repo in repositories}) != 1:
                raise MergeGateError("Combined tests require one repository")
            repository = repositories[0]
            target_path = Path(repository.local_path).resolve()
            if not target_path.is_dir():
                raise MergeGateError("Integration repository path is unavailable")
            target_ref = repository.default_base_revision or "main"
            target_sha = self._git_output(target_path, "rev-parse", target_ref)
            if self._git_output(target_path, "status", "--porcelain"):
                raise MergeGateError("Target repository must be clean before combined tests")
            for worktree in source_worktrees:
                # Real-run fix (2026-08-28): chained task worktrees branch
                # from their dependencies' evidence-checkpoint commits, so
                # their base_revision is legitimately AHEAD of the target
                # ref; the historical equality check rejected exactly that.
                # The correct invariant is that the target ref is an
                # ancestor of the source worktree's branch.
                source_probe_path = Path(worktree.worktree_path).resolve()
                source_probe_ref = self._git_output(
                    source_probe_path, "symbolic-ref", "--short", "HEAD"
                )
                source_probe_sha = self._git_output(source_probe_path, "rev-parse", source_probe_ref)
                if not self._git_status_success(
                    source_probe_path,
                    "merge-base",
                    "--is-ancestor",
                    target_sha,
                    source_probe_sha,
                ):
                    raise MergeGateError(
                        "Source worktree branch is not based on the target ref"
                    )

            worktree_id = generate_integration_worktree_id()
            parent = Path(tempfile.gettempdir()) / "zero-integration-worktrees"
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            integration_path = Path(
                tempfile.mkdtemp(prefix=f"review-{review_id.value}-", dir=parent)
            )
            integration_path.rmdir()
            self._git(
                target_path,
                "worktree",
                "add",
                "--detach",
                str(integration_path),
                target_sha,
            )
            self._repo.insert_integration_worktree(
                worktree_id=worktree_id,
                project_id=project_id,
                execution_id=review.execution_id,
                repository_id=repository.id.value,
                worktree_path=str(integration_path),
                branch_name=f"review/{review_id.value}",
                base_revision=target_sha,
                state="prepared",
            )
            worktree_persisted = True
            for worktree in source_worktrees:
                source_path = Path(worktree.worktree_path).resolve()
                source_branch = self._git_output(source_path, "symbolic-ref", "--short", "HEAD")
                branch_sha = self._git_output(source_path, "rev-parse", source_branch)
                if branch_sha != target_sha:
                    # Real-run fix (2026-08-28): task branches now carry
                    # evidence-checkpoint commits (worktree chaining), so
                    # this merge actually runs for the first time. The
                    # historical --no-commit form left MERGE_HEAD pending,
                    # and the SECOND source's merge then failed with
                    # "You have not concluded your merge". Each source
                    # merge is now concluded (committed) before the next
                    # source is integrated, with an injected identity.
                    self._git(
                        integration_path,
                        "-c",
                        "user.name=Zero Integration",
                        "-c",
                        "user.email=zero-integration@localhost",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        source_branch,
                    )
                uncommitted = self._git_output(source_path, "diff", "--binary")
                if uncommitted:
                    self._git_input(integration_path, ("apply", "--index", "-"), uncommitted)
                self._copy_untracked_files(source_path, integration_path)

            exit_code, timed_out, stdout, stderr = self._run_bounded_test(
                command,
                args,
                cwd=integration_path,
                timeout_seconds=timeout_seconds,
            )
            result = "pass" if exit_code == 0 and not timed_out else "fail"
            self._repo.update_integration_worktree(
                worktree_id,
                state="checks_passed" if result == "pass" else "failed",
            )
        except Exception as exc:
            evidence_kind = "failure"
            stderr = redact_sensitive_text(str(exc))[: self._max_test_output_bytes]
            result = "fail"
            if worktree_id is None:
                raise
        finally:
            if worktree_persisted and integration_path is not None:
                assert worktree_id is not None
                evidence = CombinedTestEvidence(
                    id=generate_integration_test_evidence_id(),
                    project_id=project_id,
                    review_id=review_id,
                    execution_id=review.execution_id,
                    integration_worktree_id=worktree_id,
                    worktree_path=str(integration_path),
                    kind=evidence_kind,  # type: ignore[arg-type]
                    command=command,
                    args=args,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    stdout=redact_sensitive_text(stdout)[: self._max_test_output_bytes],
                    stderr=redact_sensitive_text(stderr)[: self._max_test_output_bytes],
                    content_hash=hashlib.sha256(
                        (stdout + "\0" + stderr).encode("utf-8", errors="replace")
                    ).hexdigest(),
                    created_at=_now_utc_iso(),
                )
                self._repo.insert_review_evidence(evidence)
                if target_path is not None:
                    try:
                        self._git(
                            target_path, "worktree", "remove", "--force", str(integration_path)
                        )
                    except MergeGateError:
                        shutil.rmtree(integration_path, ignore_errors=True)
                else:
                    shutil.rmtree(integration_path, ignore_errors=True)
                self._repo.update_integration_worktree(worktree_id, state="removed")
            elif integration_path is not None:
                shutil.rmtree(integration_path, ignore_errors=True)

        return self.record_combined_test_result(
            project_id=project_id,
            review_id=review_id,
            result=result,
            integration_worktree_id=worktree_id,
            actor_id=actor_id,
            source=source,
        )

    def list_review_evidence(
        self,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
    ) -> list[CombinedTestEvidence]:
        return self._repo.list_review_evidence(project_id, review_id)

    def _validate_test_command(
        self,
        command: str,
        args: tuple[str, ...],
        timeout_seconds: int,
    ) -> None:
        if (
            not command
            or Path(command).name != command
            or "/" in command
            or "\\\\" in command
            or command not in self._allowed_commands
        ):
            raise MergeGateError("combined test command is not permitted by policy")
        if not isinstance(args, tuple) or len(args) > 64:
            raise MergeGateError("combined test arguments exceed policy limit")
        if any(not isinstance(arg, str) or "\\x00" in arg or len(arg) > 8192 for arg in args):
            raise MergeGateError("combined test argument violates policy")
        if timeout_seconds < 1 or timeout_seconds > self._max_test_timeout_seconds:
            raise MergeGateError("combined test timeout is outside policy")

    def _run_bounded_test(
        self,
        command: str,
        args: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> tuple[int | None, bool, str, str]:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(cwd),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        proc = subprocess.Popen(
            [command, *args],
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            _terminate_process_tree(proc)
            stdout_bytes, stderr_bytes = proc.communicate()
            if not stdout_bytes and exc.output:
                stdout_bytes = exc.output
            if not stderr_bytes and exc.stderr:
                stderr_bytes = exc.stderr

        def bounded(value: bytes | str | None) -> str:
            if value is None:
                return ""
            text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            encoded = text.encode("utf-8")
            if len(encoded) > self._max_test_output_bytes:
                return encoded[: self._max_test_output_bytes].decode("utf-8", errors="replace")
            return text

        return (
            None if timed_out else proc.returncode,
            timed_out,
            bounded(stdout_bytes),
            bounded(stderr_bytes),
        )

    def get_review(
        self, project_id: ProjectId, review_id: IntegrationReviewId
    ) -> IntegrationReview:
        return self._repo.get_review(project_id, review_id)

    def list_reviews(
        self, execution_id: ExecutionId, *, project_id: ProjectId
    ) -> list[IntegrationReview]:
        return self._repo.list_reviews_for_execution(execution_id, project_id=project_id)

    # ------------------------------------------------------------------
    # Merge proposal
    # ------------------------------------------------------------------

    def create_merge_proposal(
        self,
        *,
        project_id: ProjectId,
        review_id: IntegrationReviewId,
        execution_id: ExecutionId,
        source_tasks: tuple[TaskId, ...],
        source_diffs: tuple[str, ...] = (),
        risks: tuple[str, ...] = (),
        actor_id: UserId,
        source: AuditSource = "system",
    ) -> MergeProposal:
        """Create a merge proposal from an approved integration review.

        Per PLAN.md M11: "Controlled merge proposal containing source
        tasks, diffs, checks, risks, and required approval."

        Per PLAN.md M11: "Merge requires explicit authority and passing
        gates." The review must be approved (combined tests passed, no
        human_decision_required conflicts).
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        review = self._repo.get_review(project_id, review_id)
        if review.execution_id != execution_id or tuple(review.source_task_ids) != tuple(
            source_tasks
        ):
            raise MergeGateError("Proposal lineage does not match the reviewed execution/tasks")
        current_impact = self.derive_impact_set(
            execution_id=execution_id,
            task_ids=source_tasks,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        derived_diffs = tuple(sorted({e.artifact_id for e in current_impact if e.artifact_id}))
        if source_diffs and tuple(sorted(source_diffs)) != derived_diffs:
            raise MergeGateError("Proposal contains stale or foreign diff artifacts")
        source_diffs = derived_diffs
        if review.state != "approved":
            raise MergeGateError(
                f"Cannot create merge proposal: review is in state {review.state!r}, not 'approved'"
            )
        proposal = MergeProposal(
            id=MergeProposalId(generate_merge_proposal_id()),
            project_id=project_id,
            integration_review_id=review_id,
            execution_id=execution_id,
            source_tasks=source_tasks,
            source_diffs=source_diffs,
            checks_passed=review.combined_test_result == "pass",
            risks=risks,
            state="proposed",
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_proposal(proposal)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.propose",
                target_type="merge_proposal",
                target_id=proposal.id.value,
                result="success",
                redacted_summary=(f"Proposed merge of {len(source_tasks)} tasks"),
                correlation_id=execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return proposal

    def approve_merge(
        self,
        *,
        project_id: ProjectId,
        proposal_id: MergeProposalId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> MergeProposal:
        """Approve a merge proposal.

        Per PLAN.md M11: "Merge requires explicit authority and passing
        gates." The actor must have the ``integration.authorize_merge``
        permission.
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        proposal = self._repo.get_proposal(project_id, proposal_id)
        # Gate: checks must have passed.
        if not proposal.checks_passed:
            raise MergeGateError("Cannot approve merge: combined tests did not pass")
        self._repo.update_proposal_state(proposal_id, "approved", approved_by=actor_id)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.approve",
                target_type="merge_proposal",
                target_id=proposal_id.value,
                result="success",
                redacted_summary=f"Approved merge {proposal_id.value}",
                correlation_id=proposal.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_proposal(project_id, proposal_id)

    def reject_merge(
        self,
        *,
        project_id: ProjectId,
        proposal_id: MergeProposalId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> MergeProposal:
        """Reject a merge proposal.

        Per PLAN.md M11: "Rejected integration does not update accepted
        memory."
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        proposal = self._repo.get_proposal(project_id, proposal_id)
        self._repo.update_proposal_state(proposal_id, "rejected")
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="merge.reject",
                target_type="merge_proposal",
                target_id=proposal_id.value,
                result="success",
                redacted_summary=f"Rejected merge {proposal_id.value}",
                correlation_id=proposal.execution_id.value,
                created_at=_now_utc_iso(),
            )
        )
        return self._repo.get_proposal(project_id, proposal_id)

    def execute_merge(
        self,
        *,
        project_id: ProjectId,
        proposal_id: MergeProposalId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> MergeProposal:
        """Apply an approved proposal through an isolated, real Git workspace."""
        # Authorization must precede proposal lookup or filesystem access.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="integration.authorize_merge",
            source=source,
        )
        proposal = self._repo.get_proposal(project_id, proposal_id)
        if proposal.state != "approved" or not proposal.checks_passed:
            raise MergeGateError(
                "Cannot execute merge: proposal is not approved with passing checks"
            )
        review = self._repo.get_review(project_id, proposal.integration_review_id)
        if (
            review.execution_id != proposal.execution_id
            or tuple(review.source_task_ids) != tuple(proposal.source_tasks)
            or review.combined_test_result != "pass"
        ):
            raise MergeGateError("Proposal review lineage or combined checks are stale")
        current_impact = self.derive_impact_set(
            execution_id=proposal.execution_id,
            task_ids=proposal.source_tasks,
            project_id=project_id,
            actor_id=actor_id,
            source=source,
        )
        if current_impact != review.impact_set:
            raise MergeGateError("Source diff provenance changed after review")

        worktrees = []
        repositories = []
        for task_id in proposal.source_tasks:
            worktree = self._resolve_worktree(
                project_id=project_id,
                execution_id=proposal.execution_id,
                task_id=task_id,
            )
            if worktree is None:
                raise MergeGateError(f"Missing source worktree for {task_id.value}")
            repository = self._worktree_repo.get_repository(project_id, worktree.repository_id)
            worktrees.append(worktree)
            repositories.append(repository)
        if not repositories or len({repo.id.value for repo in repositories}) != 1:
            raise MergeGateError("Multi-repository integration is not supported")
        repository = repositories[0]
        target_path = Path(repository.local_path).resolve()
        if not target_path.is_dir():
            raise MergeGateError("Integration repository path is unavailable")
        target_ref = repository.default_base_revision or "main"
        target_ref_name = (
            target_ref if target_ref.startswith("refs/") else f"refs/heads/{target_ref}"
        )
        target_sha = self._git_output(target_path, "rev-parse", target_ref)
        if self._git_output(target_path, "status", "--porcelain"):
            raise MergeGateError("Target repository must be clean before integration")
        for worktree in worktrees:
            # Real-run fix (2026-08-28): chained task worktrees branch from
            # dependency checkpoint commits (ahead of the target ref), so
            # base equality no longer holds; the branch must simply contain
            # the reviewed target revision.
            source_probe_path = Path(worktree.worktree_path).resolve()
            source_probe_ref = self._git_output(
                source_probe_path, "symbolic-ref", "--short", "HEAD"
            )
            source_probe_sha = self._git_output(source_probe_path, "rev-parse", source_probe_ref)
            if not self._git_status_success(
                source_probe_path,
                "merge-base",
                "--is-ancestor",
                target_sha,
                source_probe_sha,
            ):
                raise MergeGateError("Source worktree is stale relative to target ref")
            source_path = Path(worktree.worktree_path).resolve()
            if not self._git_status_success(
                source_path, "merge-base", "--is-ancestor", worktree.base_revision, "HEAD"
            ):
                raise MergeGateError("Source ref is not based on the reviewed target revision")

        worktree_id = generate_integration_worktree_id()
        parent = Path(tempfile.gettempdir()) / "zero-integration-worktrees"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        integration_path = Path(tempfile.mkdtemp(prefix=f"{proposal_id.value}-", dir=parent))
        integration_path.rmdir()
        evidence_ids: list[str] = []
        try:
            self._git(target_path, "worktree", "add", "--detach", str(integration_path), target_sha)
            self._repo.insert_integration_worktree(
                worktree_id=worktree_id,
                project_id=project_id,
                execution_id=proposal.execution_id,
                repository_id=repository.id.value,
                worktree_path=str(integration_path),
                branch_name=f"integration/{proposal_id.value}",
                base_revision=target_sha,
            )
            self._record_evidence(
                evidence_ids,
                project_id=project_id,
                execution_id=proposal.execution_id,
                proposal_id=proposal_id,
                worktree_id=worktree_id,
                kind="command",
                command="git",
                args=("worktree", "add", "--detach", str(integration_path), target_sha),
                exit_code=0,
                content="isolated integration worktree created",
            )
            self._repo.update_integration_worktree(worktree_id, state="prepared")

            for worktree in worktrees:
                source_path = Path(worktree.worktree_path).resolve()
                source_branch = self._git_output(source_path, "symbolic-ref", "--short", "HEAD")
                branch_sha = self._git_output(source_path, "rev-parse", source_branch)
                if not self._git_status_success(
                    source_path, "merge-base", "--is-ancestor", target_sha, branch_sha
                ):
                    raise MergeGateError("Source ref is not based on the reviewed target revision")
                # Identity injection: a registered repository may not carry
                # user.name/user.email, and the per-source merge now really
                # runs now that task branches carry checkpoint commits.
                self._git(
                    integration_path,
                    "-c",
                    "user.name=Zero Integration",
                    "-c",
                    "user.email=zero-integration@localhost",
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    source_branch,
                )
                self._record_evidence(
                    evidence_ids,
                    project_id=project_id,
                    execution_id=proposal.execution_id,
                    proposal_id=proposal_id,
                    worktree_id=worktree_id,
                    kind="command",
                    command="git",
                    args=("merge", "--no-ff", "--no-edit", source_branch),
                    exit_code=0,
                    content=f"merged source ref {source_branch} ({branch_sha})",
                )
                uncommitted = self._git_output(source_path, "diff", "--binary")
                if uncommitted:
                    self._git_input(integration_path, ("apply", "--index", "-"), uncommitted)
                self._copy_untracked_files(source_path, integration_path)

            self._git(integration_path, "add", "--all")
            self._git(integration_path, "diff", "--cached", "--check")
            self._record_evidence(
                evidence_ids,
                project_id=project_id,
                execution_id=proposal.execution_id,
                proposal_id=proposal_id,
                worktree_id=worktree_id,
                kind="test",
                command="git",
                args=("diff", "--cached", "--check"),
                exit_code=0,
                content="staged diff check passed",
            )
            if not self._git_output(integration_path, "diff", "--cached"):
                raise MergeGateError("Integration produced no changes")
            commit_message = (
                f"Integrate {proposal_id.value}\\n\\n"
                f"Execution: {proposal.execution_id.value}\\n"
                f"Tasks: {', '.join(task.value for task in proposal.source_tasks)}"
            )
            self._git(
                integration_path,
                "-c",
                "user.name=Zero Integration",
                "-c",
                "user.email=zero-integration@localhost",
                "commit",
                "-m",
                commit_message,
            )
            integration_sha = self._git_output(integration_path, "rev-parse", "HEAD")
            self._record_evidence(
                evidence_ids,
                project_id=project_id,
                execution_id=proposal.execution_id,
                proposal_id=proposal_id,
                worktree_id=worktree_id,
                kind="commit",
                command="git",
                args=("commit", "-m", f"Integrate {proposal_id.value}"),
                exit_code=0,
                content=f"integration commit {integration_sha}",
                ref_name=integration_sha,
            )
            self._git(integration_path, "diff", "--check", f"{integration_sha}^", integration_sha)
            self._repo.update_integration_worktree(
                worktree_id, state="checks_passed", target_revision=integration_sha
            )

            target_ref_name = (
                target_ref if target_ref.startswith("refs/") else f"refs/heads/{target_ref}"
            )
            self._git(
                target_path,
                "update-ref",
                target_ref_name,
                integration_sha,
                target_sha,
            )
            # Keep a checked-out target worktree truthful when this repository
            # itself is on the target branch. A clean check above makes this safe.
            self._git_status_success(target_path, "checkout", "--force", target_ref)
            resulting_sha = self._git_output(target_path, "rev-parse", target_ref)
            if resulting_sha != integration_sha or self._git_output(
                target_path, "status", "--porcelain"
            ):
                raise MergeGateError("Target ref verification failed after update")
            self._record_evidence(
                evidence_ids,
                project_id=project_id,
                execution_id=proposal.execution_id,
                proposal_id=proposal_id,
                worktree_id=worktree_id,
                kind="merge",
                command="git",
                args=("update-ref", target_ref_name, integration_sha, target_sha),
                exit_code=0,
                content=f"target {target_ref_name} advanced from {target_sha} to {integration_sha}",
                ref_name=integration_sha,
            )
            self._repo.update_integration_worktree(
                worktree_id, state="merged", target_revision=integration_sha
            )
            self._repo.update_proposal_evidence(
                proposal_id,
                integration_worktree_id=worktree_id,
                target_revision=integration_sha,
                rollback_revision=target_sha,
                evidence_ids=tuple(evidence_ids),
            )
            self._repo.update_proposal_state(proposal_id, "merged", merged_at=_now_utc_iso())
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=actor_id,
                    source=source,
                    operation="merge.execute",
                    target_type="merge_proposal",
                    target_id=proposal_id.value,
                    result="success",
                    redacted_summary=(
                        f"Executed verified merge {proposal_id.value} at {integration_sha}"
                    ),
                    correlation_id=proposal.execution_id.value,
                    created_at=_now_utc_iso(),
                )
            )
            return self._repo.get_proposal(project_id, proposal_id)
        except Exception as exc:
            # The target ref is changed only near the end. If persistence or
            # verification fails after that point, restore the expected ref.
            try:
                if self._git_status_success(target_path, "rev-parse", "--verify", "HEAD"):
                    current = self._git_output(target_path, "rev-parse", target_ref)
                    if current != target_sha:
                        self._git_status_success(
                            target_path, "update-ref", target_ref_name, target_sha, current
                        )
                        self._git_status_success(target_path, "checkout", "--force", target_ref)
            except MergeGateError as cleanup_exc:
                logger.debug(
                    "merge rollback cleanup failed: %s",
                    type(cleanup_exc).__name__,
                )
            if isinstance(exc, MergeGateError):
                raise
            raise MergeGateError(f"Integration failed before merge commit: {exc}") from exc

    def recover_inflight_merges(self) -> list[str]:
        """Reconcile merge side-effect windows left by a crash.

        Per the release audit (Phase 3): a crash between moving the
        target Git reference and persisting the final proposal state
        leaves external Git state and database state temporarily
        inconsistent. Recovery policy:

        - If the recorded integration commit is reachable from the live
          target ref (including equal), the external side effect truly
          happened: finalize the proposal as ``merged``.
        - If not reachable, never guess or force refs in either
          direction; record an audit event for operator review.

        Returns the list of proposal ids finalized as merged.
        """
        recovered: list[str] = []
        for window in self._repo.list_merge_side_effect_windows():
            proposal_id_value = window["proposal_id"]
            project_id = ProjectId(window["project_id"])
            try:
                repository = self._worktree_repo.get_repository(
                    project_id, RepositoryId(window["repository_id"])
                )
                target_path = Path(repository.local_path).resolve()
                if not target_path.is_dir():
                    raise RuntimeError("repository path is unavailable")
                target_ref = repository.default_base_revision or "main"
                current_sha = self._git_output(target_path, "rev-parse", target_ref)
                recorded_sha = window["target_revision"]
                reachable = current_sha == recorded_sha or self._git_status_success(
                    target_path,
                    "merge-base",
                    "--is-ancestor",
                    recorded_sha,
                    current_sha,
                )
            except (OSError, RuntimeError) as exc:
                logger.warning(
                    "merge reconciliation deferred for %s: %s",
                    proposal_id_value,
                    type(exc).__name__,
                )
                continue
            if reachable:
                # The external side effect happened; make the database
                # tell the truth about it.
                self._repo.update_proposal_state(
                    MergeProposalId(proposal_id_value),
                    "merged",
                    merged_at=_now_utc_iso(),
                )
                result = "success"
                summary = (
                    f"Crash-window merge finalized from Git evidence: "
                    f"{recorded_sha[:12]} reachable from {target_ref}"
                )
                recovered.append(proposal_id_value)
            else:
                result = "failure"
                summary = (
                    f"Merge crash-window requires operator review: recorded "
                    f"{recorded_sha[:12]} is not reachable from {target_ref} "
                    f"(currently {current_sha[:12]})"
                )
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project_id,
                    actor_id=None,
                    source="system",
                    operation="merge.reconcile",
                    target_type="merge_proposal",
                    target_id=proposal_id_value,
                    result=result,
                    redacted_summary=summary,
                    correlation_id=None,
                    created_at=_now_utc_iso(),
                )
            )
        return recovered

    def _git(self, cwd: Path, *args: str, input_text: str | None = None) -> str:
        """Run a fixed-argv Git command with bounded output and no prompts."""
        env = os.environ.copy()
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        argv = ["git", "-C", str(cwd), *args]
        try:
            if input_text is None:
                output, returncode, truncated = _run_bounded_git_process(
                    argv,
                    cwd=cwd,
                    env=env,
                    timeout=60,
                    max_bytes=_MAX_GIT_OUTPUT_BYTES,
                )
            else:
                with tempfile.TemporaryFile(mode="w+b") as input_file:
                    input_file.write(input_text.encode("utf-8"))
                    input_file.seek(0)
                    output, returncode, truncated = _run_bounded_git_process(
                        argv,
                        cwd=cwd,
                        env=env,
                        timeout=60,
                        max_bytes=_MAX_GIT_OUTPUT_BYTES,
                        stdin=input_file,
                    )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MergeGateError(f"Git command failed: {args[0] if args else 'git'}") from exc
        if truncated:
            raise MergeGateError("Git command output exceeded the configured limit")
        if returncode != 0:
            raise MergeGateError(f"Git command failed: {args[0] if args else 'git'}")
        return output.strip()

    def _git_output(self, cwd: Path, *args: str) -> str:
        return self._git(cwd, *args)

    def _git_input(self, cwd: Path, args: tuple[str, ...], input_text: str) -> str:
        return self._git(cwd, *args, input_text=input_text)

    def _git_status_success(self, cwd: Path, *args: str) -> bool:
        try:
            self._git(cwd, *args)
            return True
        except MergeGateError:
            return False

    def _copy_untracked_files(self, source: Path, destination: Path) -> None:
        status = self._git_output(source, "status", "--porcelain=v1", "-uall")
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            relative = Path(line[3:].strip())
            if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                raise MergeGateError("Unsafe untracked path in source worktree")
            src = source / relative
            dst = destination / relative
            if dst.exists():
                if src.is_file() and dst.is_file() and src.read_bytes() == dst.read_bytes():
                    continue
                raise MergeGateError(f"Untracked path collides during integration: {relative}")
            self._reject_untracked_links_and_special_files(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=False)
            else:
                shutil.copy2(src, dst)

    @staticmethod
    def _reject_untracked_links_and_special_files(path: Path) -> None:
        """Reject task-controlled symlinks and non-regular files before copy."""
        import stat

        if path.is_symlink():
            raise MergeGateError("Unsafe symlink in untracked source path")
        if path.is_dir():
            for current, directories, files in os.walk(path, followlinks=False):
                current_path = Path(current)
                for name in (*directories, *files):
                    child = current_path / name
                    if child.is_symlink():
                        raise MergeGateError("Unsafe symlink in untracked source tree")
                    if not child.is_dir() and not stat.S_ISREG(child.stat().st_mode):
                        raise MergeGateError("Unsupported special file in untracked source tree")
        elif not stat.S_ISREG(path.stat().st_mode):
            raise MergeGateError("Unsupported special file in untracked source path")

    def _record_evidence(
        self,
        evidence_ids: list[str],
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        proposal_id: MergeProposalId,
        worktree_id: str,
        kind: str,
        command: str | None,
        args: tuple[str, ...],
        exit_code: int | None,
        content: str,
        ref_name: str | None = None,
    ) -> None:
        evidence_id = generate_integration_evidence_id()
        bounded = content[:8192]
        self._repo.insert_integration_evidence(
            evidence_id=evidence_id,
            project_id=project_id,
            execution_id=execution_id,
            proposal_id=proposal_id,
            integration_worktree_id=worktree_id,
            kind=kind,
            command=command,
            args=args,
            exit_code=exit_code,
            content=bounded,
            content_hash=hashlib.sha256(bounded.encode("utf-8")).hexdigest(),
            ref_name=ref_name,
        )
        evidence_ids.append(evidence_id)

    def get_proposal(self, project_id: ProjectId, proposal_id: MergeProposalId) -> MergeProposal:
        return self._repo.get_proposal(project_id, proposal_id)

    def list_proposals(
        self, execution_id: ExecutionId, *, project_id: ProjectId
    ) -> list[MergeProposal]:
        return self._repo.list_proposals_for_execution(execution_id, project_id=project_id)
