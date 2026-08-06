"""Zero v2 TopicBinding — ADR T-4.4, T-4.5, T-4.6.

Central data model. Each Topic has at most one binding with
``mode ∈ {normal, dev, disabled}``.

Schema (T-4.4 acceptance):
    - group_id, topic_id, mode, memory_scope_id, project_id?,
      configured_by, configured_at, status

DB constraints (already in sqlite_backend.py normal_topic_bindings):
    - mode='dev' requires project_id (CHECK)
    - mode ∈ {'normal', 'disabled'} forbids project_id (CHECK)
    - mode and memory_scope_id are independent fields
    - At most one active binding per (group_id, topic_id) (PK)

Mode detection (T-4.6) is **deterministic, no LLM**:
    1. Private chat → PERSONAL (always)
    2. Group → look up TopicBinding by (group_id, topic_id)
    3. No binding → GroupPolicy.default_unconfigured_topic_mode
    4. `disabled` → complete silence
    5. Mode visible via /status command
    6. Mode switch = explicit admin action
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from zero.core.scope import Mode, Scope

__all__ = [
    "BindingStatus",
    "GroupPolicy",
    "GroupPolicyStore",
    "ModeResolutionResult",
    "TopicBinding",
    "TopicBindingStore",
    "resolve_mode",
]


ModeValue = Literal["normal", "dev", "disabled"]
"""The three values allowed in TopicBinding.mode."""


class BindingStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------- TopicBinding

@dataclass(slots=True)
class TopicBinding:
    """A single Topic → Mode binding.

    The schema field ``mode`` accepts three values: ``normal``, ``dev``,
    ``disabled``. Note that ``personal`` is NOT a valid TopicBinding.mode —
    personal mode is implicit for private chats, never for groups.
    """

    group_id: str
    topic_id: int  # 0 for non-Forum groups
    mode: ModeValue
    memory_scope_id: str
    configured_by: str  # user_id
    project_id: str | None = None  # required iff mode='dev'
    status: BindingStatus = BindingStatus.ACTIVE
    configured_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = field(default_factory=lambda: f"tb_{uuid.uuid4().hex[:16]}")

    def __post_init__(self) -> None:
        # Enforce ADR 0003 §1 + T-4.4 constraints.
        if self.mode not in ("normal", "dev", "disabled"):
            raise ValueError(
                f"TopicBinding.mode must be normal/dev/disabled, got {self.mode!r}"
            )
        if self.mode == "dev" and self.project_id is None:
            raise ValueError(
                "TopicBinding.mode='dev' requires project_id — "
                "set project_id to a valid prj_<ulid>"
            )
        if self.mode in ("normal", "disabled") and self.project_id is not None:
            raise ValueError(
                f"TopicBinding.mode={self.mode!r} forbids project_id — "
                "switch to mode='dev' to bind a Project"
            )

    @property
    def is_active(self) -> bool:
        return self.status is BindingStatus.ACTIVE

    def to_log_dict(self) -> dict[str, str | int | None]:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "topic_id": self.topic_id,
            "mode": self.mode,
            "memory_scope_id": self.memory_scope_id,
            "project_id": self.project_id,
            "status": self.status.value,
            "configured_by": self.configured_by,
        }


# ---------------------------------------------------------------------- GroupPolicy

@dataclass(slots=True)
class GroupPolicy:
    """Per-group policy for unbound topics.

    Per ADR T-4.5:
        - ``default_unconfigured_topic_mode`` per group (default: ``normal``)
        - Only admins can change
        - Explicit binding always wins over policy
    """

    group_id: str
    default_unconfigured_topic_mode: Literal["normal", "disabled"] = "normal"
    forum_activation_suggestion_enabled: bool = True  # P2
    forum_activation_suggestion_max_per_30d: int = 1   # P2

    def to_log_dict(self) -> dict[str, str]:
        return {
            "group_id": self.group_id,
            "default_unconfigured_topic_mode": self.default_unconfigured_topic_mode,
        }


# ---------------------------------------------------------------------- stores

class TopicBindingStore:
    """In-memory TopicBinding store (base class).

    For production use, prefer :class:`zero.telegram.db_stores.DbTopicBindingStore`
    which persists to the ``normal_topic_bindings`` table.
    """

    def __init__(self) -> None:
        # Key: (group_id, topic_id) → TopicBinding
        self._bindings: dict[tuple[str, int], TopicBinding] = {}

    def upsert(self, binding: TopicBinding) -> TopicBinding:
        """Insert or replace a binding. At most one active per (group_id, topic_id)."""
        key = (binding.group_id, binding.topic_id)
        self._bindings[key] = binding
        return binding

    def get(self, group_id: str, topic_id: int) -> TopicBinding | None:
        binding = self._bindings.get((group_id, topic_id))
        if binding is None or not binding.is_active:
            return None
        return binding

    def list_for_group(self, group_id: str) -> list[TopicBinding]:
        return [b for (gid, _), b in self._bindings.items() if gid == group_id and b.is_active]

    def archive(self, group_id: str, topic_id: int) -> bool:
        b = self._bindings.get((group_id, topic_id))
        if b is None:
            return False
        b.status = BindingStatus.ARCHIVED
        return True


class GroupPolicyStore:
    """In-memory GroupPolicy store with sensible defaults."""

    def __init__(self) -> None:
        self._policies: dict[str, GroupPolicy] = {}

    def get_or_default(self, group_id: str) -> GroupPolicy:
        if group_id not in self._policies:
            self._policies[group_id] = GroupPolicy(group_id=group_id)
        return self._policies[group_id]

    def set(self, policy: GroupPolicy) -> GroupPolicy:
        self._policies[policy.group_id] = policy
        return policy


# ---------------------------------------------------------------------- mode resolution

@dataclass(frozen=True, slots=True)
class ModeResolutionResult:
    """Result of mode resolution for an incoming message.

    Carries the resolved Mode AND the Scope (with memory_scope_id pre-set).
    """

    mode: Mode
    scope: Scope
    binding: TopicBinding | None = None  # None for PERSONAL or unbound NORMAL
    policy_applied: bool = False  # True if mode came from GroupPolicy (no binding)
    silenced: bool = False  # True if mode=disabled → complete silence


def resolve_mode(
    *,
    is_private: bool,
    user_id: str,
    group_id: str | None = None,
    topic_id: int | None = None,
    binding_store: TopicBindingStore,
    policy_store: GroupPolicyStore,
) -> ModeResolutionResult:
    """Resolve the Mode for an incoming Telegram message.

    Per ADR 0003 §1 + T-4.6:

        1. Private chat → PERSONAL (always; no policy lookup).
        2. Group → look up TopicBinding by (group_id, topic_id).
        3. No binding → GroupPolicy.default_unconfigured_topic_mode.
        4. mode='disabled' → silenced=True.
        5. Non-Forum group → topic_id=0, treated like any other topic.

    Returns a :class:`ModeResolutionResult` with the resolved Scope (including
    auto-derived memory_scope_id).

    **This function MUST be deterministic.** No LLM, no content inspection.
    """
    if is_private:
        scope = Scope.personal(user_id=user_id).with_default_memory_scope()
        return ModeResolutionResult(
            mode=Mode.PERSONAL,
            scope=scope,
            binding=None,
            policy_applied=False,
            silenced=False,
        )

    # Group chat — group_id and topic_id required.
    if group_id is None or topic_id is None:
        raise ValueError(
            "group_id and topic_id are required for non-private chats "
            "(use topic_id=0 for non-Forum groups)"
        )

    binding = binding_store.get(group_id, topic_id)

    if binding is not None:
        if binding.mode == "disabled":
            # Complete silence — return a scope that won't be acted on.
            scope = Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
            return ModeResolutionResult(
                mode=Mode.NORMAL,  # silenced mode uses NORMAL as default scope type
                scope=scope,
                binding=binding,
                policy_applied=False,
                silenced=True,
            )
        if binding.mode == "normal":
            scope = Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
            return ModeResolutionResult(
                mode=Mode.NORMAL,
                scope=scope,
                binding=binding,
                policy_applied=False,
                silenced=False,
            )
        if binding.mode == "dev":
            # DEVELOPMENT requires org/workspace/project.
            # The binding has project_id; we construct a scope with the project_id.
            # The caller (TelegramBot) should use resolve_mode_with_db() from
            # zero.telegram.db_stores to look up the real org_id/workspace_id
            # from the dev_projects table.
            # If no DB is available, we use derived identifiers that are stable
            # and traceable to the project_id.
            scope = Scope.development(
                org_id=f"org_for_{binding.project_id}",
                workspace_id=f"ws_for_{binding.project_id}",
                project_id=binding.project_id,  # type: ignore[arg-type]
                group_id=group_id,
                topic_id=topic_id,
            ).with_default_memory_scope()
            return ModeResolutionResult(
                mode=Mode.DEVELOPMENT,
                scope=scope,
                binding=binding,
                policy_applied=False,
                silenced=False,
            )
        # Unreachable due to TopicBinding.__post_init__ validation.
        raise ValueError(f"unknown binding mode {binding.mode!r}")

    # No binding — use GroupPolicy.
    policy = policy_store.get_or_default(group_id)
    if policy.default_unconfigured_topic_mode == "disabled":
        scope = Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
        return ModeResolutionResult(
            mode=Mode.NORMAL,  # silenced by policy; NORMAL is the scope type
            scope=scope,
            binding=None,
            policy_applied=True,
            silenced=True,
        )
    # Default: NORMAL.
    scope = Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
    return ModeResolutionResult(
        mode=Mode.NORMAL,
        scope=scope,
        binding=None,
        policy_applied=True,
        silenced=False,
    )
