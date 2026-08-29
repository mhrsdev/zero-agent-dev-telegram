"""Cross-process advisory lock preventing duplicate Telegram polling.

Bug fix (2026-08-29, dead-bot session): with the database drift repaired,
`zero start` and `zero-develop serve` can legitimately run at the same
time (that exact setup was observed on the operator's Windows machine).
Both load the same config.yaml, both resolve the same bot token, and
both would call ``getUpdates`` — Telegram answers the loser with
HTTP 409 Conflict forever, updates get split between the two engines,
and both logs fill with errors.

Telegram allows exactly ONE long-poll consumer per bot token. This
module gives the polling worker an advisory, cross-process, per-token
lock so the second engine SKIPS polling deliberately (with a clear log)
instead of fighting the first one.

Design constraints:
- works on Windows and POSIX (O_CREAT|O_EXCL file creation; the lock
  file stores the holding pid);
- steals only STALE locks (holder pid no longer alive) so a crashed
  process cannot block polling forever;
- never logs or stores the bot token itself — only its SHA-256
  fingerprint prefix.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

LOCK_DIRNAME = "poll-locks"


def _pid_alive(pid: int) -> bool:
    """Liveness probe that never signals the target process.

    Windows-safe: ``os.kill(pid, 0)`` maps to TerminateProcess there,
    so the kernel query-handle approach is used instead (same trick as
    ``zero.manage.cli._pid_alive`` — kept local so the worker's import
    stays light).
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            if exit_code.value != 259:  # STILL_ACTIVE
                return False
        wait = kernel32.WaitForSingleObject(handle, 0)
        return wait == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def token_fingerprint(bot_token: str) -> str:
    """A non-reversible, log-safe identifier for one bot token."""
    return hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]


class TokenPollLock:
    """Advisory per-bot-token lock rooted at ``$ZERO_HOME/poll-locks``."""

    def __init__(self, home: Path | None = None) -> None:
        base = Path(home) if home is not None else Path(
            os.environ.get("ZERO_HOME", str(Path.home() / ".zero"))
        )
        self._dir = base / LOCK_DIRNAME
        self._held: set[str] = set()

    @property
    def lock_dir(self) -> Path:
        return self._dir

    def holder_pid(self, bot_token: str) -> int | None:
        """The pid recorded in the lock file, when one exists."""
        path = self._dir / f"{token_fingerprint(bot_token)}.lock"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("pid", 0)) or None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def try_acquire(self, bot_token: str) -> tuple[bool, int | None]:
        """Attempt to own the polling right for ``bot_token``.

        Returns ``(acquired, other_pid)``. When not acquired,
        ``other_pid`` is the process already polling that token — a
        different live process, OR this very process (two worker hosts
        in one process must also not double-poll). A stale lock (dead
        holder / corrupt file) is stolen once.
        """
        fp = token_fingerprint(bot_token)
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{fp}.lock"
        payload = json.dumps(
            {"pid": os.getpid(), "ts": time.time(), "cmd": sys.argv[0][:120]}
        )
        for attempt in range(2):
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, payload.encode("utf-8"))
                finally:
                    os.close(fd)
                self._held.add(fp)
                return True, None
            except FileExistsError:
                other = self.holder_pid(bot_token)
                if other is not None:
                    if other == os.getpid():
                        # Another host instance IN THIS PROCESS holds it.
                        return False, other
                    if _pid_alive(other):
                        return False, other
                # Stale (dead holder) or corrupt lock — steal and retry once.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    return False, other
            except OSError:
                # Locking is best-effort (read-only home etc.) — allow polling.
                return True, None
        return False, self.holder_pid(bot_token)

    def release(self, bot_token: str) -> None:
        """Drop our own lock (never another process's)."""
        fp = token_fingerprint(bot_token)
        if fp not in self._held:
            return
        path = self._dir / f"{fp}.lock"
        try:
            if self.holder_pid(bot_token) == os.getpid():
                path.unlink(missing_ok=True)
        except OSError:
            pass
        self._held.discard(fp)

    def release_all(self) -> None:
        for fp in list(self._held):
            path = self._dir / f"{fp}.lock"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if int(data.get("pid", 0)) == os.getpid():
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            self._held.discard(fp)


__all__ = ["TokenPollLock", "token_fingerprint"]
