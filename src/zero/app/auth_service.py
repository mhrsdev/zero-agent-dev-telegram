"""Opaque per-user access tokens for the HTTP control plane."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status

from zero.app.identity_service import IdentityService
from zero.config import Settings
from zero.domain.audit import AuditEvent, AuditEventId
from zero.domain.identity import User, UserId
from zero.domain.ids import generate_audit_event_id
from zero.persistence.connection import Database
from zero.persistence.repositories.audit_repository import AuditRepository


class AuthenticationError(RuntimeError):
    """The presented credential is missing, invalid, expired, or revoked."""


class BootstrapError(RuntimeError):
    """The one-time bootstrap boundary rejected the request."""


_current_actor: ContextVar[UserId | None] = ContextVar("zero_current_actor", default=None)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """Issue, verify, and revoke server-owned opaque access tokens."""

    def __init__(
        self,
        database: Database,
        identity: IdentityService,
        audit_repo: AuditRepository,
        settings: Settings,
    ) -> None:
        self._database = database
        self._identity = identity
        self._audit_repo = audit_repo
        self._settings = settings

    def bootstrap(
        self,
        *,
        display_name: str,
        supplied_secret: str,
        existing_user_id: UserId | None = None,
    ) -> tuple[User, str, str]:
        configured = self._settings.bootstrap_token
        expected = configured.get_secret_value() if configured else ""
        if not expected or not hmac.compare_digest(supplied_secret, expected):
            raise AuthenticationError("Invalid bootstrap credential")

        with self._database.transaction():
            conn = self._database.connect()
            if conn.execute("SELECT 1 FROM access_tokens LIMIT 1").fetchone():
                raise BootstrapError("Bootstrap has already been completed")
            user_count = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            if user_count == 0:
                if existing_user_id is not None:
                    raise BootstrapError("No existing user can be selected")
                user = self._identity.create_user(
                    display_name=display_name,
                    source="web",
                    commit=False,
                )
            else:
                if existing_user_id is None:
                    raise BootstrapError("existing_user_id is required for an upgraded database")
                user = self._identity.get_user(existing_user_id)
            token, expires_at = self._issue(user.id)
        return user, token, expires_at

    def issue_access_token(self, user_id: UserId) -> tuple[str, str]:
        user = self._identity.get_user(user_id)
        if user.status != "active":
            raise AuthenticationError("Inactive users cannot receive access tokens")
        with self._database.transaction():
            return self._issue(user.id)

    def _issue(self, user_id: UserId) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        expires_at = _iso(_now() + timedelta(hours=24))
        token_id = f"tok_{secrets.token_urlsafe(18)}"
        conn = self._database.connect()
        conn.execute(
            "INSERT INTO access_tokens (id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
            (token_id, user_id.value, _token_hash(token), expires_at),
        )
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=None,
                actor_id=user_id,
                source="web",
                operation="auth.token.issue",
                target_type="access_token",
                target_id=token_id,
                result="success",
                redacted_summary="Issued access token",
                created_at=_iso(_now()),
            ),
            commit=False,
        )
        return token, expires_at

    def authenticate(self, token: str) -> UserId:
        if not token or len(token) > 256:
            raise AuthenticationError("Invalid access token")
        conn = self._database.connect()
        row = conn.execute(
            "SELECT t.user_id FROM access_tokens AS t "
            "JOIN users AS u ON u.id = t.user_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL "
            "AND t.expires_at > ? AND u.status = 'active'",
            (_token_hash(token), _iso(_now())),
        ).fetchone()
        if row is None:
            raise AuthenticationError("Invalid access token")
        return UserId(row["user_id"])

    def revoke(self, token: str, actor_id: UserId) -> None:
        digest = _token_hash(token)
        with self._database.transaction():
            conn = self._database.connect()
            cursor = conn.execute(
                "UPDATE access_tokens SET revoked_at = ? "
                "WHERE token_hash = ? AND user_id = ? AND revoked_at IS NULL",
                (_iso(_now()), digest, actor_id.value),
            )
            if cursor.rowcount != 1:
                raise AuthenticationError("Invalid access token")
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=None,
                    actor_id=actor_id,
                    source="web",
                    operation="auth.token.revoke",
                    target_type="access_token",
                    result="success",
                    redacted_summary="Revoked access token",
                    created_at=_iso(_now()),
                ),
                commit=False,
            )


def request_actor(request: Request, claimed_id: str | None = None) -> UserId:
    """Return the authenticated actor and reject client-side impersonation."""
    authenticated = getattr(request.state, "user_id", None)
    if authenticated is not None:
        if claimed_id is not None and claimed_id != authenticated.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The claimed actor does not match the authenticated user",
            )
        return authenticated
    if claimed_id is not None:
        return UserId(claimed_id)
    # Auth-disabled test mode keeps legacy transport tests isolated.
    return UserId("zu_system")


def bind_actor(actor_id: UserId) -> Token[UserId | None]:
    return _current_actor.set(actor_id)


def reset_actor(token: Token[UserId | None]) -> None:
    _current_actor.reset(token)


def authenticated_actor(claimed_id: str | None = None) -> UserId:
    """Resolve the request principal without trusting adapter payloads."""
    actor = _current_actor.get()
    if actor is not None:
        if claimed_id is not None and claimed_id != actor.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The claimed actor does not match the authenticated user",
            )
        return actor
    if claimed_id is not None:
        return UserId(claimed_id)
    return UserId("zu_system")
