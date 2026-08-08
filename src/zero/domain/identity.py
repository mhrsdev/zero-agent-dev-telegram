"""Canonical identity domain types.

Per ``zero-control-plane-trust`` §"Identity is a link, not a name":
A person exists in Zero through a stable server-issued identifier.
Telegram IDs, Discord IDs, email identities, and future platform
identities are verified links to that person. Display names and
usernames remain useful labels but weak authority.

Per ``zero-project-isolation-evidence`` §"Scope begins before access":
Project scope belongs in canonical keys, authorization decisions, query
construction, artifact paths or namespaces, caches, jobs, and audit
correlation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ----------------------------------------------------------------------
# Stable server-issued identifiers
# ----------------------------------------------------------------------

#: Prefix for Zero User IDs. Server-issued, never derived from a name.
USER_ID_PREFIX = "zu_"
#: Prefix for Zero Project IDs.
PROJECT_ID_PREFIX = "p_"
#: Prefix for External Identity IDs.
EXTERNAL_IDENTITY_ID_PREFIX = "ei_"
#: Prefix for Project Membership composite IDs (project_id + ":" + user_id).
# (Memberships use a composite key; no separate ID prefix needed.)


@dataclass(frozen=True)
class UserId:
    """Stable server-issued Zero User ID.

    Authority follows this ID, never a display name or external username.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("UserId must be a non-empty string")
        if not self.value.startswith(USER_ID_PREFIX):
            raise ValueError(
                f"UserId must start with {USER_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ProjectId:
    """Stable server-issued Zero Project ID."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ProjectId must be a non-empty string")
        if not self.value.startswith(PROJECT_ID_PREFIX):
            raise ValueError(
                f"ProjectId must start with {PROJECT_ID_PREFIX!r}; "
                f"got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ExternalIdentityId:
    """Stable server-issued ID for an external identity link."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ExternalIdentityId must be a non-empty string")
        if not self.value.startswith(EXTERNAL_IDENTITY_ID_PREFIX):
            raise ValueError(
                f"ExternalIdentityId must start with "
                f"{EXTERNAL_IDENTITY_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


# ----------------------------------------------------------------------
# User and external identity
# ----------------------------------------------------------------------

UserStatus = Literal["active", "suspended", "deleted"]
ExternalPlatform = Literal["telegram", "discord", "web", "email", "other"]


@dataclass(frozen=True)
class User:
    """A person in Zero Develop.

    The ``display_name`` is a label for human convenience. It is **not**
    authority: it can change, collide, or be imitated. The ``id`` is
    the only authority.

    Per ``zero-control-plane-trust`` §"Identity is a link, not a name":
    ``role=user`` alone never proves human identity. Two users may have
    identical display names; they remain distinct identities.
    """

    id: UserId
    display_name: str
    status: UserStatus = "active"
    created_at: str = ""


@dataclass(frozen=True)
class ExternalIdentity:
    """A verified link from an external platform identity to a Zero User.

    Per ``zero-control-plane-trust`` §"Identity is a link, not a name":
    External IDs are links, not identities by name. The same Telegram
    user ID may be linked to at most one Zero User per platform.

    Attributes:
        id: stable server-issued ID for this link record.
        user_id: the Zero User this link belongs to.
        platform: the external platform (telegram, discord, ...).
        external_id: the platform's stable identifier for the person
            (e.g. Telegram user ID as a string). Stored as text to
            preserve 64-bit values without truncation (per
            ``zero-interface-adapter-model`` Telegram findings §8).
        external_username: the platform's display username, if any.
            This is a label only; it is NOT authority and may change
            at any time.
        verified_at: timestamp when the link was verified. ``None``
            means the link has been recorded but not yet verified;
            such links cannot be used for authentication until
            verification completes.
    """

    id: ExternalIdentityId
    user_id: UserId
    platform: ExternalPlatform
    external_id: str
    external_username: str | None = None
    verified_at: str | None = None
    created_at: str = ""


# ----------------------------------------------------------------------
# Project and membership
# ----------------------------------------------------------------------

ProjectRole = Literal["owner", "member", "viewer"]


@dataclass(frozen=True)
class Project:
    """A Zero Develop project.

    The project is the unit of isolation: memory, retrieval, execution,
    tools, and audit are all scoped by ``id``.

    Attributes:
        id: stable server-issued Project ID.
        name: human-readable project name. Labels only; not authority.
        owner_user_id: the Zero User who owns this project. The owner
            has full permissions within the project.
        created_at: ISO-8601 timestamp.
    """

    id: ProjectId
    name: str
    owner_user_id: UserId
    created_at: str = ""


@dataclass(frozen=True)
class ProjectMembership:
    """A user's membership in a project.

    Per ``zero-control-plane-trust`` §"Project scope is part of every
    fact": membership is the canonical link between a user and a
    project's scoped state.

    The composite key (project_id, user_id) is unique; a user is a
    member of a project at most once.

    Attributes:
        project_id: the project.
        user_id: the member.
        role: the member's role in this project. Determines the
            permissions granted by the authorization matrix.
        created_at: ISO-8601 timestamp.
    """

    project_id: ProjectId
    user_id: UserId
    role: ProjectRole
    created_at: str = ""


@dataclass(frozen=True)
class ProjectScope:
    """Resolved project scope for an actor.

    Per ``zero-project-isolation-evidence`` §"Scope begins before
    access": a project-scoped operation should know its project and
    actor before data access.

    Attributes:
        project_id: the project being accessed.
        actor_id: the user attempting the access.
        role: the actor's role in this project, or ``None`` if the
            actor is not a member.
        is_member: True if the actor is a member of this project.
    """

    project_id: ProjectId
    actor_id: UserId
    role: ProjectRole | None = None
    is_member: bool = False

    @classmethod
    def for_member(
        cls, project_id: ProjectId, actor_id: UserId, role: ProjectRole
    ) -> ProjectScope:
        return cls(
            project_id=project_id, actor_id=actor_id, role=role, is_member=True
        )

    @classmethod
    def for_non_member(
        cls, project_id: ProjectId, actor_id: UserId
    ) -> ProjectScope:
        return cls(
            project_id=project_id, actor_id=actor_id, role=None, is_member=False
        )


# ----------------------------------------------------------------------
# Failures (typed domain errors per zero-control-plane-trust §"Failure
# shapes teach the boundary")
# ----------------------------------------------------------------------


class IdentityError(RuntimeError):
    """Base class for identity-domain typed failures."""


class DuplicateUserError(IdentityError):
    """A user with the same stable identifier already exists."""


class DuplicateExternalIdentityError(IdentityError):
    """An external identity is already linked to a Zero User."""


class UserNotFoundError(IdentityError):
    """No user exists with the given stable identifier."""


class ProjectNotFoundError(IdentityError):
    """No project exists with the given stable identifier."""


class MembershipNotFoundError(IdentityError):
    """The user is not a member of the project."""


class MembershipAlreadyExistsError(IdentityError):
    """The user is already a member of the project."""


class ExternalIdentityNotVerifiedError(IdentityError):
    """The external identity link has not been verified yet."""


class CrossProjectAccessError(IdentityError):
    """An operation attempted to cross project boundaries.

    Per ``zero-project-isolation-evidence``: cross-project read and
    write attempts must return zero data and make no mutation. This
    error is raised when such an attempt is detected at the application
    layer; the database constraints provide a second layer of defense.
    """
