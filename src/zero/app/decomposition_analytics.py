"""S7 recovery analytics for LLM task decomposition.

Every :class:`~zero.app.task_decomposition.TaskDecomposer` outcome is
recorded here with enough structure to answer the operational questions
that motivated S7 in the first place:

- Is a given model honoring the forced ``emit_task_graph`` tool-call
  contract on the first ask, or is it leaning on escalation / legacy
  text?
- How often does it emit NEAR-MISS dependency keys (typos) that the
  deterministic ``repair_dangling_dependencies`` recovery has to rescue?
  That rate is tracked **per model** because provider families differ
  wildly in slug discipline, and a regression shows up as a drifting
  typo-rate long before graphs start failing outright.
- When decomposition gives up entirely and falls back to the single
  ``implementation`` task — the silent capability killer for large
  plans — it must be visible, not just logged.

Outcomes append as one JSON line each to an optional sink file
(``ZERO_DECOMPOSITION_ANALYTICS_PATH``) so live servers leave durable
evidence without touching the database schema. Aggregates are computed
in memory per ``(provider, model_name)`` from the same events.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Outcome taxonomy (closed set; drivers/tests may rely on these).
OUTCOME_NATIVE_FIRST_ASK = "native_first_ask"
OUTCOME_ESCALATED_OK = "escalated_retry_ok"
OUTCOME_RECOVERED_REPAIR = "recovered_key_repair"
OUTCOME_RECOVERED_ORDER = "recovered_order_normalization"
OUTCOME_LEGACY_TEXT_OK = "legacy_text_ok"
OUTCOME_DEGRADED_LEGACY = "degraded_to_legacy_unsupported"
OUTCOME_SINGLE_TASK_FALLBACK = "single_task_fallback"
OUTCOME_TRANSPORT_ERROR = "transport_error"
OUTCOME_DECOMPOSER_EXCEPTION = "decomposer_exception"

#: Paths through the ladder.
PATH_NATIVE = "native_forced_ladder"
PATH_LEGACY = "legacy_text"

_TERMINAL_OK = frozenset(
    {
        OUTCOME_NATIVE_FIRST_ASK,
        OUTCOME_ESCALATED_OK,
        OUTCOME_RECOVERED_REPAIR,
        OUTCOME_RECOVERED_ORDER,
        OUTCOME_LEGACY_TEXT_OK,
    }
)


@dataclass(frozen=True)
class DependencyRepair:
    """One rescued near-miss ``depends_on`` reference."""

    task_key: str
    raw_dependency: str
    repaired_to: str
    #: Token Jaccard score behind the repair; ``None`` marks a case-only
    #: fix that went through the exact lowercase index instead of the
    #: fuzzy matcher.
    similarity: float | None


@dataclass(frozen=True)
class DecompositionOutcome:
    """Everything worth knowing about one ``decompose()`` call."""

    ts_utc: str
    revision_id: str
    provider: str
    model_name: str
    outcome: str
    path: str
    attempts_used: int
    task_count: int
    edge_count: int
    repairs: tuple[DependencyRepair, ...] = ()
    elapsed_ms: int = 0

    def to_jsonable(self) -> dict:
        return {
            "ts_utc": self.ts_utc,
            "revision_id": self.revision_id,
            "provider": self.provider,
            "model_name": self.model_name,
            "outcome": self.outcome,
            "path": self.path,
            "attempts_used": self.attempts_used,
            "task_count": self.task_count,
            "edge_count": self.edge_count,
            "repairs": [
                {
                    "task_key": r.task_key,
                    "raw_dependency": r.raw_dependency,
                    "repaired_to": r.repaired_to,
                    "similarity": r.similarity,
                }
                for r in self.repairs
            ],
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class _ModelAggregate:
    attempts: int = 0
    ok: int = 0
    first_ask_ok: int = 0
    escalated_ok: int = 0
    recovered_repair: int = 0
    recovered_order: int = 0
    legacy_ok: int = 0
    degraded_legacy: int = 0
    fallbacks: int = 0
    transport_errors: int = 0
    exceptions: int = 0
    typo_refs: int = 0
    task_count_sum: int = 0
    duration_ms_sum: int = 0


def resolve_sink_path(environ: dict | None = None) -> Path | None:
    """Resolve the JSONL sink from ``ZERO_DECOMPOSITION_ANALYTICS_PATH``."""
    raw = (
        (environ if environ is not None else os.environ)
        .get("ZERO_DECOMPOSITION_ANALYTICS_PATH", "")
        .strip()
    )
    return Path(raw) if raw else None


_shared_instances: dict[str, DecompositionAnalytics] = {}
_shared_lock = threading.Lock()


class DecompositionAnalytics:
    """Thread-safe in-memory aggregate + optional JSONL evidence sink.

    Use :meth:`get_or_create` at composition time so every Services
    build within a process (API app, background workers, admin loops)
    shares ONE aggregate per sink path instead of fragmenting counts.
    """

    def __init__(self, *, sink_path: Path | None = None) -> None:
        self._sink_path = sink_path
        self._lock = threading.Lock()
        self._events: list[DecompositionOutcome] = []
        self._by_model: dict[tuple[str, str], _ModelAggregate] = defaultdict(_ModelAggregate)

    @classmethod
    def get_or_create(cls, sink_path: Path | None) -> DecompositionAnalytics:
        key = str(sink_path) if sink_path is not None else ""
        with _shared_lock:
            existing = _shared_instances.get(key)
            if existing is None:
                existing = cls(sink_path=sink_path)
                _shared_instances[key] = existing
            return existing

    @property
    def sink_path(self) -> Path | None:
        return self._sink_path

    def record(self, outcome: DecompositionOutcome) -> None:
        """Append one completed decompose() outcome to the ledger."""
        with self._lock:
            self._events.append(outcome)
            agg = self._by_model[(outcome.provider, outcome.model_name)]
            agg.attempts += 1
            agg.duration_ms_sum += max(0, outcome.elapsed_ms)
            if outcome.outcome in _TERMINAL_OK:
                agg.ok += 1
                agg.task_count_sum += outcome.task_count
            if outcome.outcome == OUTCOME_NATIVE_FIRST_ASK:
                agg.first_ask_ok += 1
            elif outcome.outcome == OUTCOME_ESCALATED_OK:
                agg.escalated_ok += 1
            elif outcome.outcome == OUTCOME_RECOVERED_REPAIR:
                agg.recovered_repair += 1
                agg.typo_refs += len(outcome.repairs)
            elif outcome.outcome == OUTCOME_RECOVERED_ORDER:
                agg.recovered_order += 1
            elif outcome.outcome == OUTCOME_LEGACY_TEXT_OK:
                agg.legacy_ok += 1
            elif outcome.outcome == OUTCOME_DEGRADED_LEGACY:
                # Degradation means this attempt produced no graph; count
                # only when nothing valid came out downstream either.
                agg.degraded_legacy += 1
            elif outcome.outcome == OUTCOME_SINGLE_TASK_FALLBACK:
                agg.fallbacks += 1
            elif outcome.outcome == OUTCOME_TRANSPORT_ERROR:
                agg.transport_errors += 1
            elif outcome.outcome == OUTCOME_DECOMPOSER_EXCEPTION:
                agg.exceptions += 1
        if self._sink_path is not None:
            self._append_sink(outcome)

    def _append_sink(self, outcome: DecompositionOutcome) -> None:
        try:
            self._sink_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(outcome.to_jsonable(), ensure_ascii=False, sort_keys=True)
            with self._lock, open(self._sink_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            logger.warning("decomposition analytics sink write failed", exc_info=True)

    def snapshot(self) -> dict:
        """Aggregates per ``provider:model`` including typo rates.

        ``typo_rate_per_graph`` is the headline metric: near-miss
        dependency references observed per validated graph. A rising
        rate predicts broken DAGs on models where recovery thresholds
        refuse ambiguous or distant typos.
        """
        with self._lock:
            models: dict[str, dict] = {}
            for (provider, model_name), agg in sorted(self._by_model.items()):
                denom = agg.ok or 0
                models[f"{provider}:{model_name}"] = {
                    "decomposition_attempts": agg.attempts,
                    "graphs_validated": denom,
                    "first_ask_ok": agg.first_ask_ok,
                    "escalated_retry_ok": agg.escalated_ok,
                    "recovered_key_repair": agg.recovered_repair,
                    "recovered_order_normalization": agg.recovered_order,
                    "legacy_text_ok": agg.legacy_ok,
                    "degraded_to_legacy": agg.degraded_legacy,
                    "single_task_fallbacks": agg.fallbacks,
                    "transport_errors": agg.transport_errors,
                    "decomposer_exceptions": agg.exceptions,
                    "typo_references_rescued": agg.typo_refs,
                    # Core S7 analytics extension: typo pressure per model.
                    "typo_rate_per_graph": round(agg.typo_refs / denom, 4) if denom else None,
                    "avg_tasks_per_graph": (
                        round(agg.task_count_sum / denom, 2) if denom else None
                    ),
                    "avg_duration_ms": (
                        round(agg.duration_ms_sum / agg.attempts, 1) if agg.attempts else None
                    ),
                    "success_rate": round(agg.ok / agg.attempts, 4) if agg.attempts else None,
                }
            return {"total_outcomes": len(self._events), "models": models}

    def render_markdown(self) -> str:
        rows = ["| model | attempts | graphs | first-ask | typo refs | typo/graph | fallbacks |"]
        rows.append("|---|---|---|---|---|---|---|")
        snap = self.snapshot()
        entries = snap["models"]
        if not entries:
            rows.append("| (no outcomes recorded) | - | - | - | - | - | - |")
        for name, agg in entries.items():
            rows.append(
                f"| {name} | {agg['decomposition_attempts']} "
                f"| {agg['graphs_validated']} | {agg['first_ask_ok']} "
                f"| {agg['typo_references_rescued']} "
                f"| {agg['typo_rate_per_graph'] if agg['typo_rate_per_graph'] is not None else '-'} "
                f"| {agg['single_task_fallbacks']} |"
            )
        return "\n".join(rows)
