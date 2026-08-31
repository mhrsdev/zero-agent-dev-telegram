"""LLM-driven task decomposition for approved plans (GAP 10).

Per ``docs/gap-designs/GAP-10-task-decomposition.md``: an optional
scheduler step asks the model to split a plan revision into a small
dependency graph of tasks. Any parse or validation failure — or the
flag being off — falls back to today's single ``implementation`` task,
so the default pipeline is byte-for-byte unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from zero.app.decomposition_analytics import (
    OUTCOME_DECOMPOSER_EXCEPTION,
    OUTCOME_DEGRADED_LEGACY,
    OUTCOME_ESCALATED_OK,
    OUTCOME_LEGACY_TEXT_OK,
    OUTCOME_NATIVE_FIRST_ASK,
    OUTCOME_RECOVERED_ORDER,
    OUTCOME_RECOVERED_REPAIR,
    OUTCOME_SINGLE_TASK_FALLBACK,
    OUTCOME_TRANSPORT_ERROR,
    PATH_LEGACY,
    PATH_NATIVE,
    DecompositionOutcome,
    DependencyRepair,
)
from zero.app.worker_service import DependencySpec, TaskSpec
from zero.domain.audit import redact_sensitive_text
from zero.domain.providers import CanonicalResponse, ToolDeclaration

#: Evidence labels a decomposed task may demand. Mirrors AgentRuntime's
#: _SUPPORTED_EVIDENCE (kept local to avoid a heavyweight import cycle):
#: anything outside this set makes the whole graph invalid, and the
#: runtime refuses tasks whose required evidence it cannot prove.
_SUPPORTED_EVIDENCE_LABELS = frozenset(
    {
        "provider_response",
        "diff",
        "test_report",
        "exit_status",
        "stdout",
        "stderr",
        "source_snapshot",
    }
)

#: Transient provider failures that a bounded same-prompt retry can
#: survive (round-7 live finding: ONE CDN edge 403 on the decomposer's
#: single provider call silently degraded every graph to the single-task
#: fallback — the two prompt attempts escalate OUTPUT QUALITY, they are
#: not transport retries). Auth failures and malformed provider output
#: stay fail-fast: retrying cannot fix them.
_TRANSIENT_PROVIDER_MARKERS = (
    "transient CDN edge 403",
    "provider temporarily unavailable",
    "provider rate limit hit",
)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """True for gateway/edge classes where an identical retry succeeds."""
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_PROVIDER_MARKERS)

logger = logging.getLogger(__name__)

MAX_TASKS = 256
MAX_EDGES = 1024
_MAX_RESPONSE_BYTES = 64 * 1024

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n|\n```$")

_FORCED_REJECTION_RE = re.compile(r"status (4\d\d)")
_TRANSIENT_STATUSES = ("408", "429")


def _forced_tool_choice_rejection_reason(exc: Exception) -> str | None:
    """Detect gateways that hard-refuse the forced tool-call shape.

    The adapters collapse upstream HTTP failures into ``ProviderError``
    messages like "provider HTTP request failed with status 400". A
    non-transient 4xx against a tools-bearing request means this
    provider will never honor forcing; treating it as a one-time
    degradation signal (not an outage) keeps decomposition functional.
    Transient classes (408/429) and 5xx stay transport errors.
    """
    message = str(exc)
    match = _FORCED_REJECTION_RE.search(message)
    if not match:
        return None
    status = match.group(1)
    if status in _TRANSIENT_STATUSES:
        return None
    return f"HTTP {status}"


DECOMPOSITION_SYSTEM_PROMPT = (
    "You are Zero's task planner. Split the approved plan into an ordered "
    "set of concrete implementation tasks. Respond with ONLY a JSON array; "
    "each element is an object with keys:\n"
    '  "key": short unique identifier (letters, digits, underscore),\n'
    '  "objective": one concrete outcome,\n'
    '  "scope": array of scope strings (may be empty),\n'
    '  "depends_on": array of earlier task keys this task requires.\n'
    '  "evidence": optional array. Evidence is verified at THAT task\'s\n'
    '    completion and failure blocks the pipeline, so match it to what\n'
    '    can actually hold true then:\n'
    '    - read-only analysis/review tasks: ["provider_response"]\n'
    '    - tasks that create files BEFORE the test suite exists: ["diff"]\n'
    '    - a task whose objective is to capture/produce the final diff\n'
    '      (aggregation): ["diff"] — its artifact is the diff itself\n'
    '    - the ONE task that runs and verifies the full test suite (after\n'
    '      tests exist): ["test_report","exit_status"] — NO diff: a\n'
    '      verification task may legitimately change nothing, and an\n'
    '      empty diff would fail it\n'
    "Rules: at most 32 tasks; keep objectives independently verifiable; "
    "dependencies must reference existing keys and form a DAG. "
    "If the plan is simple enough for one task, return exactly one."
)

#: Name of the single forced tool on the native tool-call path (S7).
DECOMPOSITION_TOOL_NAME = "emit_task_graph"

_EMITTED_TASK_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Short unique slug for the task: ASCII letters, digits, "
                            "underscore only. Example: auth_backend"
                        ),
                    },
                    "objective": {
                        "type": "string",
                        "description": "One concrete, independently verifiable outcome.",
                    },
                    "scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Scope labels this task touches (may be empty).",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys of EARLIER tasks this task requires. DAG only.",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "provider_response",
                                "diff",
                                "test_report",
                                "exit_status",
                                "stdout",
                                "stderr",
                                "source_snapshot",
                            ],
                        },
                        "description": (
                            "Durable proof this task must produce. Use "
                            "[\"provider_response\"] for read/analysis/review "
                            "tasks that change no files; use "
                            "[\"diff\"] for tasks that create or modify "
                            "code, and ALSO for a task whose objective is to "
                            "capture the final diff (aggregation) — its "
                            "artifact is the diff itself. Omit to get "
                            "the scheduler default."
                        ),
                    },
                },
                "required": ["key", "objective"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

DECOMPOSITION_TOOL_DECLARATION = ToolDeclaration(
    name=DECOMPOSITION_TOOL_NAME,
    description=(
        "Emit the approved plan's implementation graph as an ordered set of "
        "tasks with dependencies. Exactly one call; no accompanying prose."
    ),
    parameters=_EMITTED_TASK_SCHEMA,
)

DECOMPOSITION_SYSTEM_PROMPT_STRICT = (
    "You are Zero's task planner. Split the approved plan into an ordered "
    "set of concrete implementation tasks.\n\n"
    "OUTPUT CONTRACT (absolute, no exceptions):\n"
    f"1. Your ENTIRE reply MUST be exactly one call to the {DECOMPOSITION_TOOL_NAME} "
    'function with the "tasks" argument filled in.\n'
    "2. ZERO prose before or after. NO explanations. NO markdown. NO code fences. "
    "NO examples outside the function call. The reply starts with the function "
    "call as its first token and ends after it.\n"
    "3. Each task object has EXACTLY these keys:\n"
    '   - "key": short unique slug, ASCII letters/digits/underscore ONLY '
    "(no spaces, no punctuation);\n"
    '   - "objective": ONE concrete verifiable outcome sentence;\n'
    '   - "scope": array of scope label strings (may be empty);\n'
    '   - "depends_on": array of EARLIER task keys, forming a DAG; every '
    "dependency MUST reference an already-declared key; never itself.\n"
    '   - "evidence": optional array naming the durable proof the task '
    "must produce. Evidence is verified immediately at that task's "
    "completion and a failed check blocks the pipeline, so require only "
    "what can truly hold at that point: [\"provider_response\"] for "
    "read/analysis/review tasks that change no files; [\"diff\"] for "
    "tasks that create or modify files while the test suite does not yet "
    "exist; [\"diff\"] ALSO for a task whose objective is to capture or "
    "produce the final diff (aggregation) — its artifact is the diff "
    "itself; [\"test_report\",\"exit_status\"] (NO diff — a verification "
    "task may legitimately change nothing, and an empty diff would fail "
    "it) ONLY for the task that runs and verifies the complete test "
    "suite (it must depend on the tests-existing task). Reserve "
    "test_report for at most one task per plan.\n"
    "4. At most 32 tasks. Keep objectives independently verifiable. "
    "If — and only if — the plan genuinely fits one task, return exactly one."
)

DECOMPOSITION_SYSTEM_PROMPT_ESCALATED = (
    "Your previous reply violated Zero's output contract. Retry ONCE under a "
    "stricter reading of the SAME contract.\n\n"
    + DECOMPOSITION_SYSTEM_PROMPT_STRICT
    + "\n5. Any token that is not part of the single "
    f"{DECOMPOSITION_TOOL_NAME} function call makes the whole answer invalid."
)


@dataclass(frozen=True)
class DecompositionGraph:
    specs: tuple[TaskSpec, ...]
    dependencies: tuple[DependencySpec, ...]


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[: stripped.rfind("```")]
    return stripped.strip()


def validate_decomposition(payload_text: str) -> DecompositionGraph | None:
    """Parse + validate a model response into a dependency graph.

    Returns None on any violation (shape, caps, cycles, dangling keys).
    """
    if not payload_text or not payload_text.strip():
        return None
    if len(payload_text.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        return None
    try:
        raw = json.loads(_strip_code_fence(payload_text))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list) or not raw:
        return None
    if len(raw) > MAX_TASKS:
        return None
    keys_seen: set[str] = set()
    objectives: dict[str, str] = {}
    scopes: dict[str, tuple[str, ...]] = {}
    evidences: dict[str, tuple[str, ...]] = {}
    order: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        key = str(entry.get("key") or "").strip()
        objective = str(entry.get("objective") or "").strip()
        if not key or not key.replace("_", "").isalnum():
            return None
        if not objective or len(objective) > 8192:
            return None
        if key in keys_seen:
            return None
        raw_scope = entry.get("scope")
        if raw_scope is None:
            raw_scope = ()
        if not isinstance(raw_scope, (list, tuple)):
            return None
        scope = tuple(str(item)[:256] for item in list(raw_scope)[:64] if str(item).strip())
        raw_evidence = entry.get("evidence")
        evidence: tuple[str, ...] = ()
        if raw_evidence is not None:
            if not isinstance(raw_evidence, (list, tuple)):
                return None
            cleaned: list[str] = []
            for item in list(raw_evidence)[:8]:
                if not isinstance(item, str) or item.strip() not in _SUPPORTED_EVIDENCE_LABELS:
                    return None
                cleaned.append(item.strip())
            evidence = tuple(dict.fromkeys(cleaned))
        keys_seen.add(key)
        objectives[key] = objective
        scopes[key] = scope
        evidences[key] = evidence
        order.append(key)
    edge_count = 0
    dependencies: list[DependencySpec] = []
    for entry in raw:
        task_key = str(entry.get("key") or "")
        raw_depends = entry.get("depends_on")
        if raw_depends is None:
            raw_depends = ()
        if not isinstance(raw_depends, (list, tuple)):
            return None
        for dep in list(raw_depends)[:64]:
            dep_key = str(dep).strip()
            if dep_key not in keys_seen or dep_key == task_key:
                return None
            dependencies.append(DependencySpec(task_key=task_key, depends_on_key=dep_key))
            edge_count += 1
            if edge_count > MAX_EDGES:
                return None
    if _has_cycle(order, dependencies):
        return None
    specs = tuple(
        TaskSpec(
            key=key,
            objective=objectives[key],
            permitted_scope=scopes[key],
            expected_evidence=evidences[key],
        )
        for key in order
    )
    return DecompositionGraph(specs=tuple(specs), dependencies=tuple(dependencies))


def _tasks_from_payload(payload) -> list | None:
    """Normalize a parsed tool-call argument payload to a raw task list."""
    if isinstance(payload, list):
        return payload[:MAX_TASKS]
    if isinstance(payload, dict):
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            return tasks[:MAX_TASKS]
    return None


def extract_task_graph_payload(response: CanonicalResponse) -> list | None:
    """Extract the raw task list from a forced-tool-call response.

    Preference order:
    1. ``emit_task_graph`` tool calls in ``response.tool_calls`` — the
       native forced path.
    2. A bare JSON array (or ``{"tasks": [...]}``) in the message
       content: models occasionally inline the answer despite forcing,
       and accepting it keeps a valid graph usable. The same strict
       validator runs on either shape afterwards.

    Returns None when neither shape is present or parsable.
    """
    for call in response.tool_calls or ():
        if call.tool_name != DECOMPOSITION_TOOL_NAME or not call.arguments:
            continue
        try:
            payload = json.loads(call.arguments)
        except json.JSONDecodeError:
            continue
        tasks = _tasks_from_payload(payload)
        if tasks is not None:
            return tasks
    content = (response.content or "").strip()
    if not content or len(content.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError:
        return None
    return _tasks_from_payload(payload)


def _token_jaccard(dep_name: str, candidate: str) -> float:
    """Jaccard similarity of two snake_case key names.

    ``build_vendor_dashboard`` scores 2/4 against
    ``develop_vendor_dashboard`` — exactly the shape of real model
    near-misses — while shared-token decoys such as
    ``build_multi_vendor_checkout`` score lower (2/5), giving a unique
    deterministic maximum to repair toward.
    """
    dep_tokens = {part for part in dep_name.lower().split("_") if part}
    cand_tokens = {part for part in candidate.lower().split("_") if part}
    if not dep_tokens or not cand_tokens:
        return 0.0
    union = dep_tokens | cand_tokens
    return len(dep_tokens & cand_tokens) / len(union)


def plan_dependency_repairs(tasks: list) -> dict[tuple[str, str], DependencyRepair]:
    """Compute deterministic repairs for near-miss ``depends_on`` keys.

    Pure planning step (no mutation): returns a mapping from
    ``(task_key, raw_dependency)`` to the resolved :class:`DependencyRepair`.
    An empty mapping means nothing needs fixing (or the input is not
    even a list of objects). The resolution rules are the conservative,
    deterministic ones that have shipped since S7:

    - case-only mismatches resolve through the exact lowercase index;
    - everything else must map to its UNIQUE strictly-highest snake_case
      Jaccard match among known keys with score >= 0.5; any tie or lower
      score yields no repair for that reference (leaving it dangling so
      downstream validation stays strict).
    """
    if not isinstance(tasks, list):
        return {}
    known_keys: list[str] = []
    for entry in tasks:
        if not isinstance(entry, dict):
            return {}
        key = str(entry.get("key") or "").strip()
        if not key:
            return {}
        known_keys.append(key)
    lowered = {key.lower(): key for key in known_keys}

    repairs: dict[tuple[str, str], DependencyRepair] = {}
    for entry in tasks:
        task_key = str(entry["key"]).strip()
        depends = entry.get("depends_on")
        if not isinstance(depends, (list, tuple)):
            continue
        for dep in depends:
            dep_key = str(dep).strip()
            if dep_key == task_key:
                continue  # self-dependency never repaired
            if dep_key in lowered:
                continue
            canonical = lowered.get(dep_key.lower())
            if canonical:
                repairs[(task_key, dep_key)] = DependencyRepair(
                    task_key=task_key,
                    raw_dependency=dep_key,
                    repaired_to=canonical,
                    similarity=None,
                )
                continue
            scored = sorted(
                ((_token_jaccard(dep_key, orig), orig) for orig in known_keys),
                key=lambda item: item[0],
                reverse=True,
            )
            if scored and scored[0][0] >= 0.5 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                repairs[(task_key, dep_key)] = DependencyRepair(
                    task_key=task_key,
                    raw_dependency=dep_key,
                    repaired_to=scored[0][1],
                    similarity=round(scored[0][0], 4),
                )
    return repairs


def apply_dependency_repairs(
    tasks: list, repairs: dict[tuple[str, str], DependencyRepair]
) -> tuple[list | None, str]:
    """Apply planned repairs, collapsing self/duplicate edges per task."""
    if not repairs:
        return tasks, "nothing to repair"

    def remap(task_key: str, dep_key: str) -> str:
        fix = repairs.get((str(task_key).strip(), dep_key))
        return fix.repaired_to if fix is not None else dep_key

    repaired: list[dict] = []
    used_pairs: set[tuple[str, str]] = set()
    for entry in tasks:
        clone = dict(entry)
        depends = clone.get("depends_on")
        if isinstance(depends, (list, tuple)):
            fresh: list[str] = []
            for dep in depends:
                mapped = remap(entry["key"], str(dep).strip())
                pair = (str(clone["key"]), mapped)
                if mapped == str(clone["key"]) or pair in used_pairs:
                    continue  # collapse self/duplicate edges introduced by remap
                used_pairs.add(pair)
                fresh.append(mapped)
            clone["depends_on"] = fresh
        repaired.append(clone)
    names = ", ".join(
        f"{fix.raw_dependency}->{fix.repaired_to}"
        for fix in sorted(repairs.values(), key=lambda r: (r.task_key, r.raw_dependency))
    )
    return repaired, f"repaired dangling refs: {names}"


def repair_dangling_dependencies(tasks: list) -> tuple[list | None, str]:
    """Repair ``depends_on`` entries that point at a mistyped key.

    Models occasionally write a NEAR-MISS key ("build_vendor_dashboard"
    when only "develop_vendor_dashboard" exists); a dangling reference
    otherwise dooms an otherwise-valid graph. Repair is conservative
    and deterministic:

    - each unresolved name must map to its UNIQUE strictly-highest
      snake_case Jaccard match among known keys, scoring >= 0.5; any
      tie or lower score aborts the whole repair (None), leaving the
      escalation/legacy paths in charge;
    - case-only mismatches resolve through the exact lowercase index;
    - duplicate edges introduced by remapping are collapsed while
      preserving per-task dependency order.

    Returns ``(repaired_list | None, note)``.
    """
    if not isinstance(tasks, list):
        return None, "not a list"
    for entry in tasks:
        if not isinstance(entry, dict):
            return None, "non-object entry"
        key = str(entry.get("key") or "").strip()
        if not key:
            return None, "empty key"
    repairs = plan_dependency_repairs(tasks)
    return apply_dependency_repairs(tasks, repairs)


def normalize_task_order(tasks: list) -> list | None:
    """Deterministically reorder tasks so every ``depends_on`` entry
    references an already-listed key.

    Models frequently emit a semantically-valid DAG whose only defect is
    a forward reference (task A depends on B, but B is declared after
    A). Strict revalidation would reject the whole graph over an
    ordering convention. For STRUCTURED tool-call output we can safely
    repair this: edges are preserved exactly, only list order changes,
    and the result must pass the same validator afterwards.

    Returns the reordered list, the original list when already ordered,
    or None when repair is impossible (dangling keys, cycles, wrong
    element shapes).
    """
    if not isinstance(tasks, list):
        return None
    positions: dict[str, int] = {}
    for index, entry in enumerate(tasks):
        if not isinstance(entry, dict):
            return None
        key = str(entry.get("key") or "").strip()
        if not key or key in positions:
            return None
        positions[key] = index

    adjacency: dict[str, list[str]] = {key: [] for key in positions}
    indegree = {key: 0 for key in positions}
    for entry in tasks:
        task_key = str(entry.get("key") or "").strip()
        depends = entry.get("depends_on")
        if depends is None:
            continue
        if not isinstance(depends, (list, tuple)):
            return None
        for dep in depends:
            dep_key = str(dep).strip()
            if dep_key == task_key:
                return None  # self-dependency: semantic defect, not ordering
            if dep_key not in positions:
                return None  # dangling reference: stay strict
            adjacency[dep_key].append(task_key)
            indegree[task_key] += 1

    # Kahn's algorithm with stable tie-breaking on first-declaration
    # order so output is deterministic for identical inputs.
    ready = sorted(
        (key for key, degree in indegree.items() if degree == 0),
        key=positions.__getitem__,
    )
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for neighbor in adjacency[current]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                ready.append(neighbor)
                ready.sort(key=positions.__getitem__)
    if len(ordered) != len(positions):
        return None  # cycle

    by_key = {str(entry.get("key")): entry for entry in tasks}
    return [by_key[key] for key in ordered]


def _has_cycle(order: list[str], edges: list[DependencySpec]) -> bool:
    """Kahn's algorithm over the declared keys."""
    indegree = {key: 0 for key in order}
    adjacency: dict[str, list[str]] = {key: [] for key in order}
    for edge in edges:
        adjacency[edge.depends_on_key].append(edge.task_key)
        indegree[edge.task_key] += 1
    ready = [key for key, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for neighbor in adjacency[current]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                ready.append(neighbor)
    return visited != len(order)


class TaskDecomposer:
    """LLM decomposition with idempotent caching per plan revision.

    Optional S7 recovery analytics: when an ``analytics`` sink is wired
    (the composition root shares one instance process-wide), every
    completed ``decompose()`` call appends one structured outcome —
    attempts spent, rescued near-miss dependency repairs with their
    similarity scores, escalations, degradations and fallbacks — keyed
    by provider/model so per-model output discipline stays observable
    instead of hiding in log lines.
    """

    def __init__(
        self,
        *,
        providers,
        analytics=None,
        transport_retries: int = 4,
        sleeper=time.sleep,
        retry_backoff_seconds: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0),
    ) -> None:
        self._providers = providers
        self._analytics = analytics
        self._cache: dict[str, DecompositionGraph | None] = {}
        # Bounded transport retry budget for TRANSIENT gateway classes
        # (CDN edge 403 / 429 / 503). 0 keeps the historical fail-fast.
        # The backoff curve spans ~110s: the operator's gateway flaps in
        # multi-minute 403 storms (round-7 live evidence), so second-
        # scale retries never outlive one.
        self._transport_retries = max(0, int(transport_retries))
        self._retry_backoff = tuple(
            max(0.0, float(s)) for s in retry_backoff_seconds
        ) or (5.0,)
        self._sleeper = sleeper

    def decompose(
        self,
        *,
        project_id,
        actor_id,
        revision_id: str,
        revision_content,
        provider: str,
        model_name: str,
        source: str = "system",
    ) -> DecompositionGraph | None:
        """Return a validated graph for the revision (cached) or None."""
        cache_key = f"{project_id.value}:{revision_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        started = time.perf_counter()
        try:
            graph, meta = self._decompose_uncached(
                project_id=project_id,
                actor_id=actor_id,
                revision_id=revision_id,
                revision_content=revision_content,
                provider=provider,
                model_name=model_name,
                source=source,
            )
        except Exception:
            self._record_outcome(
                revision_id=revision_id,
                provider=provider,
                model_name=model_name,
                outcome=OUTCOME_DECOMPOSER_EXCEPTION,
                path=PATH_NATIVE,
                attempts_used=0,
                graph=None,
                repairs=(),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        self._record_outcome(
            revision_id=revision_id,
            provider=provider,
            model_name=model_name,
            outcome=meta["outcome"],
            path=meta["path"],
            attempts_used=meta["attempts"],
            graph=graph,
            repairs=meta["repairs"],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        # Transport-level failures say nothing about the model's ability
        # to produce graphs — they are provider weather. Pinning None
        # into the cache here would permanently sentence the revision
        # to single-task fallback over a blip another tick could ride
        # through; leave the slot open instead.
        if meta["outcome"] not in {OUTCOME_TRANSPORT_ERROR, OUTCOME_DECOMPOSER_EXCEPTION}:
            self._cache[cache_key] = graph
        return graph

    def _record_outcome(
        self,
        *,
        revision_id: str,
        provider: str,
        model_name: str,
        outcome: str,
        path: str,
        attempts_used: int,
        graph: DecompositionGraph | None,
        repairs,
        elapsed_ms: int,
    ) -> None:
        """Best-effort analytics append; never breaks decomposition."""
        if self._analytics is None:
            return
        try:
            self._analytics.record(
                DecompositionOutcome(
                    ts_utc=datetime.now(UTC).isoformat(timespec="milliseconds"),
                    revision_id=revision_id,
                    provider=provider,
                    model_name=model_name,
                    outcome=outcome,
                    path=path,
                    attempts_used=attempts_used,
                    task_count=len(graph.specs) if graph is not None else 0,
                    edge_count=len(graph.dependencies) if graph is not None else 0,
                    repairs=tuple(repairs),
                    elapsed_ms=elapsed_ms,
                )
            )
        except Exception:
            logger.warning("decomposition analytics recording failed", exc_info=True)

    def _decompose_uncached(
        self,
        *,
        project_id,
        actor_id,
        revision_id: str,
        revision_content,
        provider: str,
        model_name: str,
        source: str,
    ) -> tuple[DecompositionGraph | None, dict]:
        """Forced tool-call path with escalation, then legacy fallback.

        Ladder (S7):
        1. Strict prompt + ``emit_task_graph`` forced via ``tool_choice``.
        2. On an unusable reply: one escalated re-ask with even stricter
           phrasing.
        3. When the provider/model cannot do native tools at all
           (service-level capability rejection): the original text-JSON
           attempt, unchanged, so decomposition still works there.

        Transport-level failures stop the ladder immediately — hammering
        a failing provider twice more is waste; the scheduler's existing
        single-task fallback and retry budget own recovery from there.

        Returns ``(graph, meta)`` where ``meta`` carries the analytics
        context keys ``path``, ``outcome``, ``attempts`` and ``repairs``.
        """
        from zero.domain.providers import CanonicalMessage, CanonicalRequest

        user_content = (
            f"Objective: {redact_sensitive_text(revision_content.objective)}\n"
            f"Scope: {'; '.join(revision_content.scope)}\n"
            f"Constraints: {'; '.join(revision_content.constraints)}\n"
            f"Acceptance criteria: {'; '.join(revision_content.acceptance_criteria)}"
        )[:16_000]

        meta: dict = {
            "path": PATH_NATIVE,
            "outcome": OUTCOME_SINGLE_TASK_FALLBACK,
            "attempts": 0,
            "repairs": [],
        }
        native_unsupported = False

        for attempt_index, system_prompt in enumerate(
            (
                DECOMPOSITION_SYSTEM_PROMPT_STRICT,
                DECOMPOSITION_SYSTEM_PROMPT_ESCALATED,
            ),
            start=1,
        ):
            request = CanonicalRequest(
                provider=provider,
                model_name=model_name,
                messages=(CanonicalMessage(role="user", content=user_content),),
                max_tokens=4096,
                temperature=0.0,
                tools=(DECOMPOSITION_TOOL_DECLARATION,),
                tool_choice={"type": "function", "name": DECOMPOSITION_TOOL_NAME},
                system_message=system_prompt,
                # Fix 19b (live drill): stream the long decomposition JSON —
                # the gateway edge kills long silent bodies (see planner).
                stream=True,
            )
            transport_attempt = 0
            while True:
                try:
                    _request, response = self._providers.send_request(
                        project_id=project_id,
                        actor_id=actor_id,
                        request=request,
                        idempotency_key=f"decompose:{revision_id}:t{attempt_index}",
                        source=source,
                    )
                    break
                except ValueError as exc:
                    if "native tools" in str(exc):
                        # Registered model lacks the native_tools capability;
                        # forcing would fail every time. Degrade once to the
                        # legacy text contract instead of burning attempts.
                        logger.info(
                            "task decomposition: model %s:%s lacks native tools; "
                            "using legacy text path for revision %s",
                            provider,
                            model_name,
                            revision_id,
                        )
                        native_unsupported = True
                        break
                    raise
                except Exception as exc:  # noqa: BLE001 - fallback is mandatory behavior
                    rejection = _forced_tool_choice_rejection_reason(exc)
                    if rejection is not None:
                        # The gateway itself refused the forced tool-call
                        # shape (non-transient 4xx). One silent degradation
                        # keeps decomposition alive there; never re-send the
                        # same poison payload.
                        logger.info(
                            "task decomposition: provider rejected forced tool-call "
                            "(%s); using legacy text path for revision %s",
                            rejection,
                            revision_id,
                        )
                        native_unsupported = True
                        break
                    if (
                        _is_transient_provider_error(exc)
                        and transport_attempt < self._transport_retries
                    ):
                        # Round-7 fix: a transient CDN edge 403 / 429 / 503
                        # used to silently degrade the graph to the
                        # single-task fallback — the prompt attempts are
                        # output-quality escalation, NOT transport
                        # retries. Burn the bounded transport budget
                        # first; an identical request verifiably succeeds
                        # seconds later on this gateway.
                        transport_attempt += 1
                        backoff = self._retry_backoff[
                            min(transport_attempt - 1, len(self._retry_backoff) - 1)
                        ]
                        self._sleeper(backoff)
                        logger.warning(
                            "task decomposition: transient provider error "
                            "(transport retry %d/%d) for revision %s: %s: %s",
                            transport_attempt,
                            self._transport_retries,
                            revision_id,
                            type(exc).__name__,
                            redact_sensitive_text(str(exc))[:200] or "<no detail>",
                        )
                        continue
                    logger.warning(
                        "task decomposition provider call failed (attempt %d) for revision %s: %s: %s",
                        attempt_index,
                        revision_id,
                        type(exc).__name__,
                        redact_sensitive_text(str(exc))[:300] or "<no detail>",
                    )
                    meta["outcome"] = OUTCOME_TRANSPORT_ERROR
                    return None, meta
            if native_unsupported:
                # The degrade paths above broke the TRANSPORT retry loop;
                # escalate out of the prompt loop to the legacy text path.
                break
            meta["attempts"] = attempt_index
            tasks = extract_task_graph_payload(response)
            graph = None
            if tasks:
                graph = validate_decomposition(json.dumps(tasks))
                if graph is not None:
                    meta["outcome"] = (
                        OUTCOME_NATIVE_FIRST_ASK if attempt_index == 1 else OUTCOME_ESCALATED_OK
                    )
                    return graph, meta
                # Deterministic recovery of STRUCTURED output before
                # spending an escalated re-ask: conservative key
                # repair, then dependency-order normalization. Every
                # candidate must still pass the same validator.
                recovery_candidates: list[tuple[list, str]] = []
                repaired, note = repair_dangling_dependencies(tasks)
                if repaired is not None and repaired != tasks:
                    recovery_candidates.append((repaired, note))
                base = recovery_candidates[-1][0] if recovery_candidates else tasks
                ordered = normalize_task_order(base)
                if ordered is not None and ordered != base:
                    recovery_candidates.append((ordered, "dependency order normalized"))
                for payload, note in recovery_candidates:
                    candidate_graph = validate_decomposition(json.dumps(payload))
                    if candidate_graph is not None:
                        logger.info(
                            "task decomposition recovered structured output (%s) for revision %s",
                            note,
                            revision_id,
                        )
                        if "repaired dangling refs" in note:
                            meta["outcome"] = OUTCOME_RECOVERED_REPAIR
                            # Repair details are computed against the ORIGINAL
                            # payload so the audit trail shows raw typos.
                            meta["repairs"] = list(plan_dependency_repairs(tasks).values())
                        else:
                            meta["outcome"] = OUTCOME_RECOVERED_ORDER
                        return candidate_graph, meta
                # Recovery refused (ambiguous/distant typos, cycles):
                # fall through to the escalated re-ask.
                graph = None
            if graph is not None:
                if attempt_index > 1:
                    logger.info(
                        "task decomposition succeeded on escalated "
                        "re-ask (attempt %d) for revision %s",
                        attempt_index,
                        revision_id,
                    )
                return graph, meta
            logger.info(
                "task decomposition output unusable on attempt %d for "
                "revision %s (%d task-call items extracted); escalating",
                attempt_index,
                revision_id,
                len(tasks or ()),
            )

        if native_unsupported:
            legacy_meta_path = PATH_LEGACY
            legacy_outcome_ok = self._legacy_text_attempt(
                project_id=project_id,
                actor_id=actor_id,
                revision_id=revision_id,
                revision_content=revision_content,
                provider=provider,
                model_name=model_name,
                source=source,
                user_content=user_content,
                meta=meta,
            )
            if legacy_outcome_ok is None and meta.get("legacy_transport_error"):
                meta["path"] = legacy_meta_path
                meta["outcome"] = OUTCOME_TRANSPORT_ERROR
                return None, meta
            meta["attempts"] += 1
            meta["path"] = legacy_meta_path
            if legacy_outcome_ok is not None:
                meta["outcome"] = OUTCOME_LEGACY_TEXT_OK
            else:
                meta["outcome"] = OUTCOME_DEGRADED_LEGACY
            return legacy_outcome_ok, meta
        logger.info(
            "task decomposition output invalid after all attempts for "
            "revision %s; single-task fallback",
            revision_id,
        )
        meta["outcome"] = OUTCOME_SINGLE_TASK_FALLBACK
        return None, meta

    def _legacy_text_attempt(
        self,
        *,
        project_id,
        actor_id,
        revision_id: str,
        revision_content,
        provider: str,
        model_name: str,
        source: str,
        user_content: str,
        meta: dict | None = None,
    ) -> DecompositionGraph | None:
        """Original pre-S7 free-text JSON contract, byte-for-byte."""
        from zero.domain.providers import CanonicalMessage, CanonicalRequest

        try:
            _request, response = self._providers.send_request(
                project_id=project_id,
                actor_id=actor_id,
                request=CanonicalRequest(
                    provider=provider,
                    model_name=model_name,
                    messages=(CanonicalMessage(role="user", content=user_content),),
                    max_tokens=4096,
                    temperature=0.0,
                    system_message=DECOMPOSITION_SYSTEM_PROMPT,
                    # Fix 19b: stream the fallback decomposition too.
                    stream=True,
                ),
                idempotency_key=f"decompose:{revision_id}",
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - fallback is mandatory behavior
            logger.warning(
                "task decomposition provider call failed for revision %s: %s",
                revision_id,
                type(exc).__name__,
            )
            if meta is not None:
                meta["legacy_transport_error"] = True
            return None
        graph = validate_decomposition(response.content or "")
        if graph is None:
            logger.info(
                "task decomposition output invalid for revision %s; single-task fallback",
                revision_id,
            )
        return graph


__all__ = [
    "DECOMPOSITION_SYSTEM_PROMPT",
    "DECOMPOSITION_SYSTEM_PROMPT_ESCALATED",
    "DECOMPOSITION_SYSTEM_PROMPT_STRICT",
    "DECOMPOSITION_TOOL_DECLARATION",
    "DECOMPOSITION_TOOL_NAME",
    "MAX_EDGES",
    "MAX_TASKS",
    "DecompositionGraph",
    "TaskDecomposer",
    "apply_dependency_repairs",
    "extract_task_graph_payload",
    "normalize_task_order",
    "plan_dependency_repairs",
    "repair_dangling_dependencies",
    "validate_decomposition",
]
