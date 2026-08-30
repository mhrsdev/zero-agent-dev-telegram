"""Deterministic messaging transport primitives.

The adapters in this package deliberately do not own project state.  They
only validate a provider envelope, normalize it into ``NormalizedEvent``,
and dispatch through an injected application callback.  HTTP is injected so
all serialization, retry, and authentication behavior can be tested without
live credentials.
"""

from __future__ import annotations

import hmac
import html
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from httpx import RequestError

from zero.domain.interfaces import NormalizedEvent


class AdapterError(RuntimeError):
    """Base class for deterministic interface adapter failures."""


class WebhookAuthError(AdapterError):
    """The webhook signature or secret did not validate."""


class UnsupportedUpdateError(AdapterError):
    """The provider payload is not a supported event envelope."""


class TransportError(AdapterError):
    """An outbound provider request failed after bounded retries."""


class PermanentTransportError(TransportError):
    """The provider rejected the request without a retryable transport signal."""


# Bot tokens ride inside URLs of the form .../bot<id>:<secret>/method —
# httpx embeds the full URL in every RequestError message, so a naive
# error log would publish the credential into `zero logs`.
_BOT_TOKEN_RE = None


def redact_bot_token(text: str) -> str:
    """Redact Telegram bot tokens embedded in error text/URLs."""
    global _BOT_TOKEN_RE
    if _BOT_TOKEN_RE is None:
        import re

        _BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{8,}")
    return _BOT_TOKEN_RE.sub("bot[REDACTED]", str(text or ""))


def _cause_summary(exc: BaseException, *, limit: int = 200) -> str:
    """Bounded, token-redacted one-line summary of an underlying error.

    Bug fix (2026-08-29, flaky-network session): the polling worker logged
    only ``type(exc).__name__`` — operators saw an anonymous
    ``TransportError`` wall every few seconds and could not tell DNS
    failure from TCP refusal from a read timeout from a proxy outage.
    The chained cause is now carried (sanitized) in the error message.
    """
    text = " ".join(str(exc or "").split())
    if not text:
        text = "(no detail)"
    return f"{type(exc).__name__}: {redact_bot_token(text)[:limit]}"


def _error_body_snippet(response: Any, *, limit: int = 200) -> str:
    """Bounded token-redacted body snippet for a non-2xx response.

    Live-streaming edits must REACT to the Bot API's actual rejection
    reason ("Bad Request: message is not modified", "Too Many Requests:
    retry after 17"), but the historical transport error carried ONLY the
    numeric status — the description in the JSON body was discarded, so
    callers could not distinguish a harmless redundant edit from a real
    failure. The snippet is squeezed to one line and the bot token is
    redacted before the message leaves this module; unreadable bodies
    contribute nothing.
    """
    body: Any = None
    try:
        raw = getattr(response, "content", None)
        if raw:
            body = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:  # noqa: BLE001 - snippet is best-effort only
        return ""
    if not isinstance(body, Mapping):
        return ""
    description = str(body.get("description") or "").strip()
    if not description:
        return ""
    squeezed = " ".join(description.split())
    return f" — {redact_bot_token(squeezed)[:limit]}"


class HttpResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class RetryPolicy:
    """Small, explicit retry budget for provider HTTP calls."""

    attempts: int = 3
    backoff_seconds: float = 0.25
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 5:
            raise ValueError("attempts must be between 1 and 5")
        if self.backoff_seconds < 0 or self.backoff_seconds > 30:
            raise ValueError("backoff_seconds must be between 0 and 30")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise ValueError("timeout_seconds must be between 0 and 120")


def safe_render_text(text: str, *, platform: str, limit: int = 4096) -> str:
    """Render bounded, non-authoritative provider text.

    Telegram uses HTML parse mode; Discord receives escaped Markdown-like
    punctuation.  The renderer never interpolates raw exception details or
    callback authority into a message and always enforces a provider bound.
    """
    value = str(text or "")
    if platform == "telegram":
        rendered = html.escape(value, quote=False)
    else:
        rendered = value.replace("\\", "\\\\")
        for char in ("*", "_", "~", "`", ">"):
            rendered = rendered.replace(char, f"\\{char}")
    return rendered[:limit]


