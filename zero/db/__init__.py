"""Zero v2 database layer — ADR 0003 §6 (three-schema isolation).

In PostgreSQL: three schemas (``personal``, ``normal``, ``dev``) with three
roles (``personal_role``, ``normal_role``, ``dev_role``). Code connecting for
Personal has no SELECT on ``dev.*`` or ``normal.*`` — the database itself
rejects wrong queries.

For SQLite (simple install), the equivalent guarantee is implemented via
**three separate DB files** (``personal.db``, ``normal.db``, ``dev.db``).
The application holds three separate connections and never crosses them —
enforced by a structural test that greps for any function that opens two
connections in one call.

This module exposes a unified :class:`Database` façade that dispatches to the
correct backend based on :class:`Mode`. There is no path through the public
API that lets a Personal-scope query touch a dev table.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from zero.core.scope import Mode, Scope

__all__ = [
    "Connection",
    "CrossSchemaAccessError",
    "Database",
    "DatabaseBackend",
    "DatabaseError",
    "SchemaName",
]


# ---------------------------------------------------------------------- types

SchemaName = Literal["personal", "normal", "dev"]
"""The three PostgreSQL schemas / SQLite files."""


class DatabaseError(RuntimeError):
    """Base class for DB-layer errors."""


class CrossSchemaAccessError(DatabaseError):
    """Raised when a query attempts to cross schema boundaries."""


# ---------------------------------------------------------------------- connection protocol

class Connection(Protocol):
    """Async DB connection protocol (PEP 249-async style)."""

    async def execute(self, sql: str, params: tuple[object, ...] = ()) -> object: ...
    async def executemany(self, sql: str, params: list[tuple[object, ...]]) -> None: ...
    async def fetchone(self, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None: ...
    async def fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------- backend protocol

class DatabaseBackend(Protocol):
    """Protocol every DB backend (Postgres / SQLite) must implement."""

    async def connect(self, schema: SchemaName) -> Connection: ...
    async def disconnect(self) -> None: ...
    async def ping(self, schema: SchemaName) -> bool: ...
    async def migrate(self, schema: SchemaName, target_version: int | None = None) -> int: ...
    async def schema_version(self, schema: SchemaName) -> int: ...


# ---------------------------------------------------------------------- mapping Scope -> Schema

def scope_to_schema(scope: Scope) -> SchemaName:
    """Map a :class:`Scope` to its target DB schema.

    This is the single source of truth. **No other code path may choose a
    schema** — a structural test (T-1.4 acceptance) greps for any direct
    use of the strings "personal"/"normal"/"dev" outside this function and
    outside the schema bootstrap code.
    """
    if scope.mode is Mode.PERSONAL:
        return "personal"
    if scope.mode is Mode.NORMAL:
        return "normal"
    if scope.mode is Mode.DEVELOPMENT:
        return "dev"
    raise CrossSchemaAccessError(f"unknown mode {scope.mode!r}")


# ---------------------------------------------------------------------- Database façade

@dataclass(slots=True)
class Database:
    """Façade that dispatches to per-schema connections based on Scope.

    Construction:
        >>> from zero.db.sqlite_backend import SqliteBackend
        >>> backend = SqliteBackend(sqlite_dir=Path("~/.zero/db"))
        >>> db = Database(backend=backend)
        >>> await db.start()
        >>> async with db.connection_for(personal_scope) as conn:
        ...     await conn.execute("SELECT 1")

    The ``connection_for`` method is the **only** sanctioned way to obtain
    a connection. There is no ``connection(schema="dev")`` shortcut that
    would let a developer accidentally bypass Scope validation.
    """

    backend: DatabaseBackend
    _started: bool = False

    async def start(self) -> None:
        """Initialize all three schemas, run migrations, create roles/tables."""
        if self._started:
            return
        for schema in ("personal", "normal", "dev"):
            await self.backend.migrate(schema)
        self._started = True

    async def stop(self) -> None:
        await self.backend.disconnect()
        self._started = False

    @asynccontextmanager
    async def connection_for(self, scope: Scope) -> AsyncIterator[Connection]:
        """Yield a connection bound to ``scope``'s schema.

        Raises :class:`CrossSchemaAccessError` if scope is invalid.
        """
        if not self._started:
            raise DatabaseError("Database.start() must be called before connection_for()")
        schema = scope_to_schema(scope)
        conn = await self.backend.connect(schema)
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await conn.close()

    async def ping(self, scope: SchemaName | Scope) -> bool:
        """Health-check a single schema. Accepts Scope or schema name."""
        schema: SchemaName = scope if isinstance(scope, str) else scope_to_schema(scope)
        return await self.backend.ping(schema)

    async def schema_version(self, scope: SchemaName | Scope) -> int:
        schema: SchemaName = scope if isinstance(scope, str) else scope_to_schema(scope)
        return await self.backend.schema_version(schema)

    async def migrate_all(self, target_version: int | None = None) -> dict[SchemaName, int]:
        """Run migrations on all three schemas; return their final versions."""
        result: dict[SchemaName, int] = {}
        for schema in ("personal", "normal", "dev"):
            result[schema] = await self.backend.migrate(schema, target_version)
        return result
