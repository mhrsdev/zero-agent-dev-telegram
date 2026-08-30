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
from typing import Any

from zero.app.clock import now_utc_iso
from zero.app.authorization_service import AuthorizationService
from zero.domain.agent_types import AgentTypeId, AgentTypeNotFoundError
from zero.domain.context import (
    InjectionLedger,
    InjectionLedgerId,
    RetrievalCandidate,
    context_remaining,
)
from zero.domain.execution import ExecutionId
from zero.domain.identity import ProjectId, UserId
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


def _knowledge_relevance(query: str, content: str) -> float:
    """Term-overlap relevance for agent knowledge records.

    A full verbatim query match still dominates (1.0); otherwise the
    score is proportional to how many distinctive query terms appear in
    the record, so a multi-word query no longer has to occur as one
    verbatim substring to rank relevantly.
    """
    import re

    query_terms = {term for term in re.findall(r"[a-z0-9_]+", query.lower()) if len(term) > 2}
    content_lower = content.lower()
    if not query_terms:
        return 0.1
    if all(term in content_lower for term in query_terms):
        return 1.0
    hits = sum(1 for term in query_terms if term in content_lower)
    coverage = hits / len(query_terms)
    return max(0.1, round(coverage, 4))


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
        authorization_service: AuthorizationService,
    ) -> None:
        self._artifact_repo = artifact_repo
        self._agent_type_repo = agent_type_repo
        self._context_repo = context_repo
        self._authz = authorization_service

    def retrieve(
        self,
        *,
        project_id: ProjectId,
        execution_id: ExecutionId,
        actor_id: UserId,
        agent_type_id: AgentTypeId | None,
        query: str,
        budget_tokens: int,
        context_version: int,
        model_name: str | None = None,
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
        if budget_tokens < 0:
            raise ValueError("budget_tokens must be non-negative")
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="project.view",
        )
        # GAP 11: model-aware counting when a model name is supplied;
        # None keeps the historical bytes÷4 estimate exactly.
        from zero.manage.core.tokenizer import count_tokens

        def _tokens(text: str) -> int:
            return count_tokens(text, model_name)

        # 1+2. Generate candidates from RAG and agent memory.
        candidates: list[RetrievalCandidate] = []
        # RAG candidates.
        if query.strip():
            rag_results = self._artifact_repo.search_rag(project_id, query, limit=20)
            for doc, score in rag_results:
                candidates.append(
                    RetrievalCandidate(
                        source="rag_document",
                        record_id=doc.id.value,
                        title=doc.title,
                        content=doc.content,
                        token_count=_tokens(doc.content),
                        score=score,
                    )
                )
        # Agent memory candidates.
        if agent_type_id is not None:
            try:
                self._agent_type_repo.get_agent_type(project_id, agent_type_id)
            except AgentTypeNotFoundError as exc:
                raise ValueError("agent type does not belong to project") from exc
            knowledge = self._agent_type_repo.list_knowledge_for_type(
                agent_type_id, include_archived=False
            )
            for record in knowledge:
                # Term-overlap relevance rather than verbatim-substring
                # gating: distinctive query terms are matched
                # individually and coverage drives the score.
                score = _knowledge_relevance(query, record.content)
                candidates.append(
                    RetrievalCandidate(
                        source="knowledge_record",
                        record_id=record.id.value,
                        title=f"{record.kind}: {record.content[:50]}",
                        content=record.content,
                        token_count=_tokens(record.content),
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
            selected=tuple((c.source, c.record_id, c.token_count) for c in selected),
            omitted=tuple(omitted),
            total_candidates=len(deduped),
            total_tokens=used_tokens,
            budget_tokens=budget_tokens,
            created_at=now_utc_iso(),
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
        actor_id: UserId,
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
        model_name: str | None = None,
    ) -> tuple[str, InjectionLedger]:
        """Build a context string from named regions.

        Returns the rendered context text and the injection ledger.

        ``model_name`` (GAP 11) switches budget arithmetic to exact
        tiktoken counts when available for the named model; ``None``
        keeps the historical heuristic unchanged.
        """
        # GAP 11 seam.
        from zero.manage.core.tokenizer import count_tokens

        def _tokens(text: str) -> int:
            return count_tokens(text, model_name)

        # 1. Compute the output reserve.
        output_reserve = context_window * output_reserve_percent // 100
        # 2. Compute fixed regions (system + prefix + plan + snapshot).
        fixed_tokens = (
            _tokens(system_message)
            + _tokens(user_prefix)
            + _tokens(plan_contract)
            + _tokens(execution_snapshot)
        )
        # 3. Compute the conversation tail tokens.
        conv_text = json.dumps(conversation_tail, ensure_ascii=False)
        conv_tokens = _tokens(conv_text)
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
        version_count = self._context_repo.count_context_versions(execution_id)
        context_version = version_count + 1
        candidates, ledger = self._router.retrieve(
            project_id=project_id,
            execution_id=execution_id,
            actor_id=actor_id,
            agent_type_id=agent_type_id,
            query=query,
            budget_tokens=max(0, retrieval_budget),
            context_version=context_version,
            model_name=model_name,
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

        # Persist a durable ContextVersion for every rendered context.
        # Per the release audit (§5.3): normal runtime execution must
        # persist a normal ContextVersion so compaction, recovery, and
        # token accounting operate on real state rather than only on
        # compaction-created versions.
        total_tokens = fixed_tokens + conv_tokens + ledger.total_tokens
        self._persist_context_version(
            project_id=project_id,
            execution_id=execution_id,
            version=context_version,
            system_message=system_message,
            user_prefix=user_prefix,
            plan_contract=plan_contract,
            execution_snapshot=execution_snapshot,
            retrieved_context=json.dumps(
                [
                    {"source": source, "record_id": record_id, "tokens": tokens}
                    for source, record_id, tokens in ledger.selected
                ]
            ),
            conversation_tail=conv_text,
            token_count=total_tokens,
        )
        return context_text, ledger

    def _persist_context_version(
        self,
        *,
        project_id: Any,
        execution_id: Any,
        version: int,
        system_message: str,
        user_prefix: str,
        plan_contract: str,
        execution_snapshot: str,
        retrieved_context: str,
        conversation_tail: str,
        token_count: int,
    ) -> None:
        import logging

        from zero.domain.context import ContextVersion, ContextVersionId
        from zero.domain.execution import ExecutionId
        from zero.domain.identity import ProjectId
        from zero.domain.ids import generate_context_version_id

        logger = logging.getLogger(__name__)
        try:
            # Version allocation happens inside the same BEGIN IMMEDIATE
            # write transaction as the insert and activation so two
            # concurrent builders cannot collide on the version number.
            with self._context_repo.database.transaction():
                actual_version = self._context_repo.count_context_versions(execution_id) + 1
                if actual_version != version:
                    logger.debug(
                        "context version drifted from %s to %s for execution %s",
                        version,
                        actual_version,
                        execution_id.value,
                    )
                cv = ContextVersion(
                    id=ContextVersionId(generate_context_version_id()),
                    project_id=ProjectId(project_id.value),
                    execution_id=ExecutionId(execution_id.value),
                    version=actual_version,
                    # Bug fix (real run): this used to be active=True, but
                    # context_versions carries UNIQUE(execution_id) WHERE
                    # active = 1 — inserting the second version of an
                    # execution while the previous one is still active
                    # violated the index and the WHOLE context version was
                    # dropped ("persistence skipped … IntegrityError" on
                    # every task after the first). Insert inactive and let
                    # activate_context_version below do the atomic
                    # deactivate-all → activate-new flip inside this same
                    # transaction.
                    active=False,
                    system_message=system_message,
                    user_prefix=user_prefix,
                    plan_contract=plan_contract,
                    execution_snapshot=execution_snapshot,
                    retrieved_context=retrieved_context,
                    conversation_tail=conversation_tail,
                    compaction_summary="",
                    transcript_artifact_id=None,
                    token_count=token_count,
                    created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._context_repo.insert_context_version(cv, commit=False)
                self._context_repo.activate_context_version(
                    execution_id,
                    actual_version,
                    commit=False,
                )
        except Exception as exc:  # noqa: BLE001 - persistence must not break prompts
            # A persistence failure must not break prompt assembly; the
            # ledger already records the injection facts. It is surfaced
            # loudly rather than swallowed silently.
            logger.warning(
                "context version persistence skipped for execution %s: %s",
                execution_id.value,
                type(exc).__name__,
            )
