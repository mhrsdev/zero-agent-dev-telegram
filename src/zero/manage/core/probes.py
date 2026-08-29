"""Network probes used by the wizard/doctor. Read-only, minimal-cost.

Hardening (bug fix): every probe used to build headers/URLs directly
from user input, so a key pasted with a literal ellipsis (a truncated
copy like ``sk-ab…xy`` — the wizard's own draft mask!) crashed httpx
with ``UnicodeEncodeError: 'ascii' codec can't encode character
'\\u2026'`` and took down the wizard with a traceback. All probes now

1. clean/validate secrets first and return ``{"ok": False, "error":
   "…"}`` for anything that could never be a valid key/token,
2. treat *any* request-building or transport exception as a probe
   failure (never let it escape),
3. guard response body parsing (non-JSON bodies from proxies/guest
   wifi portals must not crash the caller).
"""

from __future__ import annotations

import os

import httpx

# Characters that silently corrupt pasted keys: zero-width, BOM, NBSP,
# bidi controls. Never legitimate in an api key or bot token.
_INVISIBLE = "".join(
    chr(c) for c in range(0x200B, 0x200F + 1)
) + "\u2060\ufeff\u00ad\u00a0"


def _clean_secret(value: str) -> str | None:
    """Strip paste artifacts; return None if the value can't be a secret.

    A returned secret is guaranteed ASCII printable, so it is safe in
    HTTP headers and URLs.
    """
    text = str(value or "").strip().strip(_INVISIBLE).strip()
    if not text:
        return None
    # Invisible characters *inside* the value are removed as well —
    # they are never meaningful; remaining non-ASCII (e.g. "…" from a
    # truncated copy) means the user did not paste a real key.
    cleaned = text.translate({ord(ch): None for ch in _INVISIBLE}).strip()
    if not cleaned or not cleaned.isascii() or not cleaned.isprintable():
        return None
    return cleaned


def _secret_error(kind: str = "api key") -> dict[str, object]:
    return {
        "ok": False,
        "error": (
            f"{kind} contains invalid characters (often a truncated copy "
            "ending in '…' or pasted with hidden characters) — re-copy "
            "the full value"
        ),
    }


def clean_secret(value: str) -> str | None:
    """Public sanitize hook for UIs: strip paste artifacts from secrets.

    Returns None when the value could never be a valid key/token (e.g.
    it still contains visible non-ASCII like the '…' of a truncated
    copy). UIs call this at input time so users get a re-prompt instead
    of a failing probe later.
    """
    return _clean_secret(value)


def _telegram_base() -> str:
    """Bot API base; ZERO_TELEGRAM_API_BASE supports gateways/tests."""
    return os.environ.get("ZERO_TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")


def _telegram_proxy() -> str | None:
    """Explicit Telegram proxy (ZERO_TELEGRAM_PROXY_URL), if configured.

    Standard HTTPS_PROXY/ALL_PROXY variables keep working through httpx's
    trust_env default; this covers only the Zero-specific escape hatch so
    doctor/wizard probes exercise the same egress path the engine uses.
    """
    value = (os.environ.get("ZERO_TELEGRAM_PROXY_URL") or "").strip()
    return value or None


