"""Network probes used by the wizard/doctor. Read-only, minimal-cost."""

from __future__ import annotations

import os

import httpx


def _telegram_base() -> str:
    """Bot API base; ZERO_TELEGRAM_API_BASE supports gateways/tests."""
    return os.environ.get("ZERO_TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def telegram_get_me(bot_token: str, *, timeout: float = 10.0) -> dict[str, object]:
    """Validate a bot token via getMe; returns {ok, username?, id?, error?}."""
    url = f"{_telegram_base()}/bot{bot_token}/getMe"
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}"}
    data = resp.json()
    result = data.get("result") or {}
    return {
        "ok": bool(data.get("ok")),
        "id": result.get("id"),
        "username": result.get("username"),
        "can_join_groups": result.get("can_join_groups"),
    }


def openai_list_models(base_url: str, api_key: str, *, timeout: float = 15.0) -> dict[str, object]:
    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}"}
    try:
        ids = [m["id"] for m in resp.json().get("data", [])]
    except (KeyError, TypeError, ValueError):
        ids = []
    return {"ok": True, "models": ids}


def anthropic_ping(
    base_url: str, api_key: str, model: str, *, timeout: float = 20.0
) -> dict[str, object]:
    """Minimal 1-token completion to validate auth + reachability."""
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        detail = ""
        if resp.status_code == 429 and resp.headers.get("retry-after"):
            detail = f" (retry_after={resp.headers['retry-after']})"
        return {"ok": False, "error": f"http {resp.status_code}{detail}"}
    return {"ok": True}


def openai_completion_probe(
    base_url: str, api_key: str, model: str, *, timeout: float = 30.0
) -> dict[str, object]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}"}
    return {"ok": True}


def telegram_recent_chats(bot_token: str, *, timeout: float = 12.0) -> dict[str, object]:
    """Best-effort group discovery from one getUpdates poll (offset skip)."""
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates?timeout=0"
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}", "chats": []}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}", "chats": []}
    chats: dict[str, str] = {}
    for update in resp.json().get("result", []):
        msg = (
            update.get("message")
            or update.get("channel_post")
            or (update.get("callback_query") or {}).get("message")
            or {}
        )
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        ctype = chat.get("type")
        title = chat.get("title") or chat.get("username") or (chat.get("first_name") or "")
        if cid is not None and ctype in {"group", "supergroup", "channel"}:
            chats[str(cid)] = title
    return {"ok": True, "chats": [{"chat_id": k, "title": v} for k, v in chats.items()]}
