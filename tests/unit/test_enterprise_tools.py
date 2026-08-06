"""Tests for enterprise builtin tools — PatchFile, SearchFiles, BashExec, etc."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from zero.core.scope import Scope
from zero.tools.base import ToolContext, ToolError
from zero.tools.builtin_tools import (
    BashExecTool,
    ClarifyTool,
    GitStatusTool,
    ListFilesTool,
    MemorySearchTool,
    PatchFileTool,
    ReadFileTool,
    SearchFilesTool,
    TodoTool,
    WebFetchTool,
    WriteFileTool,
    set_memory_store,
    set_todo_store,
    submit_clarification,
)
from zero.tools.patch_parser import parse_patch, apply_patch


@pytest.fixture
def dev_scope() -> Scope:
    return Scope.development(
        org_id="org_01HABC", workspace_id="ws_01HABC",
        project_id="prj_01HABC", group_id="grp_01HABC", topic_id=100,
    ).with_default_memory_scope()


@pytest.fixture
def ctx(dev_scope: Scope) -> ToolContext:
    return ToolContext(
        scope=dev_scope,
        actor_id="usr_01HALICE",
        tool_call_id="tc_test_01",
    )


# ---------------------------------------------------------------------- ReadFileTool

class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_with_offset(
        self, ctx: ToolContext, tmp_path: Path
    ) -> None:
        tool = ReadFileTool()
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = await tool.execute({"path": str(f), "offset": 2, "max_lines": 2}, ctx)
        assert "line2" in result
        assert "line3" in result
        assert "line4" not in result

    @pytest.mark.asyncio
    async def test_read_truncation(self, ctx: ToolContext, tmp_path: Path) -> None:
        tool = ReadFileTool()
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(100)))
        result = await tool.execute({"path": str(f), "max_lines": 10}, ctx)
        assert "line0" in result
        assert "line9" in result
        assert "truncated" in result.lower()
        assert "line50" not in result


# ---------------------------------------------------------------------- WriteFileTool

class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_and_append(self, ctx: ToolContext, tmp_path: Path) -> None:
        tool = WriteFileTool()
        f = tmp_path / "out.txt"
        await tool.execute({"path": str(f), "content": "hello\n"}, ctx)
        assert f.read_text() == "hello\n"
        # Append.
        await tool.execute({"path": str(f), "content": "world\n", "append": True}, ctx)
        assert f.read_text() == "hello\nworld\n"


# ---------------------------------------------------------------------- PatchFileTool

class TestPatchFileTool:
    @pytest.mark.asyncio
    async def test_apply_update_patch(self, ctx: ToolContext, tmp_path: Path) -> None:
        # Create original file.
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    return 'world'\n")

        tool = PatchFileTool()
        patch = f"""*** Begin Patch
*** Update File: {f}
@@ def hello():
 def hello():
-    return 'world'
+    return 'universe'
*** End Patch"""
        result = await tool.execute({"patch": patch}, ctx)
        assert "updated" in result.lower()
        assert "return 'universe'" in f.read_text()

    @pytest.mark.asyncio
    async def test_apply_add_file_patch(self, ctx: ToolContext, tmp_path: Path) -> None:
        f = tmp_path / "new.py"
        tool = PatchFileTool()
        patch = f"""*** Begin Patch
