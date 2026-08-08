"""Interface adapter service — scope management, event normalization,
plan presentation, callback processing.

Per ``zero-interface-adapter-model`` SKILL.md:

- Zero has one product and multiple interfaces. The website, Telegram,
  Discord, and future clients express the same backend capabilities in
  different interaction shapes.
- An adapter translates transport facts into a canonical event and
  translates a canonical result back into transport-specific presentation.
- Business decisions remain in the control plane.
- Interface scope is narrower than project membership.
- Messaging interactions are short-lived views of durable state.
- Idempotency belongs at both transport and domain boundaries.
- Fast acknowledgement and durable processing are different outcomes.
- UI controls carry opaque references, not authority.

Per PLAN.md M13 invariants:
- External IDs map to stable Zero User IDs through a verified link.
- Owner selects enabled project/channel/topic scopes.
- Telegram General and unrelated topics are not enabled by default.
- Normal conversation does not become execution.
- Approval actions use the same plan revision and authorization rules.
- Adapter-local storage is not authoritative project state.

Per PLAN.md M13 validation:
- Unknown and unlinked users cannot act.
- Disabled topics/channels produce no planning or execution side effects.
- Duplicate webhook/update delivery is idempotent.
- Edited or stale approval messages cannot approve a newer revision.
- Website and messaging actions observe the same durable state.
- Platform outage does not lose backend execution state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zero.app.authorization_service import AuthorizationService
from zero.app.plan_service import PlanService
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.identity import ProjectId, UserId
from zero.domain.ids import (
    generate_audit_event_id,
    generate_callback_token_id,
    generate_interface_binding_id,
    generate_interface_event_id,
)
from zero.domain.interfaces import (
    CallbackAction,
    CallbackToken,
    CallbackTokenId,
    CallbackTokenNotFoundError,
    InterfaceBinding,
    InterfaceBindingId,
    InterfaceEventId,
    InterfaceEventLogEntry,
    NormalizedEvent,
    Platform,
)
from zero.domain.plans import (
    PlanId,
    StaleRevisionError,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.identity_repository import IdentityRepository
from zero.persistence.repositories.interface_repository import (
    InterfaceRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class InterfaceAdapterService:
    """Application operations for messaging platform adapters.

    The service:
    - manages interface bindings (scope configuration);
    - normalizes inbound events into canonical envelopes;
    - resolves external identities to Zero Users;
    - routes messages to conversation event intake;
    - creates callback tokens for plan approval;
    - processes callbacks (approve/reject/edit) with stale revision
      defense;
    - logs all events for idempotent processing.
    """

    def __init__(
        self,
        interface_repo: InterfaceRepository,
        audit_repo: AuditRepository,
        plan_service: PlanService,
        authorization_service: AuthorizationService,
        identity_repo: IdentityRepository,
    ) -> None:
        self._repo = interface_repo
        self._audit_repo = audit_repo
        self._plan_service = plan_service
        self._authz = authorization_service
        self._identity_repo = identity_repo

    # ------------------------------------------------------------------
    # Scope management
    # ------------------------------------------------------------------

    def create_binding(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        platform: Platform,
        chat_id: str,
        topic_id: str | None = None,
        bot_token_ref: str | None = None,
        is_enabled: bool = False,
        source: AuditSource = "web",
    ) -> InterfaceBinding:
        """Create an interface binding (scope configuration).

        Per TELEGRAM_FINDINGS §10: Telegram General is NOT enabled by
        default. The owner must explicitly enable each scope.

        Per ``zero-interface-adapter-model`` §"Interface scope is
        narrower than project membership": the owner selects which
        scopes are enabled.
        """
        # Authorize: only owners can manage interface bindings.
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        # Check for an existing binding.
        existing = self._repo.get_binding(platform, chat_id, topic_id)
        if existing is not None:
            return existing
        binding = InterfaceBinding(
            id=InterfaceBindingId(generate_interface_binding_id()),
            project_id=project_id,
            platform=platform,
            bot_token_ref=bot_token_ref,
            chat_id=str(chat_id),
            topic_id=str(topic_id) if topic_id is not None else None,
            is_enabled=is_enabled,
            created_by=actor_id,
            created_at=_now_utc_iso(),
            updated_at=_now_utc_iso(),
        )
        self._repo.insert_binding(binding)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project_id,
                actor_id=actor_id,
                source=source,
                operation="interface.binding.create",
                target_type="interface_binding",
                target_id=binding.id.value,
                result="success",
                redacted_summary=(
                    f"Created {platform} binding for chat {chat_id}"
                    + (f" topic {topic_id}" if topic_id else "")
                    + f" (enabled={is_enabled})"
                ),
                created_at=_now_utc_iso(),
            )
        )
        return binding

    def enable_binding(
        self,
        *,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> InterfaceBinding:
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        self._repo.update_binding_enabled(binding_id, True)
        return self._repo.get_binding_by_id(project_id, binding_id)

    def disable_binding(
        self,
        *,
        project_id: ProjectId,
        binding_id: InterfaceBindingId,
        actor_id: UserId,
        source: AuditSource = "web",
    ) -> InterfaceBinding:
        """Disable an interface binding.

        Per PLAN.md M13 checkpoint: "Disable the adapter without
        modifying canonical project state."
        """
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="agent.manage",
            source=source,
        )
        self._repo.update_binding_enabled(binding_id, False)
        return self._repo.get_binding_by_id(project_id, binding_id)

    def get_binding(
        self,
        platform: Platform,
        chat_id: str,
        topic_id: str | None,
    ) -> InterfaceBinding | None:
        return self._repo.get_binding(platform, chat_id, topic_id)

    def list_bindings(
        self, project_id: ProjectId
    ) -> list[InterfaceBinding]:
        return self._repo.list_bindings_for_project(project_id)

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def process_inbound_event(
        self,
        event: NormalizedEvent,
    ) -> InterfaceEventLogEntry:
        """Process an inbound event from a messaging platform.

        Per ``zero-interface-adapter-model`` §"One canonical event
        envelope": the adapter normalizes the transport-specific event
        into a canonical envelope, then this service processes it.

        Per PLAN.md M13 validation:
        - Unknown and unlinked users cannot act.
        - Disabled topics/channels produce no planning or execution
          side effects.
        - Duplicate webhook/update delivery is idempotent.

        Returns the event log entry recording the processing result.
        """
        # 1. Check for duplicate delivery (transport idempotency).
        if self._repo.event_already_processed(
            event.platform, event.external_event_id
        ):
            # Idempotent: return a synthetic "already processed" entry.
            return InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=None,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=None,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content="[duplicate event]",
                processing_result="processed",
                processing_detail="duplicate delivery ignored",
                created_at=_now_utc_iso(),
            )

        # 2. Resolve the interface binding (scope check).
        binding = self._repo.get_binding(
            event.platform, event.chat_id, event.topic_id
        )
        if binding is None or not binding.is_enabled:
            # Disabled or unbound scope: no side effects.
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id if binding else None,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=None,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content=event.content[:200],  # redacted
                processing_result="ignored_disabled",
                processing_detail="scope not enabled",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # 3. Resolve the external identity to a Zero User.
        try:
            identity = self._identity_repo.require_verified_external_identity(
                event.platform,
                event.external_actor_id,
            )
            resolved_user_id = identity.user_id
        except Exception:
            # Unlinked user: cannot act.
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=None,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content=event.content[:200],
                processing_result="ignored_unlinked",
                processing_detail="external identity not linked or not verified",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # 4. Process based on event kind.
        if event.event_kind == "callback_query":
            # Process a callback (approve/reject/edit).
            return self._process_callback(
                binding=binding,
                event=event,
                user_id=resolved_user_id,
            )
        elif event.event_kind == "message":
            # Ingest as a conversation event.
            return self._process_message(
                binding=binding,
                event=event,
                user_id=resolved_user_id,
            )
        else:
            # Other event kinds: log and ignore.
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=resolved_user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content=event.content[:200],
                processing_result="processed",
                processing_detail=f"event kind {event.event_kind} logged",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

    def _process_message(
        self,
        *,
        binding: InterfaceBinding,
        event: NormalizedEvent,
        user_id: UserId,
    ) -> InterfaceEventLogEntry:
        """Process a message event: ingest as a conversation event.

        Per PLAN.md M13: "Normal conversation does not become execution."
        The message is ingested as a conversation event; it does NOT
        trigger plan creation or execution.
        """
        # Ingest the conversation event.
        conv_event = self._plan_service.ingest_conversation_event(
            project_id=binding.project_id,
            actor_id=user_id,
            source=binding.platform,  # type: ignore[arg-type]
            origin_kind="authenticated_human",
            content=event.content,
            external_event_id=event.external_event_id,
        )
        entry = InterfaceEventLogEntry(
            id=InterfaceEventId(generate_interface_event_id()),
            project_id=binding.project_id,
            platform=event.platform,
            external_event_id=event.external_event_id,
            external_actor_id=event.external_actor_id,
            resolved_user_id=user_id,
            chat_id=event.chat_id,
            topic_id=event.topic_id,
            event_kind=event.event_kind,
            event_content=event.content[:200],
            processing_result="processed",
            processing_detail=f"ingested as conversation event {conv_event.id.value}",
            created_at=_now_utc_iso(),
        )
        self._repo.insert_event_log(entry)
        return entry

    def _process_callback(
        self,
        *,
        binding: InterfaceBinding,
        event: NormalizedEvent,
        user_id: UserId,
    ) -> InterfaceEventLogEntry:
        """Process a callback query (approve/reject/edit).

        Per PLAN.md M13: "Edited or stale approval messages cannot
        approve a newer revision."

        Per ``zero-interface-adapter-model`` §"UI controls carry opaque
        references, not authority": the callback token is an opaque
        reference; the server still resolves current state and
        permission.
        """
        if not event.callback_token:
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content="[no callback token]",
                processing_result="error",
                processing_detail="callback query without token",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # Look up the callback token.
        token_id = CallbackTokenId(event.callback_token)
        try:
            token = self._repo.get_callback_token(token_id)
        except CallbackTokenNotFoundError:
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content="[invalid callback token]",
                processing_result="error",
                processing_detail="callback token not found",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # Check if the token is already used (idempotent).
        if token.is_used:
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content="[callback already used]",
                processing_result="processed",
                processing_detail="callback token already used (idempotent)",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # Check if the token is expired.
        now = datetime.now(UTC)
        expires = datetime.fromisoformat(
            token.expires_at
        )
        if now > expires:
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content="[callback expired]",
                processing_result="error",
                processing_detail="callback token expired",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # Check for stale revision: the callback's revision must match
        # the plan's current revision.
        plan = self._plan_service.get_plan(token.plan_id)
        if token.revision_number != plan.current_revision_number:
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content=f"[stale callback: rev {token.revision_number} vs current {plan.current_revision_number}]",
                processing_result="denied",
                processing_detail="stale callback: revision mismatch",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # Mark the token as used (atomic, idempotent).
        used = self._repo.mark_callback_token_used(token_id, _now_utc_iso())
        if not used:
            # Race condition: another process used it first.
            entry = InterfaceEventLogEntry(
                id=InterfaceEventId(generate_interface_event_id()),
                project_id=binding.project_id,
                platform=event.platform,
                external_event_id=event.external_event_id,
                external_actor_id=event.external_actor_id,
                resolved_user_id=user_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                event_kind=event.event_kind,
                event_content="[callback race: already used]",
                processing_result="processed",
                processing_detail="callback token used by another process",
                created_at=_now_utc_iso(),
            )
            self._repo.insert_event_log(entry)
            return entry

        # Perform the action (approve/reject/edit).
        idempotency_key = f"callback_{token.id.value}"
        try:
            if token.action == "approve":
                self._plan_service.approve_revision(
                    plan_id=token.plan_id,
                    actor_id=user_id,
                    expected_revision_number=token.revision_number,
                    idempotency_key=idempotency_key,
                    source=binding.platform,  # type: ignore[arg-type]
                )
            elif token.action == "reject":
                self._plan_service.reject_revision(
                    plan_id=token.plan_id,
                    actor_id=user_id,
                    expected_revision_number=token.revision_number,
                    idempotency_key=idempotency_key,
                    source=binding.platform,  # type: ignore[arg-type]
                )
            result = "processed"
            detail = f"callback {token.action} executed for revision {token.revision_number}"
        except StaleRevisionError:
            result = "denied"
            detail = "stale revision during callback"
        except Exception as exc:
            result = "error"
            detail = f"callback action failed: {type(exc).__name__}"

        entry = InterfaceEventLogEntry(
            id=InterfaceEventId(generate_interface_event_id()),
            project_id=binding.project_id,
            platform=event.platform,
            external_event_id=event.external_event_id,
            external_actor_id=event.external_actor_id,
            resolved_user_id=user_id,
            chat_id=event.chat_id,
            topic_id=event.topic_id,
            event_kind=event.event_kind,
            event_content=f"[callback {token.action}]",
            processing_result=result,  # type: ignore[arg-type]
            processing_detail=detail,
            created_at=_now_utc_iso(),
        )
        self._repo.insert_event_log(entry)
        return entry

    # ------------------------------------------------------------------
    # Callback token management
    # ------------------------------------------------------------------

    def create_callback_token(
        self,
        *,
        project_id: ProjectId,
        plan_id: PlanId,
        revision_number: int,
        action: CallbackAction,
        created_by: UserId,
        expires_in_hours: int = 24,
    ) -> CallbackToken:
        """Create a callback token for plan approval/rejection.

        Per ``zero-interface-adapter-model`` §"UI controls carry opaque
        references, not authority": the token is an opaque reference
        carrying the plan ID, revision number, and action. The server
        still resolves current state and permission when the callback
        is used.
        """
        expires_at = (
            datetime.now(UTC) + timedelta(hours=expires_in_hours)
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        token = CallbackToken(
            id=CallbackTokenId(generate_callback_token_id()),
            project_id=project_id,
            plan_id=plan_id,
            revision_number=revision_number,
            action=action,
            expires_at=expires_at,
            used_at=None,
            created_by=created_by,
            created_at=_now_utc_iso(),
        )
        self._repo.insert_callback_token(token)
        return token

    def get_callback_token(
        self, token_id: CallbackTokenId
    ) -> CallbackToken:
        return self._repo.get_callback_token(token_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_event_log(
        self,
        project_id: ProjectId,
        *,
        limit: int = 100,
    ) -> list[InterfaceEventLogEntry]:
        return self._repo.list_event_log_for_project(
            project_id, limit=limit
        )
