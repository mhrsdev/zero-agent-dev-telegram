"""Identity service — application operations for users, projects, memberships.

This service orchestrates the identity domain. It depends on the
:class:`IdentityRepository` for persistence and on
:class:`AuditService` for audit events. It is the only place where
identity mutations are allowed; HTTP handlers and future adapters
call this service rather than touching the repository directly.

Per ``zero-control-plane-trust`` §"Project scope is part of every
fact": project isolation is enforced at the service layer (every
project-scoped operation takes a ``project_id`` and the repository
filters by it) and at the database constraint layer (FK + UNIQUE
constraints).
"""

from __future__ import annotations

from datetime import UTC, datetime

from zero.app.authorization_service import AuthorizationService
from zero.domain.audit import AuditEvent, AuditEventId, AuditSource
from zero.domain.identity import (
    ExternalIdentity,
    ExternalIdentityId,
    ExternalPlatform,
    Project,
    ProjectId,
    ProjectMembership,
    ProjectRole,
    ProjectScope,
    User,
    UserId,
    UserNotFoundError,
)
from zero.domain.ids import (
    generate_audit_event_id,
    generate_external_identity_id,
    generate_project_id,
    generate_user_id,
)
from zero.persistence.repositories.audit_repository import AuditRepository
from zero.persistence.repositories.identity_repository import (
    IdentityRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class IdentityService:
    """Application operations for identity and project scope.

    The service is the only place where identity mutations happen.
    Adapters (HTTP, Telegram, etc.) call this service rather than the
    repository directly, so the trust boundary is in one place.
    """

    def __init__(
        self,
        identity_repo: IdentityRepository,
        audit_repo: AuditRepository,
        authorization: AuthorizationService,
    ) -> None:
        self._identity_repo = identity_repo
        self._audit_repo = audit_repo
        self._authorization = authorization

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(
        self,
        *,
        display_name: str,
        source: AuditSource = "system",
        commit: bool = True,
    ) -> User:
        """Create a new Zero User.

        The display name is a label only; the server-issued ID is the
        authority. Two users may have identical display names; they
        remain distinct identities.

        Per ``zero-control-plane-trust`` §"Identity is a link, not a
        name": display names are not authority.
        """
        if not display_name or not display_name.strip():
            raise ValueError("display_name must not be empty")
        user = User(
            id=UserId(generate_user_id()),
            display_name=display_name.strip(),
            status="active",
            created_at=_now_utc_iso(),
        )
        self._identity_repo.insert_user(user, commit=commit)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=None,
                actor_id=user.id,
                source=source,
                operation="user.create",
                target_type="user",
                target_id=user.id.value,
                result="success",
                redacted_summary=f"Created user {user.id.value}",
                created_at=_now_utc_iso(),
            ),
            commit=commit,
        )
        return user

    def get_user(self, user_id: UserId) -> User:
        return self._identity_repo.get_user(user_id)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(
        self,
        *,
        owner_id: UserId,
        name: str,
        source: AuditSource = "system",
    ) -> Project:
        """Create a new project with an owner.

        Per ``zero-control-plane-trust`` §"Atomicity follows the
        business fact": creating a project + creating the owner
        membership is one atomic business fact. The repository does
        both in a single transaction.
        """
        if not name or not name.strip():
            raise ValueError("project name must not be empty")
        # Verify the owner exists.
        owner = self._identity_repo.get_user(owner_id)
        project = Project(
            id=ProjectId(generate_project_id()),
            name=name.strip(),
            owner_user_id=owner.id,
            created_at=_now_utc_iso(),
        )
        owner_membership = ProjectMembership(
            project_id=project.id,
            user_id=owner.id,
            role="owner",
            created_at=project.created_at,
        )
        self._identity_repo.insert_project(project, owner_membership=owner_membership)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=project.id,
                actor_id=owner.id,
                source=source,
                operation="project.create",
                target_type="project",
                target_id=project.id.value,
                result="success",
                redacted_summary=f"Created project {project.id.value}",
                created_at=_now_utc_iso(),
            )
        )
        return project

    def get_project(self, project_id: ProjectId) -> Project:
        return self._identity_repo.get_project(project_id)

    def list_projects(self) -> list[Project]:
        """List every project (deployment-scoped, for managed workers)."""
        return self._identity_repo.list_projects()

    # ------------------------------------------------------------------
    # Memberships
    # ------------------------------------------------------------------

    def add_member(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        member_id: UserId,
        role: ProjectRole,
        source: AuditSource = "system",
    ) -> ProjectMembership:
        """Add a user to a project with the given role.

        Per ``zero-control-plane-trust`` §"Authorization is a domain
        decision": only an actor with ``member.manage`` may add members.
        """
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="member.manage",
            source=source,
        )
        # Verify project and member exist.
        project = self._identity_repo.get_project(project_id)
        member = self._identity_repo.get_user(member_id)
        membership = ProjectMembership(
            project_id=project.id,
            user_id=member.id,
            role=role,
            created_at=_now_utc_iso(),
        )
        with self._identity_repo._database.transaction():
            self._identity_repo.insert_membership(membership, commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project.id,
                    actor_id=actor_id,
                    source=source,
                    operation="member.add",
                    target_type="membership",
                    target_id=f"{project.id.value}:{member.id.value}",
                    result="success",
                    redacted_summary=(
                        f"Added user {member.id.value} to project {project.id.value} as {role}"
                    ),
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )
        return membership

    def remove_member(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        member_id: UserId,
        source: AuditSource = "system",
    ) -> None:
        """Remove a user from a project.

        The owner cannot be removed (they must transfer ownership
        first — a future milestone). This prevents a project from
        becoming orphaned.
        """
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="member.manage",
            source=source,
        )
        project = self._identity_repo.get_project(project_id)
        if project.owner_user_id == member_id:
            raise ValueError("Cannot remove the project owner; transfer ownership first.")
        with self._identity_repo._database.transaction():
            self._identity_repo.delete_membership(project.id, member_id, commit=False)
            self._audit_repo.insert(
                AuditEvent(
                    id=AuditEventId(generate_audit_event_id()),
                    project_id=project.id,
                    actor_id=actor_id,
                    source=source,
                    operation="member.remove",
                    target_type="membership",
                    target_id=f"{project.id.value}:{member_id.value}",
                    result="success",
                    redacted_summary=(
                        f"Removed user {member_id.value} from project {project.id.value}"
                    ),
                    created_at=_now_utc_iso(),
                ),
                commit=False,
            )

    def list_members(self, project_id: ProjectId, actor_id: UserId) -> list[ProjectMembership]:
        """List members of a project.

        Per ``zero-project-isolation-evidence`` §"Scope begins before
        access": the repository filters by ``project_id``; no other
        project's memberships are ever loaded.
        """
        self._authorization.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="project.view",
        )
        return self._identity_repo.list_memberships_for_project(project_id)

    def resolve_scope(self, project_id: ProjectId, actor_id: UserId) -> ProjectScope:
        """Resolve the actor's role in a project.

        This is the canonical "who is this user in this project?" call.
        It is used by :class:`AuthorizationService` and by every
        project-scoped operation.
        """
        return self._identity_repo.resolve_scope(project_id, actor_id)

    # ------------------------------------------------------------------
    # External identities
    # ------------------------------------------------------------------

    def link_external_identity(
        self,
        *,
        user_id: UserId,
        platform: ExternalPlatform,
        external_id: str,
        external_username: str | None = None,
        verified: bool = False,
        source: AuditSource = "system",
    ) -> ExternalIdentity:
        """Link an external platform identity to a Zero User.

        Per ``zero-control-plane-trust`` §"Identity is a link, not a
        name": external IDs are links, not identities by name. The
        link is recorded with ``verified=False`` by default; the
        verification ceremony (Telegram OIDC login, Discord OAuth,
        etc.) sets ``verified=True`` through
        :meth:`verify_external_identity`.

        Per ``zero-interface-adapter-model`` §"Transport identity is
        evidence for linking": a Telegram username may help the user
        recognize the account but cannot establish authority.
        """
        if not external_id or not external_id.strip():
            raise ValueError("external_id must not be empty")
        # Verify the user exists.
        user = self._identity_repo.get_user(user_id)
        identity = ExternalIdentity(
            id=ExternalIdentityId(generate_external_identity_id()),
            user_id=user.id,
            platform=platform,
            external_id=external_id.strip(),
            external_username=external_username,
            verified_at=_now_utc_iso() if verified else None,
            created_at=_now_utc_iso(),
        )
        self._identity_repo.insert_external_identity(identity)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=None,
                actor_id=user.id,
                source=source,
                operation="external_identity.link",
                target_type="external_identity",
                target_id=identity.id.value,
                result="success",
                redacted_summary=(f"Linked {platform} identity to user {user.id.value}"),
                # NOTE: external_id is intentionally NOT included in
                # the summary; it is platform-specific PII.
                created_at=_now_utc_iso(),
            )
        )
        return identity

    def verify_external_identity(
        self,
        *,
        platform: ExternalPlatform,
        external_id: str,
        source: AuditSource = "system",
    ) -> ExternalIdentity:
        """Mark an external identity link as verified.

        This is called after the platform-specific verification
        ceremony (e.g. Telegram OIDC login) has completed. Until this
        is called, the link cannot be used for authentication.
        """
        # We look up by (platform, external_id) which raises if not found.
        existing = self._identity_repo.get_external_identity(platform, external_id)
        if existing is None:
            raise UserNotFoundError(f"No external identity found for {platform}:{external_id}")
        if existing.verified_at is not None:
            # Idempotent: already verified.
            return existing
        verified_at = _now_utc_iso()
        self._identity_repo.mark_external_identity_verified(existing.id, verified_at)
        self._audit_repo.insert(
            AuditEvent(
                id=AuditEventId(generate_audit_event_id()),
                project_id=None,
                actor_id=existing.user_id,
                source=source,
                operation="external_identity.verify",
                target_type="external_identity",
                target_id=existing.id.value,
                result="success",
                redacted_summary=(
                    f"Verified {platform} identity for user {existing.user_id.value}"
                ),
                created_at=verified_at,
            )
        )
        return ExternalIdentity(
            id=existing.id,
            user_id=existing.user_id,
            platform=existing.platform,
            external_id=existing.external_id,
            external_username=existing.external_username,
            verified_at=verified_at,
            created_at=existing.created_at,
        )

    def resolve_user_by_external_identity(
        self,
        *,
        platform: ExternalPlatform,
        external_id: str,
    ) -> User:
        """Resolve a Zero User from a verified external identity.

        Per ``zero-control-plane-trust`` §"Identity is a link, not a
        name": only verified links can be used for authentication.
        Unverified links raise :class:`ExternalIdentityNotVerifiedError`.
        """
        identity = self._identity_repo.require_verified_external_identity(platform, external_id)
        return self._identity_repo.get_user(identity.user_id)
