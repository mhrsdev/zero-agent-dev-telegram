# ADR 0006 — Canonical Identity Model

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 2 (Central Control Plane, Identity, Project Isolation)
- Skills applied: `zero-control-plane-trust`, `zero-project-isolation-evidence`

## Context

`PLAN.md` §7 (Milestone 2) requires stable server-issued IDs as
authority, with external platform IDs as links to a Zero identity.
`zero-control-plane-trust` §"Identity is a link, not a name":
"Telegram IDs, Discord IDs, email identities, and future platform
identities are verified links to that person. Display names and
usernames remain useful labels but weak authority because they can
change, collide, or be imitated."

`zero-project-isolation-evidence` §"Canonical constraints and policy
complement each other": "Database constraints can enforce ownership,
foreign-key lineage, and uniqueness. Application policy decides
whether the current actor may perform the current operation. Neither
replaces the other."

## Decision

Adopt a canonical identity model with three layers:

1. **Users** — stable server-issued `zu_`-prefixed IDs. Display names
   are labels only; they can change, collide, or be imitated. Two
   users may have identical display names; they remain distinct
   identities because the ID is the authority.
2. **External Identities** — links from external platforms (Telegram,
   Discord, web, email) to a Zero User. Each link is unique per
   (platform, external_id). Links have a `verified_at` timestamp;
   until verified, they cannot be used for authentication. The
   external_id is stored as TEXT to preserve 64-bit Telegram IDs
   without truncation (per Telegram findings §8).
3. **Projects** — stable `p_`-prefixed IDs. Each project has an
   owner_user_id (FK to users). The owner is the initial member with
   role='owner'.
4. **Project Memberships** — composite key (project_id, user_id)
   with a role (owner, member, viewer). Unique constraint prevents
   duplicate memberships.

### Constraints enforced at the database layer

- `users.id` PRIMARY KEY (stable server-issued).
- `external_identities` UNIQUE(platform, external_id) — same
  external ID cannot link to two Zero Users.
- `project_memberships` PRIMARY KEY(project_id, user_id) — a user
  is a member of a project at most once.
- `projects.owner_user_id` REFERENCES users(id) — owner must exist.
- All FK constraints are enforced (`PRAGMA foreign_keys = ON` on
  every connection).

### Constraints enforced at the application layer

- The owner cannot be removed from a project (would orphan it).
- External identity links must be verified before they can be used
  for authentication.
- Display name changes do not affect identity or authority.

## Rejected alternatives

- **Use display name as identity**: explicitly rejected by
  `zero-control-plane-trust` §"Identity is a link, not a name" and
  by PLAN.md invariant #2.
- **Use external platform ID directly as the Zero User ID**: would
  couple Zero's canonical state to a platform that may change its
  ID scheme. External IDs are links, not authority.
- **Auto-verify external identity links on creation**: would skip
  the verification ceremony (Telegram OIDC, Discord OAuth, etc.).
  Verification is a separate step performed by the platform-specific
  adapter (M13).

## Consequences

- The identity model is provider-neutral: changing the messaging
  platform or adding a new one does not affect canonical identity.
- Project isolation starts at the schema: every project-scoped table
  added in later milestones has a `project_id` FK to `projects`.
- The audit log records identity transitions (user.create,
  project.create, member.add, external_identity.link,
  external_identity.verify) with stable IDs, never with display
  names.
