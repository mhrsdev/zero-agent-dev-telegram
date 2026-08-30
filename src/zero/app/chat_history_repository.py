"""Durable per-scope chat history for conversational messaging.

Hermes session parity (round 5): Hermes persists every gateway turn in
``state.db`` keyed by ``platform:chat_type:chat_id[:thread][:user]`` so
sessions survive restarts. Zero's Telegram path historically had no
session memory at all — each inbound message reached the planner in
isolation. This repository stores the conversational fallback transcript
per ``(platform, chat_id, topic_id)`` scope with a bounded oldest-first
read window.

It is presentation-layer memory only: never plan state, never execution
state, never a source of authorization. Content stored here is the
already-redacted text the caller provides.
"""

from __future__ import annotations

from zero.persistence.connection import Database


class ChatHistoryRepository:
    """Append/read a bounded rolling transcript per conversation scope."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def append(
        self,
        *,
        project_id: str,
        platform: str,
        chat_id: str,
        topic_id: str | None,
        role: str,
        content: str,
        created_at: str,
    ) -> None:
        """Persist one transcript turn. Role is restricted to
        user/assistant at the domain layer; the CHECK constraint is the
        durable backstop."""
        from zero.domain.ids import generate_chat_message_id

        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO chat_messages "
                "(id, project_id, platform, chat_id, topic_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generate_chat_message_id(),
                    str(project_id),
                    str(platform),
                    str(chat_id),
                    str(topic_id) if topic_id is not None else None,
                    str(role),
                    str(content),
                    str(created_at),
                ),
            )

    def recent(
        self,
        *,
        platform: str,
        chat_id: str,
        topic_id: str | None,
        limit: int = 12,
    ) -> list[dict[str, str]]:
        """Return up to ``limit`` most-recent turns, OLDEST FIRST, as
        ``{"role", "content"}`` dicts ready for the provider chain."""
        if limit < 1:
            return []
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM ("
                "  SELECT role, content, created_at, id FROM chat_messages"
                "  WHERE platform = ? AND chat_id = ? AND topic_id IS ?"
                "  ORDER BY created_at DESC, id DESC LIMIT ?"
                ") ORDER BY created_at ASC, id ASC",
                (str(platform), str(chat_id), topic_id, int(limit)),
            ).fetchall()
        return [{"role": str(row["role"]), "content": str(row["content"])} for row in rows]

    def clear(
        self,
        *,
        platform: str,
        chat_id: str,
        topic_id: str | None,
    ) -> int:
        """Forget one conversation scope (e.g. a future /reset command).
        Returns the number of deleted turns."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM chat_messages "
                "WHERE platform = ? AND chat_id = ? AND topic_id IS ?",
                (str(platform), str(chat_id), topic_id),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)


__all__ = ["ChatHistoryRepository"]
