# ADR 0016 — Lossless Knowledge Migration with Provenance

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 7 (Dynamic Sub Agent Type Lifecycle)
- Skills applied: `zero-context-memory`, `zero-agent-execution-lifecycle`,
  `zero-artifact-provenance-model`

## Context

`PLAN.md` §12 (Milestone 7) requires:
- Split routes all mandatory knowledge to destinations or archive.
- Merge deduplicates without losing provenance.
- Provenance links from source knowledge to destination scopes.

`zero-context-memory` §"Non-negotiable invariants": "Removing, splitting,
or merging a sub-agent type never deletes its knowledge."

`zero-agent-execution-lifecycle` §"Lossless does not mean duplicating
everything forever": "Losslessness means every mandatory source record
has a known destination, archive location, or supersession lineage. It
does not require injecting every old record into every new agent
context. Canonical evidence may remain stored once while destination
indexes and ownership links change."

`zero-artifact-provenance-model` §"Provenance forms a graph": "Useful
lineage answers: which event or tool produced this artifact; which
repository revision and task it describes; which summary, memory record,
or integration decision derived from it; which model/provider/version
participated; which artifact supersedes another without erasing it."

## Decision

Adopt a lossless knowledge migration model with provenance links:

1. **Never hard-delete**: knowledge records are archived (state =
   ``archived``), never deleted. The ``archived`` state is terminal
   for records, but the record remains in the database for audit and
   potential future retrieval.

2. **Migrated records carry provenance**: when a record is migrated
   from one type to another (during split/merge), the ``migrated_from``
   field is set to the record's own ID. This creates a self-referential
   provenance link that says "this record was migrated from its
   original location." The record's ID does not change; only its
   ``agent_type_id`` changes.

3. **Unrouted records are archived**: during split, records not
   explicitly routed to a destination are archived in place (they stay
   under the source type's ID with state = ``archived``). This ensures
   no record is lost even if the caller forgets to route it.

4. **Reconciliation verifies accounting**: after migration, the service
   verifies that every original record still exists by looking it up
   by ID. If any record is missing, ``KnowledgeReconciliationError`` is
   raised with the list of unaccounted record IDs.

5. **Supersession lineage**: records can be superseded by a newer
   record (``superseded_by`` field). The old record is not deleted; it
   transitions to ``superseded`` state. This allows knowledge to evolve
   without losing history.

### Provenance graph

Each knowledge record carries:
- ``id``: stable identity.
- ``agent_type_id``: current owner (changes on migration).
- ``migrated_from``: the record's own ID if it was migrated (self-
  referential; indicates the record moved from its original type).
- ``superseded_by``: the ID of a newer record that replaces this one.
- ``provenance``: free-text description of where the record came from
  (e.g. "task_abc123", "plan_revision_pr_xyz").

This forms a provenance graph that can answer:
- "Where did this record come from?" → ``provenance`` + ``migrated_from``.
- "What does this record replace?" → ``superseded_by``.
- "Who owns this record now?" → ``agent_type_id``.

## Rejected alternatives

- **Copy-on-migrate (duplicate records)**: rejected by
  ``zero-agent-execution-lifecycle`` §"Lossless does not mean duplicating
  everything forever". Duplicating records would double the storage and
  create confusion about which copy is authoritative.
- **Delete-on-migrate**: explicitly rejected by
  ``zero-context-memory`` §"Non-negotiable invariants".
- **Separate migration log**: rejected. Provenance should be on the
  record itself, not in a separate log that can diverge from the
  records it describes.
- **Skip reconciliation**: rejected by PLAN.md M7: "Retirement is
  blocked until reconciliation passes." Reconciliation is the safety
  net that catches bugs in the migration logic.

## Consequences

- Knowledge is never lost during split/merge/retire.
- Every migrated record carries provenance linking it to its origin.
- Unrouted records are archived in place, not deleted.
- Reconciliation catches migration bugs before activation.
- The provenance graph supports audit and debugging.
