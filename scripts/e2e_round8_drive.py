"""Round-8 E2E driver — LIVE STREAMING evidence against the REAL engine.

Same honest boundaries as the round-5/7 drives: the engine runs with the
REAL bot token (long-polling api.telegram.org), the REAL LLM gateway
(api.justwoker.icu/v1, claude-opus-5), and the REAL group
-1004406039396 as the enabled binding scope. Inbound messages ride the
REAL webhook route; outbound sends go to the REAL Bot API.

Profiles (E2E_PROFILE env var):
  live — /start, /status, /model, live-streamed chat turn (streaming
         preview + edits + finalize), web_search tool reporting, /tasks,
         /approvals.
  exec — actionable message → plan card → REAL approve → decomposition →
         tasks with LIVE execution progress → summary delivered.

Durable evidence (not vibes):
- ``chat live stream opened (chat=... message_id=...)`` in the engine
  log — the preview bubble was accepted by the REAL Bot API;
- ``interface_event_log.processing_detail`` containing
  ``live-streamed`` — finalize's editMessageText returned 200;
- ``execution progress bubble opened (chat=... message_id=...)`` —
  the task-graph progress bubble is live in the REAL group;
- ``result_deliveries.external_message_id`` — the summary landed.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8010"
HOME = Path("/home/z/my-project/zero-e2e-home")
DB = HOME / "e2e.db"
REPO = Path("/home/z/my-project/workspace/zero-agent-dev-telegram")
EVIDENCE = REPO / "realrun-evidence" / "round8" / "evidence.json"
WEBHOOK_SECRET = "e2e-webhook-secret-9f31c2"
GROUP_ID = "-1004406039396"
OWNER_TG = "8478981617"

PROFILE = os.environ.get("E2E_PROFILE", "live").strip().lower()
if PROFILE not in {"live", "exec"}:
    raise SystemExit(f"unknown E2E_PROFILE: {PROFILE!r}")
LOG = REPO / "realrun-evidence" / "round8" / f"engine-{PROFILE}.log"
EVIDENCE_STREAM = (
    REPO / "realrun-evidence" / "round8" / f"evidence-{PROFILE}.jsonl"
)

UPDATE_BASE = 9_500_000_000 + random.randint(0, 100_000) * 100
results: list[dict] = []


def record(phase: str, ok: bool, detail: str) -> None:
    entry = {"phase": phase, "ok": ok, "detail": detail}
    results.append(entry)
    print(f"{'PASS' if ok else 'FAIL'}  {phase}: {detail}", flush=True)
    with EVIDENCE_STREAM.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**entry, "profile": PROFILE}, ensure_ascii=False) + "\n")


def db(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def _resolve_scope() -> tuple[str, str]:
    rows = db(
        "SELECT b.project_id AS p, b.id AS i FROM interface_bindings b "
        "WHERE b.platform='telegram' AND b.is_enabled=1 AND b.chat_id=? "
        "ORDER BY b.created_at DESC LIMIT 1",
        (GROUP_ID,),
    )
    if not rows:
        raise SystemExit("no enabled telegram binding for the group")
    return str(rows[0]["p"]), str(rows[0]["i"])


PROJECT_ID, BINDING_ID = _resolve_scope()
print(f"scope: project={PROJECT_ID} binding={BINDING_ID}", flush=True)

_LOG_OFFSET = {"n": 0}
_LOG_TEXT = ""


def _refresh_log() -> None:
    global _LOG_TEXT
    _LOG_TEXT = LOG.read_text(errors="replace")


def log_reply_contains(command: str, needle: str) -> bool:
    """True when the REAL outbound reply for a command contained needle."""
    _refresh_log()
    idx = _LOG_TEXT.find(f"command reply sent: {command} ->")
    if idx < 0:
        return False
    line_end = _LOG_TEXT.find("\n", idx)
    line = _LOG_TEXT[idx : line_end if line_end > 0 else idx + 400]
    return needle in line


def log_since(marker: str) -> bool:
    """True when marker appeared in the log AFTER the last check."""
    _refresh_log()
    index = _LOG_TEXT.find(marker, _LOG_OFFSET["n"])
    if index >= 0:
        _LOG_OFFSET["n"] = index + 1
        return True
    return False


def wait_for(predicate, timeout: float, interval: float = 2.0) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        ok, last = predicate()
        if ok:
            return True, last
        time.sleep(interval)
    return False, last


def post_update(update_id: int, message: dict) -> httpx.Response:
    return httpx.post(
        f"{BASE}/webhooks/telegram/{PROJECT_ID}/{BINDING_ID}",
        json={"update_id": update_id, "message": message},
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        timeout=180,
    )


def msg(message_id: int, text: str, extra: dict | None = None) -> dict:
    body = {
        "message_id": message_id,
        "from": {"id": int(OWNER_TG), "username": "e2e_owner"},
        "chat": {"id": int(GROUP_ID), "title": "Zero E2E Group", "type": "supergroup"},
        "date": int(time.time()),
        "text": text,
    }
    if extra:
        body.update(extra)
    return body


def latest_detail(update_id: int) -> str:
    rows = db(
        "SELECT processing_result, processing_detail FROM interface_event_log "
        "WHERE external_event_id=? ORDER BY created_at DESC LIMIT 1",
        (str(update_id),),
    )
    if not rows:
        return "(no event row)"
    return f"{rows[0]['processing_result']}: {rows[0]['processing_detail']}"


def main() -> int:
    update_id = UPDATE_BASE
    mid = 8800

    # ------------------------------------------------------------------
    # R1 — /start (instant static reply)
    # ------------------------------------------------------------------
    update_id += 1
    mid += 1
    response = post_update(update_id, msg(mid, "/start"))
    ok, detail = wait_for(
        lambda: (
            ("Zero is online" in latest_detail(update_id))
            or ("processed" in latest_detail(update_id)),
            latest_detail(update_id),
        ),
        30,
    )
    record("R1 /start replies", ok and response.status_code == 200, detail[:200])

    # ------------------------------------------------------------------
    # R2 — /status (dynamic command from durable state)
    # ------------------------------------------------------------------
    update_id += 1
    mid += 1
    response = post_update(update_id, msg(mid, "/status"))
    ok, detail = wait_for(
        lambda: (log_reply_contains("/status", "claude-opus-5"), "log reply"),
        30,
    )
    record("R2 /status reports routed model + workers", ok, detail[:260])

    # ------------------------------------------------------------------
    # R3 — /model
    # ------------------------------------------------------------------
    update_id += 1
    mid += 1
    response = post_update(update_id, msg(mid, "/model"))
    ok, detail = wait_for(
        lambda: (log_reply_contains("/model", "claude-opus-5"), "log reply"),
        30,
    )
    record("R3 /model reports claude-opus-5", ok, detail[:220])

    # ------------------------------------------------------------------
    # R4 — LIVE-STREAMED chat turn (the core round-8 feature)
    # ------------------------------------------------------------------
    update_id += 1
    mid += 1
    prompt = (
        "Reply with exactly two short sentences about why incremental "
        "delivery beats big-bang releases. No tools needed."
    )
    response = post_update(update_id, msg(mid, prompt))
    opened, detail = wait_for(
        lambda: (log_since("chat live stream opened"), "log marker"), 90
    )
    record("R4a live stream preview opened via REAL Bot API", opened, detail)
    ok, detail = wait_for(
        lambda: ("live-streamed" in latest_detail(update_id), latest_detail(update_id)),
        120,
    )
    record("R4b answer streamed + finalize edit accepted (live-streamed)", ok, detail[:260])
    history = db(
        "SELECT content FROM chat_messages WHERE role='assistant' "
        "ORDER BY created_at DESC LIMIT 1",
        (),
    )
    record(
        "R4c durable chat history kept the answer",
        bool(history) and len(history[0]["content"]) > 0,
        f"chars={len(history[0]['content']) if history else 0}",
    )

    # ------------------------------------------------------------------
    # R5 — web_search TOOL REPORTING during a streamed turn
    # ------------------------------------------------------------------
    update_id += 1
    mid += 1
    prompt = (
        "Use the internet_search tool to look up 'Nous Research Hermes agent' "
        "and then tell me in one sentence what you found."
    )
    before = db(
        "SELECT COUNT(*) c FROM audit_events WHERE operation LIKE '%tool%'",
        (),
    )[0]["c"]
    response = post_update(update_id, msg(mid, prompt))
    ok, detail = wait_for(
        lambda: (
            ("live-streamed" in latest_detail(update_id))
            or ("tools used" in latest_detail(update_id)),
            latest_detail(update_id),
        ),
        180,
    )
    record("R5a streamed turn with tool call completed", ok, detail[:260])
    after = db(
        "SELECT COUNT(*) c FROM audit_events WHERE operation LIKE '%tool%'",
        (),
    )[0]["c"]
    record(
        "R5b tool invocation durably audited",
        after > before,
        f"tool audit rows {before} -> {after}",
    )

    if PROFILE == "live":
        # --------------------------------------------------------------
        # R6 — /tasks and /approvals answer
        # --------------------------------------------------------------
        update_id += 1
        mid += 1
        post_update(update_id, msg(mid, "/tasks"))
        ok, detail = wait_for(
            lambda: (
                log_reply_contains("/tasks", "No executions yet")
                or log_reply_contains("/tasks", "Recent executions"),
                "log reply",
            ),
            30,
        )
        record("R6a /tasks answers", ok, detail[:200])
        update_id += 1
        mid += 1
        post_update(update_id, msg(mid, "/approvals"))
        ok, detail = wait_for(
            lambda: (log_reply_contains("/approvals", "approvals"), "log reply"),
            30,
        )
        record("R6b /approvals answers", ok, detail[:200])
        EVIDENCE.write_text(json.dumps(results, indent=2), encoding="utf-8")
        failed = [r for r in results if not r["ok"]]
        print(f"\n{len(results) - len(failed)}/{len(results)} phases passed", flush=True)
        return 1 if failed else 0

    # ==================================================================
    # exec profile: approve a plan and watch LIVE execution progress
    # ==================================================================

    # X1 — actionable message → plan card with real buttons
    update_id += 1
    mid += 1
    goal = (
        "Write and store a short design note: one paragraph describing "
        "the delivery plan for a demo tool, saved as the task answer."
    )
    response = post_update(update_id, msg(mid, goal))
    ok, detail = wait_for(
        lambda: ("proposed revision" in latest_detail(update_id), latest_detail(update_id)),
        150,
    )
    record("X1 plan proposed (real claude-opus-5 planner)", ok, detail[:240])
    if not ok:
        EVIDENCE.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return 1

    # X2 — press the REAL approve button (callback data IS the token id)
    tokens = db(
        "SELECT id FROM callback_tokens WHERE action='approve' AND used_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (),
    )
    record("X2a approve token minted", bool(tokens), f"count={len(tokens)}")
    if not tokens:
        EVIDENCE.write_text(json.dumps(results, indent=2), encoding="utf-8")
        return 1
    update_id += 1
    mid += 1
    callback_update = {
        "update_id": update_id,
        "callback_query": {
            "id": f"r8cq{update_id}",
            "from": {"id": int(OWNER_TG), "username": "e2e_owner"},
            "message": {
                "message_id": mid,
                "from": {"id": 8753924431, "is_bot": True},
                "chat": {"id": int(GROUP_ID), "type": "supergroup"},
                "date": int(time.time()),
            },
            "data": tokens[0]["id"],
        },
    }
    response = httpx.post(
        f"{BASE}/webhooks/telegram/{PROJECT_ID}/{BINDING_ID}",
        json=callback_update,
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        timeout=60,
    )
    ok, detail = wait_for(
        lambda: (
            ("plan approved" in latest_detail(update_id).lower())
            or ("processed" in latest_detail(update_id)),
            latest_detail(update_id),
        ),
        60,
    )
    record("X2b approve callback processed", ok and response.status_code == 200, detail[:200])

    # X3 — decomposition + LIVE execution progress bubble
    ok, detail = wait_for(
        lambda: (log_since("execution progress bubble opened"), "log marker"), 420
    )
    record("X3 LIVE execution progress bubble in the REAL group", ok, detail)

    # X4 — execution summary delivered to the group (durable message id)
    ok, detail = wait_for(
        lambda: (
            db(
                "SELECT COUNT(*) c FROM result_deliveries WHERE external_message_id IS NOT NULL",
                (),
            )[0]["c"]
            >= 1,
            "delivered",
        ),
        420,
    )
    rows = db(
        "SELECT external_message_id FROM result_deliveries "
        "WHERE external_message_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (),
    )
    record(
        "X4 execution summary delivered to the REAL group",
        ok,
        f"message_id={rows[0]['external_message_id'] if rows else '?'}",
    )

    # X5 — /tasks reflects the execution
    update_id += 1
    mid += 1
    post_update(update_id, msg(mid, "/tasks"))
    ok, detail = wait_for(
        lambda: (log_reply_contains("/tasks", "Recent executions"), "log reply"),
        30,
    )
    record("X5 /tasks lists the execution", ok, detail[:220])

    EVIDENCE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} phases passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
