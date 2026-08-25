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
from dataclasses import dataclass

from zero.app.worker_service import DependencySpec, TaskSpec
from zero.domain.audit import redact_sensitive_text

logger = logging.getLogger(__name__)

MAX_TASKS = 256
MAX_EDGES = 1024
_MAX_RESPONSE_BYTES = 64 * 1024

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n|\n```$")

DECOMPOSITION_SYSTEM_PROMPT = (
    "You are Zero's task planner. Split the approved plan into an ordered "
    "set of concrete implementation tasks. Respond with ONLY a JSON array; "
    "each element is an object with keys:\n"
    '  "key": short unique identifier (letters, digits, underscore),\n'
    '  "objective": one concrete outcome,\n'
    '  "scope": array of scope strings (may be empty),\n'
    '  "depends_on": array of earlier task keys this task requires.\n'
    "Rules: at most 32 tasks; keep objectives independently verifiable; "
    "dependencies must reference existing keys and form a DAG. "
    "If the plan is simple enough for one task, return exactly one."
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
        keys_seen.add(key)
        objectives[key] = objective
        scopes[key] = scope
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
            expected_evidence=(),
        )
        for key in order
    )
    return DecompositionGraph(specs=tuple(specs), dependencies=tuple(dependencies))


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
    """LLM decomposition with idempotent caching per plan revision."""

    def __init__(self, *, providers) -> None:
        self._providers = providers
        self._cache: dict[str, DecompositionGraph | None] = {}

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
        graph = self._decompose_uncached(
            project_id=project_id,
            actor_id=actor_id,
            revision_id=revision_id,
            revision_content=revision_content,
            provider=provider,
            model_name=model_name,
            source=source,
        )
        self._cache[cache_key] = graph
        return graph

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
    ) -> DecompositionGraph | None:
        from zero.domain.providers import CanonicalMessage, CanonicalRequest

        user_content = (
            f"Objective: {redact_sensitive_text(revision_content.objective)}\n"
            f"Scope: {'; '.join(revision_content.scope)}\n"
            f"Constraints: {'; '.join(revision_content.constraints)}\n"
            f"Acceptance criteria: {'; '.join(revision_content.acceptance_criteria)}"
        )[:16_000]
        try:
            from zero.app.provider_service import estimate_request_tokens  # noqa: F401

            _request, response = self._providers.send_request(
                project_id=project_id,
                actor_id=actor_id,
                request=CanonicalRequest(
                    provider=provider,
                    model_name=model_name,
                    system_message=DECOMPOSITION_SYSTEM_PROMPT,
                    messages=(CanonicalMessage(role="user", content=user_content),),
                    max_tokens=4096,
                    temperature=0.0,
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
    "MAX_EDGES",
    "MAX_TASKS",
    "DecompositionGraph",
    "TaskDecomposer",
    "validate_decomposition",
]
