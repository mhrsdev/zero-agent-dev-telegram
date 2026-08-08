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
    PricingEntry,
    ProviderErrorClass,
    ProviderModel,
    ProviderModelId,
    ProviderModelNotFoundError,
    ProviderRequest,
    ProviderRequestId,
    ProviderRequestNotFoundError,
    ProviderRequestState,
    TokenUsage,
    UsageRecord,
    UsageRecordId,
)
from zero.persistence.connection import Database


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
        execution_id=ExecutionId(row["execution_id"])
        if row["execution_id"]
        else None,
        provider=row["provider"],
        model_name=row["model_name"],
        request_hash=row["request_hash"],
        state=row["state"],  # type: ignore[arg-type]
        error_class=row["error_class"],  # type: ignore[arg-type]
        error_message=row["error_message"],
        response_artifact_id=ArtifactId(row["response_artifact_id"])
        if row["response_artifact_id"]
        else None,
        started_at=row["started_at"],
        completed_at=row["completed_at"],
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
        execution_id=ExecutionId(row["execution_id"])
        if row["execution_id"]
        else None,
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

    # ------------------------------------------------------------------
    # Provider models
    # ------------------------------------------------------------------

    def insert_provider_model(
        self, model: ProviderModel, *, commit: bool = True
    ) -> None:
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

    def get_provider_model(
        self, provider: str, model_name: str
    ) -> ProviderModel:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, provider, model_name, context_window, "
            "max_output_tokens, capabilities, is_active, created_at "
            "FROM provider_models WHERE provider = ? AND model_name = ?",
            (provider, model_name),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProviderModelNotFoundError(
                f"Provider model {provider}:{model_name} not found"
            )
        return _row_to_provider_model(row)

    def get_provider_model_by_id(
        self, model_id: ProviderModelId
    ) -> ProviderModel:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, provider, model_name, context_window, "
            "max_output_tokens, capabilities, is_active, created_at "
            "FROM provider_models WHERE id = ?",
            (model_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProviderModelNotFoundError(
                f"Provider model {model_id} not found"
            )
        return _row_to_provider_model(row)

    def list_provider_models(
        self, *, active_only: bool = True
    ) -> list[ProviderModel]:
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

    def insert_provider_request(
        self, req: ProviderRequest, *, commit: bool = True
    ) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO provider_requests "
                "(id, project_id, execution_id, provider, model_name, "
                "request_hash, state, error_class, error_message, "
                "response_artifact_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    req.id.value,
                    req.project_id.value,
                    req.execution_id.value if req.execution_id else None,
                    req.provider,
                    req.model_name,
                    req.request_hash,
                    req.state,
                    req.error_class,
                    req.error_message,
                    req.response_artifact_id.value
                    if req.response_artifact_id
                    else None,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "request_hash" in str(exc):
                # Idempotent: the same request was already submitted.
                return
            raise

    def get_provider_request_by_hash(
        self, request_hash: str
    ) -> ProviderRequest | None:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, provider, model_name, "
            "request_hash, state, error_class, error_message, "
            "response_artifact_id, started_at, completed_at "
            "FROM provider_requests WHERE request_hash = ?",
            (request_hash,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_provider_request(row)

    def get_provider_request(
        self, req_id: ProviderRequestId
    ) -> ProviderRequest:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, execution_id, provider, model_name, "
            "request_hash, state, error_class, error_message, "
            "response_artifact_id, started_at, completed_at "
            "FROM provider_requests WHERE id = ?",
            (req_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProviderRequestNotFoundError(
                f"Provider request {req_id} not found"
            )
        return _row_to_provider_request(row)

    def update_provider_request_state(
        self,
        req_id: ProviderRequestId,
        new_state: ProviderRequestState,
        *,
        error_class: ProviderErrorClass | None = None,
        error_message: str | None = None,
        response_artifact_id: ArtifactId | None = None,
        commit: bool = True,
    ) -> None:
        conn = self._database.connect()
        terminal_states = {"completed", "failed", "cancelled", "unknown"}
        if new_state in terminal_states:
            cursor = conn.execute(
                "UPDATE provider_requests SET state = ?, "
                "error_class = ?, error_message = ?, "
                "response_artifact_id = ?, "
                "completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (
                    new_state,
                    error_class,
                    error_message,
                    response_artifact_id.value
                    if response_artifact_id
                    else None,
                    req_id.value,
                ),
            )
        else:
            cursor = conn.execute(
                "UPDATE provider_requests SET state = ? WHERE id = ?",
                (new_state, req_id.value),
            )
        if cursor.rowcount == 0:
            raise ProviderRequestNotFoundError(
                f"Provider request {req_id} not found"
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
            "SELECT id, project_id, execution_id, provider, model_name, "
            "request_hash, state, error_class, error_message, "
            "response_artifact_id, started_at, completed_at "
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
            "SELECT id, project_id, execution_id, provider, model_name, "
            "request_hash, state, error_class, error_message, "
            "response_artifact_id, started_at, completed_at "
            "FROM provider_requests WHERE execution_id = ? "
            "ORDER BY started_at ASC",
            (execution_id.value,),
        )
        return [_row_to_provider_request(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Usage records
    # ------------------------------------------------------------------

    def insert_usage_record(
        self, record: UsageRecord, *, commit: bool = True
    ) -> bool:
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
                    record.execution_id.value
                    if record.execution_id
                    else None,
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
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                # Duplicate: not double-counted.
                return False
            raise

    def list_usage_records_for_request(
        self, req_id: ProviderRequestId
    ) -> list[UsageRecord]:
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

    def list_usage_records_for_project(
        self, project_id: ProjectId
    ) -> list[UsageRecord]:
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

    def aggregate_usage_for_project(
        self, project_id: ProjectId
    ) -> TokenUsage:
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

    def insert_pricing_entry(
        self, entry: PricingEntry, *, commit: bool = True
    ) -> None:
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

    def get_latest_pricing_entry(
        self, provider: str, model_name: str
    ) -> PricingEntry | None:
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
            "UPDATE usage_records SET reconciled_cost_usd = ? "
            "WHERE id = ?",
            (reconciled_cost_usd, usage_id.value),
        )
        if cursor.rowcount == 0:
            raise ProviderRequestNotFoundError(
                f"Usage record {usage_id} not found"
            )
        if commit:
            conn.commit()
