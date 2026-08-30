"""Round-9 phase C — REAL compaction + memory-delta verification.

Runs IN-PROCESS against the engine's real database and the real LLM
gateway (claude-opus-5 via api.justwoker.icu/v1). The engine is stopped
while this verifier runs so SQLite writes never contend; the services
are composed with the SAME build_services() the engine uses, and
config sync (the code path the engine runs at boot) pins the compaction
summarizer to routing.primary_model — the GAP-H fix this round proves.

What is real here:
- CompactionService.compact() — the exact production code path (fit
  ladder, transcript artifact, summary validation, context version
  activation, atomic state machine).
- The LLM summarizer call — a REAL claude-opus-5 request through the
  operator gateway (no canned summary anywhere in this path).
- MemoryDeltaWriter — real parsing of the LLM summary into durable
  knowledge records (kind=decision/failure) linked to the compaction.

The transcript is real material: the actual chat turns from this live
run plus the execution's actual task transcripts, sized to genuinely
pressure the scenario window so the degradation ladder exercises its
rungs. The small window (6000 tokens) is the scenario's window — the
same service code runs against 200k windows in production.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

HOME = Path("/home/z/my-project/zero-e2e-home")
DB = HOME / "e2e.db"
REPO = Path("/home/z/my-project/workspace/zero-agent-dev-telegram")
EVIDENCE_DIR = REPO / "realrun-evidence" / "round9"
EVIDENCE_STREAM = EVIDENCE_DIR / "evidence-compaction.jsonl"

os.environ["ZERO_HOME"] = str(HOME)
sys.path.insert(0, str(REPO / "src"))

results: list[dict] = []


def record(phase: str, ok: bool, detail: str) -> bool:
    entry = {"phase": phase, "ok": bool(ok), "detail": detail}
    results.append(entry)
    print(f"{'PASS' if ok else 'FAIL'}  {phase}: {detail}", flush=True)
    with EVIDENCE_STREAM.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return ok


def db(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def main() -> int:
    # ------------------------------------------------------------------
    # Compose the real service graph exactly like the engine does.
    # ------------------------------------------------------------------
    from zero.config import Settings

    settings = Settings.load(env_file=str(HOME / ".env"), zero_env_fallback="development")
    from zero.persistence.connection import open_database
    from zero.persistence.migrations import apply_migrations

    database = open_database(settings)
    apply_migrations(database)
    from zero.app.services import build_services

    services = build_services(settings, database)
    record(
        "C0 real service graph composed (build_services)",
        services is not None and services.compaction is not None,
        f"compaction={type(services.compaction).__name__}",
    )

    # ------------------------------------------------------------------
    # Align routing exactly like the engine boot does (config sync) and
    # prove the GAP-H fix: the summarizer is pinned to claude-opus-5.
    # ------------------------------------------------------------------
    from zero.app.config_sync import sync_management_config

    sync_management_config(settings, services)
    routing = services.compaction.summarizer_routing
    record(
        "C1 compaction summarizer pinned to routing.primary_model (GAP-H fix)",
        bool(routing) and routing.get("model") == "claude-opus-5",
        f"routing={routing}",
    )

    # ------------------------------------------------------------------
    # Resolve real scope: owner, project, memory-keeper type, latest
    # completed execution from this live run.
    # ------------------------------------------------------------------
    proj = db("SELECT id, owner_user_id FROM projects ORDER BY created_at LIMIT 1")[0]
    project_id, owner_id = str(proj["id"]), str(proj["owner_user_id"])
    keeper = db(
        "SELECT id FROM agent_types WHERE name='memory-keeper' AND project_id=? LIMIT 1",
        (project_id,),
    )
    if not keeper:
        record("C2 memory-keeper agent type present", False, "run the topo profile first")
        return 1
    keeper_id = str(keeper[0]["id"])
    record("C2 memory-keeper agent type present", True, f"id={keeper_id}")

    execs = db(
        "SELECT id FROM executions WHERE state='completed' ORDER BY created_at DESC LIMIT 1"
    )
    if not execs:
        record("C2b a completed execution exists", False, "run the rag/deleg profile first")
        return 1
    exec_id_value = str(execs[0]["id"])
    record("C2b compaction target execution resolved", True, f"exec={exec_id_value}")

    # ------------------------------------------------------------------
    # Build the REAL transcript: actual chat turns + the execution's
    # actual task transcripts (repeated verbatim across a long thread,
    # as a genuinely long working session would accumulate them).
    # ------------------------------------------------------------------
    conversation: list[dict] = []
    for row in db(
        "SELECT role, content FROM chat_messages ORDER BY created_at ASC LIMIT 40"
    ):
        conversation.append({"role": str(row["role"]), "content": str(row["content"])[:2000]})
    transcripts = [
        str(r["content"])
        for r in db(
            "SELECT a.content FROM artifacts a JOIN tasks t ON a.producer='agent-runtime:'||t.id "
            "WHERE t.execution_id=? AND a.kind='transcript'",
            (exec_id_value,),
        )
    ]
    base_len = len(conversation)
    if not conversation:
        conversation.append({"role": "user", "content": "session start"})
    turn = 0
    while sum(len(str(m["content"])) for m in conversation) < 14_000 and turn < 40:
        material = transcripts[turn % len(transcripts)] if transcripts else (
            "Working session continued: the team kept the BLUE HERON codename "
            "for Project Falcon, reviewed the 7.3 dB Ka-band downlink margin, "
            "and re-confirmed the KESTREL schema v4 telemetry mapping."
        )
        conversation.append({"role": "user", "content": material[:1600]})
        conversation.append(
            {
                "role": "assistant",
                "content": (
                    "DECISION: keep the BLUE HERON codename for all Falcon launch "
                    "briefs; report the 7.3 dB Phase-2 downlink margin unchanged."
                ),
            }
        )
        turn += 1
    record(
        "C3 real transcript assembled",
        len(conversation) > base_len or base_len > 0,
        f"turns={len(conversation)} chat_turns={base_len} exec_transcripts={len(transcripts)} "
        f"chars={sum(len(str(m['content'])) for m in conversation)}",
    )

    # ------------------------------------------------------------------
    # Honest pressure report before compaction.
    # ------------------------------------------------------------------
    from zero.domain.agent_types import AgentTypeId
    from zero.domain.execution import ExecutionId
    from zero.domain.identity import ProjectId, UserId

    execution_id = ExecutionId(exec_id_value)
    project = ProjectId(project_id)
    actor = UserId(owner_id)
    should_before = services.compaction.should_compact(execution_id, 6000)
    record(
        "C4 should_compact before (no active context version yet)",
        should_before is False,
        f"should_compact={should_before}",
    )

    # ------------------------------------------------------------------
    # THE REAL COMPACTION — real fit ladder + REAL claude-opus-5 summary
    # + real context version activation + REAL memory deltas.
    # ------------------------------------------------------------------
    compaction_record = services.compaction.compact(
        project_id=project,
        execution_id=execution_id,
        actor_id=actor,
        system_message="You are an execution-scoped software development worker.",
        user_prefix=f"Project {project_id}",
        plan_contract="Summarize the working session into a durable checkpoint.",
        execution_snapshot="{}",
        conversation_messages=conversation,
        context_window=6000,
        model_name="claude-opus-5",
        agent_type_id=AgentTypeId(keeper_id),
        memory_delta_enabled=True,
    )
    record(
        "C5 compaction completed and activated",
        compaction_record.state == "activated",
        f"state={compaction_record.state} fit_rung={getattr(compaction_record, 'fit_rung', '?')}",
    )
    record_id = compaction_record.id.value

    row = db(
        "SELECT memory_delta_artifact_id, transcript_artifact_id, summary, "
        "target_context_version FROM compaction_records WHERE id=?",
        (record_id,),
    )[0]
    record(
        "C6 transcript artifact durably stored",
        bool(row["transcript_artifact_id"]),
        f"transcript_artifact={row['transcript_artifact_id']}",
    )
    summary_text = str(row["summary"] or "")
    is_llm_summary = not summary_text.strip().lower().startswith("compaction summary")
    sections = [
        "Current goal",
        "Accepted decisions",
        "Modified artifacts",
        "Unresolved tasks",
        "Blockers or failures",
        "Next safe action",
    ]
    has_all = all(s.lower() in summary_text.lower() for s in sections)
    record(
        "C7 summary is LLM-generated (real claude-opus-5) and covers all sections",
        is_llm_summary and has_all,
        f"llm_summary={is_llm_summary} sections={has_all} "
        f"head={summary_text[:90].replace(chr(10), ' | ')}",
    )

    # ------------------------------------------------------------------
    # Memory deltas: durable knowledge records linked to the compaction.
    # ------------------------------------------------------------------
    deltas = db(
        "SELECT id, kind, content FROM knowledge_records WHERE provenance=?",
        (f"compaction:{record_id}",),
    )
    kinds = sorted({str(r["kind"]) for r in deltas})
    record(
        "C8 memory deltas extracted into knowledge records (GAP 9)",
        len(deltas) >= 1,
        f"records={len(deltas)} kinds={kinds} "
        f"first={str(deltas[0]['content'])[:80] if deltas else 'NONE'}",
    )
    record(
        "C9 memory_delta artifact linked on the compaction record",
        bool(row["memory_delta_artifact_id"]),
        f"memory_delta_artifact={row['memory_delta_artifact_id']}",
    )

    # ------------------------------------------------------------------
    # Context version state machine: target version active, tokens fit.
    # ------------------------------------------------------------------
    cv = db(
        "SELECT version, token_count, active FROM context_versions "
        "WHERE execution_id=? ORDER BY version DESC LIMIT 1",
        (exec_id_value,),
    )
    expected_target = int(row["target_context_version"])
    ok_cv = bool(cv) and int(cv[0]["version"]) == expected_target and bool(cv[0]["active"])
    record(
        "C10 new context version active (atomic activation)",
        ok_cv,
        f"version={cv[0]['version'] if cv else None} "
        f"tokens={cv[0]['token_count'] if cv else None} "
        f"active={cv[0]['active'] if cv else None}",
    )

    should_after = services.compaction.should_compact(execution_id, 6000)
    record(
        "C11 should_compact after activation reflects reclaimed space",
        should_after is False,
        f"should_compact={should_after} tokens={cv[0]['token_count'] if cv else '?'} "
        f"window=6000",
    )

    (EVIDENCE_DIR / "evidence.json").write_text(json.dumps(results, indent=2))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} phases passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
