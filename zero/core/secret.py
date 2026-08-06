"""Zero v2 secret resolution — ADR 0007 implementation.

Secrets are **never** stored as raw values in code, config files, or DB rows.
Instead they are referenced via ``secret://<provider>/<path>[#<key>]`` and
resolved at the moment of use.

Two-layer masking (ADR 0007 §4):

    1. ``SecretValue`` wrapper — masks ``__repr__``/``__str__``/``__format__``
       and is excluded from ``json.dumps`` and ``pydantic.model_dump()``.
    2. Pattern-based redaction in the logger (see ``core/logging.py``) —
       catches Telegram tokens, OpenAI/Anthropic keys, GitHub tokens etc.

If one layer fails the other still works.

Backends: ``env`` and ``file`` (built-in). Extension: ``vault``, ``sops``.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "REDACTION_PATTERNS",
    "SECRET_REF_PATTERN",
    "CompositeSecretResolver",
    "EnvSecretBackend",
    "FileSecretBackend",
    "SecretError",
    "SecretMetadata",
    "SecretNotFoundError",
    "SecretPermissionError",
    "SecretResolver",
    "SecretValue",
    "is_secret_ref",
    "parse_secret_ref",
    "redact_text",
]


# ---------------------------------------------------------------------- errors

class SecretError(RuntimeError):
    """Base class for secret resolution errors."""


class SecretNotFoundError(SecretError):
    """Raised when a ``secret://`` reference points to nothing."""


class SecretPermissionError(SecretError):
    """Raised when a secret file has too-open permissions (>0600)."""


# ---------------------------------------------------------------------- reference parsing