class BaseMessagingAdapter:
    """Shared injected transport/retry and event dispatch behavior."""

    platform: str

    def __init__(
        self,
        event_handler: Callable[[NormalizedEvent], Any] | None = None,
        *,
        event_sink: Callable[[NormalizedEvent], Any] | None = None,
        transport: HttpTransport | Callable[..., HttpResponse] | None = None,
        retry_policy: RetryPolicy | None = None,
        retry_attempts: int | None = None,
        retry_backoff_seconds: float | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if event_handler is None:
            event_handler = event_sink
        if event_handler is None:
            raise TypeError("event_handler is required")
        self._event_handler = event_handler
        self._transport = transport
        if retry_policy is None:
            retry_policy = RetryPolicy(
                attempts=retry_attempts if retry_attempts is not None else 3,
                backoff_seconds=(
                    retry_backoff_seconds if retry_backoff_seconds is not None else 0.25
                ),
            )
        self._retry_policy = retry_policy
        self._sleeper = sleeper

    def _dispatch(self, event: NormalizedEvent) -> Any:
        return self._event_handler(event)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: Any = None,
    ) -> HttpResponse:
        if self._transport is None:
            raise TransportError("an injected HTTP transport is required")
        last_error: Exception | None = None
        retryable_statuses = {408, 425, 429}
        for attempt in range(self._retry_policy.attempts):
            try:
                if callable(self._transport) and not hasattr(self._transport, "request"):
                    response = self._transport(
                        method,
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self._retry_policy.timeout_seconds,
                    )
                else:
                    response = self._transport.request(  # type: ignore[union-attr]
                        method,
                        url,
                        headers=headers,
                        json=payload,
                        timeout=self._retry_policy.timeout_seconds,
                    )
                status_code = int(response.status_code)
                if status_code in retryable_statuses or status_code >= 500:
                    last_error = TransportError(
                        f"provider returned retryable HTTP status {status_code}"
                        + _error_body_snippet(response)
                    )
                    if attempt + 1 < self._retry_policy.attempts:
                        self._sleeper(self._retry_policy.backoff_seconds * (2**attempt))
                        continue
                    raise last_error
                if status_code < 200 or status_code >= 300:
                    raise PermanentTransportError(
                        f"provider returned HTTP status {status_code}"
                        + _error_body_snippet(response)
                    )
                return response
            except (TimeoutError, ConnectionError, OSError, RequestError) as exc:
                last_error = exc
                if attempt + 1 < self._retry_policy.attempts:
                    self._sleeper(self._retry_policy.backoff_seconds * (2**attempt))
                    continue
                raise TransportError(
                    f"provider transport failed after retries — {_cause_summary(exc)}"
                ) from exc
        raise TransportError(
            f"provider transport failed — {_cause_summary(last_error)}"
        ) from last_error

    @staticmethod
    def _response_json(response: HttpResponse) -> Any:
        value = response.json
        return value() if callable(value) else value

    @staticmethod
    def _decode_payload(payload: Mapping[str, Any] | bytes | str) -> dict[str, Any]:
        if isinstance(payload, Mapping):
            return dict(payload)
        try:
            decoded = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise UnsupportedUpdateError("webhook body is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise UnsupportedUpdateError("webhook body must be a JSON object")
        return decoded

    decode_payload = _decode_payload


def verify_secret_header(headers: Mapping[str, str], *, header_name: str, expected: str) -> None:
    """Constant-time validation for a shared webhook secret."""
    supplied = next(
        (value for key, value in headers.items() if key.lower() == header_name.lower()),
        "",
    )
    if not expected or not hmac.compare_digest(str(supplied), str(expected)):
        raise WebhookAuthError("webhook secret validation failed")


def verify_ed25519_signature(
    body: bytes,
    headers: Mapping[str, str],
    *,
    public_key: bytes | str,
) -> None:
    """Verify Discord's timestamp + Ed25519 signature envelope."""
    try:
        timestamp = next(
            value for key, value in headers.items() if key.lower() == "x-signature-timestamp"
        )
        signature_hex = next(
            value for key, value in headers.items() if key.lower() == "x-signature-ed25519"
        )
        signature = bytes.fromhex(signature_hex)
        key_bytes = bytes.fromhex(public_key) if isinstance(public_key, str) else public_key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            signature, timestamp.encode("utf-8") + body
        )
    except (StopIteration, ValueError, TypeError, ImportError) as exc:
        raise WebhookAuthError("webhook signature validation failed") from exc
    except Exception as exc:
        # cryptography raises InvalidSignature, which intentionally does not
        # expose provider-specific detail at the HTTP boundary.
        raise WebhookAuthError("webhook signature validation failed") from exc


def _cursor_get(store: Any, platform: str, scope_key: str) -> str | None:
    if store is None:
        return None
    return store.get_cursor(platform, scope_key)


def _cursor_set(store: Any, platform: str, scope_key: str, value: str) -> None:
    if store is None:
        return
    if hasattr(store, "advance_cursor"):
        store.advance_cursor(platform, scope_key, value)
    else:
        store.set_cursor(platform, scope_key, value)


__all__ = [
    "AdapterError",
    "BaseMessagingAdapter",
    "HttpResponse",
    "HttpTransport",
    "PermanentTransportError",
    "RetryPolicy",
    "TransportError",
    "UnsupportedUpdateError",
    "WebhookAuthError",
    "_cursor_get",
    "_cursor_set",
    "redact_bot_token",
    "safe_render_text",
    "verify_ed25519_signature",
    "verify_secret_header",
]
