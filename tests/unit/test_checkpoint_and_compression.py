"""Tests for checkpoint manager and context compressor."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from zero.tools.checkpoint_manager import CheckpointManager, CheckpointError
from zero.tools.context_compressor import (
    ContextCompressor,
    TrajectoryCompressor,
    DEFAULT_KEEP_LAST,
)


# ---------------------------------------------------------------------- CheckpointManager

class TestCheckpointManager:
    @pytest.fixture
    def temp_workdir(self, tmp_path: Path) -> Path:
        """Create a temporary working directory with some files."""
        d = tmp_path / "project"
        d.mkdir()
        (d / "file1.py").write_text("print('hello')\n")
        (d / "file2.py").write_text("x = 42\n")
        (d / "subdir").mkdir()
        (d / "subdir" / "file3.py").write_text("# sub file\n")
        return d

    @pytest.fixture
    def manager(self, tmp_path: Path) -> CheckpointManager:
        store_dir = tmp_path / "checkpoint_store"
        return CheckpointManager(store_dir=store_dir)

    @pytest.mark.asyncio
    async def test_create_checkpoint(self, manager: CheckpointManager, temp_workdir: Path) -> None:
        cp = await manager.create_checkpoint(temp_workdir)
        assert cp.checkpoint_id.startswith("cp_")
        assert cp.file_count == 3
        assert cp.size_bytes > 0

    @pytest.mark.asyncio
    async def test_restore_checkpoint(
        self, manager: CheckpointManager, temp_workdir: Path
    ) -> None:
        """Create checkpoint, modify files, restore — files should be back."""
        cp = await manager.create_checkpoint(temp_workdir)

        # Modify files.
        (temp_workdir / "file1.py").write_text("print('modified')\n")
        (temp_workdir / "file2.py").unlink()

        # Restore.
        await manager.restore_checkpoint(cp)

        # Verify files are restored.
        assert (temp_workdir / "file1.py").read_text() == "print('hello')\n"
        assert (temp_workdir / "file2.py").exists()

    @pytest.mark.asyncio
    async def test_list_checkpoints(
        self, manager: CheckpointManager, temp_workdir: Path
    ) -> None:
        await manager.create_checkpoint(temp_workdir)
        await manager.create_checkpoint(temp_workdir)
        checkpoints = await manager.list_checkpoints(temp_workdir)
        assert len(checkpoints) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_dir_raises(self, manager: CheckpointManager, tmp_path: Path) -> None:
        with pytest.raises(CheckpointError, match="does not exist"):
            await manager.create_checkpoint(tmp_path / "nonexistent")

    @pytest.mark.asyncio
    async def test_prune_orphaned(
        self, manager: CheckpointManager, temp_workdir: Path, tmp_path: Path
    ) -> None:
        """Orphaned checkpoints (working dir gone) are pruned."""
        await manager.create_checkpoint(temp_workdir)

        # Delete the working directory → checkpoint becomes orphan.
        shutil.rmtree(temp_workdir)

        count = await manager.prune_checkpoints()
        assert count >= 1


# ---------------------------------------------------------------------- ContextCompressor

class TestContextCompressor:
    def test_no_compression_needed(self) -> None:
        """Short history is not compressed."""
        compressor = ContextCompressor()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = compressor.compress(messages, max_tokens=10000)
        assert result.messages == messages
        assert result.compressed_count == 0

    def test_compresses_long_history(self) -> None:
        """Long history is compressed with summary."""
        compressor = ContextCompressor()
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"Question {i} " * 100})
            messages.append({"role": "assistant", "content": f"Answer {i} " * 100})

        result = compressor.compress(messages, keep_last=3, max_tokens=500)
        assert result.compressed_count > 0
        assert result.kept_count < len(messages)
        assert result.summary is not None
        assert "Compressed" in result.summary
        # First message should be the summary.
        assert result.messages[0]["role"] == "system"
        assert "[CONTEXT COMPACTION]" in result.messages[0]["content"]

    def test_boundary_snaps_to_user_message(self) -> None:
        """Boundary snaps backwards to nearest user message."""
        compressor = ContextCompressor()
        messages = [
            {"role": "user", "content": "q1 " * 100},
            {"role": "assistant", "content": "a1 " * 100},
            {"role": "user", "content": "q2 " * 100},
            {"role": "assistant", "content": "a2 " * 100},
            {"role": "user", "content": "q3 " * 100},
            {"role": "assistant", "content": "a3 " * 100},
        ]
        result = compressor.compress(messages, keep_last=1, max_tokens=100)
        # The last user message should be in the kept section.
        kept_user_msgs = [m for m in result.messages if m["role"] == "user"]
        assert any("q3" in m["content"] for m in kept_user_msgs)

    def test_custom_summarizer(self) -> None:
        """Custom summarizer function is called."""
        compressor = ContextCompressor()
        messages = [
            {"role": "user", "content": "q " * 100},
            {"role": "assistant", "content": "a " * 100},
            {"role": "user", "content": "q2 " * 100},
            {"role": "assistant", "content": "a2 " * 100},
        ]

        def summarizer(old_msgs: list) -> str:
            return f"Custom summary of {len(old_msgs)} messages"

        result = compressor.compress(messages, keep_last=1, max_tokens=100, summarizer=summarizer)
        assert "Custom summary" in result.summary


# ---------------------------------------------------------------------- TrajectoryCompressor

class TestTrajectoryCompressor:
    def test_short_trajectory_not_compressed(self) -> None:
        tc = TrajectoryCompressor()
        traj = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = tc.compress(traj)
        assert result == traj

    def test_long_trajectory_compressed(self) -> None:
        tc = TrajectoryCompressor(target_max_tokens=100)
        traj = []
        for i in range(20):
            traj.append({"role": "user", "content": f"q{i} " * 100, "from": "human", "value": f"q{i} " * 100})
            traj.append({"role": "assistant", "content": f"a{i} " * 100, "from": "gpt", "value": f"a{i} " * 100})

        result = tc.compress(traj)
        assert len(result) < len(traj)
        # Head should be preserved.
        assert result[0] == traj[0]
        # Should contain a compressed summary message.
        assert any("COMPRESSED" in str(m.get("content", "")) for m in result)