*** Add File: {f}
+print('hello world')
+print('second line')
*** End Patch"""
        result = await tool.execute({"patch": patch}, ctx)
        assert "added" in result.lower()
        assert f.exists()
        assert "hello world" in f.read_text()


# ---------------------------------------------------------------------- SearchFilesTool

class TestSearchFilesTool:
    @pytest.mark.asyncio
    async def test_search_finds_matches(self, ctx: ToolContext, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def hello():\n    pass\n")
        (tmp_path / "b.py").write_text("def world():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            {"path": str(tmp_path), "pattern": "hello"}, ctx,
        )
        assert "a.py" in result
        assert "def hello" in result

    @pytest.mark.asyncio
    async def test_search_no_matches(self, ctx: ToolContext, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("nothing here\n")
        tool = SearchFilesTool()
        result = await tool.execute(
            {"path": str(tmp_path), "pattern": "missing"}, ctx,
        )
        assert "no matches" in result.lower()


# ---------------------------------------------------------------------- BashExecTool

class TestBashExecTool:
    @pytest.mark.asyncio
    async def test_exec_echo(self, ctx: ToolContext) -> None:
        tool = BashExecTool()
        result = await tool.execute({"command": "echo hello world"}, ctx)
        assert "exit_code=0" in result
        assert "hello world" in result

    @pytest.mark.asyncio
    async def test_exec_failure(self, ctx: ToolContext) -> None:
        tool = BashExecTool()
        result = await tool.execute({"command": "exit 42"}, ctx)
        assert "exit_code=42" in result

    @pytest.mark.asyncio
    async def test_exec_timeout(self, ctx: ToolContext) -> None:
        tool = BashExecTool()
        result = await tool.execute(
            {"command": "sleep 10", "timeout_seconds": 1}, ctx,
        )
        assert "timed out" in result.lower()


# ---------------------------------------------------------------------- ListFilesTool

class TestListFilesTool:
    @pytest.mark.asyncio
    async def test_list_recursive(self, ctx: ToolContext, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("y")
        tool = ListFilesTool()
        result = await tool.execute(
            {"path": str(tmp_path), "recursive": True}, ctx,
        )
        assert "a.py" in result
        assert "b.py" in result


# ---------------------------------------------------------------------- ClarifyTool

class TestClarifyTool:
    @pytest.mark.asyncio
    async def test_clarify_with_response(self, ctx: ToolContext) -> None:
        """ClarifyTool returns user response when submit_clarification is called."""
        tool = ClarifyTool()
        # Start the clarify call in a task.
        task = asyncio.create_task(tool.execute(
            {"question": "Pick one", "choices": ["A", "B"], "timeout_seconds": 5},
            ctx,
        ))
        # Wait a bit for the future to be registered.
        await asyncio.sleep(0.05)
        # Find the pending clarify ID and submit a response.
        from zero.tools.builtin_tools.clarify import _pending_clarify
        assert len(_pending_clarify) == 1
        clarify_id = next(iter(_pending_clarify))
        submit_clarification(clarify_id, "A")
        result = await task
        assert result == "A"

    @pytest.mark.asyncio
    async def test_clarify_timeout(self, ctx: ToolContext) -> None:
        tool = ClarifyTool()
        result = await tool.execute(
            {"question": "Pick one", "choices": ["A"], "timeout_seconds": 1},
            ctx,
        )
        assert "timeout" in result.lower()


# ---------------------------------------------------------------------- GitStatusTool

class TestGitStatusTool:
    @pytest.mark.asyncio
    async def test_git_status_in_repo(self, ctx: ToolContext, tmp_path: Path) -> None:
        import subprocess
        # Init a git repo.
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
        (tmp_path / "file.txt").write_text("content")
        tool = GitStatusTool()
        result = await tool.execute({"cwd": str(tmp_path)}, ctx)
        # Should show the new file as untracked.
        assert "file.txt" in result or "clean" in result.lower()


# ---------------------------------------------------------------------- MemorySearchTool

class TestMemorySearchTool:
    @pytest.mark.asyncio
    async def test_search_memory(self, ctx: ToolContext) -> None:
        from zero.memory.entry import MemoryEntry, MemoryKind, MemorySource
        from zero.memory.store import MemoryStore

        store = MemoryStore()
        store.store(MemoryEntry(
            scope=ctx.scope, kind=MemoryKind.SEMANTIC,
            content="Python is great", source=MemorySource(type="t", ref="1"),
            created_by="usr_test",
        ))
        set_memory_store(store)
        tool = MemorySearchTool()
        result = await tool.execute({"query": "Python"}, ctx)
        assert "Python is great" in result

    @pytest.mark.asyncio
    async def test_search_no_memory_store(self, ctx: ToolContext) -> None:
        set_memory_store(None)  # type: ignore[arg-type]
        tool = MemorySearchTool()
        result = await tool.execute({"query": "test"}, ctx)
        assert "not initialized" in result.lower()


# ---------------------------------------------------------------------- TodoTool (DB-backed)

class TestTodoToolWithDb:
    @pytest.mark.asyncio
    async def test_todo_with_db_store(self, ctx: ToolContext) -> None:
        from zero.db import Database
        from zero.db.sqlite_backend import InMemorySqliteBackend
        from zero.stores.todo_store import DbTodoStore

        backend = InMemorySqliteBackend()
        db = Database(backend=backend)
        await db.start()
        try:
            store = DbTodoStore(db)
            set_todo_store(store)
            tool = TodoTool()

            # Add.
            result = await tool.execute(
                {"action": "add", "item": "buy milk"}, ctx,
            )
            assert "added" in result.lower()

            # List.
            result = await tool.execute({"action": "list"}, ctx)
            assert "buy milk" in result

            # Complete.
            result = await tool.execute(
                {"action": "complete", "index": 1}, ctx,
            )
            assert "completed" in result.lower()

            # List again — should show as completed.
            result = await tool.execute({"action": "list"}, ctx)
            assert "[x]" in result
        finally:
            await db.stop()
            set_todo_store(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- patch_parser unit tests

class TestPatchParser:
    def test_parse_update_patch(self) -> None:
        patch = """*** Begin Patch
*** Update File: test.py
@@ def hello():
 def hello():
-    return 'world'
+    return 'universe'
*** End Patch"""
        ops = parse_patch(patch)
        assert len(ops) == 1
        assert ops[0].op_type.value == "update"
        assert ops[0].path == "test.py"
        assert len(ops[0].hunks) == 1

    def test_parse_add_file_patch(self) -> None:
        patch = """*** Begin Patch
*** Add File: new.py
+print('hello')
+print('world')
*** End Patch"""
        ops = parse_patch(patch)
        assert len(ops) == 1
        assert ops[0].op_type.value == "add"
        assert ops[0].content is not None
        assert "hello" in ops[0].content

    def test_parse_delete_file_patch(self) -> None:
        patch = """*** Begin Patch
*** Delete File: old.py
*** End Patch"""
        ops = parse_patch(patch)
        assert len(ops) == 1
        assert ops[0].op_type.value == "delete"
        assert ops[0].path == "old.py"

    def test_apply_update_patch(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    return 'world'\n")
        patch = f"""*** Begin Patch
*** Update File: {f}
@@ def hello():
 def hello():
-    return 'world'
+    return 'universe'
*** End Patch"""
        ops = parse_patch(patch)
        results = apply_patch(ops)
        assert "updated" in results[0]
        assert "return 'universe'" in f.read_text()
