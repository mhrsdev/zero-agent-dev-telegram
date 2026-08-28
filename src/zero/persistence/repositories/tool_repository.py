"""Tool registry and capability grant repository.

Per ``zero-tool-capability-runtime`` SKILL.md §"Registry metadata and
runtime capability differ": the registry describes what a tool can do;
a capability grant describes who may invoke one bounded part of it in
one context.
"""

from __future__ import annotations

import json
import sqlite3

from zero.domain.identity import ProjectId
from zero.domain.tools import (
    AgentScope,
    Tool,
    ToolAlreadyExistsError,
    ToolGrant,
    ToolGrantId,
    ToolGrantNotFoundError,
    ToolId,
    ToolNotFoundError,
)
from zero.persistence.connection import Database


def _row_to_tool(row: sqlite3.Row | tuple) -> Tool:
    return Tool(
        id=ToolId(row["id"]),
        name=row["name"],
        description=row["description"],
        input_schema=json.loads(row["input_schema"]),
        output_schema=json.loads(row["output_schema"]),
        handler_key=row["handler_key"],
    )


def _row_to_tool_grant(row: sqlite3.Row | tuple) -> ToolGrant:
    return ToolGrant(
        id=ToolGrantId(row["id"]),
        project_id=ProjectId(row["project_id"]),
        tool_id=ToolId(row["tool_id"]),
        agent_scope=row["agent_scope"],  # type: ignore[arg-type]
        max_invocations=row["max_invocations"],
        timeout_seconds=row["timeout_seconds"],
    )


class ToolRepository:
    """Database-backed tool registry and grant repository."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def insert_tool(self, tool: Tool, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO tools "
                "(id, name, description, input_schema, output_schema, handler_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tool.id.value,
                    tool.name,
                    tool.description,
                    json.dumps(tool.input_schema),
                    json.dumps(tool.output_schema),
                    tool.handler_key,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc) and "tools.name" in str(exc):
                raise ToolAlreadyExistsError(f"Tool {tool.name!r} is already registered") from exc
            raise

    def get_tool_by_id(self, tool_id: ToolId) -> Tool:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, name, description, input_schema, output_schema, handler_key "
            "FROM tools WHERE id = ?",
            (tool_id.value,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ToolNotFoundError(f"Tool {tool_id} not found")
        return _row_to_tool(row)

    def update_tool_declaration(
        self,
        tool_id: ToolId,
        *,
        description: str,
        input_schema: dict,
        output_schema: dict,
        commit: bool = True,
    ) -> None:
        """Refresh a persistent tool's model-facing declaration.

        Real-run fix (2026-08-28): tool rows persist across process
        restarts while handlers are re-bound in code. When a trusted
        server-owned tool's declared schema evolves (e.g. run_command
        gaining stdout/stderr result fields), the persisted row kept the
        STALE schema and every invocation failed output validation
        against a contract the handler no longer satisfies. The
        declaration is server-owned metadata, so refreshing it on re-bind
        is safe and keeps the rows in lockstep with the code.
        """
        conn = self._database.connect()
        try:
            conn.execute(
                "UPDATE tools SET description = ?, input_schema = ?, output_schema = ? "
                "WHERE id = ?",
                (
                    description,
                    json.dumps(input_schema),
                    json.dumps(output_schema),
                    tool_id.value,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.Error:
            if commit:
                conn.rollback()
            raise

    def get_tool_by_name(self, name: str) -> Tool:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, name, description, input_schema, output_schema, handler_key "
            "FROM tools WHERE name = ?",
            (name,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ToolNotFoundError(f"Tool {name!r} not found")
        return _row_to_tool(row)

    def list_tools(self) -> list[Tool]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, name, description, input_schema, output_schema, handler_key "
            "FROM tools ORDER BY name"
        )
        return [_row_to_tool(row) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Tool grants
    # ------------------------------------------------------------------

    def insert_grant(self, grant: ToolGrant, *, commit: bool = True) -> None:
        conn = self._database.connect()
        try:
            conn.execute(
                "INSERT INTO tool_grants "
                "(id, project_id, tool_id, agent_scope, max_invocations, timeout_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    grant.id.value,
                    grant.project_id.value,
                    grant.tool_id.value,
                    grant.agent_scope,
                    grant.max_invocations,
                    grant.timeout_seconds,
                ),
            )
            if commit:
                conn.commit()
        except sqlite3.IntegrityError as exc:
            if commit:
                conn.rollback()
            if "UNIQUE" in str(exc):
                # The grant already exists; treat as idempotent success
                # because grants are uniquely identified by
                # (project_id, tool_id, agent_scope).
                return
            raise

    def get_grant(
        self,
        project_id: ProjectId,
        tool_id: ToolId,
        agent_scope: AgentScope,
    ) -> ToolGrant:
        """Return the grant for a project + tool + scope.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the query filters by ``project_id`` before any row is
        loaded.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, tool_id, agent_scope, max_invocations, "
            "timeout_seconds, created_at FROM tool_grants "
            "WHERE project_id = ? AND tool_id = ? AND agent_scope = ?",
            (project_id.value, tool_id.value, agent_scope),
        )
        row = cursor.fetchone()
        if row is None:
            raise ToolGrantNotFoundError(
                f"No grant for tool {tool_id} in scope {agent_scope} in project {project_id}"
            )
        return _row_to_tool_grant(row)

    def list_grants_for_project(self, project_id: ProjectId) -> list[ToolGrant]:
        conn = self._database.connect()
        cursor = conn.execute(
            "SELECT id, project_id, tool_id, agent_scope, max_invocations, "
            "timeout_seconds, created_at FROM tool_grants "
            "WHERE project_id = ? ORDER BY created_at",
            (project_id.value,),
        )
        return [_row_to_tool_grant(row) for row in cursor.fetchall()]

    def delete_grant(
        self,
        project_id: ProjectId,
        tool_id: ToolId,
        agent_scope: AgentScope,
        *,
        commit: bool = True,
    ) -> None:
        """Revoke a tool grant.

        Per ``zero-tool-capability-runtime`` §"Tool choice and tool
        permission are separate": revoking a grant takes effect
        immediately through all implemented interfaces.
        """
        conn = self._database.connect()
        cursor = conn.execute(
            "DELETE FROM tool_grants WHERE project_id = ? AND tool_id = ? AND agent_scope = ?",
            (project_id.value, tool_id.value, agent_scope),
        )
        if cursor.rowcount == 0:
            raise ToolGrantNotFoundError(
                f"No grant for tool {tool_id} in scope {agent_scope} in project {project_id}"
            )
        if commit:
            conn.commit()

    def reserve_invocation(self, grant_id: ToolGrantId) -> bool:
        """Atomically consume one invocation from a grant's cap."""
        conn = self._database.connect()
        cursor = conn.execute(
            "UPDATE tool_grants SET invocation_count = invocation_count + 1 "
            "WHERE id = ? AND (max_invocations IS NULL "
            "OR invocation_count < max_invocations)",
            (grant_id.value,),
        )
        conn.commit()
        return cursor.rowcount == 1
