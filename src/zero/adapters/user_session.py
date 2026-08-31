"""User-session Telegram mode (GAP 4): the agent acts as a personal account.

Per ``docs/gap-designs/GAP-04-user-session.md``:

- Backed by Telethon under the optional ``[session]`` extra; disabled
  unless the extra is importable AND ``ZERO_TELEGRAM_MODE=user_session``
  is explicitly set.
- Inbound events normalize into the same ``NormalizedEvent`` intake as
  :class:`~zero.adapters.telegram.TelegramAdapter` and flow through the
  identical access-policy gate.
- Outbound sends pass a token-bucket rate limiter (30 msgs/min default,
  hard cap 60) to protect the account from agent-loop spam.
- Session strings live only in the encrypted secret store; OTP codes
  and 2FA passwords exist in memory during interactive login and are
  never written to disk, logs, or audit records.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from zero.domain.interfaces import NormalizedEvent

from .messaging import AdapterError, BaseMessagingAdapter

logger = logging.getLogger(__name__)

#: Outbound anti-spam budget (design: max 30 messages/minute).
DEFAULT_OUTBOUND_PER_MINUTE = 30
MAX_OUTBOUND_PER_MINUTE = 60


class AdapterRateLimitError(AdapterError):
    """The outbound rate limiter refused another send right now."""


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401

        return True
    except ImportError:
        return False


def user_session_mode_requested() -> bool:
    return os.environ.get("ZERO_TELEGRAM_MODE", "").strip().lower() == "user_session"


def user_session_mode_enabled() -> bool:
    """True only when the mode is explicitly requested AND usable."""
    if not user_session_mode_requested():
        return False
    if not telethon_available():
        logger.warning(
            "ZERO_TELEGRAM_MODE=user_session requires the [session] extra; staying on Bot API"
        )
        return False
    return True


SESSION_LOGIN_DISCLAIMER = (
    "WARNING — User-session mode uses YOUR personal Telegram account via "
    "MTProto. This may violate Telegram's Terms of Service and can lead to "
    "a permanent account ban. Prefer Bot API for anything automated.\n"
    "Type 'I UNDERSTAND' (exactly) to continue, anything else to abort."
)


def run_session_login(
    *,
    api_id: int,
    api_hash: str,
    phone: str | None = None,
    input_prompt=None,
    secret_prompt=None,
    confirm_prompt=None,
) -> str:
    """Interactive login: disclaimer → phone → OTP → optional 2FA → session string.

    OTP codes and 2FA passwords are captured through prompt callables
    (getpass by default), held in local variables only, and never
    persisted or logged. Returns the Telethon StringSession blob for
    encrypted storage via SecretService.
    """
    import getpass

    confirm = (confirm_prompt or input)(SESSION_LOGIN_DISCLAIMER + "\n> ")
    if confirm.strip() != "I UNDERSTAND":
        raise AdapterError("user-session login aborted: disclaimer not accepted")

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:  # pragma: no cover - guarded by mode check
        raise AdapterError(
            "the [session] extra is required: pip install 'zero-develop[session]'"
        ) from exc

    phone_value = phone or (input_prompt or input)("Phone number: ")
    otp_prompt = secret_prompt or (lambda p: getpass(p))
    client = TelegramClient(StringSession(), int(api_id), str(api_hash))
    try:
        client.connect()
        result = client.send_code_request(phone_value)
        otp = otp_prompt("Login code (sent via Telegram): ").strip()
        try:
            client.sign_in(phone=phone_value, code=otp, phone_code_hash=result.phone_code_hash)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - telethon raises varied 2FA errors
            logger.debug("sign_in fell through to the 2FA step: %s", type(exc).__name__)
            password = otp_prompt("2FA password: ")
            client.sign_in(password=password)
        session_string = client.session.save()  # type: ignore[attr-defined]
    finally:
        try:
            client.disconnect()
        except Exception as exc:  # noqa: BLE001 - best-effort teardown
            logger.debug("login disconnect failed: %s", type(exc).__name__)
    # Local secrets go out of scope here; nothing was logged or stored.
    return str(session_string)


class _LoopThread:
    """A private asyncio loop so sync callers can drive Telethon."""

    def __init__(self) -> None:
        import asyncio

        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio

        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, timeout: float = 60.0):
        import asyncio

        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


class UserSessionTelegramAdapter(BaseMessagingAdapter):
    """Telethon-backed adapter normalizing into the shared intake contract."""

    platform = "telegram"

    def __init__(
        self,
        event_handler=None,
        *,
        event_sink=None,
        api_id: int,
        api_hash: str,
        session_string: str,
        outbound_per_minute: int = DEFAULT_OUTBOUND_PER_MINUTE,
        client_factory=None,
        loop_thread=None,
        sleeper=None,
    ) -> None:
        super().__init__(event_handler, event_sink=event_sink, sleeper=sleeper)
        if not user_session_mode_enabled():
            raise AdapterError(
                "user-session mode is not enabled: install the [session] extra "
                "and set ZERO_TELEGRAM_MODE=user_session explicitly"
            )
        if outbound_per_minute < 1 or outbound_per_minute > MAX_OUTBOUND_PER_MINUTE:
            raise ValueError(f"outbound_per_minute must be between 1 and {MAX_OUTBOUND_PER_MINUTE}")
        self._api_id = int(api_id)
        self._api_hash = str(api_hash)
        self._session_string = session_string
        self._loop = loop_thread
        self._owns_loop = loop_thread is None
        self._client = None
        self._make_client = client_factory or self._default_client_factory
        from zero.app.chat_service import TokenBucketRateLimiter

        self._limiter = TokenBucketRateLimiter(outbound_per_minute)

    @staticmethod
    def _default_client_factory(session_string, api_id, api_hash):
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(session_string), api_id, api_hash)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        self._client = self._make_client(self._session_string, self._api_id, self._api_hash)

    def _ensure_loop(self):
        if self._loop is None:
            self._loop = _LoopThread()
        return self._loop

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and self._loop is not None:
            try:
                self._submit(client.disconnect())
            except Exception as exc:  # noqa: BLE001 - teardown best-effort
                logger.debug("disconnect during close failed: %s", type(exc).__name__)
        if self._owns_loop and self._loop is not None:
            self._loop.stop()
            self._loop = None

    def _submit(self, coro_like):
        """Await coroutines on the private loop; pass values through."""
        import inspect

        if inspect.iscoroutine(coro_like):
            return self._ensure_loop().submit(coro_like)
        return coro_like

    # ------------------------------------------------------------------
    # Inbound: same NormalizedEvent intake as the Bot API adapter
    # ------------------------------------------------------------------

    def normalize_update(self, update: dict[str, Any]) -> NormalizedEvent | None:
        """Translate a Telethon message mapping into a canonical event."""
        sender_id = update.get("sender_id")
        chat_id = update.get("chat_id")
        if sender_id is None or chat_id is None:
            return None
        content = str(update.get("message") or "")
        kind = "command" if content.startswith("/") else "message"
        # Live audit fix (2026-08-31): the fallback event id used to omit
        # the chat id, so two updates with the same numeric message id
        # from DIFFERENT chats collided on the same
        # (platform, binding_scope, external_event_id) claim key and the
        # second message was swallowed by idempotency. Chat-scoped ids
        # match the Telethon host path (us_{chat_id}_{message.id}).
        event_id = update.get("event_id") or f"us_{chat_id}_{update.get('id', 0)}"
        return NormalizedEvent(
            platform="telegram",
            external_event_id=str(event_id),
            external_actor_id=str(sender_id),
            chat_id=str(chat_id),
            topic_id=None,
            event_kind=kind,  # type: ignore[arg-type]
            content=content,
        )

    def dispatch_inbound(self, update: dict[str, Any]) -> Any:
        event = self.normalize_update(update)
        if event is None:
            return None
        return self._dispatch(event)

    # ------------------------------------------------------------------
    # Outbound: rate-limited sends
    # ------------------------------------------------------------------

    def send_message(self, *, chat_id: str, text: str, **_kwargs) -> Any:
        if not self._limiter.allow(f"user_session:{self._session_string[:8]}"):
            raise AdapterRateLimitError(
                f"outbound rate limit exceeded ({self._limiter.per_minute}/min)"
            )
        if self._client is None:
            raise AdapterError("user-session client is not connected")
        return self._submit(self._client.send_message(str(chat_id), text))


def build_adapter_from_secrets(event_handler, *, resolve_secret) -> UserSessionTelegramAdapter:
    """Compose the adapter from encrypted refs resolved at runtime."""
    api_id = resolve_secret("telegram_session_api_id")
    api_hash = resolve_secret("telegram_session_api_hash")
    session_string = resolve_secret("telegram_session_string")
    return UserSessionTelegramAdapter(
        event_handler,
        api_id=int(str(api_id)),
        api_hash=str(api_hash),
        session_string=str(session_string),
    )


__all__ = [
    "DEFAULT_OUTBOUND_PER_MINUTE",
    "MAX_OUTBOUND_PER_MINUTE",
    "SESSION_LOGIN_DISCLAIMER",
    "AdapterRateLimitError",
    "UserSessionTelegramAdapter",
    "build_adapter_from_secrets",
    "run_session_login",
    "telethon_available",
    "user_session_mode_enabled",
    "user_session_mode_requested",
]
