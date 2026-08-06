"""Filesystem checkpoint manager — ported from Hermes (``tools/checkpoint_manager.py``).

Transparent filesystem snapshots via a single shared shadow git store.

Per ADR T-7.5: every file-mutating operation creates a checkpoint first.
On error, the checkpoint can be restored.

Architecture (from Hermes):
    - Single shared git store at ``~/.zero/checkpoints/store/``
    - Per-project branch: ``refs/hermes/<hash16>``
    - Per-project git index: ``indexes/<hash16>``
    - Per-project metadata: ``projects/<hash16>.json``
    - Uses ``GIT_DIR`` + ``GIT_WORK_TREE`` + ``GIT_INDEX_FILE`` so no git
      state leaks into user's project.

Triggers:
    - Once per conversation turn, before file-mutating operations
    - ``write_file``, ``patch``, destructive ``terminal``

Auto-maintenance:
    - Prune checkpoints whose working dir no longer exists (orphan)
    - Prune checkpoints older than ``retention_days`` (stale)
    - ``git gc --prune=now`` after pruning
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zero.core.logging import get_logger

__all__ = ["CheckpointManager", "Checkpoint", "CheckpointError"]

_log = get_logger("zero.tools.checkpoint")

CHECKPOINT_STORE_DIR = Path.home() / ".zero" / "checkpoints" / "store"
CHECKPOINT_PROJECTS_DIR = Path.home() / ".zero" / "checkpoints" / "projects"
CHECKPOINT_INDEXES_DIR = Path.home() / ".zero" / "checkpoints" / "indexes"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_TOTAL_SIZE_MB = 500


class CheckpointError(RuntimeError):
    """Raised when checkpoint creation or restoration fails."""


@dataclass(slots=True)
class Checkpoint:
    """A single filesystem checkpoint."""

    checkpoint_id: str
    project_hash: str  # hash of working dir path
    working_dir: Path
    created_at: datetime
    file_count: int = 0
    size_bytes: int = 0
    git_ref: str = ""  # refs/zero/<hash16>
    metadata_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.metadata_path = CHECKPOINT_PROJECTS_DIR / f"{self.project_hash}.json"

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "project_hash": self.project_hash,
            "working_dir": str(self.working_dir),
            "created_at": self.created_at.isoformat(),
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
        }


class CheckpointManager:
    """Manages filesystem checkpoints via a shared shadow git store.

    Usage:
        >>> mgr = CheckpointManager()
        >>> cp = await mgr.create_checkpoint(Path("/project"))
        >>> # ... mutate files ...
        >>> # On error:
        >>> await mgr.restore_checkpoint(cp)
    """

    def __init__(
        self,
        *,
        store_dir: Path = CHECKPOINT_STORE_DIR,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        max_total_size_mb: int = DEFAULT_MAX_TOTAL_SIZE_MB,
    ) -> None:
        self._store_dir = store_dir
        self._retention_days = retention_days
        self._max_total_size_bytes = max_total_size_mb * 1024 * 1024
        self._store_dir.mkdir(parents=True, exist_ok=True)
        CHECKPOINT_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        CHECKPOINT_INDEXES_DIR.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Initialize the git store if not already done."""
        if self._initialized:
            return
        # Check if the store is already a git repo.
        git_dir = self._store_dir / ".git"
        if not git_dir.exists():
            # Initialize bare git repo.
            await self._run_git(
                ["init", "--bare", str(self._store_dir)],
                env={},
                cwd=self._store_dir,
                check=False,
            )
        # Set user config for commits.
        await self._run_git(
            ["config", "user.email", "zero@checkpoint.local"],
            env={"GIT_DIR": str(self._store_dir)},
            cwd=self._store_dir,
            check=False,
        )
        await self._run_git(
            ["config", "user.name", "Zero Checkpoint"],
            env={"GIT_DIR": str(self._store_dir)},
            cwd=self._store_dir,
            check=False,
        )
        self._initialized = True

    def _project_hash(self, working_dir: Path) -> str:
        """Stable hash of the working directory path."""
        return hashlib.sha256(str(working_dir.resolve()).encode()).hexdigest()[:16]

    def _git_ref(self, project_hash: str) -> str:
        return f"refs/zero/{project_hash}"

    def _index_file(self, project_hash: str) -> Path:
        return CHECKPOINT_INDEXES_DIR / project_hash

    async def create_checkpoint(self, working_dir: Path) -> Checkpoint:
        """Create a checkpoint of the current state of ``working_dir``.

        Uses git to snapshot all files. The snapshot is stored in the shared
        shadow store, not in the user's project.
        """
        if not working_dir.exists():
            raise CheckpointError(f"working dir does not exist: {working_dir}")
        if not working_dir.is_dir():
            raise CheckpointError(f"working dir is not a directory: {working_dir}")

        await self._ensure_initialized()

        project_hash = self._project_hash(working_dir)
        git_ref = self._git_ref(project_hash)
        index_file = self._index_file(project_hash)

        # Count files + sizes for metadata.
        file_count = 0
        size_bytes = 0
        for p in working_dir.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                file_count += 1
                try:
                    size_bytes += p.stat().st_size
                except OSError:
                    pass

        checkpoint = Checkpoint(
            checkpoint_id=f"cp_{int(time.time()):x}_{project_hash[:8]}",
            project_hash=project_hash,
            working_dir=working_dir,
            created_at=datetime.now(UTC),
            file_count=file_count,
            size_bytes=size_bytes,
            git_ref=git_ref,
        )

        # Run git add + commit in the shadow store.
        env = {
            "GIT_DIR": str(self._store_dir),
            "GIT_WORK_TREE": str(working_dir),
            "GIT_INDEX_FILE": str(index_file),
        }
        # git add -A (stage all changes).
        await self._run_git(["add", "-A"], env=env, cwd=working_dir)
        # git commit (create tree object).
        commit_result = await self._run_git(
            ["commit", "-m", f"checkpoint {checkpoint.checkpoint_id}", "--allow-empty"],
            env=env,
            cwd=working_dir,
            check=False,  # commit may fail if nothing changed
        )
        # Update the ref to point to the latest commit.
        await self._run_git(
            ["update-ref", git_ref, "HEAD"],
            env=env,
            cwd=working_dir,
        )

        # Save metadata.
        self._save_metadata(checkpoint)
        _log.info(f"checkpoint created: {checkpoint.checkpoint_id} ({file_count} files)")
        return checkpoint

    async def restore_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Restore files from a checkpoint.

        This overwrites the current working directory with the snapshot.
        """
        env = {
            "GIT_DIR": str(self._store_dir),
            "GIT_WORK_TREE": str(checkpoint.working_dir),
            "GIT_INDEX_FILE": str(self._index_file(checkpoint.project_hash)),
        }
        # git checkout -- . (restore files from the ref's tree).
        await self._run_git(
            ["checkout", checkpoint.git_ref, "--", "."],
            env=env,
            cwd=checkpoint.working_dir,
        )
        _log.info(f"checkpoint restored: {checkpoint.checkpoint_id}")

    async def list_checkpoints(self, working_dir: Path) -> list[Checkpoint]:
        """List all checkpoints for a working directory."""
        project_hash = self._project_hash(working_dir)
        metadata_path = CHECKPOINT_PROJECTS_DIR / f"{project_hash}.json"
        if not metadata_path.exists():
            return []
        try:
            data = json.loads(metadata_path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        checkpoints: list[Checkpoint] = []
        for cp_data in data.get("checkpoints", []):
            cp = Checkpoint(
                checkpoint_id=cp_data["checkpoint_id"],
                project_hash=project_hash,
                working_dir=Path(cp_data["working_dir"]),
                created_at=datetime.fromisoformat(cp_data["created_at"]),
                file_count=cp_data.get("file_count", 0),
                size_bytes=cp_data.get("size_bytes", 0),
                git_ref=cp_data.get("git_ref", ""),
            )
            checkpoints.append(cp)
        return checkpoints

    async def prune_checkpoints(self) -> int:
        """Delete orphaned (working dir gone) and stale (old) checkpoints.

        Runs ``git gc --prune=now`` after pruning.
        Returns count of pruned checkpoints.
        """
        count = 0
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=self._retention_days)

        for meta_file in CHECKPOINT_PROJECTS_DIR.glob("*.json"):
            try:
                data = json.loads(meta_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            project_hash = data.get("project_hash", "")
            working_dir = Path(data.get("working_dir", "/nonexistent"))
            is_orphan = not working_dir.exists()

            kept: list[dict[str, Any]] = []
            for cp_data in data.get("checkpoints", []):
                created_at = datetime.fromisoformat(cp_data["created_at"])
                is_stale = created_at < cutoff

                if is_orphan or is_stale:
                    count += 1
                    # Delete the git ref.
                    git_ref = cp_data.get("git_ref", "")
                    if git_ref:
                        env = {"GIT_DIR": str(self._store_dir)}
                        await self._run_git(
                            ["update-ref", "-d", git_ref],
                            env=env,
                            cwd=self._store_dir,
                            check=False,
                        )
                else:
                    kept.append(cp_data)

            if not kept:
                meta_file.unlink(missing_ok=True)
                # Also delete the index file.
                self._index_file(project_hash).unlink(missing_ok=True)
            else:
                data["checkpoints"] = kept
                meta_file.write_text(json.dumps(data, indent=2))

        # Run git gc.
        await self._run_git(
            ["gc", "--prune=now"],
            env={"GIT_DIR": str(self._store_dir)},
            cwd=self._store_dir,
            check=False,
        )

        if count > 0:
            _log.info(f"pruned {count} checkpoints")
        return count

    def _save_metadata(self, checkpoint: Checkpoint) -> None:
        """Save/update checkpoint metadata."""
        metadata_path = checkpoint.metadata_path
        try:
            data = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            data = {}

        data["project_hash"] = checkpoint.project_hash
        data["working_dir"] = str(checkpoint.working_dir)
        data.setdefault("checkpoints", []).append({
            "checkpoint_id": checkpoint.checkpoint_id,
            "created_at": checkpoint.created_at.isoformat(),
            "file_count": checkpoint.file_count,
            "size_bytes": checkpoint.size_bytes,
            "git_ref": checkpoint.git_ref,
            "working_dir": str(checkpoint.working_dir),
        })

        metadata_path.write_text(json.dumps(data, indent=2))

    async def _run_git(
        self,
        args: list[str],
        *,
        env: dict[str, str],
        cwd: Path,
        check: bool = True,
    ) -> subprocess_result:
        """Run a git command with the given environment."""
        import subprocess  # noqa: PLC0415

        full_env = dict(os.environ)
        full_env.update(env)
        try:
            result = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=str(cwd),
                env=full_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise CheckpointError("git not found in PATH") from e

        stdout, stderr = await result.communicate()
        if check and result.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[:200]
            # Don't raise on "nothing to commit" — that's OK.
            if "nothing to commit" not in err_text and "no changes added" not in err_text:
                raise CheckpointError(
                    f"git {' '.join(args)} failed (exit {result.returncode}): {err_text}"
                )
        return subprocess_result(
            returncode=result.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


@dataclass(slots=True)
class subprocess_result:
    """Result of a subprocess execution."""

    returncode: int
    stdout: str
    stderr: str


# Need os for _run_git
import os  # noqa: E402, PLC0415
