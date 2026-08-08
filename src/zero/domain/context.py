"""Context, retrieval, and compaction domain types.

Per ``zero-context-memory`` SKILL.md §"Implementation sequence":
- Staged Retrieval Router: authorize, generate candidates, rank,
  deduplicate/diversify, budget, render, and record provenance.
- Deterministic Context Builder with stable and volatile regions.
- Token Budget Manager supporting provider counts and a conservative
  fallback.
- Compaction lifecycle with pre-flush, fit, summary validation, durable
  commit, atomic context replacement, and no-thrash protection.
- Context-injection ledger explaining selected and omitted records.

Per ``zero-claude-token-economics`` SKILL.md §"Reserve output before
filling input": for each request, resolve the model context window,
reserve required output/reasoning capacity, reserve fixed system and
safety context, allocate the remainder to plan state, retrieved
evidence, recent conversation, and tool results, reject or compact
before crossing the usable limit.

Per PLAN.md M9 invariants:
- Authorization happens before candidate retrieval.
- Context is assembled from named regions with explicit budgets.
- Output/reasoning headroom is reserved before input filling.
- One token-accounting contract drives preflight, thresholds, telemetry,
  and UI.
- Compaction never replaces typed execution state or durable memory.
- Omitted material remains recoverable through immutable references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zero.domain.artifacts import ArtifactId
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId

#: Prefix for Context Version IDs.
CONTEXT_VERSION_ID_PREFIX = "cv_"
#: Prefix for Context Injection Ledger IDs.
INJECTION_LEDGER_ID_PREFIX = "il_"
#: Prefix for Compaction Record IDs.
COMPACTION_RECORD_ID_PREFIX = "comp_"

# ----------------------------------------------------------------------
# Token accounting
# ----------------------------------------------------------------------

#: Bytes per token for the conservative deterministic fallback.
#: Per zero-context-memory reference: this is a cheap estimate for gates,
#: not billing. The real provider tokenizer is used when available.
BYTES_PER_TOKEN = 4

#: Default compaction threshold as a percentage of the context window.
DEFAULT_COMPACTION_THRESHOLD_PERCENT = 85

#: Default output reserve as a percentage of the context window.
DEFAULT_OUTPUT_RESERVE_PERCENT = 15


def estimate_tokens(text: str) -> int:
    """Cheap provider-independent token estimate for gates, not billing.

    Per ``zero-context-memory`` reference: ``len(text.encode('utf-8')) // 4``.
    """
    if not text:
        return 0
    return max(1, len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def exceeds_threshold(
    used: int,
    context_window: int,
    threshold_percent: int = DEFAULT_COMPACTION_THRESHOLD_PERCENT,
    headroom: int = 0,
) -> bool:
    """Check whether token usage exceeds the compaction threshold.

    Per ``zero-context-memory`` reference: returns True when
    ``used * 100 >= context_window * threshold_percent - headroom * 100``.
    """
    if context_window <= 0:
        return False
    return used * 100 >= max(0, context_window * threshold_percent - headroom * 100)


def context_remaining(
    *,
    context_window: int,
    used_tokens: int,
    reserved_output_tokens: int,
) -> int:
    """Return usable tokens left after preserving the output safety reserve.

    Per ``zero-claude-token-economics`` reference: the output reserve
    is subtracted before filling input context.
    """
    for name, value in (
        ("context_window", context_window),
        ("used_tokens", used_tokens),
        ("reserved_output_tokens", reserved_output_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if reserved_output_tokens > context_window:
        raise ValueError("reserved_output_tokens exceeds context_window")
    return max(0, context_window - reserved_output_tokens - used_tokens)


# ----------------------------------------------------------------------
# Context regions
# ----------------------------------------------------------------------

ContextRegionName = Literal[
    "system_policy",       # immutable security/system policy
    "project_identity",    # project and agent identity
    "plan_contract",       # approved plan and current task contract
    "execution_snapshot",  # typed execution state (survives compaction)
    "retrieved_context",   # Project RAG + agent memory
    "conversation_tail",   # recent valid exchange messages
    "compaction_summary",  # compaction summary + recovery pointers
]


@dataclass(frozen=True)
class ContextRegion:
    """A named region of the context with its own budget.

    Per ``zero-context-memory`` §"Separate context into deterministic
    regions": build prompts from named regions with independent budgets.
    """

    name: ContextRegionName
    content: str
    token_count: int
    budget_tokens: int
    # Optional: the source records that contributed to this region.
    source_record_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextVersionId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("ContextVersionId must be a non-empty string")
        if not self.value.startswith(CONTEXT_VERSION_ID_PREFIX):
            raise ValueError(
                f"ContextVersionId must start with "
                f"{CONTEXT_VERSION_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ContextVersion:
    """A durable context version for an execution.

    Per ``zero-context-memory`` §"Replace context only after durable
    commit": a crash at any point must leave either the old context
    active or a fully recoverable new context.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        execution_id: the execution this context belongs to.
        version: incremented for each new context version.
        active: True if this is the active context for the execution.
        system_message: immutable system/security policy text.
        user_prefix: project and agent identity text.
        plan_contract: current plan and task contract text.
        execution_snapshot: typed execution state (JSON) that survives
            compaction.
        retrieved_context: rendered retrieval output (JSON array).
        conversation_tail: recent valid exchange messages (JSON array).
        compaction_summary: summary text if produced by compaction.
        transcript_artifact_id: artifact containing the full pre-
            compaction transcript. None for non-compacted contexts.
        token_count: estimated tokens in this context.
        created_at: ISO-8601 timestamp.
    """

    id: ContextVersionId
    project_id: ProjectId
    execution_id: ExecutionId
    version: int
    active: bool
    system_message: str
    user_prefix: str
    plan_contract: str = ""
    execution_snapshot: str = "{}"
    retrieved_context: str = "[]"
    conversation_tail: str = "[]"
    compaction_summary: str | None = None
    transcript_artifact_id: ArtifactId | None = None
    token_count: int = 0
    created_at: str = ""


# ----------------------------------------------------------------------
# Retrieval router
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalCandidate:
    """A candidate record from the retrieval router.

    Attributes:
        source: the source type (rag_document, knowledge_record, etc.).
        record_id: the stable ID of the record.
        title: a short title.
        content: the content text.
        token_count: estimated tokens in the content.
        score: relevance score (higher is better).
    """

    source: str
    record_id: str
    title: str
    content: str
    token_count: int
    score: float = 0.0


@dataclass(frozen=True)
class InjectionLedgerId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("InjectionLedgerId must be a non-empty string")
        if not self.value.startswith(INJECTION_LEDGER_ID_PREFIX):
            raise ValueError(
                f"InjectionLedgerId must start with "
                f"{INJECTION_LEDGER_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class InjectionLedger:
    """A ledger explaining which records were selected/omitted.

    Per ``zero-context-memory`` §"Context-injection ledger explaining
    selected and omitted records" and PLAN.md M9 deliverable.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        execution_id: the execution.
        context_version: the context version this ledger describes.
        selected: tuple of (source, record_id, token_count) for injected
            records.
        omitted: tuple of (source, record_id, reason) for omitted records.
        total_candidates: total number of candidate records considered.
        total_tokens: total tokens in selected records.
        budget_tokens: the token budget that was in effect.
        created_at: ISO-8601 timestamp.
    """

    id: InjectionLedgerId
    project_id: ProjectId
    execution_id: ExecutionId
    context_version: int
    selected: tuple[tuple[str, str, int], ...] = ()
    omitted: tuple[tuple[str, str, str], ...] = ()
    total_candidates: int = 0
    total_tokens: int = 0
    budget_tokens: int = 0
    created_at: str = ""


# ----------------------------------------------------------------------
# Compaction
# ----------------------------------------------------------------------

CompactionState = Literal[
    "pre_flush",
    "fit",
    "summary_validated",
    "committed",
    "activated",
    "failed",
    "no_thrash_blocked",
]

#: The fixed degradation ladder for fitting summarizer input.
#: Per ``zero-context-memory`` reference: verbatim, oldest history
#: removal, oversized tool-result truncation, oldest current-step
#: removal, newest-item emergency truncation.
FIT_LADDER: tuple[str, ...] = (
    "verbatim",
    "history_turn_selected",
    "tool_truncated",
    "step_turns_selected",
    "emergency",
)


@dataclass(frozen=True)
class CompactionRecordId:
    value: str

    def __post_init__(self) -> None:
        if not self.value or not isinstance(self.value, str):
            raise ValueError("CompactionRecordId must be a non-empty string")
        if not self.value.startswith(COMPACTION_RECORD_ID_PREFIX):
            raise ValueError(
                f"CompactionRecordId must start with "
                f"{COMPACTION_RECORD_ID_PREFIX!r}; got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class CompactionRecord:
    """A durable compaction lifecycle record.

    Per ``zero-context-memory`` §"Replace context only after durable
    commit": the safe commit order is:
    1. canonical memory transaction committed;
    2. transcript/segment artifact stored and verified;
    3. typed execution snapshot stored;
    4. compact summary validated;
    5. provider-ready history validated;
    6. active context pointer advanced atomically;
    7. audit event recorded.

    A crash at any point must leave either the old context active or a
    fully recoverable new context.

    Attributes:
        id: stable server-issued ID.
        project_id: the project (denormalized).
        execution_id: the execution.
        source_context_version: the context version before compaction.
        target_context_version: the new context version after compaction.
        source_event_range: JSON {start_event_id, end_event_id}.
        memory_delta_artifact_id: artifact with accepted memory deltas.
        transcript_artifact_id: artifact with the full transcript.
        summary: the compaction summary text.
        fit_rung: which rung of the degradation ladder was used.
        state: the compaction's state.
        no_thrash_count: consecutive compactions without meaningful
            reclaimed space.
        created_at: ISO-8601 timestamp.
    """

    id: CompactionRecordId
    project_id: ProjectId
    execution_id: ExecutionId
    source_context_version: int
    target_context_version: int
    source_event_range: str
    memory_delta_artifact_id: ArtifactId | None = None
    transcript_artifact_id: ArtifactId | None = None
    summary: str = ""
    fit_rung: str = "verbatim"
    state: CompactionState = "pre_flush"
    no_thrash_count: int = 0
    created_at: str = ""


# ----------------------------------------------------------------------
# Typed failures
# ----------------------------------------------------------------------


class ContextError(RuntimeError):
    """Base class for context-domain typed failures."""


class ContextVersionNotFoundError(ContextError):
    pass


class CompactionNotNeededException(ContextError):
    """Compaction was requested but the context is not over threshold."""


class CompactionBlockerError(ContextError):
    """Compaction cannot proceed due to a typed blocker.

    Per PLAN.md M9: "Repeated ineffective compaction stops with a typed
    blocker."
    """
