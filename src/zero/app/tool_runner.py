"""Process-boundary execution for server-owned tools.

Tool handlers are untrusted application extensions. They run in a child
process with a deadline and a serialized output budget. A timeout kills the
whole child process group, not merely the direct handler process.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from typing import Any


class ToolRunnerError(RuntimeError):
    """Base class for isolated runner failures."""


class ToolRunnerTimeout(ToolRunnerError):
    """The handler exceeded its configured deadline."""


class ToolRunnerOutputLimit(ToolRunnerError):
    """The serialized handler result exceeded the output budget."""


class ToolRunnerHandlerError(ToolRunnerError):
    """The handler raised or returned an unserializable result."""


def _run_child(
    send_conn: Any,
    handler: Callable[..., Any],
    input_data: dict[str, Any],
    context: Any,
    max_output_bytes: int,
) -> None:
    """Execute one handler and send only a bounded, typed outcome."""
    try:
        if hasattr(os, "setsid"):
            os.setsid()
        output = handler(input_data, context)
        encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_output_bytes:
            send_conn.send(("output_limit", len(encoded)))
        else:
            send_conn.send(("ok", output))
    except BaseException as exc:  # noqa: BLE001 - child boundary must capture exits
        send_conn.send(("handler_error", type(exc).__name__))
    finally:
        send_conn.close()


class IsolatedToolRunner:
    """Run a callable in a fresh process with bounded lifetime/output.

    POSIX production hosts use ``fork`` so the child inherits nothing
    beyond what the handler closes over. Platforms without ``fork``
    (for example Windows) fall back to the ``spawn`` start method; that
    requires picklable handlers, so an unpicklable handler fails with a
    typed :class:`ToolRunnerError` rather than silently running
    in-process.
    """

    def __init__(
        self,
        *,
        default_timeout_seconds: float = 30.0,
        max_output_bytes: int = 64 * 1024,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.default_timeout_seconds = float(default_timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        methods = set(multiprocessing.get_all_start_methods())
        if "fork" in methods:
            self._start_method = "fork"
        elif "spawn" in methods:
            self._start_method = "spawn"
        else:  # pragma: no cover - every supported platform has one
            raise RuntimeError("isolated tool execution requires a process boundary")
        self._context = multiprocessing.get_context(self._start_method)

    def run(
        self,
        handler: Callable[..., Any],
        input_data: dict[str, Any],
        context: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = (
            self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        )
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        recv_conn, send_conn = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_run_child,
            args=(
                send_conn,
                handler,
                input_data,
                context,
                self.max_output_bytes,
            ),
            name="zero-tool",
        )
        started = time.monotonic()
        try:
            process.start()
        except Exception as exc:
            send_conn.close()
            recv_conn.close()
            raise ToolRunnerError("could not start isolated tool process") from exc
        finally:
            # The child has its own descriptor after start.
            send_conn.close()

        try:
            remaining = max(0.0, timeout - (time.monotonic() - started))
            if not recv_conn.poll(remaining):
                self._kill_process_group(process)
                raise ToolRunnerTimeout(f"tool handler timed out after {timeout:g} seconds")
            try:
                status, value = recv_conn.recv()
            except (EOFError, OSError) as exc:
                raise ToolRunnerHandlerError(
                    "isolated tool process exited without a result"
                ) from exc
            if status == "output_limit":
                raise ToolRunnerOutputLimit(f"tool output exceeds {self.max_output_bytes} bytes")
            if status == "handler_error":
                raise ToolRunnerHandlerError(f"tool handler raised {value}")
            if status != "ok" or not isinstance(value, dict):
                raise ToolRunnerHandlerError("tool handler returned invalid output")
            return value
        finally:
            if process.is_alive():
                self._kill_process_group(process)
            process.join(timeout=2.0)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
            recv_conn.close()

    @staticmethod
    def _kill_process_group(process: Any) -> None:
        if process.pid is None:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - non-POSIX fallback
                process.kill()
        except ProcessLookupError:
            pass