# secret://<provider>/<path>[#<key>]
SECRET_REF_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""
    ^secret://
    (?P<provider>[a-z][a-z0-9_-]*)
    /
    (?P<path>[^#\s]+)
    (?:\#(?P<key>[A-Za-z0-9_.\-]+))?
    $
    """,
    re.VERBOSE,
)


def parse_secret_ref(ref: str) -> tuple[str, str, str | None]:
    """Parse a ``secret://`` reference into (provider, path, key).

    Raises ``SecretError`` if the format is invalid.
    """
    m = SECRET_REF_PATTERN.match(ref)
    if m is None:
        raise SecretError(
            f"invalid secret reference {ref!r}; expected secret://<provider>/<path>[#<key>]"
        )
    return m.group("provider"), m.group("path"), m.group("key")


def is_secret_ref(value: str) -> bool:
    """True if ``value`` looks like a ``secret://`` reference."""
    return isinstance(value, str) and value.startswith("secret://")


# ---------------------------------------------------------------------- SecretValue

class SecretValue:
    """Wrapper around a secret string that never reveals itself accidentally.

    The wrapped value is stored in the ``_value`` attribute, which is internal
    (single underscore) and never appears in ``repr()``, ``str()``,
    ``format()``, ``json.dumps()``, or ``pydantic.model_dump()``.

    To obtain the raw value, call :meth:`reveal` — this is the **only** path.
    """

    __slots__ = ("_source_ref", "_value")

    _value: str
    _source_ref: str

    def __init__(self, value: str, *, source_ref: str = "<inline>") -> None:
        if not isinstance(value, str):
            raise SecretError(f"SecretValue requires str, got {type(value).__name__}")
        self._value = value
        self._source_ref = source_ref

    # The ONLY escape hatch.
    def reveal(self) -> str:
        """Return the underlying secret value. Use only at the point of use."""
        return self._value

    @property
    def source_ref(self) -> str:
        return self._source_ref

    @property
    def length(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"SecretValue(***{self.length}chars*** from {self._source_ref!r})"

    def __str__(self) -> str:
        return "***"

    def __format__(self, spec: str) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        # Hash the raw value so SecretValue can be a dict key. Do NOT hash the
        # masked repr (it'd collide for every SecretValue).
        return hash(self._value)

    def __len__(self) -> int:
        return self.length

    # Make sure pydantic doesn't try to serialize us as a string.
    def __getstate__(self) -> dict[str, str]:
        # Pickle: we DO transport the value (e.g. via multiprocessing).
        return {"_value": self._value, "source_ref": self._source_ref}

    def __setstate__(self, state: Mapping[str, str]) -> None:
        self._value = state["_value"]
        self._source_ref = state["source_ref"]


# ---------------------------------------------------------------------- metadata

@dataclass(frozen=True, slots=True)
class SecretMetadata:
    """Non-sensitive metadata about a secret, suitable for dashboard display."""

    ref: str
    configured: bool
    last_rotated_at: str | None = None
    length_hint: int | None = None  # length only, never content


# ---------------------------------------------------------------------- resolver protocol

@runtime_checkable
class SecretResolver(Protocol):
    """Protocol every secret backend must implement."""

    def resolve(self, ref: str) -> SecretValue:
        """Return the secret value for ``ref``. Raises ``SecretNotFoundError``."""
        ...

    def exists(self, ref: str) -> bool:
        """Whether the secret exists. Never raises."""
        ...

    def metadata(self, ref: str) -> SecretMetadata:
        """Return non-sensitive metadata. Never raises."""
        ...


# ---------------------------------------------------------------------- backends

class EnvSecretBackend:
    """Resolves ``secret://env/<VAR_NAME>`` from environment variables.

    The ``#key`` suffix is unsupported for env vars (a single var = single value).
    """

    __slots__ = ("_environ",)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve(self, ref: str) -> SecretValue:
        provider, path, key = parse_secret_ref(ref)
        if provider != "env":
            raise SecretNotFoundError(f"EnvSecretBackend cannot resolve {ref!r}")
        if key is not None:
            raise SecretError(
                f"env backend does not support #key fragment in {ref!r}"
            )
        value = self._environ.get(path)
        if value is None or value == "":
            raise SecretNotFoundError(
                f"environment variable {path!r} is not set (or empty) — "
                f"required by secret reference {ref!r}"
            )
        return SecretValue(value, source_ref=ref)

    def exists(self, ref: str) -> bool:
        try:
            provider, path, key = parse_secret_ref(ref)
        except SecretError:
            return False
        if provider != "env" or key is not None:
            return False
        v = self._environ.get(path)
        return v is not None and v != ""

    def metadata(self, ref: str) -> SecretMetadata:
        try:
            v = self.resolve(ref)
            return SecretMetadata(ref=ref, configured=True, length_hint=v.length)
        except SecretError:
            return SecretMetadata(ref=ref, configured=False)


class FileSecretBackend:
    """Resolves ``secret://file/<path>[#key]`` from a file on disk.

    Rules (ADR 0007 §3):

        - File permission must be ``0600`` (or stricter). Anything looser →
          :class:`SecretPermissionError` (startup failure).
        - Without ``#key``: file content (stripped) is the secret.
        - With ``#key``: file is parsed as YAML; the key selects a single field.
          (Useful for ``secrets.yaml`` with multiple named secrets.)
        - ``~`` is expanded.
    """

    __slots__ = ()

    def resolve(self, ref: str) -> SecretValue:
        provider, path, key = parse_secret_ref(ref)
        if provider != "file":
            raise SecretNotFoundError(f"FileSecretBackend cannot resolve {ref!r}")

        p = Path(path).expanduser()
        if not p.exists():
            raise SecretNotFoundError(f"secret file not found: {p}")
        if not p.is_file():
            raise SecretNotFoundError(f"secret path is not a regular file: {p}")

        # Permission check — fail closed.
        self._check_permissions(p)

        content = p.read_text(encoding="utf-8")

        if key is None:
            # Whole-file content (stripped of trailing whitespace).
            return SecretValue(content.strip(), source_ref=ref)

        # Parse as YAML and extract key.
        # Local import to keep module load fast for env-only installs.
        import yaml  # noqa: PLC0415

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise SecretError(f"secret file {p} is not valid YAML: {e}") from e
        if not isinstance(data, dict):
            raise SecretError(
                f"secret file {p} must be a YAML mapping when using #key, got {type(data).__name__}"
            )
        if key not in data:
            raise SecretNotFoundError(f"secret key {key!r} not found in file {p}")
        value = data[key]
        if not isinstance(value, str):
            raise SecretError(
                f"secret value for {key!r} in {p} must be a string, got {type(value).__name__}"
            )
        return SecretValue(value, source_ref=ref)

    def exists(self, ref: str) -> bool:
        try:
            provider, path, _ = parse_secret_ref(ref)
        except SecretError:
            return False
        if provider != "file":
            return False
        return Path(path).expanduser().is_file()

    def metadata(self, ref: str) -> SecretMetadata:
        try:
            v = self.resolve(ref)
            return SecretMetadata(ref=ref, configured=True, length_hint=v.length)
        except SecretError:
            return SecretMetadata(ref=ref, configured=False)

    @staticmethod
    def _check_permissions(p: Path) -> None:
        """Enforce file permission ≤ 0600. Fail closed (raise) if looser."""
        try:
            mode = p.stat().st_mode
        except OSError as e:
            raise SecretPermissionError(f"cannot stat secret file {p}: {e}") from e
        # Mask to permission bits only.
        perm = stat.S_IMODE(mode)
        # Allowed: 0600, 0400, 0200, 0000 (yes, even 0000 — file owner can still
        # read after chmod). Disallowed: anything with group or other bits set.
        if perm & 0o077:  # any group/other bit
            raise SecretPermissionError(
                f"secret file {p} has permission {oct(perm)} — "
                f"must be 0600 or stricter (no group/other access). "
                f"Run: chmod 600 {p}"
            )


# ---------------------------------------------------------------------- composite resolver

class CompositeSecretResolver:
    """Routes a secret reference to the correct backend by ``<provider>``.

    Unknown provider → ``SecretNotFoundError``.
    """

    __slots__ = ("_backends",)

    def __init__(self, backends: Mapping[str, SecretResolver] | None = None) -> None:
        default_backends: dict[str, SecretResolver] = {
            "env": EnvSecretBackend(),
            "file": FileSecretBackend(),
        }
        if backends:
            default_backends.update(backends)
        self._backends = default_backends

    def register_backend(self, provider: str, backend: SecretResolver) -> None:
        self._backends[provider] = backend

    def resolve(self, ref: str) -> SecretValue:
        provider, _, _ = parse_secret_ref(ref)
        backend = self._backends.get(provider)
        if backend is None:
            raise SecretNotFoundError(
                f"no secret backend registered for provider {provider!r} "
                f"(ref: {ref!r}). Available: {sorted(self._backends)}"
            )
        return backend.resolve(ref)

    def exists(self, ref: str) -> bool:
        try:
            provider, _, _ = parse_secret_ref(ref)
        except SecretError:
            return False
        backend = self._backends.get(provider)
        if backend is None:
            return False
        return backend.exists(ref)

    def metadata(self, ref: str) -> SecretMetadata:
        try:
            provider, _, _ = parse_secret_ref(ref)
        except SecretError:
            return SecretMetadata(ref=ref, configured=False)
        backend = self._backends.get(provider)
        if backend is None:
            return SecretMetadata(ref=ref, configured=False)
        return backend.metadata(ref)


# ---------------------------------------------------------------------- redaction patterns

# Compiled once at module import. Used by core/logging.py and any other place
# that wants to scrub secrets from arbitrary text.
_REDACTION_PATTERNS_RAW: Final[dict[str, str]] = {
    # Telegram bot tokens: <bot_id>:<35+ char token>
    "telegram_bot_token": r"\b(\d{8,12}):([A-Za-z0-9_-]{30,})\b",
    # OpenAI / Anthropic / Router / GitHub tokens
    "openai_api_key": r"\bsk-[A-Za-z0-9]{20,}\b",
    "anthropic_api_key": r"\bsk-ant-[A-Za-z0-9_\-]{30,}\b",
    "router_token": r"\bzr_(?:live|test)_[A-Za-z0-9]{20,}\b",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    # Generic bearer / authorization headers
    "bearer_token": r"\b[Bb]earer\s+[A-Za-z0-9_\-\.=]{20,}\b",
    # AWS access keys (best-effort)
    "aws_access_key": r"\bAKIA[0-9A-Z]{16}\b",
    # Generic password in URL: scheme://user:pass@host
    "url_password": r"(://[^\s:/@]+):([^\s/@]+)@",
    # Slack tokens
    "slack_token": r"\bxox[abp]-[A-Za-z0-9\-]{10,}\b",
}

REDACTION_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    name: re.compile(pat) for name, pat in _REDACTION_PATTERNS_RAW.items()
}


def redact_text(text: str) -> str:
    """Apply all known redaction patterns to ``text``.

    Used by the logger as the second layer of secret masking (ADR 0007 §4).
    """
    if not text:
        return text
    redacted = text
    for pattern in REDACTION_PATTERNS.values():
        redacted = pattern.sub("***", redacted)
    return redacted


# ---------------------------------------------------------------------- literal type

SecretProviderLiteral = Literal["env", "file", "vault", "sops"]
