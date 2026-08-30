"""Provider repository — provider models, requests, usage records, pricing.

Per ``zero-claude-token-economics``:
- Token classes remain separate (input, output, cache creation, cache read).
- Whole-agent-tree usage is counted exactly once.
- Estimated cost is distinct from authoritative reconciled billing.
- Persist adapter/model/version with every request and usage record.

Per ``zero-project-isolation-evidence``: all queries filter by
``project_id`` before any row is loaded.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.artifacts import ArtifactId
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId
from zero.domain.providers import (
    ALL_CAPABILITIES,
    PricingEntry,
    ProviderErrorClass,
    ProviderModel,
    ProviderModelId,
    ProviderModelNotFoundError,
    ProviderRequest,
    ProviderRequestId,
    ProviderRequestNotFoundError,
    ProviderRequestState,
    ProviderRequestStateError,
    TokenUsage,
    UsageRecord,
    UsageRecordId,
)
from zero.persistence.connection import Database

_PROVIDER_REQUEST_COLUMNS = (
    "id, project_id, execution_id, provider, model_name, request_hash, "
    "idempotency_key, state, error_class, error_message, response_artifact_id, "
    "started_at, completed_at, attempt_count, claim_owner, claim_token, "
    "lease_expires_at, heartbeat_at"
)


def _row_to_provider_model(row: sqlite3.Row) -> ProviderModel:
    caps = tuple(json.loads(row["capabilities"]))
    return ProviderModel(
        id=ProviderModelId(row["id"]),
        provider=row["provider"],
        model_name=row["model_name"],
        context_window=row["context_window"],
        max_output_tokens=row["max_output_tokens"],
        capabilities=caps,  # type: ignore[arg-type]
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def _row_to_provider_request(row: sqlite3.Row) -> ProviderRequest:
    return ProviderRequest(
        id=ProviderRequestId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        execution_id=ExecutionId(row["execution_id"]) if row["execution_id"] else None,
        provider=row["provider"],
        model_name=row["model_name"],
        request_hash=row["request_hash"],
        state=row["state"],  # type: ignore[arg-type]
        idempotency_key=row["idempotency_key"],
        error_class=row["error_class"],  # type: ignore[arg-type]
        error_message=row["error_message"],
        response_artifact_id=ArtifactId(row["response_artifact_id"])
        if row["response_artifact_id"]
        else None,
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        attempt_count=int(row["attempt_count"] or 0),
        claim_owner=row["claim_owner"],
        claim_token=row["claim_token"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
    )


def _row_to_usage_record(row: sqlite3.Row) -> UsageRecord:
    usage = TokenUsage(
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_creation_input_tokens=row["cache_creation_input_tokens"],
        cache_read_input_tokens=row["cache_read_input_tokens"],
    )
    return UsageRecord(
        id=UsageRecordId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        provider_request_id=ProviderRequestId(row["provider_request_id"]),
        execution_id=ExecutionId(row["execution_id"]) if row["execution_id"] else None,
        provider_message_id=row["provider_message_id"],
        usage=usage,
        estimated_cost_usd=row["estimated_cost_usd"],
        pricing_catalog_version=row["pricing_catalog_version"],
        reconciled_cost_usd=row["reconciled_cost_usd"],
        is_whole_tree=bool(row["is_whole_tree"]),
        created_at=row["created_at"],
    )


class ProviderRepository:
    """Database-backed provider model, request, usage, and pricing
    repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def database(self) -> Database:
        return self._database

    # ------------------------------------------------------------------
    # Provider models
    # ------------------------------------------------------------------

    def insert_provider_model(self, model: ProviderModel, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO provider_models "
                "(id, provider, model_name, context_window, "
                "max_output_tokens, capabilities, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    model.id.value,
                    model.provider,
                    model.model_name,
                    model.context_window,
                    model.max_output_tokens,
                    json.dumps(list(model.capabilities)),
                    1 if model.is_active else 0,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def set_provider_model_capabilities(
        self,
        model: ProviderModel,
        capabilities: tuple[str, ...],
        *,
        commit: bool = True,
    ) -> ProviderModel:
        """Persist an OBSERVED capability set for one provider model.

        Capabilities describe observed contract support (per
        ``zero-provider-adapter-contract``). The live-probe path uses
        this to record that a gateway silently strips the native
        ``tools`` parameter: every native-tool request would otherwise
        sail through validation and produce hallucinated answers with
        ``tool_calls: []`` forever.
        """
        cleaned: list[str] = []
        for capability in capabilities:
            if capability not in ALL_CAPABILITIES:
                raise ValueError(f"unknown provider capability: {capability!r}")
            if capability not in cleaned:
                cleaned.append(capability)
        conn = self._database.connect()
        conn.execute(
            "UPDATE provider_models SET capabilities = ? WHERE id = ?",
            (json.dumps(cleaned), model.id.value),
        )
        if commit:
            conn.commit()
        return ProviderModel(
            id=model.id,
            provider=model.provider,
            model_name=model.model_name,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            capabilities=tuple(cleaned),
            is_active=model.is_active,
            created_at=model.created_at,
        )

    def get_provider_model(self, provider: str, model_name: str) -> ProviderModel:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, provider, model_name, context_window, "
            "max_output_tokens, capabilities, is_active, created_at "
            "FROM provider_models WHERE provider = ? AND model_name = ?",
            (provider, model_name),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProviderModelNotFoundError(f"Provider model {provider}:{model_name} not found")
        return _row_to_provider_model(row)

    def get_provider_model_by_id(self, model_id: ProviderModelId) -> ProviderModel:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, provider, model_name, context_window, "
            "max_output_tokens, capabilities, is_active, created_at "
            "FROM provider_models WHERE id = ?",
            (model_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProviderModelNotFoundError(f"Provider model {model_id} not found")
        return _row_to_provider_model(row)

    def list_provider_models(self, *, active_only: bool = True) -> list[ProviderModel]:
        conn = self._database.connect()
        if active_only:
            cursor = conn.execute(
                "SELECT id, provider, model_name, context_window, "
                "max_output_tokens, capabilities, is_active, created_at "
                "FROM provider_models WHERE is_active = 1 "
                "ORDER BY provider, model_name"
            )
        else:
            cursor = conn.execute(
                "SELECT id, provider, model_name, context_window, "
                "max_output_tokens, capabilities, is_active, created_at "
                "FROM provider_models ORDER BY provider, model_name"
            )
        return [_row_to_provider_model(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Provider requests
    # ------------------------------------------------------------------

    def insert_provider_request(self, req: ProviderRequest, *, commit: bool = True) -> bool:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO provider_requests "
                "(id, project_id, execution_id, provider, model_name, "
                "request_hash, idempotency_key, state, error_class, error_message, "
                "response_artifact_id, attempt_count, claim_owner, claim_token, "
                "lease_expires_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req.id.value,
                    req.project_id.value,
                    req.execution_id.value if req.execution_id else None,
                    req.provider,
                    req.model_name,
                    req.request_hash,
                    req.idempotency_key,
                    req.state,
                    req.error_class,
                    req.error_message,
                    req.response_artifact_id.value if req.response_artifact_id else None,
                    req.attempt_count,
                    req.claim_owner,
                    req.claim_token,
                    req.lease_expires_at,
                    req.heartbeat_at,
                ),
            )
            if commit:
                conn.commit()
            return True
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if (
                "UNIQUE constraint failed: provider_requests.project_id, provider_requests.request_hash"
                in str(exc)
                or (
                    "UNIQUE constraint failed: provider_requests.project_id, provider_requests.idempotency_key"
                    in str(exc)
                )
            ):
                # The caller lost the durable provider request claim.
                return False
            raise

    def get_provider_request_by_hash(
        self,
        project_id: ProjectId,
        request_hash: str,
    ) -> ProviderRequest | None:
        conn = self._database.connect()
        cursor = conn.execute(
            f"SELECT {_PROVIDER_REQUEST_COLUMNS} "
            "FROM provider_requests WHERE project_id = ? AND request_hash = ?",
            (project_id.value, request_hash),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_provider_request(row)

    def get_provider_request_by_idempotency_key(
        self,
        project_id: ProjectId,
        idempotency_key: str | None,
    ) -> ProviderRequest | None:
        """Load the one logical request claim for a project/key pair."""
        if idempotency_key is None:
            return None
        conn = self._database.connect()
        cursor = conn.execute(
            f"SELECT {_PROVIDER_REQUEST_COLUMNS} "
            "FROM provider_requests WHERE project_id = ? AND idempotency_key = ?",
            (project_id.value, idempotency_key),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_provider_request(row)

    def get_provider_request(self, req_id: ProviderRequestId) -> ProviderRequest:
        conn = self._database.connect()
        cursor = conn.execute(
            f"SELECT {_PROVIDER_REQUEST_COLUMNS} FROM provider_requests WHERE id = ?",
            (req_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProviderRequestNotFoundError(f"Provider request {req_id} not found")
        return _row_to_provider_request(row)

    def claim_provider_request(
        self,
        req_id: ProviderRequestId,
        *,
        claim_owner: str,
        lease_seconds: int = 300,
    ) -> ProviderRequest:
        """Claim a pending/retryable request with a fenced lease token."""
        if not claim_owner or len(claim_owner) > 256:
            raise ValueError("claim_owner must be between 1 and 256 characters")
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("provider lease must be between 1 and 86400 seconds")
        import secrets

        token = secrets.token_urlsafe(24)
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE provider_requests SET state = 'streaming', "
            "attempt_count = attempt_count + 1, claim_owner = ?, claim_token = ?, "
            "heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), "
            "completed_at = NULL "
            "WHERE id = ? AND state IN ('pending', 'failed') "
            "AND (claim_token IS NULL OR lease_expires_at IS NULL)",
            (claim_owner, token, f"+{lease_seconds} seconds", req_id.value),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT state FROM provider_requests WHERE id = ?",
                (req_id.value,),
            ).fetchone()
            if row is None:
                raise ProviderRequestNotFoundError(f"Provider request {req_id} not found")
            raise ProviderRequestStateError(
                f"provider request {req_id} cannot be claimed from {row['state']!r}"
            )
        conn.commit()
        return self.get_provider_request(req_id)

    def heartbeat_provider_request(
        self,
        req_id: ProviderRequestId,
        *,
        claim_token: str,
        lease_seconds: int = 300,
    ) -> bool:
        if not claim_token:
            raise ValueError("claim_token must be non-empty")
        if lease_seconds < 1 or lease_seconds > 86_400:
            raise ValueError("provider lease must be between 1 and 86400 seconds")
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE provider_requests SET heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "lease_expires_at = strftime('%Y-%m-%dT%H:%M:%fZ','now', ?) "
            "WHERE id = ? AND state = 'streaming' AND claim_token = ?",
            (f"+{lease_seconds} seconds", req_id.value, claim_token),
        )
        conn.commit()
        return cursor.rowcount == 1

    def update_provider_request_state(
        self,
        req_id: ProviderRequestId,
        new_state: ProviderRequestState,
        *,
        error_class: ProviderErrorClass | None = None,
        error_message: str | None = None,
        response_artifact_id: ArtifactId | None = None,
        claim_token: str | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        terminal_states = {"completed", "failed", "cancelled", "unknown"}
        allowed_predecessors: dict[str, tuple[str, ...]] = {
            "streaming": ("pending", "failed"),
            "completed": ("streaming",),
            "failed": ("pending", "streaming"),
            "cancelled": ("pending", "streaming"),
            "unknown": ("pending", "streaming"),
        }
        predecessors = allowed_predecessors.get(new_state)
        if predecessors is None:
            raise ProviderRequestStateError(f"provider request cannot transition to {new_state!r}")
        placeholders = ", ".join("?" for _ in predecessors)
        where = f"WHERE id = ? AND state IN ({placeholders})"
        where_params: list[object] = [req_id.value, *predecessors]
        if claim_token is not None:
            where += " AND claim_token = ?"
            where_params.append(claim_token)
        if new_state in terminal_states:
            cursor = conn.execute(
                "UPDATE provider_requests SET state = ?, "
                "error_class = ?, error_message = ?, "
                "response_artifact_id = ?, claim_owner = NULL, claim_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, "
                "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"{where}",
                (
                    new_state,
                    error_class,
                    error_message,
                    response_artifact_id.value if response_artifact_id else None,
                    *where_params,
                ),
            )
        else:
            cursor = conn.execute(
                f"UPDATE provider_requests SET state = ? {where}",
                (new_state, *where_params),
            )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT state FROM provider_requests WHERE id = ?",
                (req_id.value,),
            ).fetchone()
            if row is None:
                raise ProviderRequestNotFoundError(f"Provider request {req_id} not found")
            raise ProviderRequestStateError(
                f"provider request {req_id} cannot transition from {row['state']!r} "
                f"to {new_state!r}"
            )
        if commit:
            conn.commit()

    def list_provider_requests_for_project(
        self,
        project_id: ProjectId,
        *,
        limit: int = 100,
    ) -> list[ProviderRequest]:
        conn = self._database.connect()
        cursor = conn.execute(
            f"SELECT {_PROVIDER_REQUEST_COLUMNS} "
            "FROM provider_requests WHERE project_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (project_id.value, limit),
        )
        return [_row_to_provider_request(row) for row in cursor.fetchall()]

    def list_provider_requests_for_execution(
        self, execution_id: ExecutionId
    ) -> list[ProviderRequest]:
        conn = self._database.connect()
        cursor = conn.execute(
            f"SELECT {_PROVIDER_REQUEST_COLUMNS} "
            "FROM provider_requests WHERE execution_id = ? "
            "ORDER BY started_at ASC",
            (execution_id.value,),
        )
        return [_row_to_provider_request(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Usage records
    # ------------------------------------------------------------------

    def insert_usage_record(self, record: UsageRecord, *, commit: bool = True) -> bool:
        """Insert a usage record. Returns True if inserted, False if
        duplicate (idempotent deduplication).

        Per ``zero-claude-token-economics``: duplicate streamed usage
        is not double-counted. The UNIQUE(provider_request_id,
        provider_message_id) constraint ensures idempotency.
        """
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO usage_records "
                "(id, project_id, provider_request_id, execution_id, "
                "provider_message_id, input_tokens, output_tokens, "
                "cache_creation_input_tokens, cache_read_input_tokens, "
                "estimated_cost_usd, pricing_catalog_version, "
                "reconciled_cost_usd, is_whole_tree) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id.value,
                    record.project_id.value,
                    record.provider_request_id.value,
                    record.execution_id.value if record.execution_id else None,
                    record.provider_message_id,
                    record.usage.input_tokens,
                    record.usage.output_tokens,
                    record.usage.cache_creation_input_tokens,
                    record.usage.cache_read_input_tokens,
                    record.estimated_cost_usd,
                    record.pricing_catalog_version,
                    record.reconciled_cost_usd,
                    1 if record.is_whole_tree else 0,
                ),
            )
            if commit:
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            # Only the declared logical usage-deduplication constraints are
            # idempotent. A generated-ID collision or a lineage/foreign-key
            # failure must remain visible to the caller. Detection probes the
            # stored rows instead of pattern-matching engine-specific error
            # text, which varies across SQLite versions and locales.
            if self._logical_usage_duplicate_exists(record):
                return False
            raise

    def _logical_usage_duplicate_exists(self, record: UsageRecord) -> bool:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT 1 FROM usage_records "
            "WHERE provider_request_id = ? AND provider_message_id IS ? LIMIT 1",
            (record.provider_request_id.value, record.provider_message_id),
        )
        return cursor.fetchone() is not None

    def list_usage_records_for_request(self, req_id: ProviderRequestId) -> list[UsageRecord]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, provider_request_id, execution_id, "
            "provider_message_id, input_tokens, output_tokens, "
            "cache_creation_input_tokens, cache_read_input_tokens, "
            "estimated_cost_usd, pricing_catalog_version, "
            "reconciled_cost_usd, is_whole_tree, created_at "
            "FROM usage_records WHERE provider_request_id = ? "
            "ORDER BY created_at ASC",
            (req_id.value,),
        )
        return [_row_to_usage_record(row) for row in cursor.fetchall()]

    def list_usage_records_for_project(self, project_id: ProjectId) -> list[UsageRecord]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, provider_request_id, execution_id, "
            "provider_message_id, input_tokens, output_tokens, "
            "cache_creation_input_tokens, cache_read_input_tokens, "
            "estimated_cost_usd, pricing_catalog_version, "
            "reconciled_cost_usd, is_whole_tree, created_at "
            "FROM usage_records WHERE project_id = ? "
            "ORDER BY created_at ASC",
            (project_id.value,),
        )
        return [_row_to_usage_record(row) for row in cursor.fetchall()]

    def aggregate_usage_for_project(self, project_id: ProjectId) -> TokenUsage:
        """Aggregate all usage for a project into one TokenUsage.

        Per ``zero-claude-token-economics`` §"Whole-tree child usage
        aggregation": sum all non-duplicate records.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT "
            "COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(cache_creation_input_tokens), 0), "
            "COALESCE(SUM(cache_read_input_tokens), 0) "
            "FROM usage_records WHERE project_id = ?",
            (project_id.value,),
        )
        row = cursor.fetchone()
        return TokenUsage(
            input_tokens=int(row[0]),
            output_tokens=int(row[1]),
            cache_creation_input_tokens=int(row[2]),
            cache_read_input_tokens=int(row[3]),
        )

    # ------------------------------------------------------------------
    # Pricing catalog
    # ------------------------------------------------------------------

    def insert_pricing_entry(self, entry: PricingEntry, *, commit: bool = True) -> None:
        from zero.domain.ids import generate_pricing_entry_id

        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO pricing_catalog_entries "
                "(id, catalog_version, provider, model_name, "
                "input_price_per_million, output_price_per_million, "
                "cache_creation_price_per_million, "
                "cache_read_price_per_million, effective_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generate_pricing_entry_id(),
                    entry.catalog_version,
                    entry.provider,
                    entry.model_name,
                    entry.input_price_per_million,
                    entry.output_price_per_million,
                    entry.cache_creation_price_per_million,
                    entry.cache_read_price_per_million,
                    entry.effective_at,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError:
            if commit:
                conn.rollback()
            raise

    def get_pricing_entry(
        self,
        catalog_version: int,
        provider: str,
        model_name: str,
    ) -> PricingEntry | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT catalog_version, provider, model_name, "
            "input_price_per_million, output_price_per_million, "
            "cache_creation_price_per_million, "
            "cache_read_price_per_million, effective_at "
            "FROM pricing_catalog_entries "
            "WHERE catalog_version = ? AND provider = ? AND model_name = ?",
            (catalog_version, provider, model_name),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return PricingEntry(
            catalog_version=row["catalog_version"],
            provider=row["provider"],
            model_name=row["model_name"],
            input_price_per_million=row["input_price_per_million"],
            output_price_per_million=row["output_price_per_million"],
            cache_creation_price_per_million=row["cache_creation_price_per_million"],
            cache_read_price_per_million=row["cache_read_price_per_million"],
            effective_at=row["effective_at"],
        )

    def get_latest_pricing_entry(self, provider: str, model_name: str) -> PricingEntry | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT catalog_version, provider, model_name, "
            "input_price_per_million, output_price_per_million, "
            "cache_creation_price_per_million, "
            "cache_read_price_per_million, effective_at "
            "FROM pricing_catalog_entries "
            "WHERE provider = ? AND model_name = ? "
            "ORDER BY catalog_version DESC LIMIT 1",
            (provider, model_name),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return PricingEntry(
            catalog_version=row["catalog_version"],
            provider=row["provider"],
            model_name=row["model_name"],
            input_price_per_million=row["input_price_per_million"],
            output_price_per_million=row["output_price_per_million"],
            cache_creation_price_per_million=row["cache_creation_price_per_million"],
            cache_read_price_per_million=row["cache_read_price_per_million"],
            effective_at=row["effective_at"],
        )

    def reconcile_usage_cost(
        self,
        project_id: ProjectId,
        usage_id: UsageRecordId,
        reconciled_cost_usd: str,
        *,
        commit: bool = True,
    ) -> None:
        """Set the authoritative reconciled cost for a usage record.

        Per ``zero-claude-token-economics`` §"Estimated cost is not
        billing truth": reconciled_cost_usd is separate from
        estimated_cost_usd.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE usage_records SET reconciled_cost_usd = ? WHERE id = ? AND project_id = ?",
            (reconciled_cost_usd, usage_id.value, project_id.value),
        )
        if cursor.rowcount == 0:
            raise ProviderRequestNotFoundError(f"Usage record {usage_id} not found")
        if commit:
            conn.commit()
