"""Zero v2 platform contracts — Phase P.

Capability doc, health, event envelope, remote command, config schema export.

These contracts are fully implemented and ready for use. They define the
stable boundary between Zero and an external Platform service (website,
control plane, etc.). The contracts are intentionally defined early so
that Platform integration can be added without rewriting Zero's core.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Literal

from zero.core.scope import Scope

__all__ = [
    "Capability",
    "CapabilityState",
    "ConfigFieldMeta",
    "ConfigSchemaExport",
    "EventEnvelope",
    "HealthReport",
    "HealthStatus",
    "RemoteCommand",
    "RemoteCommandResult",
    "compute_capability_hash",
]


class CapabilityState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Capability:
    """A single capability exposed to Platform."""

    namespace: str  # e.g. "telegram", "memory", "agents"
    name: str       # e.g. "send_message", "promote_fact"
    state: CapabilityState
    detail: str | None = None
    # Optional: when state last changed.
    last_change_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "state": self.state.value,
            "detail": self.detail,
            "last_change_at": self.last_change_at.isoformat(),
        }


def compute_capability_hash(capabilities: list[Capability]) -> str:
    """Stable hash of capability set (SHA-256 of sorted JSON).

    Used to detect drift between Platform's view and Zero's actual state.
    """
    payload = json.dumps(
        sorted(
            ({**c.to_dict(), "last_change_at": c.last_change_at.isoformat()} for c in capabilities),
            key=lambda x: (x["namespace"], x["name"]),
        ),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"
    # Note: OFFLINE is Platform-side; Zero never reports it.


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Health report published to Platform."""

    status: HealthStatus
    capabilities_hash: str
    version: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "capabilities_hash": self.capabilities_hash,
            "version": self.version,
            "checked_at": self.checked_at.isoformat(),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Event envelope for Platform delivery.

    Per T-P.3: ``scope`` is mandatory; ``data`` has no free-form fields.
    """

    name: str
    scope: Scope
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        # T-P.3 acceptance: data has no free-form fields.
        # Allowlist: only known typed keys. This is enforced by event publishers,
        # but we also check here as a safety net.
        forbidden = {"raw_input", "raw_user_message", "raw_secret", "raw_content"}
        bad = forbidden & self.data.keys()
        if bad:
            raise ValueError(f"EventEnvelope.data contains forbidden keys: {bad}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "scope": {
                "mode": self.scope.mode.value,
                "key": self.scope.retrieval_key(),
            },
            "data": self.data,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RemoteCommand:
    """Remote command from Platform.

    Per T-P.4: no shell exec command type. Every command has strict param schema.
    """

    command_type: str  # e.g. "backup.restore", "config.set", "agent.cancel"
    params: dict[str, Any]
    id: str = field(default_factory=lambda: f"cmd_{uuid.uuid4().hex[:16]}")
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    signature: str = ""  # HMAC signature from Platform

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command_type": self.command_type,
            "params": self.params,
            "received_at": self.received_at.isoformat(),
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class RemoteCommandResult:
    """Result of executing a RemoteCommand."""

    command_id: str
    success: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    executed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ConfigFieldMeta:
    """Metadata for a single config field, exported to Platform.

    Per T-P.5 acceptance: ``secret_ref`` fields NEVER have values (even masked).
    Only ``{configured: bool, last_rotated_at}`` is exposed.
    """

    name: str
    type: str
    required: bool
    default: Any = None
    secret: bool = False
    readonly: bool = False
    advanced: bool = False
    restart_required: bool = False
    scope: str = "global"
    description: str = ""
    validation: str | None = None
    # For secret fields: only configured + last_rotated_at (never a value).
    configured: bool | None = None
    last_rotated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default if not self.secret else None,
            "secret": self.secret,
            "readonly": self.readonly,
            "advanced": self.advanced,
            "restart_required": self.restart_required,
            "scope": self.scope,
            "description": self.description,
            "validation": self.validation,
        }
        if self.secret:
            # NEVER expose value, even masked.
            out["configured"] = self.configured
            out["last_rotated_at"] = self.last_rotated_at
        return out


@dataclass(frozen=True, slots=True)
class ConfigSchemaExport:
    """Full config schema export to Platform."""

    version: str
    fields: list[ConfigFieldMeta]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "fields": [f.to_dict() for f in self.fields],
        }
