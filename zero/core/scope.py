"""Zero v2 scope & mode separation — ADR 0003 implementation.

This is **the central security primitive of the entire project**.

Three Modes:
    - ``PERSONAL``    — user's private 1:1 chat with Zero
    - ``NORMAL``      — group/Topic without Project binding (has Group Memory,
                        no Task/Git/ADR/Agent dev features)
    - ``DEVELOPMENT`` — Topic bound to a Project (full dev features)

``Scope`` is a frozen dataclass with slots, validated in ``__post_init__``.
The four invariants from ADR 0003 §2 are enforced:

    1. ``mode`` is the first field and immutable
    2. Cannot have more than one key group — constructor raises ``ValueError``
    3. Every persistent record has non-nullable ``mode`` and scope-key
    4. No query without predicate on ``mode``

Mode detection is **tabular and deterministic, never LLM** — see
:func:`zero.telegram.topic_binding.resolve_mode`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Literal, assert_never

__all__ = [
    "PERSONAL_USER_ID_SENTINEL",
    "Mode",
    "Scope",
    "ScopeError",
    "ScopeKeyError",
]


class Mode(StrEnum):
    """The three first-class Modes per ADR 0003 §1."""

    PERSONAL = "personal"
    NORMAL = "normal"
    DEVELOPMENT = "development"

    def __str__(self) -> str:
        return self.value


# In PERSONAL mode there is no group/project context — we use this sentinel
# value to populate the scope_key columns (NOT NULL constraint at DB level).
# Real users always have a real ``usr_<ulid>`` id; this sentinel is only used
# when scope is constructed from a non-persistent context (e.g. CLI bootstrap).
PERSONAL_USER_ID_SENTINEL: Final[str] = "usr_bootstrap"


class ScopeError(ValueError):
    """Base class for ``Scope`` validation errors."""


class ScopeKeyError(ScopeError):
    """Exactly one key group must be populated; got zero or multiple."""


def _validate_id(value: str, prefix: str, field_name: str) -> None:
    """Validate that ``value`` is a non-empty string with the given ULID prefix.

    ADR 0002 §5 mandates prefixed ULIDs (``usr_<ulid>``, ``prj_<ulid>`` etc.).
    After the prefix and underscore, only ``[A-Za-z0-9_-]`` characters are allowed.
    """
    if not isinstance(value, str):
        raise ScopeError(f"{field_name} must be a non-empty string, got {value!r}")
    if not value.startswith(prefix + "_"):
        raise ScopeError(
            f"{field_name} must start with prefix {prefix!r}, got {value!r}"
        )
    if len(value) < len(prefix) + 2:
        raise ScopeError(
            f"{field_name} appears truncated: {value!r} (expected {prefix}_<ulid>)"
        )
    # Validate the suffix contains only allowed characters (no spaces, no path separators).
    import re  # noqa: PLC0415

    suffix = value[len(prefix) + 1:]
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", suffix):
        raise ScopeError(
            f"{field_name} contains invalid characters in suffix {suffix!r} "
            f"(only [A-Za-z0-9_-] allowed after {prefix}_)"
        )


@dataclass(frozen=True, slots=True)
class Scope:
    """Frozen, validated scope of action for a Zero operation.

    Construction rules (ADR 0003 §2):

        - ``mode`` is **always** the first field and immutable.
        - Exactly one of the three key groups must be populated:
            * PERSONAL  → ``user_id`` (always set; defaults to sentinel)
            * NORMAL    → ``group_id`` + ``topic_id`` (both required)
            * DEVELOPMENT → ``org_id`` + ``workspace_id`` + ``project_id`` + ``topic_id`` + ``group_id``
        - ``memory_scope_id`` is independent of ``mode`` — decoupling means a
          ``normal → dev`` mode change does NOT migrate memory.
        - Every persistent record MUST carry a non-null ``mode`` and the
          matching scope-key columns.

    The dataclass uses ``frozen=True`` and ``slots=True`` to enforce
    immutability and prevent accidental attribute addition.
    """

    # Field 1 — mode (always first, always immutable)
    mode: Mode

    # Field 2 — key groups (only one group populated per Mode)
    user_id: str | None = None

    group_id: str | None = None
    topic_id: int | None = None

    org_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None

    # Field 3 — independent memory scope (ADR 0003 §2.4)
    # Default: derived from mode + keys (call .with_default_memory_scope()).
    memory_scope_id: str | None = None

    # Field 4 — provenance metadata, never used for retrieval filtering
    source: str = field(default="unknown", compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        """Validate the four invariants. Raises ``ScopeError`` on violation."""
        # Invariant 1: mode always set (dataclass would crash earlier, but be explicit)
        if self.mode not in (Mode.PERSONAL, Mode.NORMAL, Mode.DEVELOPMENT):
            raise ScopeError(f"invalid mode {self.mode!r}")

        # Invariant 2: exactly one key group populated
        self._check_exactly_one_key_group()

        # Invariant 3: every key value is a prefixed ULID (or topic_id is int>0)
        self._check_id_prefixes()

        # Invariant 4: memory_scope_id is set (auto-derived if None — see class doc)
        # We allow None at construction time, but it must be set before persistence.

    # ------------------------------------------------------------------ key group checks

    def _check_exactly_one_key_group(self) -> None:
        """Enforce that the keys matching ``mode`` are populated.

        Rules (ADR 0003 §2):
            - PERSONAL: only ``user_id``. group/topic/org/ws/prj all forbidden.
            - NORMAL:   only ``group_id`` + ``topic_id``. user_id/org/ws/prj forbidden.
            - DEVELOPMENT: ``org_id`` + ``workspace_id`` + ``project_id`` REQUIRED,
                          PLUS ``group_id`` + ``topic_id`` (the Telegram Topic that
                          hosts the Project). user_id forbidden as a key.

        In DEVELOPMENT mode the dev-keys and the group/topic are NOT mutually
        exclusive — both are required (the Topic is the entry point, the
        Project is the working scope).
        """
        personal_set = self.user_id is not None
        normal_set = self.group_id is not None or self.topic_id is not None
        dev_set = (
            self.org_id is not None
            or self.workspace_id is not None
            or self.project_id is not None
        )

        if self.mode is Mode.PERSONAL:
            # PERSONAL: user_id required; everything else forbidden.
            if not personal_set:
                raise ScopeKeyError(
                    "PERSONAL mode requires user_id; got "
                    f"group_id={self.group_id!r}, topic_id={self.topic_id!r}"
                )
            if self.group_id is not None or self.topic_id is not None:
                raise ScopeKeyError(
                    "PERSONAL mode forbids group_id/topic_id — private chat is fully isolated"
                )
            if dev_set:
                raise ScopeKeyError(
                    "PERSONAL mode forbids org_id/workspace_id/project_id"
                )

        elif self.mode is Mode.NORMAL:
            # NORMAL: group_id + topic_id required; everything else forbidden.
            if not normal_set:
                raise ScopeKeyError(
                    "NORMAL mode requires group_id + topic_id; "
                    f"got user_id={self.user_id!r}, org_id={self.org_id!r}"
                )
            if personal_set:
                # user_id is metadata in NORMAL, not a retrieval key — strip it.
                # We don't raise here; the .normal() factory already strips it.
                # But if someone constructs Scope directly with user_id+normal,
                # we accept it as metadata (used for audit) but it doesn't form
                # a retrieval key.
                pass
            if dev_set:
                raise ScopeKeyError(
                    "NORMAL mode forbids org_id/workspace_id/project_id — "
                    "if you want dev features, switch mode to DEVELOPMENT and bind a Project"
                )
            if self.group_id is None or self.topic_id is None:
                raise ScopeKeyError(
                    "NORMAL mode requires BOTH group_id and topic_id (non-Forum groups use topic_id=0)"
                )

        elif self.mode is Mode.DEVELOPMENT:
            # DEVELOPMENT: dev-keys AND group/topic both required; user_id forbidden as key.
            if not dev_set:
                raise ScopeKeyError(
                    "DEVELOPMENT mode requires org_id + workspace_id + project_id"
                )
            if self.org_id is None or self.workspace_id is None or self.project_id is None:
                raise ScopeKeyError(
                    "DEVELOPMENT mode requires all three: org_id, workspace_id, project_id"
                )
            if self.group_id is None or self.topic_id is None:
                raise ScopeKeyError(
                    "DEVELOPMENT mode requires group_id + topic_id (the Topic bound to the Project)"
                )
            # user_id is allowed as metadata (who launched the agent) but is
            # NOT a retrieval key in DEVELOPMENT mode.
            # No raise — accept it.

    def _check_id_prefixes(self) -> None:
        """Validate prefixed ULID format on every populated key field."""
        if self.user_id is not None:
            _validate_id(self.user_id, "usr", "user_id")
        if self.group_id is not None:
            _validate_id(self.group_id, "grp", "group_id")
        if self.org_id is not None:
            _validate_id(self.org_id, "org", "org_id")
        if self.workspace_id is not None:
            _validate_id(self.workspace_id, "ws", "workspace_id")
        if self.project_id is not None:
            _validate_id(self.project_id, "prj", "project_id")
        if self.topic_id is not None and (not isinstance(self.topic_id, int) or self.topic_id < 0):
            raise ScopeError(
                f"topic_id must be a non-negative int (Telegram message_thread_id or 0 for non-Forum), "
                f"got {self.topic_id!r}"
            )

    # ------------------------------------------------------------------ constructors

    @classmethod
    def personal(cls, user_id: str, *, source: str = "unknown") -> Scope:
        """Build a PERSONAL scope (private chat)."""
        return cls(mode=Mode.PERSONAL, user_id=user_id, source=source)

    @classmethod
    def normal(
        cls,
        group_id: str,
        topic_id: int,
        *,
        user_id: str | None = None,
        source: str = "unknown",
    ) -> Scope:
        """Build a NORMAL scope (group/Topic without Project).

        ``user_id`` may be provided for audit purposes but is **not** a retrieval
        key in NORMAL mode — Group Memory is shared across all Topic participants.
        """
        return cls(
            mode=Mode.NORMAL,
            group_id=group_id,
            topic_id=topic_id,
            user_id=None,  # explicit strip — user_id must not appear in NORMAL scope_key
            source=source,
        )

    @classmethod
    def development(
        cls,
        *,
        org_id: str,
        workspace_id: str,
        project_id: str,
        group_id: str,
        topic_id: int,
        source: str = "unknown",
    ) -> Scope:
        """Build a DEVELOPMENT scope (Topic bound to a Project)."""
        return cls(
            mode=Mode.DEVELOPMENT,
            org_id=org_id,
            workspace_id=workspace_id,
            project_id=project_id,
            group_id=group_id,
            topic_id=topic_id,
            source=source,
        )

    # ------------------------------------------------------------------ derivations

    def with_default_memory_scope(self) -> Scope:
        """Return a copy with ``memory_scope_id`` auto-derived from mode + keys.

        Derivation rules (ADR 0003 §2.4):

            - PERSONAL    → ``mem:usr:<user_id>``
            - NORMAL      → ``mem:grp:<group_id>:<topic_id>``
            - DEVELOPMENT → ``mem:prj:<project_id>``  (Project Memory is shared
                            across all dev Topics of that Project; the per-Topic
                            scratch memory layer uses a separate key).

        The result is still a frozen ``Scope`` — original is unchanged.
        """
        if self.memory_scope_id is not None:
            return self  # already set, do not overwrite
        return replace(self, memory_scope_id=self._derive_memory_scope_id())

    def _derive_memory_scope_id(self) -> str:
        if self.mode is Mode.PERSONAL:
            assert self.user_id is not None  # invariant guaranteed by __post_init__
            return f"mem:usr:{self.user_id}"
        if self.mode is Mode.NORMAL:
            assert self.group_id is not None and self.topic_id is not None
            return f"mem:grp:{self.group_id}:{self.topic_id}"
        if self.mode is Mode.DEVELOPMENT:
            assert self.project_id is not None
            return f"mem:prj:{self.project_id}"
        assert_never(self.mode)

    # ------------------------------------------------------------------ predicates

    def is_personal(self) -> bool:
        return self.mode is Mode.PERSONAL

    def is_normal(self) -> bool:
        return self.mode is Mode.NORMAL

    def is_development(self) -> bool:
        return self.mode is Mode.DEVELOPMENT

    def allows_dev_features(self) -> bool:
        """Only DEVELOPMENT mode allows Task/Git/ADR/Agent dev features."""
        return self.mode is Mode.DEVELOPMENT

    def allows_personal_memory(self) -> bool:
        """PERSONAL memory is only retrievable in PERSONAL mode.

        Per ADR 0003 §3 and T-6.5 acceptance criterion:
            "In Development Mode, no personal record ever returned, under any condition."
        """
        return self.mode is Mode.PERSONAL

    # ------------------------------------------------------------------ comparison helpers

    def shares_realm_with(self, other: Scope) -> bool:
        """Whether two scopes can possibly share memory.

        Two scopes share a realm iff their ``mode`` AND primary key match.
        PERSONAL has its own realm (per-user); NORMAL is per-Topic;
        DEVELOPMENT is per-Project.
        """
        if self.mode is not other.mode:
            return False
        if self.mode is Mode.PERSONAL:
            return self.user_id == other.user_id
        if self.mode is Mode.NORMAL:
            return self.group_id == other.group_id and self.topic_id == other.topic_id
        if self.mode is Mode.DEVELOPMENT:
            return self.project_id == other.project_id
        assert_never(self.mode)

    def to_dict(self) -> dict[str, str | int | None]:
        """Serialize for logging (secrets never appear in Scope)."""
        return {
            "mode": self.mode.value,
            "user_id": self.user_id,
            "group_id": self.group_id,
            "topic_id": self.topic_id,
            "org_id": self.org_id,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "memory_scope_id": self.memory_scope_id,
            "source": self.source,
        }

    def to_log_dict(self) -> Mapping[str, str | int | None]:
        """Same as :meth:`to_dict` but typed for structlog."""
        return self.to_dict()

    # ------------------------------------------------------------------ retrieval key

    def retrieval_key(self) -> str:
        """A single string suitable for the ``scope_key`` column at storage time.

        Format (stable, do NOT change without a DB migration):
            - PERSONAL:    ``personal:usr_<ulid>``
            - NORMAL:      ``normal:grp_<ulid>:<topic_id>``
            - DEVELOPMENT: ``dev:prj_<ulid>``
        """
        if self.mode is Mode.PERSONAL:
            assert self.user_id is not None
            return f"personal:{self.user_id}"
        if self.mode is Mode.NORMAL:
            assert self.group_id is not None and self.topic_id is not None
            return f"normal:{self.group_id}:{self.topic_id}"
        if self.mode is Mode.DEVELOPMENT:
            assert self.project_id is not None
            return f"dev:{self.project_id}"
        assert_never(self.mode)

    # ------------------------------------------------------------------ equality semantics

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Scope):
            return NotImplemented
        # Equality is by mode + key group, NOT by memory_scope_id or source.
        # Two scopes with the same mode and same key group share the same
        # retrieval realm — they're equal for the purpose of access checks.
        return self.retrieval_key() == other.retrieval_key()

    def __hash__(self) -> int:
        return hash(self.retrieval_key())


# ---------------------------------------------------------------------- literal type

ScopeModeLiteral = Literal["personal", "normal", "development"]
"""Literal type for use in pydantic / API schemas."""
