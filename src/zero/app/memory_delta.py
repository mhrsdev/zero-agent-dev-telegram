"""Memory delta extraction from compaction summaries (GAP 9).

Per ``docs/gap-designs/GAP-09-memory-deltas.md``: after a successful
compaction with the LLM summarizer, structured sections become durable
knowledge records and a ``memory_delta`` artifact links them to the
compaction record. The deterministic fallback template never produces
deltas, so only genuinely summarized material enters agent memory.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from zero.domain.artifacts import ArtifactId
from zero.domain.audit import redact_sensitive_text
from zero.domain.context import CompactionRecordId
from zero.domain.identity import ProjectId, UserId

logger = logging.getLogger(__name__)

#: Headings convertible to knowledge records (GAP 9 scope).
_DECISIONS_HEADING = "accepted decisions"
_FAILURES_HEADING = "blockers or failures"

#: The deterministic fallback template starts with this header; its
#: content is boilerplate, never extracted memory.
_FALLBACK_TEMPLATE_HEADER = "compaction summary"

_MAX_RECORDS = 32
_MAX_RECORD_CHARS = 2000

_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*)?[-*]?\s*(?P<name>.+?)\s*:?\s*$")

#: The fixed summary-section vocabulary (mirrors
#: ``compaction_service.REQUIRED_SUMMARY_SECTIONS``). A line is treated
#: as a heading only when its stripped text starts with one of these.
_KNOWN_HEADINGS: tuple[str, ...] = (
    "current goal",
    "accepted decisions",
    "modified artifacts",
    "unresolved tasks",
    "blockers or failures",
    "next safe action",
)


def _as_known_heading(line: str) -> str | None:
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    name = match.group("name").strip().lower()
    for known in _KNOWN_HEADINGS:
        if name == known or name.startswith(known):
            return known
    return None


@dataclass(frozen=True)
class MemoryDeltaRecord:
    kind: str  # "decision" | "failure"
    content: str


def extract_memory_deltas(summary: str) -> list[MemoryDeltaRecord]:
    """Parse decisions/failures bullets from an LLM compaction summary.

    Returns an empty list for the deterministic fallback template or
    when neither tracked section carries bullet lines.
    """
    if not summary or not summary.strip():
        return []
    if summary.strip().lower().startswith(_FALLBACK_TEMPLATE_HEADER):
        return []

    # Split the summary into heading→body mapping using the fixed
    # section vocabulary (tolerant of markdown decorations).
    sections: dict[str, list[str]] = {}
    current = ""
    for raw_line in summary.splitlines():
        known = _as_known_heading(raw_line)
        if known is not None:
            current = known
            sections.setdefault(current, [])
            continue
        if current:
            sections.setdefault(current, []).append(raw_line)

    records: list[MemoryDeltaRecord] = []
    for heading, kind in (
        (_DECISIONS_HEADING, "decision"),
        (_FAILURES_HEADING, "failure"),
    ):
        body_lines: list[str] = []
        for key, lines in sections.items():
            if key.startswith(heading):
                body_lines = lines
                break
        for line in body_lines:
            bullet = _BULLET_RE.match(line)
            if bullet is None:
                continue
            content = redact_sensitive_text(bullet.group(1).strip())
            if not content:
                continue
            content = content[:_MAX_RECORD_CHARS]
            records.append(MemoryDeltaRecord(kind=kind, content=content))
            if len(records) >= _MAX_RECORDS:
                return records
    return records


class MemoryDeltaWriter:
    """Persist parsed memory deltas as knowledge + linking artifact."""

    def __init__(self, *, artifact_service, agent_type_service) -> None:
        self._artifact_service = artifact_service
        self._agent_type_service = agent_type_service

    def write(
        self,
        *,
        project_id: ProjectId,
        execution_id: Any,
        actor_id: UserId,
        agent_type_id,
        compaction_record_id: CompactionRecordId,
        summary: str,
    ) -> ArtifactId | None:
        """Write deltas for one compaction; returns the artifact id.

        Failures degrade to None (logged) — memory extraction must
        never fail a compaction that already committed safely.
        """
        try:
            deltas = extract_memory_deltas(summary)
            if not deltas or agent_type_id is None:
                return None
            stored: list[dict[str, str]] = []
            for delta in deltas:
                self._agent_type_service.add_knowledge(
                    project_id=project_id,
                    type_id=agent_type_id,
                    actor_id=actor_id,
                    kind=delta.kind,  # type: ignore[arg-type]
                    content=delta.content,
                    provenance=f"compaction:{compaction_record_id.value}",
                )
                stored.append({"kind": delta.kind, "content": delta.content})
            payload = json.dumps(
                {
                    "compaction_record_id": compaction_record_id.value,
                    "records": stored,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            artifact = self._artifact_service.store_artifact(
                project_id=project_id,
                actor_id=actor_id,
                # The artifacts.kind CHECK constraint predates GAP 9;
                # the delta rides kind="other" with a distinctive
                # producer prefix and a typed payload instead of a
                # risky table rebuild.
                kind="other",
                media_type="application/json",
                content=payload,
                producer=f"memory-delta:{execution_id.value}",
                provenance=json.dumps(
                    {
                        "compaction_record_id": compaction_record_id.value,
                        "artifact_semantic_kind": "memory_delta",
                    }
                ),
            )
            return artifact.id
        except Exception as exc:  # noqa: BLE001 - degraded, not fatal
            logger.warning(
                "memory delta write skipped for compaction %s: %s",
                compaction_record_id.value,
                type(exc).__name__,
            )
            return None


__all__ = [
    "MemoryDeltaRecord",
    "MemoryDeltaWriter",
    "extract_memory_deltas",
]
