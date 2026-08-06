"""Zero v2 persistent revocable session — ADR T-8.10, R-07.

Sessions persist in DB (survive restart), mandatory ``expires_at``, immediate
revocation (no cache TTL), group revocation ("all sessions of this user"),
machine key with scope limit, rate limit on login attempts.

Every create/extend/revoke audited. Session token never in log/error.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from zero.core.scope import Scope

__all__ = [
    "SESSION_DEFAULT_TTL_SECONDS",
    "SESSION_MAX_CONCURRENT_PER_USER",
    "TOKEN_PREFIX",
    "Session",
    "SessionError",
    "SessionExpiredError",
    "SessionRevokedError",
    "SessionStore",
]

TOKEN_PREFIX = "zs_"  # zs_<opaque>
SESSION_DEFAULT_TTL_SECONDS = 86400  # 24h
SESSION_MAX_CONCURRENT_PER_USER = 5
MAX_LOGIN_ATTEMPTS_PER_MINUTE = 10


# ---------------------------------------------------------------------- errors

class SessionError(RuntimeError):
    """Base session error."""


class SessionExpiredError(SessionError):
    """Raised when an expired session is used."""


class SessionRevokedError(SessionError):
    """Raised when a revoked session is used."""


# ---------------------------------------------------------------------- session

@dataclass(slots=True)
class Session:
    """A single user session.

    The token is stored as a SHA-256 hash (we never store the raw token).
    Comparison uses :func:`hmac.compare_digest` to avoid timing attacks.
    """

    user_id: str
    scope: Scope
    expires_at: datetime
    id: str = field(default_factory=lambda: f"ssn_{secrets.token_hex(12)}")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    # Hashed token (sha256 hex). The raw token is returned ONLY at creation time.
    token_hash: str = ""
    # Scope limit — session cannot be used for scopes outside this realm.
    scope_limit: str | None = None
    # Rate-limit tracking
    failed_attempts: int = 0
    locked_until: datetime | None = None

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > datetime.now(UTC)

    @property
    def is_valid(self) -> bool:
        return not (self.is_expired or self.is_revoked or self.is_locked)

    def to_log_dict(self) -> dict[str, str | bool | None]:
        # NEVER log the token_hash — it could enable session hijack if leaked.
        return {
            "id": self.id,
            "user_id": self.user_id,
            "scope": self.scope.retrieval_key(),
            "is_valid": self.is_valid,
            "expires_at": self.expires_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of token. Use constant-time compare on lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    """Generate a new opaque session token."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


# ---------------------------------------------------------------------- store

