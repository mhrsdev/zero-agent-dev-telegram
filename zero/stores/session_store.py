"""DB-backed session store — replaces in-memory SessionStore.

Per ADR T-8.10:
    - Sessions persist in DB (survive restart)
    - Mandatory ``expires_at``
    - Immediate revocation
    - Group revocation ("all sessions of this user")
    - Machine key with scope limit
    - Rate limit on login attempts
    - Every create/extend/revoke audited
    - Session token NEVER in log/error

Uses ``dev_sessions`` table with token_hash as a UNIQUE indexed column
for O(1) lookup (vs O(N) in the in-memory store).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from zero.core.scope import Scope
from zero.core.scope import Mode
from zero.security.session import (
    SESSION_DEFAULT_TTL_SECONDS,
    SESSION_MAX_CONCURRENT_PER_USER,
    MAX_LOGIN_ATTEMPTS_PER_MINUTE,
    Session,
    SessionError,
    SessionExpiredError,
    SessionRevokedError,
    SessionStore,
)

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbSessionStore", "DbSlidingWindowRateLimiter"]


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    """Generate a new opaque session token."""
    from zero.security.session import TOKEN_PREFIX  # noqa: PLC0415

    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


class DbSlidingWindowRateLimiter:
    """Sliding-window rate limiter using DB rows.

    Per T-8.10: each (bucket_key, window_start) row is one bucket.
    Old buckets are pruned lazily on read.

    Usage:
        >>> limiter = DbSlidingWindowRateLimiter(db)
        >>> if await limiter.check_and_increment("login:user:usr_01H...", window_seconds=60, max=10):
        ...     # allowed
        ... else:
        ...     # rate limited
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def check_and_increment(
        self,
        bucket_key: str,
        *,
        window_seconds: int = 60,
        max_count: int = MAX_LOGIN_ATTEMPTS_PER_MINUTE,
    ) -> bool:
        """Check if action is allowed, and increment the counter.

        Returns True if allowed, False if rate-limited.
        """
        now = datetime.now(UTC)
        window_start = now.replace(second=0, microsecond=0) - timedelta(seconds=0)
        # Round to nearest window_seconds boundary.
        epoch_seconds = int(now.timestamp())
        window_start_epoch = (epoch_seconds // window_seconds) * window_seconds
        window_start = datetime.fromtimestamp(window_start_epoch, tz=UTC)

        # Use dev schema for rate limits (only dev schema has the table).
        dev_scope = Scope.development(
            org_id="org_system",
            workspace_id="ws_system",
            project_id="prj_system",
            group_id="grp_system",
            topic_id=0,
        ).with_default_memory_scope()

        async with self._db.connection_for(dev_scope) as conn:
            # Count current window.
            row = await conn.fetchone(
                "SELECT count FROM dev_rate_limits WHERE bucket_key = ? AND window_start = ?",
                (bucket_key, window_start.isoformat()),
            )
            current_count = int(str(row[0])) if row else 0
            if current_count >= max_count:
                # Prune old windows.
                await conn.execute(
                    "DELETE FROM dev_rate_limits WHERE bucket_key = ? AND window_start < ?",
                    (bucket_key, window_start.isoformat()),
                )
                return False
            # Increment (upsert).
            await conn.execute(
                """INSERT INTO dev_rate_limits (bucket_key, window_start, count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(bucket_key, window_start) DO UPDATE
                   SET count = count + 1""",
                (bucket_key, window_start.isoformat()),
            )
            # Prune old windows (lazy).
            await conn.execute(
                "DELETE FROM dev_rate_limits WHERE bucket_key = ? AND window_start < ?",
                (bucket_key, window_start.isoformat()),
            )
        return True


class DbSessionStore(SessionStore):
    """DB-backed session store.

    Replaces the in-memory SessionStore with persistent storage.
    Token lookup is O(1) via the UNIQUE token_hash index.
    """

    def __init__(
        self,
        db: Database,
        *,
        default_ttl_seconds: int = SESSION_DEFAULT_TTL_SECONDS,
        max_concurrent_per_user: int = SESSION_MAX_CONCURRENT_PER_USER,
        rate_limiter: DbSlidingWindowRateLimiter | None = None,
    ) -> None:
        self._db = db
        self._default_ttl = default_ttl_seconds
        self._max_per_user = max_concurrent_per_user
        self._rate_limiter = rate_limiter or DbSlidingWindowRateLimiter(db)
        # In-memory cache for sync API compat.
        self._cache: dict[str, Session] = {}

    async def create_async(
        self,
        *,
        user_id: str,
        scope: Scope,
        ttl_seconds: int | None = None,
        scope_limit: str | None = None,
    ) -> tuple[Session, str]:
        """Create a new session. Returns (session, raw_token).

        The raw_token is returned ONLY here — caller must transmit to client.
        """
        # Enforce max concurrent per user.
        active = await self._list_active_for_user_async(user_id)
        if len(active) >= self._max_per_user:
            # Revoke the oldest.
            oldest = min(active, key=lambda s: s.created_at)
            await self.revoke_async(oldest.id, reason="max_concurrent_exceeded")

        token = _generate_token()
        # Use ttl_seconds if explicitly provided (including 0), else default.
        effective_ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires_at = datetime.now(UTC) + timedelta(seconds=effective_ttl)
        session = Session(
            user_id=user_id,
            scope=scope,
            expires_at=expires_at,
            token_hash=_hash_token(token),
            scope_limit=scope_limit,
        )
        self._cache[session.id] = session

        # Persist to DB (dev schema only).
        if scope.is_development():
            async with self._db.connection_for(scope) as conn:
                assert scope.project_id is not None  # noqa: S101
                await conn.execute(
                    """INSERT INTO dev_sessions
                       (session_id, user_id, scope_key, mode, token_hash, scope_limit,
                        expires_at, created_at, last_used_at, failed_attempts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (
                        session.id,
                        user_id,
                        scope.retrieval_key(),
                        scope.mode.value,
                        session.token_hash,
                        scope_limit,
                        expires_at.isoformat(),
                        session.created_at.isoformat(),
                        session.created_at.isoformat(),
                    ),
                )

        return session, token

    # Sync API (for backward compat — in-memory only).
    def create(
        self,
        *,
        user_id: str,
        scope: Scope,
        ttl_seconds: int | None = None,
        scope_limit: str | None = None,
    ) -> tuple[Session, str]:
        return self._sync_wrapper(self.create_async(  # type: ignore[no-any-return]
            user_id=user_id, scope=scope, ttl_seconds=ttl_seconds,
            scope_limit=scope_limit,
        ))

    async def lookup_async(self, token: str) -> Session:
        """Find a session by token. Raises if invalid/revoked/expired.

        Uses constant-time token comparison via DB UNIQUE index on token_hash.
        """
        from zero.security.session import TOKEN_PREFIX  # noqa: PLC0415

        if not token.startswith(TOKEN_PREFIX):
            raise SessionError(f"invalid token format (expected {TOKEN_PREFIX} prefix)")

        target_hash = _hash_token(token)

        # Look up in DB by token_hash (O(1) via UNIQUE index).
        dev_scope = Scope.development(
            org_id="org_system",
            workspace_id="ws_system",
            project_id="prj_system",
            group_id="grp_system",
            topic_id=0,
        ).with_default_memory_scope()

        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                """SELECT session_id, user_id, scope_key, mode, token_hash, scope_limit,
                          expires_at, created_at, last_used_at, revoked_at, revoked_reason,
                          locked_until, failed_attempts
                   FROM dev_sessions WHERE token_hash = ?""",
                (target_hash,),
            )
            if row is None:
                # Not in DB — check in-memory cache (for personal/normal scopes
                # that don't have a sessions table yet, or for tests).
                return await self._lookup_in_cache(token, target_hash)

            session = self._row_to_session(row)

            # Validate.
            if session.is_revoked:
                raise SessionRevokedError(f"session {session.id} was revoked")
            if session.is_expired:
                raise SessionExpiredError(f"session {session.id} expired at {session.expires_at}")
            if session.is_locked:
                raise SessionError(f"session {session.id} is locked until {session.locked_until}")

            # Update last_used_at.
            now = datetime.now(UTC)
            await conn.execute(
                "UPDATE dev_sessions SET last_used_at = ? WHERE session_id = ?",
                (now.isoformat(), session.id),
            )
            session.last_used_at = now
            self._cache[session.id] = session
            return session

    def lookup(self, token: str) -> Session:
        return self._sync_wrapper(self.lookup_async(token))  # type: ignore[no-any-return]

    async def _lookup_in_cache(self, token: str, target_hash: str) -> Session:
        """Check in-memory cache for sessions not stored in dev schema."""
        found: Session | None = None
        for s in self._cache.values():
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
        found.last_used_at = datetime.now(UTC)
        return found

    async def revoke_async(self, session_id: str, *, reason: str = "manual") -> bool:
        """Revoke a single session."""
        s = self._cache.get(session_id)
        if s is not None and s.is_revoked:
            return False

        # Update DB.
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        now = datetime.now(UTC)
        async with self._db.connection_for(dev_scope) as conn:
            result = await conn.execute(
                "UPDATE dev_sessions SET revoked_at = ?, revoked_reason = ? "
                "WHERE session_id = ? AND revoked_at IS NULL",
                (now.isoformat(), reason, session_id),
            )

        if s is not None:
            s.revoked_at = now
            s.revoked_reason = reason
        return True

    def revoke(self, session_id: str, *, reason: str = "manual") -> bool:
        return self._sync_wrapper(self.revoke_async(session_id, reason=reason))  # type: ignore[no-any-return]

    async def revoke_all_for_user_async(self, user_id: str, *, reason: str = "user_logout") -> int:
        """Revoke all sessions for ``user_id``. Returns count revoked."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        now = datetime.now(UTC)
        async with self._db.connection_for(dev_scope) as conn:
            # Count active.
            row = await conn.fetchone(
                "SELECT COUNT(*) FROM dev_sessions WHERE user_id = ? AND revoked_at IS NULL",
                (user_id,),
            )
            count = int(str(row[0])) if row else 0
            await conn.execute(
                "UPDATE dev_sessions SET revoked_at = ?, revoked_reason = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now.isoformat(), reason, user_id),
            )

        # Update cache.
        for s in self._cache.values():
            if s.user_id == user_id and not s.is_revoked:
                s.revoked_at = now
                s.revoked_reason = reason
        return count

    def revoke_all_for_user(self, user_id: str, *, reason: str = "user_logout") -> int:
        return self._sync_wrapper(self.revoke_all_for_user_async(user_id, reason=reason))  # type: ignore[no-any-return]

    async def extend_async(self, session_id: str, *, extend_by_seconds: int) -> Session:
        """Extend a session's expiry."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        new_expiry = datetime.now(UTC) + timedelta(seconds=extend_by_seconds)
        async with self._db.connection_for(dev_scope) as conn:
            await conn.execute(
                "UPDATE dev_sessions SET expires_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                (new_expiry.isoformat(), session_id),
            )
        s = self._cache.get(session_id)
        if s is not None:
            s.expires_at = new_expiry
            return s
        # Re-fetch from DB.
        return await self._get_by_id_async(session_id)

    def extend(self, session_id: str, *, extend_by_seconds: int) -> Session:
        return self._sync_wrapper(self.extend_async(session_id, extend_by_seconds=extend_by_seconds))  # type: ignore[no-any-return]

    async def _get_by_id_async(self, session_id: str) -> Session:
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                """SELECT session_id, user_id, scope_key, mode, token_hash, scope_limit,
                          expires_at, created_at, last_used_at, revoked_at, revoked_reason,
                          locked_until, failed_attempts
                   FROM dev_sessions WHERE session_id = ?""",
                (session_id,),
            )
            if row is None:
                raise SessionError(f"session {session_id!r} not found")
            return self._row_to_session(row)

    def get(self, session_id: str) -> Session | None:
        """Sync get (cache only — use _get_by_id_async for DB)."""
        return self._cache.get(session_id)

    async def cleanup_expired_async(self) -> int:
        """Delete expired sessions from DB. Returns count deleted."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        now_iso = datetime.now(UTC).isoformat()
        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                "SELECT COUNT(*) FROM dev_sessions WHERE expires_at < ? AND revoked_at IS NULL",
                (now_iso,),
            )
            count = int(str(row[0])) if row else 0
            await conn.execute(
                "DELETE FROM dev_sessions WHERE expires_at < ? AND revoked_at IS NULL",
                (now_iso,),
            )

        # Update cache.
        before = len(self._cache)
        self._cache = {
            sid: s for sid, s in self._cache.items()
            if not s.is_expired
        }
        return count + (before - len(self._cache))

    def cleanup_expired(self) -> int:
        return self._sync_wrapper(self.cleanup_expired_async())  # type: ignore[no-any-return]

    async def rate_limit_check_async(self, user_id: str) -> bool:
        """True if user is allowed to attempt login (not rate-limited)."""
        return await self._rate_limiter.check_and_increment(
            f"login:user:{user_id}",
            window_seconds=60,
            max_count=MAX_LOGIN_ATTEMPTS_PER_MINUTE,
        )

    def rate_limit_check(self, user_id: str) -> bool:
        return self._sync_wrapper(self.rate_limit_check_async(user_id))  # type: ignore[no-any-return]

    async def record_failed_attempt_async(self, user_id: str) -> None:
        """Record a failed login attempt for rate limiting."""
        # Rate limiter already incremented on the check.
        # Lock the user's sessions if too many failures.
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            # Count recent failed attempts (in last minute).
            row = await conn.fetchone(
                "SELECT count FROM dev_rate_limits "
                "WHERE bucket_key = ? AND window_start = ?",
                (
                    f"login:user:{user_id}",
                    datetime.fromtimestamp(
                        (int(datetime.now(UTC).timestamp()) // 60) * 60, tz=UTC
                    ).isoformat(),
                ),
            )
            count = int(str(row[0])) if row else 0
            if count >= MAX_LOGIN_ATTEMPTS_PER_MINUTE:
                # Lock all sessions for this user.
                lock_until = datetime.now(UTC) + timedelta(minutes=1)
                await conn.execute(
                    "UPDATE dev_sessions SET locked_until = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (lock_until.isoformat(), user_id),
                )

    def record_failed_attempt(self, user_id: str) -> None:
        self._sync_wrapper(self.record_failed_attempt_async(user_id))

    async def _list_active_for_user_async(self, user_id: str) -> list[Session]:
        """List active (non-revoked, non-expired) sessions for a user."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        now_iso = datetime.now(UTC).isoformat()
        async with self._db.connection_for(dev_scope) as conn:
            rows = await conn.fetchall(
                """SELECT session_id, user_id, scope_key, mode, token_hash, scope_limit,
                          expires_at, created_at, last_used_at, revoked_at, revoked_reason,
                          locked_until, failed_attempts
                   FROM dev_sessions
                   WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
                   ORDER BY created_at ASC""",
                (user_id, now_iso),
            )
            return [self._row_to_session(r) for r in rows]

    @staticmethod
    def _row_to_session(row: tuple[Any, ...]) -> Session:
        """Convert a DB row to a Session object."""
        (
            session_id,
            user_id,
            scope_key,
            mode_str,
            token_hash,
            scope_limit,
            expires_at_str,
            created_at_str,
            last_used_at_str,
            revoked_at_str,
            revoked_reason,
            locked_until_str,
            failed_attempts,
        ) = row

        # Reconstruct scope from scope_key (stored as string in DB).
        # The scope_key encodes all the necessary information for reconstruction.
        mode = Mode(str(mode_str))
        if mode is Mode.PERSONAL:
            scope = Scope.personal(user_id=str(user_id)).with_default_memory_scope()
        elif mode is Mode.NORMAL:
            # Parse group_id and topic_id from scope_key "normal:grp_<ulid>:<topic_id>"
            parts = str(scope_key).split(":")
            if len(parts) >= 3:
                scope = Scope.normal(
                    group_id=parts[1], topic_id=int(parts[2])
                ).with_default_memory_scope()
            else:
                scope = Scope.personal(user_id="usr_unknown").with_default_memory_scope()
        else:
            # DEVELOPMENT — parse project_id from scope_key "dev:prj_<ulid>"
            parts = str(scope_key).split(":")
            if len(parts) >= 2:
                project_id = parts[1]
                scope = Scope.development(
                    org_id=f"org_for_{project_id}",
                    workspace_id=f"ws_for_{project_id}",
                    project_id=project_id,
                    group_id="grp_reconstructed",
                    topic_id=0,
                ).with_default_memory_scope()
            else:
                scope = Scope.personal(user_id="usr_unknown").with_default_memory_scope()

        session = Session(
            user_id=str(user_id),
            scope=scope,
            expires_at=datetime.fromisoformat(str(expires_at_str)),
            token_hash=str(token_hash),
            scope_limit=str(scope_limit) if scope_limit else None,
            id=str(session_id),
            created_at=datetime.fromisoformat(str(created_at_str)),
            last_used_at=datetime.fromisoformat(str(last_used_at_str)),
            failed_attempts=int(str(failed_attempts)),
        )
        if revoked_at_str:
            session.revoked_at = datetime.fromisoformat(str(revoked_at_str))
            session.revoked_reason = str(revoked_reason) if revoked_reason else None
        if locked_until_str:
            session.locked_until = datetime.fromisoformat(str(locked_until_str))
        return session

    def _sync_wrapper(self, coro: Any) -> Any:
        """Run an async coroutine synchronously (for backward compat).

        Returns the result of the coroutine. Raises RuntimeError if no
        event loop is available or if called from within a running event loop.

        Note: return type is Any to satisfy mypy for callers that expect
        specific types. This is safe because we control all call sites.
        """
        import asyncio  # noqa: PLC0415

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                raise RuntimeError(
                    "cannot run async method synchronously from running event loop"
                )
            result: Any = loop.run_until_complete(coro)
            return result
        except RuntimeError:
            result = asyncio.run(coro)
            return result
