"""S4 — REAL feature sweep against the live engine services.

Covers the remaining feature domains with the real LLM
(api.justwoker.icu, claude-opus-5) and the real encrypted store:
  1. ChatService single-turn chat WITH a tool round (real tool loop);
  2. provider fallback routing (explicit fallback model request);
  3. RAG: ingest -> approve -> FTS search -> RetrievalRouter ->
     ContextBuilder (injection ledger + context version);
  4. agent memory: knowledge retrieval through the same router;
  5. compaction: real LLM summarizer + memory-delta extraction;
  6. plugin/skill tool (user:wordcount) invoked for real.
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
    MODEL,
    build_real_services,
    management_project,
    read_state,
    record,
    setup_env,
)

setup_env()


def main() -> int:
    settings, services = build_real_services()
    project = management_project(services)
    owner = project.owner_user_id
    provider = "openai-compatible"
    out: dict = {}

    # -- 1. ChatService: chat WITH tool loop (real LLM + real tool) --------
    print("[1] ChatService chat with tool round")
    from zero.app.chat_service import ChatService, TokenBucketRateLimiter

    chat = ChatService(
        providers=services.providers,
        authorization=services.authorization,
        tools=services.tools,
        rate_limiter=TokenBucketRateLimiter(30),
    )
    turn = chat.complete(
        project_id=project.id,
        actor_id=owner,
        message=(
            "Use the wordcount tool to count the words in the phrase "
            "'zero real run counts words correctly' and report the exact number."
        ),
        provider=provider,
        model_name=MODEL,
        agent_scope="main_worker",
        max_tool_rounds=3,
        source="web",
    )
    print("    content:", turn.content[:180].replace("\n", " "))
    print("    tool calls executed:", [(t["tool_name"], t.get("status")) for t in turn.tool_calls_executed])
    out["chat"] = {
        "content_head": turn.content[:200],
        "tools": [
            {"tool": t["tool_name"], "status": t.get("status"), "result_head": str(t.get("result"))[:120]}
            for t in turn.tool_calls_executed
        ],
        "provider_request_id": turn.provider_request_id,
        "usage": turn.usage,
    }

    # -- 2. Provider fallback routing --------------------------------------
    print("[2] provider fallback routing (explicit fallback model)")
    fallback_model = "claude-opus-4-8"
    from zero.domain.providers import CanonicalMessage, CanonicalRequest

    req = CanonicalRequest(
        provider=provider,
        model_name=fallback_model,
        messages=(CanonicalMessage(role="user", content="Reply with exactly: FALLBACK-OK"),),
        tools=(),
        system_message="You are a routing probe. Answer concisely.",
        max_tokens=32,
    )
    preq, resp = services.providers.send_request_with_fallback(
        project_id=project.id,
        actor_id=owner,
        execution_id=None,
        request=req,
        source="web",
        agent_scope="main_worker",
    )
    print(f"    model={fallback_model} -> {resp.content[:60]!r}")
    out["fallback"] = {"model": fallback_model, "content": resp.content[:80]}

    # -- 3+4. RAG + agent memory -------------------------------------------
    print("[3] RAG ingest -> approve -> search -> retrieve -> context")
    doc = services.artifacts.ingest_rag_document(
        project_id=project.id,
        actor_id=owner,
        source_type="manual",
        source_id=f"realrun-s4-{__import__('uuid').uuid4().hex[:8]}",
        title="textcase conventions",
        content=(
            "The textcase package must use only the Python standard library. "
            "Every public function carries a Google-style docstring with doctest "
            "examples. Tests live in tests/test_convert.py and run via "
            "python3 -m unittest discover -s tests. Word splitting collapses any "
            "run of non-alphanumeric characters and splits camelCase, acronym and "
            "letter/digit boundaries with a Unicode-aware scanner."
        ),
        source="web",
    )
    approved = services.artifacts.approve_rag_document(
        project_id=project.id, doc_id=doc.id, actor_id=owner, source="web"
    )
    print(f"    doc {approved.id.value} state={approved.state}")
    hits = services.artifacts.search_rag(
        project_id=project.id,
        actor_id=owner,
        query="unicode word splitting rules",
        limit=3,
        source="web",
    )
    print(f"    FTS search hits: {len(hits)}; top score={hits[0][1]:.3f}" if hits else "    FTS search hits: 0")
    out["rag"] = {
        "doc_id": approved.id.value,
        "state": approved.state,
        "search_hits": [(d.id.value, round(score, 3)) for d, score in hits],
    }

    # RetrievalRouter with a fresh execution-less ledger needs an
    # execution id; use the latest completed execution for realism.
    import sqlite3

    db = sqlite3.connect("file:/home/z/my-project/zero-real-home/engine.db?mode=ro", uri=True)
    row = db.execute(
        "SELECT id FROM executions WHERE state='completed' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    exec_id = row[0]
    from zero.domain.execution import ExecutionId

    candidates, ledger = services.retrieval.retrieve(
        project_id=project.id,
        execution_id=ExecutionId(exec_id),
        actor_id=owner,
        agent_type_id=None,
        query="how should textcase split words and which conventions apply?",
        budget_tokens=4000,
        context_version=1,
        model_name=MODEL,
    )
    print(f"    retrieval candidates: {[(c.source, c.score) for c in candidates]}")
    print(
        "    ledger: selected="
        f"{len(ledger.selected)} omitted={len(ledger.omitted)} tokens={ledger.total_tokens}"
    )
    out["retrieval"] = {
        "execution_id": exec_id,
        "candidates": [(c.source, c.record_id, round(c.score, 3)) for c in candidates],
        "ledger_selected": len(ledger.selected),
        "ledger_tokens": ledger.total_tokens,
    }

    # -- 5. Compaction with the real LLM summarizer -------------------------
    print("[5] compaction (real LLM summarizer) + memory deltas")
    # NOTE: the production LLM summarizer is wired inside build_services
    # (services.compaction.summarizer = _llm_compaction_summarizer);
    # overriding it here with the raw provider service caused a TypeError
    # in the first sweep pass — do not touch it.
    should = services.compaction.should_compact(
        ExecutionId(exec_id),
        context_window=100000,
    )
    print(f"    should_compact on completed execution (100k window): {should}")
    record_obj = services.compaction.compact(
        project_id=project.id,
        execution_id=ExecutionId(exec_id),
        actor_id=owner,
        system_message="You are an execution-scoped software development worker. "
        "Only report actions supported by durable evidence.",
        user_prefix=f"Project {project.id.value}",
        plan_contract="Objective: verify the textcase package end-to-end.",
        execution_snapshot="{}",
        conversation_messages=[
            {"role": "user", "content": "We designed the textcase package API: three functions (to_snake_case, to_camel_case, to_kebab_case), one shared tokenizer, stdlib only. Decision: Unicode-aware \\W-based splitting; ASCII-only uppercase detection."},
            {"role": "assistant", "content": "Implemented convert.py and __init__.py in the worktree; created tests/test_convert.py with unittest coverage for mixed separators, idempotence, empty input, single words, ALL CAPS."},
            {"role": "user", "content": "The sub-agent review suggested consolidating duplicate test cases; suite went from 30 to 23 tests, still OK. Failure to avoid: never require diff evidence on a verification-only task."},
        ],
        context_window=1200,  # tiny window: force the degradation ladder
        threshold_percent=85,
        model_name=MODEL,
        agent_type_id=None,
        memory_delta_enabled=True,
    )
    summary = record_obj.summary or ""
    print(f"    compaction record {record_obj.id.value} state={record_obj.state} fit_rung={record_obj.fit_rung}")
    print(f"    summary ({len(summary)} chars): {summary[:260]!r}")
    from zero.app.memory_delta import extract_memory_deltas

    deltas = extract_memory_deltas(summary)
    print(f"    memory deltas extracted: {len(deltas)}")
    for d in deltas[:4]:
        print(f"      - [{d.kind}] {d.content[:100]!r}")
    out["compaction"] = {
        "should_compact": should,
        "record_id": record_obj.id.value,
        "state": record_obj.state,
        "fit_rung": record_obj.fit_rung,
        "summary_chars": len(summary),
        "summary_head": summary[:240],
        "deltas": [{"kind": d.kind, "content": d.content[:140]} for d in deltas],
    }

    # -- 6. plugin tool (user:wordcount) direct invocation -------------------
    print("[6] plugin tool user:wordcount direct invoke")
    wc = services.tools.get_tool_by_name("wordcount")
    result = services.tools.invoke(
        project_id=project.id,
        actor_id=owner,
        agent_scope="main_worker",
        tool_name="wordcount",
        input_data={"text": "zero real run counts words correctly"},
        source="web",
    )
    print(f"    wordcount result: {result.output}")
    out["wordcount"] = {"output": result.output, "status": result.status}

    record("features_sweep", out)
    print("\nS4 FEATURE SWEEP COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