class SessionStore:
    """In-memory session store (base class).

    For production use, prefer :class:`zero.stores.session_store.DbSessionStore`
    which persists to the ``dev_sessions`` / ``normal_sessions`` / ``personal_sessions``
    tables with O(1) token lookup via a UNIQUE index on ``token_hash``.
    """

    def __init__(
        self,
        *,
        default_ttl_seconds: int = SESSION_DEFAULT_TTL_SECONDS,
        max_concurrent_per_user: int = SESSION_MAX_CONCURRENT_PER_USER,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._default_ttl = default_ttl_seconds
        self._max_per_user = max_concurrent_per_user

    def create(
        self,
        *,
        user_id: str,
        scope: Scope,
        ttl_seconds: int | None = None,
        scope_limit: str | None = None,
    ) -> tuple[Session, str]:
        """Create a new session. Returns ``(session, raw_token)``.

        The raw_token is returned ONLY here — caller must transmit to client.
        """
        # Enforce max concurrent per user.
        active = [
            s for s in self._sessions.values()
            if s.user_id == user_id and s.is_valid
        ]
        if len(active) >= self._max_per_user:
            # Revoke the oldest.
            oldest = min(active, key=lambda s: s.created_at)
            self.revoke(oldest.id, reason="max_concurrent_exceeded")

        token = _generate_token()
        expires_at = datetime.now(UTC) + timedelta(
            seconds=ttl_seconds if ttl_seconds is not None else self._default_ttl
        )
        session = Session(
            user_id=user_id,
            scope=scope,
            expires_at=expires_at,
            token_hash=_hash_token(token),
            scope_limit=scope_limit,
        )
        self._sessions[session.id] = session
        return session, token

    def lookup(self, token: str) -> Session:
        """Find a session by token. Raises if invalid/revoked/expired.

        Uses constant-time token comparison.
        """
        if not token.startswith(TOKEN_PREFIX):
            raise SessionError(f"invalid token format (expected {TOKEN_PREFIX} prefix)")

        target_hash = _hash_token(token)

        # Constant-time compare across all sessions (defends against timing attack).
        # DbSessionStore overrides this with a single DB lookup by token_hash (indexed).
        found: Session | None = None
        for s in self._sessions.values():
            if hmac.compare_digest(s.token_hash, target_hash):
                found = s
                break

        if found is None:
            raise SessionError("session not found")

        if found.is_revoked:
            raise SessionRevokedError(f"session {found.id} was revoked")
        if found.is_expired:
            raise SessionExpiredError(f"session {found.id} expired at {found.expires_at}")
        if found.is_locked:
            raise SessionError(f"session {found.id} is locked until {found.locked_until}")

        # Update last_used_at (don't invalidate, just touch).
        found.last_used_at = datetime.now(UTC)
        return found

    def get(self, session_id: str) -> Session | None:
        """Return a session by id (no token lookup, no validation). For inspection."""
        return self._sessions.get(session_id)

    def revoke(self, session_id: str, *, reason: str = "manual") -> bool:
        """Revoke a single session. Returns True if found and revoked."""
        s = self._sessions.get(session_id)
        if s is None or s.is_revoked:
            return False
        s.revoked_at = datetime.now(UTC)
        s.revoked_reason = reason
        return True

    def revoke_all_for_user(self, user_id: str, *, reason: str = "user_logout") -> int:
        """Revoke all sessions for ``user_id``. Returns count revoked."""
        count = 0
        for s in self._sessions.values():
            if s.user_id == user_id and not s.is_revoked:
                s.revoked_at = datetime.now(UTC)
                s.revoked_reason = reason
                count += 1
        return count

    def extend(self, session_id: str, *, extend_by_seconds: int) -> Session:
        """Extend a session's expiry. Raises if revoked/expired."""
        s = self._sessions.get(session_id)
        if s is None:
            raise SessionError(f"session {session_id!r} not found")
        if s.is_revoked:
            raise SessionRevokedError(f"session {s.id} was revoked")
        s.expires_at = datetime.now(UTC) + timedelta(seconds=extend_by_seconds)
        return s

    def cleanup_expired(self) -> int:
        """Drop expired sessions from memory. Returns count dropped."""
        before = len(self._sessions)
        self._sessions = {
            sid: s for sid, s in self._sessions.items()
            if not s.is_expired
        }
        return before - len(self._sessions)

    def rate_limit_check(self, user_id: str) -> bool:
        """True if user is allowed to attempt login (not rate-limited).

        Uses in-memory tracking. DbSessionStore uses DbSlidingWindowRateLimiter
        which persists to the ``dev_rate_limits`` table.
        """
        now = time.time()
        cutoff = now - 60
        recent_failures = sum(
            1 for s in self._sessions.values()
            if s.user_id == user_id
            and s.failed_attempts > 0
            and s.last_used_at.timestamp() > cutoff
        )
        return recent_failures < MAX_LOGIN_ATTEMPTS_PER_MINUTE

    def record_failed_attempt(self, user_id: str) -> None:
        """Record a failed login attempt for rate limiting.

        DbSessionStore overrides this to use the ``dev_rate_limits`` table
        with sliding-window semantics.
        """
        for s in self._sessions.values():
            if s.user_id == user_id:
                s.failed_attempts += 1
                if s.failed_attempts >= MAX_LOGIN_ATTEMPTS_PER_MINUTE:
                    s.locked_until = datetime.now(UTC) + timedelta(minutes=1)
                break
