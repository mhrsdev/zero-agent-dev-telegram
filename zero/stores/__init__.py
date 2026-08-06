"""Zero v2 persistent stores — DB-backed implementations.

Each store:
    - Uses the three-schema DB isolation (per ADR 0003)
    - Survives process restart
    - Uses parameterized queries (SQL-injection safe)
    - Uses constant-time comparison where applicable (sessions)
    - Enforces constraints at DB level (CHECK constraints)
"""
from __future__ import annotations

from zero.stores.approval_store import DbApprovalStore
from zero.stores.session_store import DbSessionStore
from zero.stores.role_store import DbRoleStore, RoleBinding
from zero.stores.todo_store import DbTodoStore, TodoItem
from zero.stores.rate_limiter import DbRateLimiter
from zero.stores.conversation_store import (
    ConversationMessage,
    ConversationSession,
    DbConversationStore,
)

__all__ = [
    "DbApprovalStore",
    "DbSessionStore",
    "DbRoleStore",
    "RoleBinding",
    "DbTodoStore",
    "TodoItem",
    "DbRateLimiter",
    "DbConversationStore",
    "ConversationSession",
    "ConversationMessage",
]
