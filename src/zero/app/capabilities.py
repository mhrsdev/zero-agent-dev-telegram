"""Deployment capability declaration.

Per the release audit (Phase 0, "Make deployment truthful"): configuration
acceptance and runtime capability must agree. This module is the single
source of truth for what this deployment can actually do, and why any
capability is unavailable. The health boundary exposes it so operators
never have to infer capability from stack traces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zero.config import SUPPORTED_DATABASE_SCHEME, Settings

CAPABILITY_AVAILABLE = "available"
CAPABILITY_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Capability:
    """One named deployment capability with an actionable reason."""

    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


def database_backend_capability(settings: Settings) -> Capability:
    scheme = settings.database_url.split(":", 1)[0].strip().lower()
    if scheme == SUPPORTED_DATABASE_SCHEME:
        return Capability(
            name="database_backend",
            status=CAPABILITY_AVAILABLE,
            detail=f"sqlite ({'in-memory' if settings.is_in_memory_db else 'file'})",
        )
    return Capability(
        name="database_backend",
        status=CAPABILITY_UNAVAILABLE,
        detail=f"scheme {scheme!r} is not implemented by this release",
    )


def worktree_execution_capability(settings: Settings) -> Capability:
    """Worktree command execution availability (GAP 3 aware).

    Production refusal dominates: without a genuine sandbox backend no
    command execution is reported available, matching the config-layer
    fail-closed rule. The reported detail names the active backend so
    the capability report never overstates isolation.
    """
    if settings.is_production:
        if settings.sandbox_executor in {"docker", "firejail"}:
            return Capability(
                name="worktree_execution",
                status=CAPABILITY_AVAILABLE,
                detail=(
                    f"sandbox={settings.sandbox_executor} (image={settings.sandbox_image})"
                    if settings.sandbox_executor == "docker"
                    else f"sandbox={settings.sandbox_executor}"
                ),
            )
        return Capability(
            name="worktree_execution",
            status=CAPABILITY_UNAVAILABLE,
            detail=(
                "no production isolation backend is configured; set "
                "ZERO_SANDBOX_EXECUTOR=docker|firejail to enable sandboxed commands"
            ),
        )
    if settings.worktree_isolation_mode == "disabled":
        return Capability(
            name="worktree_execution",
            status=CAPABILITY_UNAVAILABLE,
            detail="worktree isolation mode is disabled by configuration",
        )
    if settings.sandbox_executor in {"docker", "firejail"}:
        return Capability(
            name="worktree_execution",
            status=CAPABILITY_AVAILABLE,
            detail=f"sandbox={settings.sandbox_executor}",
        )
    return Capability(
        name="worktree_execution",
        status=CAPABILITY_AVAILABLE,
        detail="host_bounded (test/development isolation mode)",
    )


def planner_provider_capability(settings: Settings) -> Capability:
    if settings.openai_api_key is not None:
        return Capability(
            name="planner_provider",
            status=CAPABILITY_AVAILABLE,
            detail=f"openai-compatible:{settings.openai_model}",
        )
    return Capability(
        name="planner_provider",
        status=CAPABILITY_UNAVAILABLE,
        detail="no provider adapter is configured (set ZERO_OPENAI_API_KEY)",
    )


def compute_capabilities(settings: Settings) -> dict[str, dict[str, str]]:
    """Compute the deployment capability report."""
    capabilities = [
        database_backend_capability(settings),
        worktree_execution_capability(settings),
        planner_provider_capability(settings),
        Capability(
            name="managed_workers",
            status=(
                CAPABILITY_AVAILABLE
                if settings.workers_enabled and not settings.is_test
                else CAPABILITY_UNAVAILABLE
            ),
            detail=(
                "scheduler/delivery/polling workers hosted in-process"
                if settings.workers_enabled and not settings.is_test
                else f"workers disabled for environment {settings.zero_env!r}"
            ),
        ),
    ]
    return {capability.name: capability.to_dict() for capability in capabilities}


def capabilities_payload(settings: Settings) -> dict[str, Any]:
    return {"environment": settings.zero_env, "capabilities": compute_capabilities(settings)}


__all__ = [
    "CAPABILITY_AVAILABLE",
    "CAPABILITY_UNAVAILABLE",
    "Capability",
    "capabilities_payload",
    "compute_capabilities",
]
