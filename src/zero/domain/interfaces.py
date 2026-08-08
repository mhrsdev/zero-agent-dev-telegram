"""Interface adapter domain types.

Per ``zero-interface-adapter-model`` SKILL.md:

- Zero has one product and multiple interfaces. The website, Telegram,
  Discord, and future clients express the same backend capabilities in
  different interaction shapes.
- An adapter translates transport facts into a canonical event and
  translates a canonical result back into transport-specific presentation.
- Business decisions remain in the control plane.
- One canonical event envelope: different transports describe the same
  human action differently. A useful canonical envelope carries the
  facts the control plane needs without treating transport payloads as
  trusted domain state.
- Transport identity is evidence for linking: Telegram and Discord user
  IDs are stable platform identifiers, but they are not Zero User IDs.
- Interface scope is narrower than project membership: a project member
  may still be outside an enabled messaging scope.
- Messaging interactions are short-lived views of durable state.
- Idempotency belongs at both transport and domain boundaries.
- Fast acknowledgement and durable processing are different outcomes.
- UI controls carry opaque references, not authority.
- Platform details remain in the adapter.

Per TELEGRAM_FINDINGS:
- update_id is a transport idempotency key; domain dedup is separate.
- Webhook success means accepted delivery, not completed domain work.
- Callback payloads are compact, replayable client data.
- Callback queries need prompt acknowledgement.
- Telegram IDs require wide integer handling (stored as TEXT).
- Forum topics are real interface scopes.
- Telegram General is NOT enabled by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.identity import ProjectId, UserId
from zero.domain.plans import PlanId

#: Prefixes for stable server-issued IDs.
INTERFACE_BINDING_ID_PREFIX = "ib_"
INTERFACE_EVENT_ID_PREFIX = "iev_"
CALLBACK_TOKEN_ID_PREFIX = "ct_"

# ----------------------------------------------------------------------
# Platform and scope types
# ----------------------------------------------------------------------

Platform = Literal["telegram", "discord", "other"]

EventKind = Literal["message", "callback_query", "command", "other"]

ProcessingResult = Literal[
    "processed",
    "ignored_unlinked",
    "ignored_disabled",
    "denied",
    "error",
]

CallbackAction = Literal["approve", "reject", "edit"]


# ----------------------------------------------------------------------
# Stable IDs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceBindingId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("InterfaceBindingId must be a non-empty string")
        if not self.value.startswith(INTERFACE_BINDING_ID_PREFIX):
            raise ValueError(
                f"InterfaceBindingId must start with "
                f"{INTERFACE_BINDING_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class InterfaceEventId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("InterfaceEventId must be a non-empty string")
        if not self.value.startswith(INTERFACE_EVENT_ID_PREFIX):
            raise ValueError(
                f"InterfaceEventId must start with "
                f"{INTERFACE_EVENT_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class CallbackTokenId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("CallbackTokenId must be a non-empty string")
        if not self.value.startswith(CALLBACK_TOKEN_ID_PREFIX):
            raise ValueError(
                f"CallbackTokenId must start with "
                f"{CALLBACK_TOKEN_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ----------------------------------------------------------------------
# Interface binding (scope configuration)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceBinding:
    """A project/channel/topic scope configuration.

    Per ``zero-interface-adapter-model`` §"Interface scope is narrower
    than project membership": the owner selects which project/channel/
    topic scopes are enabled. A project member may still be outside an
    enabled messaging scope.

    Per TELEGRAM_FINDINGS §9: forum topics are real interface scopes.
    Per TELEGRAM_FINDINGS §10: Telegram General is NOT enabled by
    default.

    Attributes:
        id: stable server-issued ID.
        project_id: the project this binding connects to.
        platform: telegram, discord, or other.
        bot_token_ref: reference to the bot token secret (not the raw token).
        chat_id: the chat/channel ID (as text for 64-bit safety).
        topic_id: optional topic/thread ID. None means no topic.
        is_enabled: whether Zero is active in this scope.
        created_by: the user who created this binding.
        created_at: ISO-8601 timestamp.
        updated_at: ISO-8601 timestamp.
    """

    id: InterfaceBindingId
    project_id: ProjectId
    platform: Platform
    bot_token_ref: str | None
    chat_id: str
    topic_id: str | None
    is_enabled: bool
    created_by: UserId
    created_at: str = ""
    updated_at: str = ""


# ----------------------------------------------------------------------
# Normalized event (canonical envelope)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedEvent:
    """A canonical event envelope from a messaging platform.

    Per ``zero-interface-adapter-model`` §"One canonical event envelope":
    different transports describe the same human action differently. A
    useful canonical envelope carries the facts the control plane needs
    without treating transport payloads as trusted domain state.

    Attributes:
        platform: the source platform.
        external_event_id: transport idempotency key (e.g. update_id).
        external_actor_id: the platform's user ID (as text).
        chat_id: the chat/channel ID.
        topic_id: optional topic/thread ID.
        event_kind: message, callback_query, command, other.
        content: the event's text content (redacted for storage).
        callback_token: for callback_query events, the opaque token.
    """

    platform: Platform
    external_event_id: str
    external_actor_id: str
    chat_id: str
    topic_id: str | None
    event_kind: EventKind
    content: str
    callback_token: str | None = None


# ----------------------------------------------------------------------
# Interface event log entry
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class InterfaceEventLogEntry:
    """A log entry for an interface event.

    Per ``zero-interface-adapter-model`` §"Idempotency belongs at both
    transport and domain boundaries": transport event IDs suppress
    duplicate ingestion, while domain idempotency keys suppress
    duplicate transitions.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (None for unlinked/disabled events).
        platform: the source platform.
        external_event_id: transport idempotency key.
        external_actor_id: the platform's user ID.
        resolved_user_id: the Zero User ID this event was resolved to.
        chat_id: the chat/channel ID.
        topic_id: the topic/thread ID.
        event_kind: message, callback_query, command, other.
        event_content: redacted summary of the event content.
        processing_result: processed, ignored_unlinked, ignored_disabled,
            denied, error.
        processing_detail: optional detail.
        created_at: ISO-8601 timestamp.
    """

    id: InterfaceEventId
    project_id: ProjectId | None
    platform: Platform
    external_event_id: str
    external_actor_id: str | None
    resolved_user_id: UserId | None
    chat_id: str | None
    topic_id: str | None
    event_kind: EventKind
    event_content: str | None
    processing_result: ProcessingResult
    processing_detail: str | None
    created_at: str = ""


# ----------------------------------------------------------------------
# Callback token
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CallbackToken:
    """An opaque action token for inline keyboard callbacks.

    Per ``zero-interface-adapter-model`` §"UI controls carry opaque
    references, not authority": a callback should carry a compact opaque
    reference or bounded action identity — not trusted role, ownership,
    or full mutable state.

    Per TELEGRAM_FINDINGS §11: callback_data is limited to 1–64 bytes.
    We store the full token in the database and pass a short opaque ID
    as callback_data.

    Attributes:
        id: stable server-issued ID.
        project_id: the project.
        plan_id: the plan this callback refers to.
        revision_number: the specific revision.
        action: approve, reject, or edit.
        expires_at: when this token expires.
        used_at: when this token was used (None if unused).
        created_by: the user who triggered the plan proposal.
        created_at: ISO-8601 timestamp.
    """

    id: CallbackTokenId
    project_id: ProjectId
    plan_id: PlanId
    revision_number: int
    action: CallbackAction
    expires_at: str
    used_at: str | None
    created_by: UserId | None
    created_at: str = ""

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if the token has expired. The caller must pass the
        current time for testability."""
        return False  # checked by the service with a datetime parameter


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class InterfaceError(RuntimeError):
    """Base class for interface-adapter-domain typed failures."""


