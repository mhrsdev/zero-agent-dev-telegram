"""Zero v2 error model — ADR T-1.6.

Stable error codes; internal details never leak to user-facing messages.
Every domain error inherits from :class:`ZeroError` and carries:

    - ``code`` — stable string, safe to log and display
    - ``message`` — user-safe message
    - ``internal`` — optional internal detail (only in logs, never user-facing)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ApprovalError",
    "ConfigError",
    "ConflictError",
    "DatabaseError",
    "ErrorCode",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "RouterError",
    "SandboxError",
    "SecretError",
    "ValidationError",
    "ZeroError",
    "serialize_error",
]


class ErrorCode(StrEnum):
    """Stable error codes — never renumber, only append."""

    # 1xxx — config / startup
    CONFIG_INVALID = "E1001_CONFIG_INVALID"
    SECRET_NOT_FOUND = "E1002_SECRET_NOT_FOUND"
    SECRET_PERMISSION = "E1003_SECRET_PERMISSION"

    # 2xxx — database
    DB_ERROR = "E2001_DB_ERROR"
    DB_CROSS_SCHEMA = "E2002_DB_CROSS_SCHEMA"
    DB_MIGRATION_FAILED = "E2003_DB_MIGRATION_FAILED"

    # 3xxx — permissions / auth
    PERMISSION_DENIED = "E3001_PERMISSION_DENIED"
    AUTH_REQUIRED = "E3002_AUTH_REQUIRED"
    SESSION_EXPIRED = "E3003_SESSION_EXPIRED"

    # 4xxx — domain
    NOT_FOUND = "E4001_NOT_FOUND"
    VALIDATION_FAILED = "E4002_VALIDATION_FAILED"
    CONFLICT = "E4003_CONFLICT"
    RATE_LIMITED = "E4004_RATE_LIMITED"

    # 5xxx — external integrations
    ROUTER_ERROR = "E5001_ROUTER_ERROR"
    ROUTER_TIMEOUT = "E5002_ROUTER_TIMEOUT"
    TELEGRAM_ERROR = "E5003_TELEGRAM_ERROR"
    GITHUB_ERROR = "E5004_GITHUB_ERROR"

    # 6xxx — agent / sandbox
    APPROVAL_REQUIRED = "E6001_APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "E6002_APPROVAL_EXPIRED"
    APPROVAL_DENIED = "E6003_APPROVAL_DENIED"
    BUDGET_EXCEEDED = "E6004_BUDGET_EXCEEDED"
    SANDBOX_ERROR = "E6005_SANDBOX_ERROR"
    AGENT_RUN_FAILED = "E6006_AGENT_RUN_FAILED"

    # 9xxx — internal
    INTERNAL = "E9999_INTERNAL"


@dataclass
class ZeroError(Exception):
    """Base class for all Zero domain errors.

    Attributes:
        code: stable :class:`ErrorCode` (never changes between releases)
        message: user-safe message (may be shown to end users)
        internal: internal detail for logs only (never user-facing)
        context: arbitrary structured data for logs/audit
    """

    code: ErrorCode = ErrorCode.INTERNAL
    message: str = "An internal error occurred"
    internal: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Make Exception happy with our dataclass fields.
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"

    def to_log_dict(self) -> dict[str, Any]:
        """Full detail for structured logs. Includes internal."""
        return {
            "code": self.code.value,
            "message": self.message,
            "internal": self.internal,
            "context": self.context,
        }

    def to_user_dict(self) -> dict[str, Any]:
        """Stripped detail for user-facing responses. No internal info."""
        return {"code": self.code.value, "message": self.message}


# ---------------------------------------------------------------------- subclasses

class ConfigError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.CONFIG_INVALID, message=message, internal=internal)


class DatabaseError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.DB_ERROR, message=message, internal=internal)


class PermissionDeniedError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED, message=message, internal=internal
        )


class NotFoundError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.NOT_FOUND, message=message, internal=internal)


class ValidationError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.VALIDATION_FAILED, message=message, internal=internal)


class ConflictError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.CONFLICT, message=message, internal=internal)


class RateLimitError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.RATE_LIMITED, message=message, internal=internal)


class RouterError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.ROUTER_ERROR, message=message, internal=internal)


class SecretError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.SECRET_NOT_FOUND, message=message, internal=internal)


class ApprovalError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.APPROVAL_REQUIRED, message=message, internal=internal)


class SandboxError(ZeroError):
    def __init__(self, message: str, *, internal: str | None = None) -> None:
        super().__init__(code=ErrorCode.SANDBOX_ERROR, message=message, internal=internal)


def serialize_error(err: ZeroError | Exception) -> dict[str, Any]:
    """Convert any exception to a user-safe dict + log dict pair."""
    if isinstance(err, ZeroError):
        return err.to_user_dict()
    return {
        "code": ErrorCode.INTERNAL.value,
        "message": "An internal error occurred",
    }
