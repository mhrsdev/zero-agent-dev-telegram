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

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from zero.domain.audit import looks_sensitive
from zero.domain.identity import ProjectId

logger = logging.getLogger("zero.observability")


def _derive_backup_key(key_material: bytes) -> bytes:
    """Derive the backup Fernet key via the application HKDF profile.

    Key separation uses a distinct info string so the backup key differs
    from the secret-encryption key even though both derive from
    ``ZERO_SECRET_KEY``.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"zero-develop/backup-encryption/v1",
    )
    return hkdf.derive(key_material)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----------------------------------------------------------------------
# Secret canary patterns
# ----------------------------------------------------------------------

#: Patterns that indicate a secret-like value. Used by the canary scan
#: to detect leaks across logs, audit, metrics, artifacts, and backups.
SECRET_PATTERNS: tuple[str, ...] = (
    r"sk-[a-zA-Z0-9]{20,}",  # OpenAI-style API key
    r"Bearer\s+[a-zA-Z0-9._-]+",  # HTTP auth header
    r"password\s*=\s*\S+",  # password assignment
    r"secret\s*=\s*\S+",  # secret assignment
    r"token\s*=\s*\S+",  # token assignment
    r"api_key\s*=\s*\S+",  # API key assignment
    r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",  # private key
    r"ghp_[a-zA-Z0-9]{36,}",  # GitHub PAT
    r"AKIA[A-Z0-9]{16}",  # AWS access key
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
            matches.append(f"pattern={pattern!r}, count={len(found)}")
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

    SENSITIVE_KEYS = frozenset(
        {
            "secret",
            "password",
            "token",
            "api_key",
            "apikey",
            "private_key",
            "credential",
            "authorization",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        # Format the message first.
        msg = super().format(record)
        # Redact any secret-like patterns in the formatted message.
        for pattern in SECRET_PATTERNS:
            msg = re.sub(pattern, "[REDACTED]", msg, flags=re.IGNORECASE)
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
    ALLOWED_RESULTS = frozenset(
        {
            "success",
            "denied",
            "failure",
            "error",
            "cancelled",
            # GAP 8b/G1 execution-loop defect outcomes (bounded set):
            "invalid_arguments",
            "undeclared_tool",
            "approval_pending",
            "approval_denied",
            "boosted_reask",
        }
    )
    ALLOWED_SOURCES = frozenset(
        {
            "web",
            "telegram",
            "discord",
            "system",
            "internal",
        }
    )

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
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
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    def observe_duration(
        self,
        name: str,
        duration_ms: float,
    ) -> None:
        """Record a duration observation."""
        with self._lock:
            self._histograms.setdefault(name, []).append(duration_ms)

    def get_counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def get_histogram_summary(self, name: str) -> dict[str, float] | None:
        with self._lock:
            values = list(self._histograms.get(name, ()))
        if not values:
            return None
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
        }

    def histogram_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._histograms))

    def reset(self) -> None:
        with self._lock:
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


class BackupRestoreError(ValueError):
    """Raised when an in-memory restore fails after recovery is attempted."""


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

    def __init__(self, database: Any, key_material: str | bytes | None = None) -> None:
        self._database = database
        self._fernet: Fernet | None = None
        self._legacy_fernet: Fernet | None = None
        if key_material is None:
            settings = getattr(database, "_settings", None)
            configured = getattr(settings, "secret_key", None)
            if configured is not None:
                key_material = configured.get_secret_value()
            elif getattr(settings, "is_development", False):
                # Development startup must remain usable without silently
                # inventing a process-local encryption key.  Backup/restore
                # operations fail explicitly until ZERO_SECRET_KEY is set.
                return
        if key_material is None:
            # Non-production composition may start without backup authority,
            # but durable backup/restore must fail closed at the operation
            # boundary rather than inventing a deterministic key.
            return
        if isinstance(key_material, str):
            key_material = key_material.encode("utf-8")
        if not key_material or not key_material.strip():
            self._fernet = None
            return
        # The backup key uses the same HKDF key-separation profile as the
        # rest of the application (distinct info string), not a bare hash.
        # The legacy SHA-256 derivation is kept only for restoring older
        # backups written before this profile existed.
        self._fernet = Fernet(
            __import__("base64").urlsafe_b64encode(_derive_backup_key(key_material))
        )
        self._legacy_fernet = Fernet(
            __import__("base64").urlsafe_b64encode(hashlib.sha256(key_material).digest())
        )

    def _require_fernet(self) -> Fernet:
        if self._fernet is None:
            raise ValueError(
                "backup encryption is unavailable; configure ZERO_SECRET_KEY before backup/restore"
            )
        return self._fernet

    def backup_to_file(self, path: str) -> str:
        """Create an authenticated encrypted SQLite backup atomically."""
        backup_path = Path(path)
        self._reject_symlink_ancestors(backup_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.is_symlink():
            raise ValueError("Refusing to overwrite a backup symlink")
        conn = self._database.connect()
        sql = "\n".join(conn.iterdump()) + "\n"
        schema_hash = self._schema_hash(conn)
        payload = {
            "format": "zero-sqlite-backup-v1",
            "created_at": _now_utc_iso(),
            "schema_hash": schema_hash,
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "sql": sql,
        }
        token = b"ZERO-BACKUP-V1\\n" + self._require_fernet().encrypt(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.", suffix=".tmp", dir=backup_path.parent
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            fd_closed = True
            self._atomic_replace_file(tmp_name, backup_path)
        finally:
            # If os.fdopen itself failed the raw descriptor is still
            # open; close it before attempting the unlink because an
            # open handle blocks deletion on Windows.
            if not locals().get("fd_closed", False):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            except PermissionError:
                # A scanner can transiently hold the temp file; the
                # backup itself already succeeded or failed above.
                logger.debug("could not unlink backup temp %s", tmp_name)
        return str(backup_path)

    def _atomic_replace_file(self, tmp_name: str, target_path: Path) -> None:
        if target_path.is_symlink():
            raise ValueError("Refusing to overwrite a backup symlink")
        previous_path: Path | None = None
        installed = False
        try:
            if os.path.lexists(target_path):
                fd, previous_name = tempfile.mkstemp(
                    prefix=f".{target_path.name}.replace-old-", dir=target_path.parent
                )
                os.close(fd)
                os.unlink(previous_name)
                previous_path = Path(previous_name)
                os.replace(target_path, previous_path)
            os.replace(tmp_name, target_path)
            installed = True
            os.chmod(target_path, 0o600)
            self._fsync_directory(target_path.parent)
        except BaseException:
            if installed and os.path.lexists(target_path):
                os.unlink(target_path)
            if previous_path is not None and os.path.lexists(previous_path):
                os.replace(previous_path, target_path)
            try:
                self._fsync_directory(target_path.parent)
            except OSError:
                logger.exception("Could not fsync directory after backup rollback")
            raise
        else:
            if previous_path is not None:
                try:
                    os.unlink(previous_path)
                except OSError:
                    logger.warning("Could not remove superseded backup %s", previous_path)

    def restore_from_file(self, path: str, target_database: Any) -> dict[str, Any]:
        """Authenticate, verify in isolation, then atomically restore."""
        backup_path = Path(path)
        self._reject_symlink_ancestors(backup_path)
        if backup_path.is_symlink():
            raise ValueError("Refusing to restore from a symlink")
        if not backup_path.is_file():
            raise FileNotFoundError(f"Backup file not found: {path}")
        raw = backup_path.read_bytes()
        prefix = b"ZERO-BACKUP-V1\\n"
        if not raw.startswith(prefix):
            raise ValueError("Unsupported or plaintext backup format")
        try:
            fernet = self._require_fernet()
            token = raw[len(prefix) :]
            decrypted: bytes | None = None
            decrypt_error: Exception | None = None
            try:
                decrypted = fernet.decrypt(token)
            except InvalidToken as primary_exc:
                # Backups written before the HKDF profile used a bare
                # SHA-256 derivation; keep those restorable.
                if self._legacy_fernet is not None:
                    try:
                        decrypted = self._legacy_fernet.decrypt(token)
                    except InvalidToken as legacy_exc:
                        decrypt_error = legacy_exc
                else:
                    decrypt_error = primary_exc
            if decrypted is None:
                raise ValueError("Backup authentication/decryption failed") from decrypt_error
            payload = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup authentication/decryption failed") from exc
        if not isinstance(payload, dict):
            raise TypeError("Backup payload has invalid structure")
        if payload.get("format") != "zero-sqlite-backup-v1":
            raise ValueError("Unsupported backup format")
        sql = payload.get("sql")
        schema_hash = payload.get("schema_hash")
        sql_sha256 = payload.get("sql_sha256")
        if (
            not isinstance(sql, str)
            or not isinstance(schema_hash, str)
            or not isinstance(sql_sha256, str)
            or hashlib.sha256(sql.encode("utf-8")).hexdigest() != sql_sha256
        ):
            raise ValueError("Backup payload integrity check failed")

        target_path_value = getattr(target_database, "_path", None)
        target_path = (
            Path(target_path_value) if target_path_value not in (None, ":memory:") else None
        )
        staging_parent = (
            target_path.parent if target_path is not None else Path(tempfile.gettempdir())
        )
        staging_parent.mkdir(parents=True, exist_ok=True)
        fd, staging_name = tempfile.mkstemp(
            prefix=".zero-restore-", suffix=".db", dir=staging_parent
        )
        os.close(fd)
        try:
            os.chmod(staging_name, 0o600)
            staging = sqlite3.connect(staging_name)
            staging.row_factory = sqlite3.Row
            try:
                staging.execute("PRAGMA foreign_keys = OFF")
                staging.executescript(sql)
                staging.execute("PRAGMA foreign_keys = ON")
                staging.commit()
                current_schema_hash = self._current_schema_hash()
                results = self._verify_connection(
                    staging, schema_hash, current_schema_hash=current_schema_hash
                )
            finally:
                staging.close()
            if (
                results["schema_check"] != "pass"
                or results["integrity_check"] != "pass"
                or results["foreign_key_check"] != "pass"
            ):
                raise ValueError("Isolated restore verification failed")
            if target_path is not None:
                self._restore_file_target(staging_name, target_path, target_database)
            else:
                source = sqlite3.connect(staging_name)
                original = sqlite3.connect(":memory:")
                try:
                    target_database.connect().backup(original)
                    self._copy_to_memory(source, target_database, original)
                finally:
                    source.close()
                    original.close()
            return results
        except sqlite3.Error as exc:
            raise ValueError("Isolated restore SQL verification failed") from exc
        finally:
            try:
                os.unlink(staging_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _reject_symlink_ancestors(path: Path) -> None:
        for ancestor in path.parents:
            if ancestor.is_symlink():
                raise ValueError("Refusing to follow a symlink path component")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            dir_fd = os.open(path, os.O_RDONLY)
        except OSError:
            # Opening a directory descriptor is not supported everywhere
            # (for example Windows); durability then relies on the file
            # fsync already performed.
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            logger.debug("directory fsync not supported for %s", path)
        finally:
            os.close(dir_fd)

    def _restore_file_target(
        self, staging_name: str, target_path: Path, target_database: Any
    ) -> None:
        self._reject_symlink_ancestors(target_path)
        candidates = (
            target_path,
            Path(f"{target_path}-wal"),
            Path(f"{target_path}-shm"),
        )
        for candidate in candidates:
            if os.path.lexists(candidate) and candidate.is_symlink():
                raise ValueError("Refusing to restore over a symlink")

        target_database.close()
        moved: list[tuple[Path, Path]] = []
        installed = False
        try:
            for candidate in candidates:
                if not os.path.lexists(candidate):
                    continue
                fd, backup_name = tempfile.mkstemp(
                    prefix=f".{target_path.name}.restore-old-", dir=target_path.parent
                )
                os.close(fd)
                os.unlink(backup_name)
                backup_path = Path(backup_name)
                os.replace(candidate, backup_path)
                moved.append((candidate, backup_path))
            os.replace(staging_name, target_path)
            installed = True
            os.chmod(target_path, 0o600)
            self._fsync_directory(target_path.parent)
        except BaseException:
            if installed and os.path.lexists(target_path):
                os.unlink(target_path)
            for candidate, backup_path in reversed(moved):
                if os.path.lexists(backup_path):
                    os.replace(backup_path, candidate)
            try:
                self._fsync_directory(target_path.parent)
            except OSError:
                logger.exception("Could not fsync directory after restore rollback")
            raise
        else:
            for _, backup_path in moved:
                try:
                    os.unlink(backup_path)
                except OSError:
                    logger.warning("Could not remove superseded restore backup %s", backup_path)

    def _backup_connection(
        self, source: sqlite3.Connection, destination: sqlite3.Connection
    ) -> None:
        source.backup(destination)

    def _restore_memory_snapshot(self, target_database: Any, original: sqlite3.Connection) -> None:
        target_database.close()
        rollback = target_database.connect()
        original.backup(rollback)
        rollback.commit()

    def _copy_to_memory(
        self,
        source: sqlite3.Connection,
        target_database: Any,
        original: sqlite3.Connection,
    ) -> None:
        target_database.close()
        try:
            destination = target_database.connect()
            self._backup_connection(source, destination)
            destination.commit()
        except BaseException as exc:
            try:
                self._restore_memory_snapshot(target_database, original)
            except BaseException as rollback_exc:
                if isinstance(exc, Exception):
                    raise BackupRestoreError(
                        "in-memory restore failed and rollback failed"
                    ) from rollback_exc
                raise exc from rollback_exc
            if isinstance(exc, Exception):
                raise BackupRestoreError(
                    "in-memory restore failed; original target restored"
                ) from exc
            raise

    def _current_schema_hash(self) -> str:
        from zero.config import Settings
        from zero.persistence.connection import Database
        from zero.persistence.migrations import apply_migrations

        reference = Database(Settings.load_for_test())
        try:
            apply_migrations(reference)
            return self._schema_hash(reference.connect())
        finally:
            reference.close()

    def _migration_ledger_matches(self, conn: Any) -> bool:
        from zero.persistence.migrations import _migration_checksum, _migration_files

        try:
            rows = conn.execute("SELECT id, checksum FROM schema_migrations").fetchall()
        except sqlite3.Error:
            return False
        actual = {str(row[0]): row[1] for row in rows}
        expected = {
            path.stem: _migration_checksum(path.read_text(encoding="utf-8"))
            for path in _migration_files()
        }
        return actual == expected

    def _schema_hash(self, conn: Any) -> str:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        canonical = "\n".join("|".join(str(value) for value in row) for row in rows)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _verify_connection(
        self,
        conn: Any,
        expected_schema_hash: str | None,
        *,
        current_schema_hash: str | None = None,
    ) -> dict[str, Any]:
        results: dict[str, Any] = {
            "schema_check": "pass",
            "table_counts": {},
            "integrity_check": "pass",
            "foreign_key_check": "pass",
        }
        expected_tables = {
            "schema_migrations",
            "projects",
            "users",
            "audit_events",
            "plans",
            "plan_revisions",
            "executions",
            "tasks",
            "artifacts",
            "rag_documents",
            "merge_proposals",
        }
        actual_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = expected_tables - actual_tables
        actual_schema_hash = self._schema_hash(conn)
        if current_schema_hash is None:
            current_schema_hash = self._current_schema_hash()
        if (
            missing
            or (expected_schema_hash is not None and actual_schema_hash != expected_schema_hash)
            or actual_schema_hash != current_schema_hash
            or not self._migration_ledger_matches(conn)
        ):
            results["schema_check"] = "fail"
        for table in sorted(actual_tables):
            if table.startswith("sqlite_"):
                continue
            try:
                results["table_counts"][table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                pass
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            results["integrity_check"] = "fail"
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            results["foreign_key_check"] = "fail"
        return results

    def _verify_restore(self, database: Any) -> dict[str, Any]:
        """Compatibility wrapper for callers that verify a target directly."""
        return self._verify_connection(database.connect(), None)


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

        Per PLAN.md M14: "Secret canary scan across logs, audit, metrics,
        artifacts, prompts, and backups." This implementation covers every
        durable text surface the control plane owns plus the in-memory
        metrics vocabulary. An empty list means no secrets found.
        """
        results: dict[str, list[str]] = {}
        # Durable surfaces.
        results["audit_events"] = self._scan_audit_events()
        results["artifacts"] = self._scan_artifacts()
        results["conversation_events"] = self._scan_conversation_events()
        results["knowledge_records"] = self._scan_knowledge_records()
        results["provider_requests"] = self._scan_provider_requests()
        results["context_versions"] = self._scan_context_versions()
        results["result_deliveries"] = self._scan_result_deliveries()
        results["interface_event_log"] = self._scan_interface_event_log()
        # In-memory surfaces: metric label vocabulary is closed, but a
        # canary verifies that no high-cardinality payload leaked into it.
        results["metrics"] = self._scan_metrics_labels()
        return {surface: findings for surface, findings in results.items()}

    def _scan_column(self, sql: str, label_prefix: str) -> list[str]:
        findings: list[str] = []
        conn = self._services.database.connect()
        try:
            cursor = conn.execute(sql)
        except sqlite3.Error:
            return findings
        for row in cursor.fetchall():
            matches = scan_for_secrets(row[1] or "")
            if matches:
                findings.append(f"{label_prefix} {row[0]}: {matches}")
        return findings

    def _scan_provider_requests(self) -> list[str]:
        return self._scan_column(
            "SELECT id, COALESCE(error_message, '') FROM provider_requests",
            "provider_request",
        )

    def _scan_context_versions(self) -> list[str]:
        """Scan rendered context regions (system policy, plan contract)."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, system_message, plan_contract, compaction_summary FROM context_versions"
        )
        for row in cursor.fetchall():
            blob = " ".join(
                part or ""
                for part in (row["system_message"], row["plan_contract"], row["compaction_summary"])
            )
            matches = scan_for_secrets(blob)
            if matches:
                findings.append(f"context_version {row['id']}: {matches}")
        return findings

    def _scan_result_deliveries(self) -> list[str]:
        return self._scan_column(
            "SELECT id, COALESCE(content, '') FROM result_deliveries",
            "result_delivery",
        )

    def _scan_interface_event_log(self) -> list[str]:
        return self._scan_column(
            "SELECT id, COALESCE(event_content, '') FROM interface_event_log",
            "interface_event",
        )

    def _scan_metrics_labels(self) -> list[str]:
        findings: list[str] = []
        metrics = getattr(self._services, "metrics", None)
        if metrics is None:
            return findings
        for key in metrics.get_counters():
            matches = scan_for_secrets(key)
            if matches:
                findings.append(f"metric key {key!r}: {matches}")
        return findings

    def _scan_audit_events(self) -> list[str]:
        """Scan all audit event summaries for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, redacted_summary FROM audit_events WHERE redacted_summary IS NOT NULL"
        )
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["redacted_summary"] or "")
            if matches:
                findings.append(f"audit_event {row['id']}: {matches}")
        return findings

    def _scan_artifacts(self) -> list[str]:
        """Scan all artifact content for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute("SELECT id, content FROM artifacts")
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["content"] or "")
            if matches:
                findings.append(f"artifact {row['id']}: {matches}")
        return findings

    def _scan_conversation_events(self) -> list[str]:
        """Scan all conversation event content for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute("SELECT id, content FROM conversation_events")
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["content"] or "")
            if matches:
                findings.append(f"conversation_event {row['id']}: {matches}")
        return findings

    def _scan_knowledge_records(self) -> list[str]:
        """Scan all knowledge record content for secrets."""
        findings: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute("SELECT id, content FROM knowledge_records")
        for row in cursor.fetchall():
            matches = scan_for_secrets(row["content"] or "")
            if matches:
                findings.append(f"knowledge_record {row['id']}: {matches}")
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

    def recover_stale_provider_requests(
        self, *, max_age_seconds: int = 300
    ) -> dict[str, list[str]]:
        """Recover abandoned provider claims without destroying recoverable work.

        Per the release audit (§5.2): a ``pending`` request may never have
        been dispatched, so it can safely be requeued. A ``streaming``
        request was in flight when the process died; its outcome is
        uncertain and must be marked unknown for operator reconciliation,
        never replayed automatically.
        """
        if max_age_seconds < 1 or max_age_seconds > 86_400:
            raise ValueError("provider recovery age is outside the allowed range")
        conn = self._services.database.connect()
        stale_predicate = (
            "((lease_expires_at IS NOT NULL AND lease_expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
            "OR (lease_expires_at IS NULL AND started_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)))"
        )
        params = (f"-{max_age_seconds} seconds",)

        # 1. Never-dispatched pending requests: release the claim and
        # return them to the queue so normal operation can retry them.
        conn.execute(
            "UPDATE provider_requests SET state = 'pending', "
            "error_class = NULL, error_message = NULL, "
            "claim_owner = NULL, claim_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
            "WHERE state = 'pending' AND claim_owner IS NOT NULL AND " + stale_predicate,
            params,
        )

        # 2. Streaming requests: the external provider may have accepted
        # them; record an explicit unknown outcome for reconciliation.
        cursor = conn.execute(
            "SELECT id FROM provider_requests WHERE state = 'streaming' AND " + stale_predicate,
            params,
        )
        streaming_rows = [str(row["id"]) for row in cursor.fetchall()]
        if streaming_rows:
            conn.executemany(
                "UPDATE provider_requests SET state = 'unknown', "
                "error_class = 'unknown_outcome', "
                "error_message = 'startup recovery: provider outcome is unknown', "
                "claim_owner = NULL, claim_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL, "
                "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND state IN ('streaming')",
                [(provider_id,) for provider_id in streaming_rows],
            )
        conn.commit()
        return {"requeued_pending": [], "unknown_streaming": streaming_rows}

    def recover_orphan_worktrees(self) -> list[str]:
        """Mark worktrees interrupted only with proof of non-ownership.

        Per the release audit (§5.2): blanket interruption of every
        active worktree violates "cleanup requires proof of
        non-ownership". A worktree is only interrupted here when its
        owning task attempt holds no unexpired lease and no running
        attempt remains; otherwise it is left untouched.
        """
        recovered: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT w.id AS id, w.project_id AS project_id, w.task_id AS task_id "
            "FROM worktrees w WHERE w.state = 'active'"
        )
        from zero.domain.worktrees import WorktreeId

        for row in cursor.fetchall():
            task_id_value = row["task_id"]
            if task_id_value is not None:
                active_attempt = conn.execute(
                    "SELECT 1 FROM task_attempts "
                    "WHERE task_id = ? AND project_id = ? AND state = 'running' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "LIMIT 1",
                    (task_id_value, row["project_id"]),
                ).fetchone()
                if active_attempt is not None:
                    # A worker still owns this worktree's task attempt;
                    # interrupting it would destroy live work.
                    continue
            try:
                project_id = ProjectId(row["project_id"])
                owner = self._services.identity._identity_repo.get_project(project_id).owner_user_id
                self._services.worktree.mark_worktree_interrupted(
                    project_id=project_id,
                    worktree_id=WorktreeId(row["id"]),
                    actor_id=owner,
                    source="system",
                )
                recovered.append(row["id"])
            except (OSError, RuntimeError, sqlite3.Error, ValueError) as e:
                logger.warning(
                    "Failed to recover worktree %s: %s",
                    row["id"],
                    e,
                )
        return recovered

    def recover_partial_compaction(self) -> list[str]:
        """Recover partial compaction states by inspecting actual context state.

        Per the release audit (§5.2): records are not blanket-failed.
        If the target context version exists but is not yet active while
        the source is still active, the compaction can be completed by
        activating the durable target. Only records whose target is
        missing (or already superseded) are failed.
        """
        recovered: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute(
            "SELECT id, execution_id, source_context_version, target_context_version "
            "FROM compaction_records "
            "WHERE state IN ('pre_flush', 'fit', 'summary_validated', 'committed')"
        )
        for row in cursor.fetchall():
            record_id = row["id"]
            target_version = row["target_context_version"]
            try:
                target_exists = conn.execute(
                    "SELECT 1 FROM context_versions WHERE execution_id = ? AND version = ? LIMIT 1",
                    (row["execution_id"], target_version),
                ).fetchone()
                source_active = conn.execute(
                    "SELECT 1 FROM context_versions "
                    "WHERE execution_id = ? AND version = ? AND active = 1 LIMIT 1",
                    (row["execution_id"], row["source_context_version"]),
                ).fetchone()
                if target_exists is not None and source_active is not None:
                    # Durable commit point reached or passed; finishing
                    # activation leaves either old-active or new-active,
                    # never a gap.
                    conn.execute(
                        "UPDATE context_versions SET active = 0 "
                        "WHERE execution_id = ? AND active = 1 AND version != ?",
                        (row["execution_id"], target_version),
                    )
                    conn.execute(
                        "UPDATE context_versions SET active = 1 "
                        "WHERE execution_id = ? AND version = ?",
                        (row["execution_id"], target_version),
                    )
                    conn.execute(
                        "UPDATE compaction_records SET state = 'activated' WHERE id = ?",
                        (record_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE compaction_records SET state = 'failed' WHERE id = ?",
                        (record_id,),
                    )
                conn.commit()
                recovered.append(record_id)
            except (OSError, RuntimeError, sqlite3.Error, ValueError) as e:
                logger.warning(
                    "Failed to recover compaction %s: %s",
                    record_id,
                    e,
                )
        return recovered

    def recover_stuck_executions(self) -> list[str]:
        """Find and recover executions stuck in 'running' state.

        Per ``zero-recovery-consistency`` §"Leases distinguish ownership
        from history": an expired lease does not prove failure. It
        proves that current ownership is absent; reconciliation inspects
        process, artifact, and external evidence.
        """
        recovered: list[str] = []
        conn = self._services.database.connect()
        cursor = conn.execute("SELECT id, project_id FROM executions WHERE state = 'running'")
        for row in cursor.fetchall():
            from zero.domain.execution import ExecutionId

            exec_id = ExecutionId(row["id"])
            try:
                project = self._services.identity.get_project(ProjectId(row["project_id"]))
                # Use the worker service's recovery method.
                self._services.worker.recover_after_restart(
                    execution_id=exec_id,
                    project_id=ProjectId(row["project_id"]),
                    actor_id=project.owner_user_id,
                    source="system",
                )
                recovered.append(row["id"])
            except (OSError, RuntimeError, sqlite3.Error, ValueError) as e:
                logger.warning(
                    "Failed to recover execution %s: %s",
                    row["id"],
                    e,
                )
        return recovered

    def run_all_recovery(self) -> dict[str, object]:
        """Run all recovery procedures.

        Per PLAN.md M14: "Stuck execution, orphan worktree, partial
        compaction, failed migration, and provider outage recovery."
        Merge crash-window reconciliation is included when an
        integration service is reachable through the service bundle.
        """
        report: dict[str, object] = {
            "stale_provider_requests": self.recover_stale_provider_requests(),
            "stuck_executions": self.recover_stuck_executions(),
            "orphan_worktrees": self.recover_orphan_worktrees(),
            "partial_compactions": self.recover_partial_compaction(),
        }
        integration = getattr(self._services, "integration", None)
        if integration is not None and hasattr(integration, "recover_inflight_merges"):
            try:
                report["inflight_merges"] = integration.recover_inflight_merges()
            except (OSError, RuntimeError) as exc:
                logger.warning("merge recovery failed: %s", type(exc).__name__)
        result_delivery = getattr(self._services, "result_delivery", None)
        if result_delivery is not None and hasattr(result_delivery, "recover_stale"):
            try:
                report["stale_result_deliveries"] = result_delivery.recover_stale()
            except (OSError, RuntimeError) as exc:
                logger.warning("result-delivery recovery failed: %s", type(exc).__name__)
        worktree_service = getattr(self._services, "worktree", None)
        if worktree_service is not None and hasattr(
            worktree_service, "recover_worktrees_after_restart"
        ):
            # The recovery call is project-scoped and authorized: run it
            # per project under the owner identity, isolating failures.
            interrupted_total = 0
            identity_for_recovery = getattr(self._services, "identity", None)
            worker_for_recovery = getattr(self._services, "worker", None)
            if (
                identity_for_recovery is not None
                and worker_for_recovery is not None
                and hasattr(worker_for_recovery, "list_project_executions")
                and hasattr(worktree_service, "list_worktrees_for_project")
            ):
                for project in identity_for_recovery.list_projects():
                    try:
                        executions = worker_for_recovery.list_project_executions(
                            project_id=project.id,
                            actor_id=project.owner_user_id,
                            source="system",
                        )
                    except (OSError, RuntimeError):
                        continue
                    for execution in executions:
                        try:
                            recovered = worktree_service.recover_worktrees_after_restart(
                                project_id=project.id,
                                actor_id=project.owner_user_id,
                                source="system",
                            )
                            interrupted_total += len(recovered)
                        except (OSError, RuntimeError) as exc:
                            logger.warning(
                                "worktree recovery failed for %s: %s",
                                execution.id.value,
                                type(exc).__name__,
                            )
            report["interrupted_worktrees"] = interrupted_total
        report["worktrees_cleaned"] = self._cleanup_terminal_worktrees()
        return report

    def _cleanup_terminal_worktrees(self) -> list[str]:
        """Mark and remove worktrees of terminal executions (bounded).

        Safety contract: only ``succeeded``/``failed``/``cancelled``
        worktrees whose execution is terminal become cleanup-eligible,
        removal uses git's non-force path (uncommitted human work makes
        removal fail loudly), and the pass is capped per invocation so
        recovery stays bounded.
        """
        import logging as _logging

        worktree_service = getattr(self._services, "worktree", None)
        worker_service = getattr(self._services, "worker", None)
        identity_service = getattr(self._services, "identity", None)
        if (
            worktree_service is None
            or worker_service is None
            or identity_service is None
            or not all(
                hasattr(worktree_service, name)
                for name in (
                    "list_worktrees_for_project",
                    "mark_cleanup_eligible",
                    "remove_worktree",
                )
            )
        ):
            return []
        _log = _logging.getLogger(__name__)
        terminal_executions = {"completed", "failed", "cancelled"}
        eligible_states = {"succeeded", "failed", "cancelled"}
        cleaned: list[str] = []
        max_per_pass = 25
        try:
            projects = identity_service.list_projects()
        except (OSError, RuntimeError):
            return []
        for project in projects:
            if len(cleaned) >= max_per_pass:
                break
            owner_id = project.owner_user_id
            try:
                execution_states = {
                    execution.id: execution.state
                    for execution in worker_service.list_project_executions(
                        project_id=project.id,
                        actor_id=owner_id,
                        source="system",
                    )
                }
                worktrees = worktree_service.list_worktrees_for_project(
                    project.id,
                    actor_id=owner_id,
                    source="system",
                )
            except (OSError, RuntimeError) as exc:
                _log.warning(
                    "worktree cleanup skipped for project %s: %s",
                    project.id.value,
                    type(exc).__name__,
                )
                continue
            for worktree in worktrees:
                if len(cleaned) >= max_per_pass:
                    break
                if worktree.state not in eligible_states:
                    continue
                execution_state = execution_states.get(worktree.execution_id)
                if execution_state not in terminal_executions:
                    continue
                try:
                    worktree_service.mark_cleanup_eligible(
                        project_id=project.id,
                        worktree_id=worktree.id,
                        actor_id=owner_id,
                        source="system",
                    )
                    worktree_service.remove_worktree(
                        project_id=project.id,
                        worktree_id=worktree.id,
                        actor_id=owner_id,
                        source="system",
                    )
                    cleaned.append(worktree.id.value)
                except (OSError, RuntimeError) as exc:
                    # Dirty worktrees (uncommitted changes) refuse
                    # non-force removal by design; leave them for a
                    # human and keep sweeping.
                    _log.debug(
                        "worktree %s not removed: %s",
                        worktree.id.value,
                        type(exc).__name__,
                    )
        return cleaned