class InterfaceBindingNotFoundError(InterfaceError):
    pass


class InterfaceScopeDisabledError(InterfaceError):
    """The interface scope is not enabled.

    Per PLAN.md M13: "Disabled topics/channels produce no planning or
    execution side effects."
    """


class UnlinkedUserError(InterfaceError):
    """The external user is not linked to a Zero User.

    Per PLAN.md M13: "Unknown and unlinked users cannot act."
    """


class DuplicateEventError(InterfaceError):
    """A duplicate event was delivered.

    Per PLAN.md M13: "Duplicate webhook/update delivery is idempotent."
    This error is raised so the caller knows the event was already
    processed, but the operation is not a failure.
    """


class CallbackTokenExpiredError(InterfaceError):
    """The callback token has expired."""


class CallbackTokenUsedError(InterfaceError):
    """The callback token has already been used."""


class CallbackTokenNotFoundError(InterfaceError):
    pass


class StaleCallbackError(InterfaceError):
    """A callback for an old revision was used against a newer revision.

    Per PLAN.md M13: "Edited or stale approval messages cannot approve
    a newer revision."
    """

    def __init__(
        self,
        message: str,
        *,
        callback_revision: int,
        current_revision: int,
    ) -> None:
        super().__init__(message)
        self.callback_revision = callback_revision
        self.current_revision = current_revision
