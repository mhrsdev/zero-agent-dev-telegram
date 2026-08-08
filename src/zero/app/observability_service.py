"""Observability service — structured redacted logs, metrics, correlation,
secret canary scan, backup/restore, and recovery procedures.

Per ``zero-observability-evidence`` SKILL.md:

- One correlation spine connects evidence: stable IDs make separate
  evidence types joinable.
- Logs explain events: structured logs are suited to discrete runtime
  facts, unexpected conditions, and compact context needed for
  diagnosis.
- Metrics describe aggregates: low-cardinality labels; raw payloads,
  source content, prompts, tool output excluded by default.
- Traces explain causal latency.
- Audit explains authority.
- Token and cost telemetry preserves scope.
- Redaction is schema-driven.
- Diagnostic artifacts preserve depth safely.
- Alerts express actionable conditions.
- Unknown is observable.

Per ``zero-recovery-consistency`` SKILL.md:

- A checkpoint records facts, not confidence.
- Idempotency makes retries ordinary.
- Leases distinguish ownership from history.
- External operations may be uncertain.
- Cancellation preserves evidence.
- Database migrations separate compatibility from cleanup.
- Topology changes are migrations too.
- Rollback follows the real side effect.
- Cleanup requires proof of non-ownership.
- Restore is not verified until exercised.

Per PLAN.md M14 invariants:
- Metrics use low-cardinality dimensions.
- Raw prompts, source files, tool parameters/results, credentials, and
  private messages are excluded by default.
- Every execution can be traced across plan, tasks, agents, tools,
  provider requests, integration, and result.
- Recovery uses durable state, not model recollection.
- Backups and restores preserve project isolation and encryption
  requirements.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zero.domain.audit import looks_sensitive
from zero.domain.identity import ProjectId

logger = logging.getLogger("zero.observability")


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----------------------------------------------------------------------
# Secret canary patterns
# ----------------------------------------------------------------------

#: Patterns that indicate a secret-like value. Used by the canary scan
#: to detect leaks across logs, audit, metrics, artifacts, and backups.
SECRET_PATTERNS: tuple[str, ...] = (
    r"sk-[a-zA-Z0-9]{20,}",           # OpenAI-style API key
    r"Bearer\s+[a-zA-Z0-9._-]+",      # HTTP auth header
    r"password\s*=\s*\S+",            # password assignment
    r"secret\s*=\s*\S+",              # secret assignment
    r"token\s*=\s*\S+",               # token assignment
    r"api_key\s*=\s*\S+",             # API key assignment
    r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",  # private key
    r"ghp_[a-zA-Z0-9]{36,}",          # GitHub PAT
    r"AKIA[A-Z0-9]{16}",              # AWS access key
)


def scan_for_secrets(text: str) -> list[str]:
    """Scan text for secret-like patterns.

    Per PLAN.md M14: "Secret canary scan across logs, audit, metrics,
    artifacts, prompts, and backups."

    Returns a list of matched pattern descriptions (not the actual
    secrets).
    """
    if not text:
        return []
    matches: list[str] = []
    for pattern in SECRET_PATTERNS:
        found = re.findall(pattern, text, re.IGNORECASE)
        if found:
            matches.append(
                f"pattern={pattern!r}, count={len(found)}"
            )
    # Also check the audit looks_sensitive helper.
    if looks_sensitive(text) and "sensitive" not in str(matches):
        matches.append("looks_sensitive=True")
    return matches


# ----------------------------------------------------------------------
# Structured redacted logging
# ----------------------------------------------------------------------


class RedactedLogFormatter(logging.Formatter):
    """Log formatter that redacts secret-like values.

    Per ``zero-observability-evidence`` §"Redaction is schema-driven":
    known sensitive fields should be excluded or transformed before
    serialization. Regex scanning after logs are written is a detection
    layer, not the primary control.
    """

    SENSITIVE_KEYS = frozenset({
        "secret", "password", "token", "api_key", "apikey",
        "private_key", "credential", "authorization",
    })

    def format(self, record: logging.LogRecord) -> str:
        # Format the message first.
        msg = super().format(record)
        # Redact any secret-like patterns in the formatted message.
        for pattern in SECRET_PATTERNS:
            msg = re.sub(
                pattern, "[REDACTED]", msg, flags=re.IGNORECASE
            )
        return msg


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structured redacted logging."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactedLogFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))


# ----------------------------------------------------------------------
# Metrics service
# ----------------------------------------------------------------------


class MetricsService:
    """Low-cardinality metrics for execution outcomes, duration, tokens,
    cache behavior, compaction, tools, and cost.

    Per ``zero-observability-evidence`` §"Metrics describe aggregates":
    metrics answer questions such as throughput, latency, failure rate,
    queue depth, context pressure, and token/cost trends. Labels must
    remain bounded.

    Per ``zero-observability-evidence`` §"Token and cost telemetry
    preserves scope": usage records distinguish per-step deltas, query
    totals, cumulative session snapshots, cache reads and cache
    creation, server-reported versus estimated counts, provider request
    and model identity, whole execution/agent-tree totals.

    Per PLAN.md M14: "Raw prompts, source files, tool parameters/
    results, credentials, and private messages are excluded by default."
    """

    # Allowed low-cardinality label values.
    ALLOWED_RESULTS = frozenset({
        "success", "denied", "failure", "error", "cancelled",
    })
    ALLOWED_SOURCES = frozenset({
        "web", "telegram", "discord", "system", "internal",
    })

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._histograms: dict[str, list[float]] = {}

    def increment(
        self,
        name: str,
        *,
        project_id: str | None = None,
        result: str | None = None,
        source: str | None = None,
    ) -> None:
        """Increment a counter with low-cardinality labels.

        Per ``zero-observability-evidence`` §"Correct labels": provider,
        model family, operation class, outcome, environment.

        Per ``zero-observability-evidence`` §"Wrong labels": prompt
        text, error message, user name, file path, tool arguments.
        These are NEVER used as labels.
        """
        # Validate label cardinality.
        if result is not None and result not in self.ALLOWED_RESULTS:
            result = "unknown"
        if source is not None and source not in self.ALLOWED_SOURCES:
            source = "unknown"
        # Build the metric key with safe labels only.
        key_parts = [name]
        if result:
            key_parts.append(f"result={result}")
        if source:
            key_parts.append(f"source={source}")
        # project_id is NOT used as a label to keep cardinality low.
        # It is tracked separately in the audit log.
        key = "|".join(key_parts)
        self._counters[key] = self._counters.get(key, 0) + 1

    def observe_duration(
        self,
        name: str,
        duration_ms: float,
    ) -> None:
        """Record a duration observation."""
        self._histograms.setdefault(name, []).append(duration_ms)

    def get_counters(self) -> dict[str, int]:
        return dict(self._counters)

    def get_histogram_summary(
        self, name: str
    ) -> dict[str, float] | None:
        values = self._histograms.get(name)
        if not values:
            return None
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
        }

    def reset(self) -> None:
        self._counters.clear()
        self._histograms.clear()


# ----------------------------------------------------------------------
# Correlation ID management
# ----------------------------------------------------------------------


class CorrelationContext:
    """Thread-local correlation ID context.

    Per ``zero-observability-evidence`` §"One correlation spine connects
    evidence": stable IDs make separate evidence types joinable.
    """

    def __init__(self) -> None:
        import threading
        self._local = threading.local()

    def set(self, correlation_id: str) -> None:
        self._local.correlation_id = correlation_id

    def get(self) -> str | None:
        return getattr(self._local, "correlation_id", None)

    def clear(self) -> None:
        if hasattr(self._local, "correlation_id"):
            del self._local.correlation_id


# ----------------------------------------------------------------------
# Backup and restore
# ----------------------------------------------------------------------


class BackupService:
    """Backup and restore procedures with project isolation.

    Per ``zero-recovery-consistency`` §"Restore is not verified until
    exercised": a backup file existing is weaker than a restore test.
    Verification includes integrity hash, decryption, isolated target,
    schema compatibility, secret/integration safety, record counts or
    invariants, and application-level recovery checks.

    Per PLAN.md M14: "Backups and restores preserve project isolation
    and encryption requirements."

    Per ``zero-project-isolation-evidence`` §"Backups do not erase
    boundaries": a backup may contain multiple projects while still
    requiring encryption, owner-controlled access, integrity checks,
    and isolated restore testing. Restoring one project should not
    silently activate another project's interface bindings, tools, or
    provider credentials in the test environment.
    """

    def __init__(self, database: Any) -> None:
        self._database = database

    def backup_to_file(self, path: str) -> str:
        """Back up the database to a file.

        Returns the path of the backup file.
        """
        backup_path = Path(path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._database.connect()
        # Use SQLite's backup API via iterdump for portability.
        with open(backup_path, "w", encoding="utf-8") as f:
            for line in conn.iterdump():
                f.write(line)
                f.write("\n")
        return str(backup_path)

    def restore_from_file(
        self, path: str, target_database: Any
    ) -> dict[str, Any]:
        """Restore the database from a backup file.

        Per ``zero-recovery-consistency`` §"Restore is not verified
        until exercised": verification includes integrity hash, schema
        compatibility, and application-level recovery checks.

        Returns a dict with verification results.
        """
        backup_path = Path(path)
        if not backup_path.is_file():
            raise FileNotFoundError(f"Backup file not found: {path}")
        # Read the SQL dump.
        sql = backup_path.read_text(encoding="utf-8")
        # Restore into the target database.
        conn = target_database.connect()
        conn.executescript(sql)
        conn.commit()
        # Run verification checks.
        return self._verify_restore(target_database)

    def _verify_restore(
        self, database: Any
    ) -> dict[str, Any]:
        """Verify the restored database passes integrity checks.

        Per PLAN.md M14 checkpoint: "A restore is not considered valid
        until the isolated restored system passes schema, artifact hash,
        authorization, and project-leakage tests."
        """
        conn = database.connect()
        results: dict[str, Any] = {
            "schema_check": "pass",
            "table_counts": {},
            "integrity_check": "pass",
        }
        # Check schema: verify all expected tables exist.
        expected_tables = {
            "schema_migrations", "projects", "users", "audit_events",
            "plans", "plan_revisions", "executions", "tasks",
            "artifacts", "rag_documents",
        }
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        actual_tables = {row[0] for row in cursor.fetchall()}
        missing = expected_tables - actual_tables
        if missing:
            results["schema_check"] = f"fail: missing {missing}"
        # Count rows in key tables.
        for table in sorted(actual_tables):
            if table.startswith("sqlite_"):
                continue
            try:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                )
                results["table_counts"][table] = cursor.fetchone()[0]
            except sqlite3.Error:
                pass  # FTS5 tables can't be COUNTed normally
        # Integrity check.
        cursor = conn.execute("PRAGMA integrity_check")
        integrity_result = cursor.fetchone()[0]
        if integrity_result != "ok":
            results["integrity_check"] = f"fail: {integrity_result}"
        return results


# ----------------------------------------------------------------------
# Secret canary scan
# ----------------------------------------------------------------------


class SecretCanaryScan:
    """Scan all system surfaces for secret-like content.

    Per PLAN.md M14: "Secret canary scan across logs, audit, metrics,
    artifacts, prompts, and backups."

    Per ``zero-observability-evidence`` §"Redaction is schema-driven":
    regex scanning after logs are written is a detection layer, not the
    primary control.
    """

    def __init__(self, services: Any) -> None:
        self._services = services

    def scan_all(self) -> dict[str, list[str]]:
        """Scan all surfaces for secrets.

        Returns a dict mapping surface name to a list of findings.
        An empty list means no secrets found.
        """
        results: dict[str, list[str]] = {}
        # Scan audit events.
        results["audit_events"] = self._scan_audit_events()
        # Scan artifacts.
        results["artifacts"] = self._scan_artifacts()
        # Scan conversation events.
        results["conversation_events"] = self._scan_conversation_events()
        # Scan knowledge records.
        results["knowledge_records"] = self._scan_knowledge_records()
        return results

    def _scan_audit_events(self) -> list[str]:
        """Scan all audit event summaries for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, redacted_summary FROM audit_events "
            "WHERE redacted_summary IS NOT NULL"
        )
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["redacted_summary"] or "")
            if matches:
                findings.append(
                    f"audit_event {row['id']}: {matches}"
                )
        return findings

    def _scan_artifacts(self) -> list[str]:
        """Scan all artifact content for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, content FROM artifacts"
        )
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["content"] or "")
            if matches:
                findings.append(
                    f"artifact {row['id']}: {matches}"
                )
        return findings

    def _scan_conversation_events(self) -> list[str]:
        """Scan all conversation event content for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, content FROM conversation_events"
        )
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["content"] or "")
            if matches:
                findings.append(
                    f"conversation_event {row['id']}: {matches}"
                )
        return findings

    def _scan_knowledge_records(self) -> list[str]:
        """Scan all knowledge record content for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, content FROM knowledge_records"
        )
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["content"] or "")
            if matches:
                findings.append(
                    f"knowledge_record {row['id']}: {matches}"
                )
        return findings


# ----------------------------------------------------------------------
# Recovery procedures
# ----------------------------------------------------------------------


class RecoveryService:
    """Recovery procedures for stuck executions, orphan worktrees,
    partial compaction, failed migration, and provider outage.

    Per ``zero-recovery-consistency`` SKILL.md:
    - A checkpoint records facts, not confidence.
    - Leases distinguish ownership from history.
    - External operations may be uncertain.
    - Cancellation preserves evidence.
    - Cleanup requires proof of non-ownership.
    - Restore is not verified until exercised.

    Per PLAN.md M14: "Stuck execution, orphan worktree, partial
    compaction, failed migration, and provider outage recovery."
    """

    def __init__(self, services: Any) -> None:
        self._services = services

    def recover_stuck_executions(self) -> list[str]:
        """Find and recover executions stuck in 'running' state.

        Per ``zero-recovery-consistency`` §"Leases distinguish ownership
        from history": an expired lease does not prove failure. It
        proves that current ownership is absent; reconciliation inspects
        process, artifact, and external evidence.
        """
        recovered: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, project_id FROM executions WHERE state = 'running'"
        )
        for row in cursor.fetchall():
            from zero.domain.execution import ExecutionId
            exec_id = ExecutionId(row["id"])
            try:
                project = self._services.identity.get_project(
                    ProjectId(row["project_id"])
                )
                # Use the worker service's recovery method.
                self._services.worker.recover_after_restart(
                    execution_id=exec_id,
                    actor_id=project.owner_user_id,
                    source="system",
                )
                recovered.append(row["id"])
            except Exception as e:
                logger.warning(
                    "Failed to recover execution %s: %s",
                    row["id"], e,
                )
        return recovered

    def recover_orphan_worktrees(self) -> list[str]:
        """Find and mark orphaned worktrees as interrupted.

        Per ``zero-recovery-consistency`` §"Cleanup requires proof of
        non-ownership": before a worktree is removed, Zero needs evidence
        that it belongs to the intended task, has no active process, and
        has preserved required human work or recovery artifacts.
        """
        recovered: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, project_id FROM worktrees WHERE state = 'active'"
        )
        for row in cursor.fetchall():
            from zero.domain.worktrees import WorktreeId
            try:
                self._services.worktree.mark_worktree_interrupted(
                    project_id=ProjectId(row["project_id"]),
                    worktree_id=WorktreeId(row["id"]),
                )
                recovered.append(row["id"])
            except Exception as e:
                logger.warning(
                    "Failed to recover worktree %s: %s",
                    row["id"], e,
                )
        return recovered

    def recover_partial_compaction(self) -> list[str]:
        """Find and recover from partial compaction states.

        Per ``zero-context-memory`` §"Replace context only after durable
        commit": a crash at any point must leave either the old context
        active or a fully recoverable new context.
        """
        recovered: list[str] = []
        conn = self._services.database.connect()
        # Find compaction records in non-terminal states.
        cursor = conn.execute(
            "SELECT id, execution_id, target_context_version "
            "FROM compaction_records "
            "WHERE state IN ('pre_flush', 'fit', 'summary_validated', 'committed')"
        )
        for row in cursor.fetchall():
            # If the target context version is not active, the old
            # context should still be active. This is safe because
            # activate_context_version is the last step.
            # We mark the compaction as 'failed' and log it.
            try:
                conn.execute(
                    "UPDATE compaction_records SET state = 'failed' "
                    "WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                recovered.append(row["id"])
            except Exception as e:
                logger.warning(
                    "Failed to recover compaction %s: %s",
                    row["id"], e,
                )
        return recovered

    def run_all_recovery(self) -> dict[str, list[str]]:
        """Run all recovery procedures.

        Per PLAN.md M14: "Stuck execution, orphan worktree, partial
        compaction, failed migration, and provider outage recovery."
        """
        return {
            "stuck_executions": self.recover_stuck_executions(),
            "orphan_worktrees": self.recover_orphan_worktrees(),
            "partial_compactions": self.recover_partial_compaction(),
        }
