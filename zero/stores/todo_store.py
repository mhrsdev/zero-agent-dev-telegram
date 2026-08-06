"""DB-backed TodoStore — replaces in-memory TodoTool._stores dict.

Per ADR T-3.1: tasks persist across restarts.

ENTERPRISE: Persists to DB for ALL scopes (personal/normal/dev). Each scope
mode has its own todos table:
    - personal: personal_todos
    - normal:   normal_todos
    - dev:      dev_todos

Tables already exist in the SQLite schema (sqlite_backend.py).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from zero.core.scope import Scope

if TYPE_CHECKING:
    from zero.db import Database

__all__ = ["DbTodoStore", "TodoItem"]


@dataclass(slots=True)
class TodoItem:
    """A single todo item."""

    todo_id: str
    scope_key: str
    item_text: str
    completed: bool
    created_by: str
    created_at: datetime
    completed_at: datetime | None
    position: int

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id,
            "scope_key": self.scope_key,
            "completed": self.completed,
            "position": self.position,
            "text_chars": len(self.item_text),
        }


def _table_for_scope(scope: Scope) -> str:
    """Return the todos table name for this scope."""
    if scope.is_personal():
        return "personal_todos"
    if scope.is_normal():
        return "normal_todos"
    return "dev_todos"


class DbTodoStore:
    """DB-backed todo store.

    Persists todos to the appropriate scope's todos table:
        - PERSONAL → personal_todos
        - NORMAL → normal_todos
        - DEVELOPMENT → dev_todos

    All three tables have identical schemas (todo_id, scope_key, item_text,
    completed, created_by, created_at, completed_at, position).
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_async(
        self,
        *,
        scope: Scope,
        text: str,
        created_by: str,
    ) -> TodoItem:
        """Add a todo item."""
        scope_key = scope.retrieval_key()
        position = await self._next_position_async(scope, scope_key)
        table = _table_for_scope(scope)

        item = TodoItem(
            todo_id=f"td_{uuid.uuid4().hex[:16]}",
            scope_key=scope_key,
            item_text=text,
            completed=False,
            created_by=created_by,
            created_at=datetime.now(UTC),
            completed_at=None,
            position=position,
        )

        async with self._db.connection_for(scope) as conn:
            await conn.execute(
                f"""INSERT INTO {table}
                   (todo_id, scope_key, item_text, completed, created_by, position)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (item.todo_id, scope_key, text, created_by, position),
            )
        return item

    async def list_async(self, *, scope: Scope) -> list[TodoItem]:
        """List todos for a scope (incomplete first, by position)."""
        scope_key = scope.retrieval_key()
        table = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            rows = await conn.fetchall(
                f"""SELECT todo_id, scope_key, item_text, completed, created_by,
                          created_at, completed_at, position
                   FROM {table} WHERE scope_key = ?
                   ORDER BY completed ASC, position ASC""",
                (scope_key,),
            )
            return [self._row_to_item(r) for r in rows]

    async def complete_async(self, *, scope: Scope, index: int) -> TodoItem | None:
        """Mark the todo at ``index`` (1-based) as completed."""
        items = await self.list_async(scope=scope)
        if not (0 <= index - 1 < len(items)):
            return None
        item = items[index - 1]
        item.completed = True
        item.completed_at = datetime.now(UTC)
        table = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            await conn.execute(
                f"""UPDATE {table} SET completed = 1, completed_at = ?
                   WHERE todo_id = ?""",
                (item.completed_at.isoformat(), item.todo_id),
            )
        return item

    async def remove_async(self, *, scope: Scope, index: int) -> TodoItem | None:
        """Remove the todo at ``index`` (1-based)."""
        items = await self.list_async(scope=scope)
        if not (0 <= index - 1 < len(items)):
            return None
        item = items[index - 1]
        table = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            await conn.execute(
                f"DELETE FROM {table} WHERE todo_id = ?",
                (item.todo_id,),
            )
        return item

    async def _next_position_async(self, scope: Scope, scope_key: str) -> int:
        """Get the next position for a new todo."""
        table = _table_for_scope(scope)
        async with self._db.connection_for(scope) as conn:
            row = await conn.fetchone(
                f"SELECT COALESCE(MAX(position), -1) + 1 FROM {table} WHERE scope_key = ?",
                (scope_key,),
            )
            return int(str(row[0])) if row else 0

    @staticmethod
    def _row_to_item(row: tuple[Any, ...]) -> TodoItem:
        """Convert a DB row to TodoItem."""
        (
            todo_id,
            scope_key,
            item_text,
            completed_int,
            created_by,
            created_at_str,
            completed_at_str,
            position,
        ) = row
        return TodoItem(
            todo_id=todo_id,
            scope_key=scope_key,
            item_text=item_text,
            completed=bool(completed_int),
            created_by=created_by,
            created_at=datetime.fromisoformat(created_at_str),
            completed_at=datetime.fromisoformat(completed_at_str) if completed_at_str else None,
            position=int(position),
        )