def _probe_http_error(exc: BaseException) -> str:
    """Bounded, token-redacted transport-error summary for probe results."""
    from zero.adapters.messaging import redact_bot_token

    text = " ".join(str(exc or "").split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {redact_bot_token(text)[:160]}"


def _http_client(*, proxy: str | None = None) -> httpx.Client:
    """Client for one probe call; honors the Telegram proxy escape hatch."""
    return httpx.Client(proxy=proxy) if proxy else httpx.Client()


def telegram_get_me(bot_token: str, *, timeout: float = 10.0) -> dict[str, object]:
    """Validate a bot token via getMe; returns {ok, username?, id?, error?}."""
    token = _clean_secret(bot_token)
    if token is None:
        return _secret_error("bot token")
    url = f"{_telegram_base()}/bot{token}/getMe"
    try:
        with _http_client(proxy=_telegram_proxy()) as client:
            resp = client.get(url, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {_probe_http_error(exc)}"}
    except Exception as exc:  # noqa: BLE001 - never crash the wizard
        return {"ok": False, "error": f"probe failed: {_probe_http_error(exc)}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}"}
    try:
        data = resp.json()
        result = data.get("result") or {}
    except ValueError:
        return {"ok": False, "error": "non-JSON response body"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "unexpected response body"}
    return {
        "ok": bool(data.get("ok")),
        "id": result.get("id") if isinstance(result, dict) else None,
        "username": result.get("username") if isinstance(result, dict) else None,
        "can_join_groups": result.get("can_join_groups") if isinstance(result, dict) else None,
    }


def openai_list_models(base_url: str, api_key: str, *, timeout: float = 15.0) -> dict[str, object]:
    key = _clean_secret(api_key)
    if key is None:
        return _secret_error()
    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    except Exception as exc:  # noqa: BLE001 - never crash the wizard
        return {"ok": False, "error": f"probe failed: {type(exc).__name__}"}
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
    key = _clean_secret(api_key)
    if key is None:
        return _secret_error()
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    except Exception as exc:  # noqa: BLE001 - never crash the wizard
        return {"ok": False, "error": f"probe failed: {type(exc).__name__}"}
    if resp.status_code != 200:
        detail = ""
        if resp.status_code == 429 and resp.headers.get("retry-after"):
            detail = f" (retry_after={resp.headers['retry-after']})"
        return {"ok": False, "error": f"http {resp.status_code}{detail}"}
    return {"ok": True}


def openai_completion_probe(
    base_url: str, api_key: str, model: str, *, timeout: float = 30.0
) -> dict[str, object]:
    key = _clean_secret(api_key)
    if key is None:
        return _secret_error()
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        resp = httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {type(exc).__name__}"}
    except Exception as exc:  # noqa: BLE001 - never crash the wizard
        return {"ok": False, "error": f"probe failed: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}"}
    return {"ok": True}


def telegram_send_message(
    bot_token: str, chat_id: str, text: str, *, timeout: float = 12.0
) -> dict[str, object]:
    """Deliver one message via the Bot API (setup 'send test message' step).

    Bug fix context: the final wizard step only COLLECTED a chat id and
    never sent anything, so operators believed delivery worked when
    nothing had been verified. This probe performs the real
    ``sendMessage`` round-trip and reports the Telegram message id.
    """
    token = _clean_secret(bot_token)
    if token is None:
        return _secret_error("bot token")
    url = f"{_telegram_base()}/bot{token}/sendMessage"
    try:
        with _http_client(proxy=_telegram_proxy()) as client:
            resp = client.post(url, json={"chat_id": chat_id, "text": text}, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {_probe_http_error(exc)}"}
    except Exception as exc:  # noqa: BLE001 - never crash the wizard
        return {"ok": False, "error": f"probe failed: {_probe_http_error(exc)}"}
    if resp.status_code != 200:
        # Telegram reports bad chat ids as http 400 + a description; the
        # description is the actionable part ("chat not found", "bot is
        # not a member ..."), so surface it instead of a bare status.
        detail = ""
        try:
            detail = str(resp.json().get("description") or "")
        except ValueError:
            pass
        return {
            "ok": False,
            "error": f"http {resp.status_code}{(' — ' + detail) if detail else ''}",
        }
    try:
        data = resp.json()
        result = data.get("result") or {}
    except ValueError:
        return {"ok": False, "error": "non-JSON response body"}
    if not data.get("ok"):
        return {"ok": False, "error": str(data.get("description") or "sendMessage rejected")}
    return {
        "ok": True,
        "message_id": result.get("message_id") if isinstance(result, dict) else None,
    }


def telegram_recent_chats(bot_token: str, *, timeout: float = 12.0) -> dict[str, object]:
    """Best-effort group discovery from one getUpdates poll (offset skip)."""
    token = _clean_secret(bot_token)
    if token is None:
        return {**_secret_error("bot token"), "chats": []}
    url = f"{_telegram_base()}/bot{token}/getUpdates?timeout=0"
    try:
        with _http_client(proxy=_telegram_proxy()) as client:
            resp = client.get(url, timeout=timeout)
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"unreachable: {_probe_http_error(exc)}", "chats": []}
    except Exception as exc:  # noqa: BLE001 - never crash the wizard
        return {"ok": False, "error": f"probe failed: {_probe_http_error(exc)}", "chats": []}
    if resp.status_code != 200:
        return {"ok": False, "error": f"http {resp.status_code}", "chats": []}
    try:
        updates = resp.json().get("result", [])
    except ValueError:
        return {"ok": False, "error": "non-JSON response body", "chats": []}
    chats: dict[str, str] = {}
    for update in updates:
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
