"""DB-backed rate limiter — standalone (used by session store + general rate limiting).

Sliding-window counter using a single DB table. Each (bucket_key, window_start)
row is one time bucket. Old buckets pruned lazily.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from zero.core.scope import Scope

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbRateLimiter"]


class DbRateLimiter:
    """Sliding-window rate limiter backed by ``dev_rate_limits`` table.

    Usage:
        >>> limiter = DbRateLimiter(db)
        >>> if await limiter.check_and_increment("api_call:user:usr_01H...", max_count=100, window_seconds=60):
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
        max_count: int = 100,
        window_seconds: int = 60,
    ) -> bool:
        """Check if action is allowed and increment counter.

        Returns True if allowed (under limit), False if rate-limited.
        """
        now = datetime.now(UTC)
        # Round to window boundary.
        epoch_seconds = int(now.timestamp())
        window_start_epoch = (epoch_seconds // window_seconds) * window_seconds
        window_start = datetime.fromtimestamp(window_start_epoch, tz=UTC)
        window_start_iso = window_start.isoformat()

        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()

        async with self._db.connection_for(dev_scope) as conn:
            # Count current window.
            row = await conn.fetchone(
                "SELECT count FROM dev_rate_limits WHERE bucket_key = ? AND window_start = ?",
                (bucket_key, window_start_iso),
            )
            current_count = int(str(row[0])) if row else 0
            if current_count >= max_count:
                # Prune old windows.
                await conn.execute(
                    "DELETE FROM dev_rate_limits WHERE bucket_key = ? AND window_start < ?",
                    (bucket_key, window_start_iso),
                )
                return False

            # Increment (upsert).
            await conn.execute(
                """INSERT INTO dev_rate_limits (bucket_key, window_start, count)
                   VALUES (?, ?, 1)
                   ON CONFLICT(bucket_key, window_start) DO UPDATE
                   SET count = count + 1""",
                (bucket_key, window_start_iso),
            )

            # Prune old windows (lazy GC).
            await conn.execute(
                "DELETE FROM dev_rate_limits WHERE bucket_key = ? AND window_start < ?",
                (bucket_key, window_start_iso),
            )
        return True

    async def get_current_count(
        self,
        bucket_key: str,
        *,
        window_seconds: int = 60,
    ) -> int:
        """Get current count in the active window (without incrementing)."""
        now = datetime.now(UTC)
        epoch_seconds = int(now.timestamp())
        window_start_epoch = (epoch_seconds // window_seconds) * window_seconds
        window_start = datetime.fromtimestamp(window_start_epoch, tz=UTC)

        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()

        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                "SELECT count FROM dev_rate_limits WHERE bucket_key = ? AND window_start = ?",
                (bucket_key, window_start.isoformat()),
            )
            return int(str(row[0])) if row else 0

    async def reset(self, bucket_key: str) -> int:
        """Reset all counters for a bucket. Returns count of rows deleted."""
        dev_scope = Scope.development(
            org_id="org_system", workspace_id="ws_system",
            project_id="prj_system", group_id="grp_system", topic_id=0,
        ).with_default_memory_scope()
        async with self._db.connection_for(dev_scope) as conn:
            row = await conn.fetchone(
                "SELECT COUNT(*) FROM dev_rate_limits WHERE bucket_key = ?",
                (bucket_key,),
            )
            count = int(str(row[0])) if row else 0
            await conn.execute(
                "DELETE FROM dev_rate_limits WHERE bucket_key = ?",
                (bucket_key,),
            )
            return count
