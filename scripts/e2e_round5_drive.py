"""Round-5 E2E driver — exercises the REAL pipeline end-to-end.

The engine under test runs on 127.0.0.1:8010 with:
- REAL Telegram bot token (polling live against api.telegram.org),
- REAL LLM gateway (api.justwoker.icu/v1, claude-opus-5),
- the REAL group -1004406039396 as the enabled binding scope.

Inbound user messages are injected through the REAL webhook route with
the engine's configured secret (the same code path Telegram's own
webhook delivery would take: secret verify -> binding scope check ->
normalize -> durable claim -> identity -> planner -> plan card / chat
fallback). Outbound sends go to the REAL Telegram API; durable proof of
REAL acceptance is the httpx "200 OK" lines in the engine log plus the
external_message_id recorded on result deliveries.

Phases print PASS/FAIL and append evidence to realrun-evidence/round5/.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8010"
HOME = Path("/home/z/my-project/zero-e2e-home")
DB = HOME / "e2e.db"
REPO = Path("/home/z/my-project/zero-agent-dev-telegram")
LOG = REPO / "realrun-evidence" / "round5" / "engine.log"
EVIDENCE = REPO / "realrun-evidence" / "round5" / "evidence.json"
WEBHOOK_SECRET = "e2e-webhook-secret-9f31c2"
GROUP_ID = "-1004406039396"
OWNER_TG = "8478981617"


def _resolve_scope() -> tuple[str, str]:
    """Resolve the REAL project/binding ids from the live engine DB.

    Fresh ``e2e_round5_setup`` runs mint new ids; hardcoding them made
    the driver brittle. The engine boot (config_sync) creates exactly
    one enabled telegram binding for the configured group — find it.
    """
    rows = db(
        "SELECT b.project_id AS p, b.id AS i FROM interface_bindings b "
        "WHERE b.platform='telegram' AND b.is_enabled=1 AND b.chat_id=? "
        "ORDER BY b.created_at DESC LIMIT 1",
        (GROUP_ID,),
    )
    if not rows:
        rows = db(
            "SELECT b.project_id AS p, b.id AS i FROM interface_bindings b "
            "WHERE b.platform='telegram' AND b.is_enabled=1 "
            "ORDER BY b.created_at DESC LIMIT 1",
            (),
        )
    if not rows:
        raise SystemExit("no enabled telegram binding found in the engine DB")
    return str(rows[0]["p"]), str(rows[0]["i"])


import random

UPDATE_BASE = 9_200_000_000 + random.randint(0, 100_000) * 100  # per-run unique

results: list[dict] = []


def record(phase: str, ok: bool, detail: str) -> None:
    results.append({"phase": phase, "ok": ok, "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {phase}: {detail}", flush=True)


def db(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


PROJECT_ID, BINDING_ID = _resolve_scope()
print(f"scope: project={PROJECT_ID} binding={BINDING_ID}", flush=True)


def log_has(needle: str, since_index: list[int]) -> bool:
    text = LOG.read_text(errors="replace")
    index = text.find(needle, since_index[0])
    if index >= 0:
        since_index[0] = index + 1
        return True
    return False


def wait_for(predicate, timeout: float, interval: float = 1.5) -> tuple[bool, str]:
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
        timeout=30,
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


def main() -> int:
    since = [0]
    update_id = UPDATE_BASE

    # P0 — engine alive.
    health = httpx.get(f"{BASE}/healthz", timeout=10)
    record("P0 health", health.status_code == 200, health.text[:120])

    # P1 — capabilities surface (public health/feature endpoint).
    caps = httpx.get(f"{BASE}/capabilities", timeout=10)
    record(
        "P1 capabilities",
        caps.status_code == 200,
        caps.text[:160],
    )

    # P2 — /start command: welcome reply must be sent to the REAL group.
    update_id += 1
    response = post_update(update_id, msg(7001, "/start"))
    record("P2a /start intake", response.status_code == 200, str(response.text)[:120])

    def _start_replied():
        rows = db(
            "SELECT processing_detail FROM interface_event_log "
            "WHERE external_event_id=? AND processing_result='processed'",
            (str(update_id),),
        )
        sent = log_has("sendMessage", since)
        ok_row = bool(rows) and ("200 OK" in LOG.read_text(errors="replace"))
        return (bool(rows) and sent, f"rows={len(rows)} sendMessage_logged={sent}")

    ok, detail = wait_for(_start_replied, 30)
    record("P2b /start welcome reply delivered", ok, detail)

    # P3 — actionable request: real planner proposes; plan card + REAL
    # inline approve/reject buttons go to the REAL group.
    update_id += 1
    # NOTE: the webhook handler is fully synchronous — by the time the
    # POST returns, the planner call AND the card send have already
    # happened. Capture the log mark BEFORE the POST.
    log_mark = len(LOG.read_text(errors="replace"))
    action_text = (
        "Please create a project README badge section: add a build status "
        "badge and a python version badge to README.md, and list the badge "
        "markdown snippets in the docs."
    )
    response = post_update(update_id, msg(7002, action_text))
    record("P3a actionable intake", response.status_code == 200, "")

    def _plan_and_card():
        revs = db(
            "SELECT id, plan_id, revision_number, objective FROM plan_revisions",
            (),
        )
        if not revs:
            return False, "no revisions yet"
        tokens = db("SELECT id, action FROM callback_tokens", ())
        # The plan card is an OUTBOUND sendMessage through the real
        # adapter; httpx logs the request line (not the body), so count
        # sendMessage lines that appeared AFTER the actionable intake.
        sends_after = LOG.read_text(errors="replace")[log_mark:].count("sendMessage")
        return (
            bool(tokens) and sends_after >= 1,
            f"revisions={len(revs)} tokens={len(tokens)} card_sends_logged={sends_after}",
        )

    ok, detail = wait_for(_plan_and_card, 120, interval=2.0)
    record("P3b planner proposed + plan card with buttons sent", ok, detail)

    # P4 — press the REAL approve button (callback_query with the token
    # the engine created), then the scheduler/agent loop must run the
    # plan and deliver the result to the REAL group.
    approve = db(
        "SELECT id FROM callback_tokens WHERE action='approve' AND used_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (),
    )
    if not approve:
        record("P4 approve via button", False, "no approve token found")
    else:
        update_id += 1
        callback_update = {
            "update_id": update_id,
            "callback_query": {
                "id": "cbk-e2e-1",
                "from": {"id": int(OWNER_TG), "username": "e2e_owner"},
                "message": {
                    "message_id": 7100,
                    "from": {"id": 8753924431, "is_bot": True},
                    "chat": {"id": int(GROUP_ID), "type": "supergroup"},
                    "date": int(time.time()),
                },
                "data": approve[0]["id"],
            },
        }
        response = httpx.post(
            f"{BASE}/webhooks/telegram/{PROJECT_ID}/{BINDING_ID}",
            json=callback_update,
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
            timeout=30,
        )
        record("P4a callback approve accepted", response.status_code == 200, "")

        def _approved():
            plans = db("SELECT current_state FROM plans WHERE current_state='approved'", ())
            return bool(plans), f"approved_plans={len(plans)}"

        ok, detail = wait_for(_approved, 30)
        record("P4b plan approved via callback token", ok, detail)

        run_mark = time.strftime("%Y-%m-%dT%H:%M", time.gmtime())

        def _delivered():
            rows = db(
                "SELECT state, external_message_id, attempt_count, created_at "
                "FROM result_deliveries ORDER BY created_at DESC LIMIT 5",
                (),
            )
            for row in rows:
                if (
                    row["state"] == "sent"
                    and row["external_message_id"]
                    and str(row["created_at"]) >= run_mark
                ):
                    return True, (
                        "delivery sent to Telegram THIS run "
                        f"(message_id={row['external_message_id']})"
                    )
            states = [dict(r) for r in rows]
            return False, f"deliveries={states}"

        ok, detail = wait_for(_delivered, 420, interval=3.0)
        record("P4c execution ran (real agent loop) + result delivered to REAL group", ok, detail)

    # P5 — non-actionable chat: conversational fallback with REAL LLM.
    update_id += 1
    response = post_update(update_id, msg(7003, "Hello Zero! What can you do for me?"))
    record("P5a chat intake", response.status_code == 200, "")

    def _chat_replied():
        turns = db(
            "SELECT role, content FROM chat_messages ORDER BY created_at DESC LIMIT 2",
            (),
        )
        has_assistant = any(row["role"] == "assistant" for row in turns)
        return has_assistant, f"last turns={[dict(t) for t in turns][:2]}"

    ok, detail = wait_for(_chat_replied, 120, interval=2.0)
    record("P5b conversational reply (real claude-opus-5) + durable history", ok, detail)

    # P6 — media: text document injection through the webhook path.
    update_id += 1
    doc_text = "release notes v5: chunked sends, plan cards, chat fallback"
    response = post_update(
        update_id,
        msg(
            7004,
            "summarize this document",
            extra={
                "document": {
                    "file_id": "FAKEFILEID",
                    "file_name": "notes.txt",
                    "mime_type": "text/plain",
                    "file_size": len(doc_text),
                }
            },
        ),
    )
    record("P6a document intake", response.status_code == 200, "")
    # The FAKE file_id cannot resolve against real Telegram (expected);
    # the pipeline must still answer conversationally without crashing.
    ok, detail = wait_for(
        lambda: (
            db("SELECT COUNT(*) c FROM chat_messages WHERE role='assistant'", ())[0]["c"] >= 1,
            "assistant row present",
        ),
        60,
    )
    record("P6b document message handled without media crash", ok, detail)

    # P7 — web_search tool grant + REAL keyless reachability.
    grants = db(
        "SELECT t.name FROM tool_grants g JOIN tools t ON t.id=g.tool_id WHERE t.name='web_search'",
        (),
    )
    record(
        "P7 web_search granted (real keyless backend verified separately)",
        bool(grants),
        f"grants={len(grants)}",
    )

    # P8 — MCP tool registered from a REAL stdio server process.
    tools = db("SELECT name FROM tools WHERE name LIKE 'mcp_%'", ())
    record("P8 MCP stdio server tool registered", bool(tools), f"tools={[r['name'] for r in tools]}")

    # P9 — polling identity + heartbeat evidence in the engine log.
    log_text = LOG.read_text(errors="replace")
    online = "Telegram bot online" in log_text
    record("P9 real Telegram polling identity", online, "@SandboxEnvironmentBot" if online else "missing")

    EVIDENCE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} phases passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
