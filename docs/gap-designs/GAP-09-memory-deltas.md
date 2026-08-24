# GAP 9 Design — Memory Delta Artifacts

Status: design accepted · Phase 3 (after Phase 1 tokenizer)

## Problem

`CompactionRecord.memory_delta_artifact_id` is reserved but never
written. Compaction summaries (LLM or fallback) contain structured
sections that should become durable `KnowledgeRecord`s.

## Architecture

New collaborator: `MemoryDeltaWriter` inside
`src/zero/app/memory_delta.py` — a small parser + writer invoked by
`CompactionService.compact()` **only when** the producing agent type has
opted in.

```
compact(...)
  └─ summary validated
  └─ if memory_delta_enabled and summarizer-produced:
        sections = parse_sections(summary)          # exact headings
        records = [KnowledgeRecord(kind=..., content=bullet)]
        for r in records: agent_type_service.add_knowledge(...)
        artifact = artifacts.store_artifact(kind="memory_delta", content=json)
        compaction record gains memory_delta_artifact_id before commit
```

### Parsing rules

- Split summary on the six required headings (`REQUIRED_SUMMARY_SECTIONS`).
- "Accepted decisions" bullets → `kind="decision"`; "Blockers or
  failures" bullets → `kind="failure"`; other sections are not
  converted.
- Only bullet lines (`- `/`* `) become records; each bounded to 2000
  chars, redacted via `redact_sensitive_text`, max 32 records total.
- Fallback-template summaries are recognizable by their deterministic
  header ("Compaction summary\n- Current goal:") — deltas are skipped
  for them, per acceptance criteria ("without LLM summarizer, no
  records created").

### Opt-in flag

`AgentType.model_policy["memory_delta_enabled"] == "1"` (model_policy
is the existing `dict[str,str]`; avoids a schema migration). When the
compacting execution's tasks have no agent type, deltas are disabled.

## Data model changes

None new — uses reserved `memory_delta_artifact_id` column and existing
knowledge tables. New artifact kind value `"memory_delta"` added to the
artifact kind check constraint only if the DB enforces one (it does
not; kinds are strings).

## API surface

- `MemoryDeltaWriter.write(summary) -> tuple[KnowledgeRecord...]`
- `CompactionRecord` unchanged shape; `memory_delta_artifact_id` now
  populated on success.
- Knowledge listing endpoints already expose new records.

## Security considerations

- Content passes through `redact_sensitive_text`; provenance records
  the compaction id for lineage.
- Failures to write deltas are logged and swallowed (degraded, not
  fatal) — compaction must never fail because memory extraction did.

## Test strategy

- Parser tests: section splitting, bullet extraction, redaction,
  bounds, fallback-template detection.
- Integration test with LLM-shaped summary: knowledge rows exist,
  artifact id set on the compaction record, queryable via repo.
- Negative tests: disabled flag → nothing written; fallback template →
  nothing written; add_knowledge failure → compaction still succeeds.

## Migration path

Additive; default off everywhere until an agent type opts in.

## Rollback strategy

Set flag off / remove writer call; reserved column returns to NULL.

## Acceptance criteria

- After compaction with LLM summarizer + opt-in: knowledge records
  exist with correct kinds; `memory_delta_artifact_id` populated.
- Without LLM summarizer: no records created.
- Suite green.
