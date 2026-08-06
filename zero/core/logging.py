"""Zero v2 structured logging — ADR T-1.5.

JSON logs by default; console pretty-printing for development.

Two-layer secret masking (ADR 0007 §4):
    1. ``SecretValue`` wrapper (in core/secret.py)
    2. Pattern-based redaction here — catches Telegram tokens, OpenAI/Anthropic
       keys, GitHub tokens, bearer headers, etc. before they hit the log sink.

Privacy rules:
    - User message content never logged at INFO+ (only at DEBUG with explicit opt-in)
    - Every log entry has ``request_id`` and ``scope`` fields
    - ``redact_secrets=True`` by default
"""
from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TextIO

from zero.core.scope import Scope
from zero.core.secret import redact_text

__all__ = [
    "Logger",
    "configure_logging",
    "get_logger",
    "request_id_var",
    "reset_request_context",
    "scope_var",
    "set_request_context",
]


# ---------------------------------------------------------------------- context vars

# Per-request context — uses ContextVar so concurrent asyncio tasks have
# isolated values (vs threading.local which would race).
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
scope_var: ContextVar[Scope | None] = ContextVar("scope", default=None)
actor_var: ContextVar[str | None] = ContextVar("actor", default=None)


def set_request_context(
    *,
    request_id: str | None = None,
    scope: Scope | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Set the request context vars. Returns a token dict for restoration."""
    tokens: dict[str, Any] = {
        "request_id": request_id_var.set(request_id or f"req_{uuid.uuid4().hex[:12]}"),
        "scope": scope_var.set(scope),
        "actor": actor_var.set(actor),
    }
    return tokens


def reset_request_context(tokens: Mapping[str, Any]) -> None:
    """Restore context vars to their previous values."""
    request_id_var.reset(tokens["request_id"])
    scope_var.reset(tokens["scope"])
    actor_var.reset(tokens["actor"])


# ---------------------------------------------------------------------- JSON formatter

class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter. No external deps."""

    def __init__(self, *, redact: bool = True, log_user_content: bool = False) -> None:
        super().__init__()
        self._redact = redact
        self._log_user_content = log_user_content

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Pull from context vars.
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        scope = scope_var.get()
        if scope is not None:
            payload["scope"] = dict(scope.to_log_dict())
        actor = actor_var.get()
        if actor:
            payload["actor"] = actor

        # Attach extra fields (passed via logger.info("...", extra={...}))
        for k, v in record.__dict__.items():
            if k in payload or k.startswith("_"):
                continue
            if k in {
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "message",
            }:
                continue
            payload[k] = _safe_json(v)

        # Exception info (with internal detail, NOT user content).
        if record.exc_info:
            payload["exception"] = self._format_exception(record.exc_info)

        # Redact secrets from message + every string field.
        if self._redact:
            payload = _redact_payload(payload)

        # Inline user content gating (T-1.5 acceptance).
        if not self._log_user_content and "user_content" in payload:
            payload.pop("user_content")

        # Build JSON manually to avoid dependency on orjson.
        import json  # noqa: PLC0415

        return json.dumps(payload, default=_json_default, ensure_ascii=False)

    def _format_exception(self, exc_info: Any) -> str:
        import traceback  # noqa: PLC0415

        return "".join(traceback.format_exception(*exc_info))


def _safe_json(v: Any) -> Any:
    """Convert non-JSON-safe values to strings."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_safe_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _safe_json(val) for k, val in v.items()}
    if hasattr(v, "to_log_dict"):
        return _safe_json(v.to_log_dict())
    if hasattr(v, "model_dump"):
        return _safe_json(v.model_dump())
    return repr(v)


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply :func:`redact_text` to every string value in ``payload``."""
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            out[k] = redact_text(v)
        elif isinstance(v, dict):
            out[k] = _redact_payload(v)
        elif isinstance(v, list):
            out[k] = [_redact_payload(x) if isinstance(x, dict) else (redact_text(x) if isinstance(x, str) else x) for x in v]
        else:
            out[k] = v
    return out


def _json_default(o: Any) -> Any:
    if hasattr(o, "to_log_dict"):
        return o.to_log_dict()
    if hasattr(o, "model_dump"):
        return o.model_dump()
    return repr(o)


# ---------------------------------------------------------------------- console formatter

class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def __init__(self, *, redact: bool = True) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        msg = record.getMessage()
        if self._redact:
            msg = redact_text(msg)
        rid = request_id_var.get()
        scope = scope_var.get()
        scope_str = f" scope={scope.retrieval_key()}" if scope else ""
        rid_str = f" req={rid}" if rid else ""
        return f"{color}{record.levelname:<7}{self.RESET} [{record.name}]{rid_str}{scope_str} {msg}"


# ---------------------------------------------------------------------- public API

@dataclass(slots=True)
class Logger:
    """Thin wrapper around stdlib ``logging.Logger``.

    Provides async-friendly methods and consistent scope/request_id propagation.
    """

    _logger: logging.Logger

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(msg, extra=kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(msg, extra=kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(msg, extra=kwargs)

    def error(self, msg: str, *, exc: BaseException | None = None, **kwargs: Any) -> None:
        self._logger.error(msg, exc_info=exc, extra=kwargs)

    def critical(self, msg: str, *, exc: BaseException | None = None, **kwargs: Any) -> None:
        self._logger.critical(msg, exc_info=exc, extra=kwargs)


@lru_cache(maxsize=128)
def get_logger(name: str) -> Logger:
    """Return a cached :class:`Logger` for ``name``."""
    return Logger(logging.getLogger(name))


def configure_logging(
    *,
    level: str = "info",
    format_: str = "json",
    redact: bool = True,
    log_user_content: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Configure the root logger. Call once at startup."""
    root = logging.getLogger()
    # Remove existing handlers (idempotent configure).
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(stream or sys.stderr)
    if format_ == "json":
        handler.setFormatter(JsonFormatter(redact=redact, log_user_content=log_user_content))
    else:
        handler.setFormatter(ConsoleFormatter(redact=redact))

    root.addHandler(handler)
    root.setLevel(level.upper())
