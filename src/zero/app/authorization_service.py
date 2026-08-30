"""Authorization service — the central decision path.

Per ``zero-control-plane-trust`` §"Authorization is a domain decision":
A centralized decision path does not require one giant authorization
class. It means every protected route converges on the same domain
policy instead of duplicating partial checks in controllers, bots,
and UI components.

This service is the single place where the role→permission matrix is
applied. HTTP handlers, future Telegram adapters, and internal
services all call :meth:`AuthorizationService.authorize` (or
:meth:`require_permission`) before performing any protected operation.

Per ``zero-control-plane-trust`` §"UI controls are not security": UI
visibility is a usability concern, not a security control. The
decision here is authoritative.
"""

from __future__ import annotations


from zero.app.clock import now_utc_iso
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.authorization import (
    AuthorizationDecision,
    AuthorizationError,
    Permission,
    role_has_permission,
)
from zero.domain.identity import (
    ProjectId,
    UserId,
    UserNotFoundError,
)
from zero.domain.ids import generate_audit_event_id
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.identity_repository import (
    IdentityRepository,
)


class AuthorizationService:
    """Central authorization decision path.

    The service combines:

    - authenticated actor (UserId);
    - project membership and ownership;
    - operation type (Permission);
    - target project (ProjectId).

    Future milestones will add: source interface when policy cares
    about it, agent role/type and delegated capability, plan or
    execution revision, explicit limits such as budget or rate.
    """

    def __init__(
        self,
        identity_repo: IdentityRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._identity_repo = identity_repo
        self._audit_repo = audit_repo

    def authorize(
        self,
        *,
        actor_id: UserId,
        project_id: ProjectId,
        permission: Permission,
        source: AuditSource = "system",
    ) -> AuthorizationDecision:
        """Decide whether ``actor_id`` may perform ``permission`` on
        ``project_id``.

        Returns an :class:`AuthorizationDecision` with ``allowed=True``
        or ``allowed=False`` plus a typed reason. Denied decisions are
        also recorded as audit events so the denial is observable.

        Per ``zero-control-plane-trust`` §"Failure shapes teach the
        boundary": denied authorization is a typed domain outcome, not
        a generic exception.
        """
        # Verify the actor exists (raises UserNotFoundError).
        try:
            self._identity_repo.get_user(actor_id)
        except UserNotFoundError:
            decision = AuthorizationDecision.deny(
                actor_id=None,
                project_id=project_id,
                permission=permission,
                role=None,
                reason="user_not_found",
            )
            self._audit_denial(decision, source)
            return decision

        # Resolve the actor's scope in the project.
        scope = self._identity_repo.resolve_scope(project_id, actor_id)

        if not scope.is_member:
            decision = AuthorizationDecision.deny(
                actor_id=actor_id,
                project_id=project_id,
                permission=permission,
                role=None,
                reason="not_member",
            )
            self._audit_denial(decision, source)
            return decision

        assert scope.role is not None  # for type checker
        if not role_has_permission(scope.role, permission):
            decision = AuthorizationDecision.deny(
                actor_id=actor_id,
                project_id=project_id,
                permission=permission,
                role=scope.role,
                reason="permission_denied",
            )
            self._audit_denial(decision, source)
            return decision

        return AuthorizationDecision.allow(
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,
            role=scope.role,
        )

    def require_permission(
        self,
        *,
        actor_id: UserId,
        project_id: ProjectId,
        permission: Permission,
        source: AuditSource = "system",
    ) -> AuthorizationDecision:
        """Authorize and raise :class:`AuthorizationError` if denied.

        Convenience for call sites that want exceptions rather than
        branching on ``decision.allowed``.
        """
        decision = self.authorize(
            actor_id=actor_id,
            project_id=project_id,
            permission=permission,
            source=source,
        )
        if not decision.allowed:
            raise AuthorizationError(decision)
        return decision

    def _audit_denial(
        self,
        decision: AuthorizationDecision,
        source: AuditSource,
    ) -> None:
        """Record a denied authorization as an audit event.

        Per ``zero-control-plane-trust`` §"Audit is evidence, not a
        transcript dump": the event carries stable identifiers and a
        compact reason, not raw payloads.
        """
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=decision.project_id,
                actor_id=decision.actor_id,
                source=source,
                operation=f"authz.{decision.permission}",
                target_type="permission",
                target_id=decision.permission,
                result="denied",
                redacted_summary=(
                    f"Denied {decision.permission} for "
                    f"actor={decision.actor_id} reason={decision.reason}"
                ),
                created_at=now_utc_iso(),
            )
        )
