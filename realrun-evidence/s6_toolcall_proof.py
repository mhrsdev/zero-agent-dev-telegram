"""S6 — fresh live tool-calling proof against the running server stack.

One real ChatService turn: real LLM decides to call the `wordcount` tool,
the tool executes server-side, and the model reports the result.
Writes a compact proof record to state.json (key: s6_toolcall_proof).
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "/home/z/my-project/scripts/realrun")
from env_common import (  # noqa: E402
    MODEL,
    build_real_services,
    management_project,
    record,
    setup_env,
)
setup_env()


def main() -> int:
    settings, services = build_real_services()
    project = management_project(services)
    from zero.app.chat_service import ChatService, TokenBucketRateLimiter

    chat = ChatService(
        providers=services.providers,
        authorization=services.authorization,
        tools=services.tools,
        rate_limiter=TokenBucketRateLimiter(30),
    )
    t0 = time.time()
    turn = chat.complete(
        project_id=project.id,
        actor_id=project.owner_user_id,
        message=(
            "Use the wordcount tool on the phrase 'tool calling is verified "
            "live right now' and report the exact word count."
        ),
        provider="openai-compatible",
        model_name=MODEL,
        agent_scope="main_worker",
        max_tool_rounds=3,
        source="web",
    )
    elapsed = round(time.time() - t0, 1)
    proof = {
        "elapsed_s": elapsed,
        "reply": turn.content[:200],
        "tool_calls": [
            {"tool": t["tool_name"], "status": t.get("status"),
             "result_head": str(t.get("result"))[:120]}
            for t in turn.tool_calls_executed
        ],
        "provider_request_id": turn.provider_request_id,
    }
    record("s6_toolcall_proof", proof)
    print(json.dumps(proof, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
