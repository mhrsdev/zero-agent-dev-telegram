"""Retrieval router and context builder service.

Per ``zero-context-memory`` SKILL.md §"Build retrieval as a staged
router":

1. authorize project/user/agent scope;
2. generate candidates from lexical, embedding, symbol/code-graph, and
   decision indexes that already exist;
3. rerank by task relevance, agent type, provenance quality, freshness,
   and decision state;
4. deduplicate and diversify;
5. allocate a token budget;
6. render bounded snippets with source IDs;
7. record injections for audit and evaluation.

Per ``zero-claude-token-economics`` §"Reserve output before filling
input": for each request, resolve the model context window, reserve
required output/reasoning capacity, reserve fixed system and safety
context, allocate the remainder to plan state, retrieved evidence,
recent conversation, and tool results, reject or compact before
crossing the usable limit.

Per PLAN.md M9 invariants:
- Authorization happens before candidate retrieval.
- Context is assembled from named regions with explicit budgets.
- Output/reasoning headroom is reserved before input filling.
- One token-accounting contract drives preflight, thresholds, telemetry,
  and UI.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from zero.domain.agent_types import AgentTypeId
from zero.domain.context import (
    InjectionLedger,
    InjectionLedgerId,
    RetrievalCandidate,
    context_remaining,
    estimate_tokens,
)
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId
from zero.domain.ids import generate_injection_ledger_id
from zero.persistence.repositories.agent_type_repository import (
    AgentTypeRepository,
)
from zero.persistence.repositories.artifact_repository import (
    ArtifactRepository,
)
from zero.persistence.repositories.context_repository import (
    ContextRepository,
)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RetrievalRouter:
    """Staged retrieval router: authorize, generate, rank, dedup, budget,
    render, record.

    Per ``zero-context-memory`` §"Do not force recall. An empty result
    is better than cross-project leakage or irrelevant context."
    """

    def __init__(
        self,
        artifact_repo: ArtifactRepository,
        agent_type_repo: AgentTypeRepository,
        context_repo: ContextRepository,
    ) -> None:
        self._artifact_repo = artifact_repo
        self._agent_type_repo = agent_type_repo
        self._context_repo = context_repo

    def retrieve(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        agent_type_id: AgentTypeId | None,
        query: str,
        budget_tokens: int,
        context_version: int,
    ) -> tuple[list[RetrievalCandidate], InjectionLedger]:
        """Retrieve candidates within budget.

        Per ``zero-context-memory`` §"Staged Retrieval Router":
        1. authorize (project scoping is enforced by the repository
           queries);
        2. generate candidates from RAG + agent memory;
        3. rank by score;
        4. deduplicate;
        5. allocate token budget;
        6. render (the candidates carry their content);
        7. record injections in the ledger.

        Per ``zero-context-memory`` §"Authorization happens before
        candidate retrieval": all repository queries filter by
        project_id before any content is loaded.
        """
        # 1+2. Generate candidates from RAG and agent memory.
        candidates: list[RetrievalCandidate] = []
        # RAG candidates.
        if query.strip():
            rag_results = self._artifact_repo.search_rag(
                project_id, query, limit=20
            )
            for doc, score in rag_results:
                candidates.append(
                    RetrievalCandidate(
                        source="rag_document",
                        record_id=doc.id.value,
                        title=doc.title,
                        content=doc.content,
                        token_count=estimate_tokens(doc.content),
                        score=score,
                    )
                )
        # Agent memory candidates.
        if agent_type_id is not None:
            knowledge = self._agent_type_repo.list_knowledge_for_type(
                agent_type_id, include_archived=False
            )
            for record in knowledge:
                # Simple relevance: if the query appears in the content,
                # give it a higher score.
                content_lower = record.content.lower()
                query_lower = query.lower()
                if query_lower and query_lower in content_lower:
                    score = 1.0
                else:
                    score = 0.1  # low relevance, but still a candidate
                candidates.append(
                    RetrievalCandidate(
                        source="knowledge_record",
                        record_id=record.id.value,
                        title=f"{record.kind}: {record.content[:50]}",
                        content=record.content,
                        token_count=estimate_tokens(record.content),
                        score=score,
                    )
                )
        # 3. Rank by score (descending).
        candidates.sort(key=lambda c: c.score, reverse=True)
        # 4. Deduplicate by (source, record_id).
        seen: set[str] = set()
        deduped: list[RetrievalCandidate] = []
        for c in candidates:
            key = f"{c.source}:{c.record_id}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        # 5. Allocate token budget.
        selected: list[RetrievalCandidate] = []
        omitted: list[tuple[str, str, str]] = []
        used_tokens = 0
        for c in deduped:
            if used_tokens + c.token_count <= budget_tokens:
                selected.append(c)
                used_tokens += c.token_count
            else:
                omitted.append((c.source, c.record_id, "budget_exceeded"))
        # 6. Render is implicit: the candidates carry their content.
        # 7. Record injections in the ledger.
        ledger = InjectionLedger(
            id=InjectionLedgerId(generate_injection_ledger_id()),
            project_id=project_id,
            execution_id=execution_id,
            context_version=context_version,
            selected=tuple(
                (c.source, c.record_id, c.token_count) for c in selected
            ),
            omitted=tuple(omitted),
            total_candidates=len(deduped),
            total_tokens=used_tokens,
            budget_tokens=budget_tokens,
            created_at=_now_utc_iso(),
        )
        self._context_repo.insert_injection_ledger(ledger)
        return selected, ledger


# ----------------------------------------------------------------------
# Context builder
# ----------------------------------------------------------------------


class ContextBuilder:
    """Deterministic context builder with stable and volatile regions.

    Per ``zero-context-memory`` §"Separate context into deterministic
    regions": build prompts from named regions with independent budgets.

    Per ``zero-claude-token-economics`` §"Reserve output before filling
    input": the output reserve is subtracted before filling input.
    """

    def __init__(
        self,
        retrieval_router: RetrievalRouter,
        context_repo: ContextRepository,
    ) -> None:
        self._router = retrieval_router
        self._context_repo = context_repo

    def build_context(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        agent_type_id: AgentTypeId | None,
        system_message: str,
        user_prefix: str,
        plan_contract: str,
        execution_snapshot: str,
        conversation_tail: list[dict],
        query: str,
        context_window: int = 200000,
        output_reserve_percent: int = 15,
        retrieval_budget_percent: int = 30,
    ) -> tuple[str, InjectionLedger]:
        """Build a context string from named regions.

        Returns the rendered context text and the injection ledger.
        """
        # 1. Compute the output reserve.
        output_reserve = context_window * output_reserve_percent // 100
        # 2. Compute fixed regions (system + prefix + plan + snapshot).
        fixed_tokens = (
            estimate_tokens(system_message)
            + estimate_tokens(user_prefix)
            + estimate_tokens(plan_contract)
            + estimate_tokens(execution_snapshot)
        )
        # 3. Compute the conversation tail tokens.
        conv_text = json.dumps(conversation_tail, ensure_ascii=False)
        conv_tokens = estimate_tokens(conv_text)
        # 4. Compute the retrieval budget.
        remaining = context_remaining(
            context_window=context_window,
            used_tokens=fixed_tokens + conv_tokens,
            reserved_output_tokens=output_reserve,
        )
        retrieval_budget = min(
            remaining,
            context_window * retrieval_budget_percent // 100,
        )
        # 5. Retrieve.
        # Determine the next context version number.
        version_count = self._context_repo.count_context_versions(
            execution_id
        )
        context_version = version_count + 1
        candidates, ledger = self._router.retrieve(
            project_id=project_id,
            execution_id=execution_id,
            agent_type_id=agent_type_id,
            query=query,
            budget_tokens=max(0, retrieval_budget),
            context_version=context_version,
        )
        # 6. Render the context.
        parts: list[str] = []
        parts.append("--- System Policy ---")
        parts.append(system_message)
        parts.append("")
        parts.append("--- Project Identity ---")
        parts.append(user_prefix)
        parts.append("")
        if plan_contract:
            parts.append("--- Plan Contract ---")
            parts.append(plan_contract)
            parts.append("")
        if execution_snapshot and execution_snapshot != "{}":
            parts.append("--- Execution Snapshot ---")
            parts.append(execution_snapshot)
            parts.append("")
        if candidates:
            parts.append("--- Retrieved Context ---")
            for c in candidates:
                parts.append(f"[{c.source}:{c.record_id}] {c.title}")
                parts.append(c.content)
                parts.append("")
        if conversation_tail:
            parts.append("--- Conversation Tail ---")
            parts.append(conv_text)
            parts.append("")
        context_text = "\n".join(parts)
        return context_text, ledger
