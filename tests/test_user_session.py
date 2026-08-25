"""GAP 4 tests: user-session Telegram gating, adapter, and login flow."""

from __future__ import annotations

import sys

import pytest

from zero.adapters import user_session as us
from zero.adapters.messaging import AdapterError
from zero.adapters.user_session import (
    MAX_OUTBOUND_PER_MINUTE,
    SESSION_LOGIN_DISCLAIMER,
    UserSessionTelegramAdapter,
)


@pytest.fixture(autouse=True)
def _clean_mode_env(monkeypatch):
    monkeypatch.delenv("ZERO_TELEGRAM_MODE", raising=False)


class TestModeGating:
    def test_disabled_by_default(self):
        assert us.user_session_mode_requested() is False
        assert us.user_session_mode_enabled() is False

    def test_requested_but_extra_missing_stays_disabled(self, monkeypatch):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        monkeypatch.setattr(us, "telethon_available", lambda: False)
        assert us.user_session_mode_enabled() is False

    def test_enabled_when_requested_and_available(self, monkeypatch):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        monkeypatch.setattr(us, "telethon_available", lambda: True)
        assert us.user_session_mode_enabled() is True


class TestAdapterConstruction:
    def _adapter_kwargs(self):
        return {
            "api_id": 12345,
            "api_hash": "hash",
            "session_string": "session-blob",
        }

    def test_refuses_when_mode_not_enabled(self):
        with pytest.raises(AdapterError, match="not enabled"):
            UserSessionTelegramAdapter(lambda event: event, **self._adapter_kwargs())

    def test_constructs_when_enabled_with_factory(self, monkeypatch):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        monkeypatch.setattr(us, "telethon_available", lambda: True)
        adapter = UserSessionTelegramAdapter(
            lambda event: event,
            client_factory=lambda s, a, h: object(),
            **self._adapter_kwargs(),
        )
        assert adapter.platform == "telegram"

    def test_outbound_rate_bounds_validated(self, monkeypatch):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        monkeypatch.setattr(us, "telethon_available", lambda: True)
        factory = lambda s, a, h: object()
        with pytest.raises(ValueError):
            UserSessionTelegramAdapter(
                lambda e: e,
                outbound_per_minute=0,
                client_factory=factory,
                **self._adapter_kwargs(),
            )
        with pytest.raises(ValueError):
            UserSessionTelegramAdapter(
                lambda e: e,
                outbound_per_minute=MAX_OUTBOUND_PER_MINUTE + 1,
                client_factory=factory,
                **self._adapter_kwargs(),
            )


class FakeTelethonClient:
    """Records sends; delivers inbound events through a stored handler."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.handler = None
        self.connected = False

    # Telethon-style surface used by the adapter.
    def add_event_handler(self, handler, event=None):  # pragma: no cover
        self.handler = handler

    async def send_message(self, entity, text):
        self.sent.append((str(entity), text))
        return {"id": 42}

    async def disconnect(self):  # pragma: no cover
        self.connected = False


class TestInboundNormalizationAndOutbound:
    def _enabled_adapter(self, monkeypatch, handler):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        monkeypatch.setattr(us, "telethon_available", lambda: True)
        client = FakeTelethonClient()
        adapter = UserSessionTelegramAdapter(
            handler,
            api_id=1,
            api_hash="h",
            session_string="sess",
            client_factory=lambda s, a, h: client,
        )
        adapter.connect()
        return adapter, client

    def test_inbound_event_flows_through_policy_gate(self, monkeypatch):
        received: list[object] = []

        def handler(event):
            received.append(event)
            return event

        adapter, _client = self._enabled_adapter(monkeypatch, handler)
        update = {
            "sender_id": 111,
            "chat_id": -100200300,
            "message": "/start hello",
            "event_id": "us_evt_9",
        }
        result = adapter.dispatch_inbound(update)
        assert len(received) == 1
        event = received[0]
        assert event.platform == "telegram"
        assert event.event_kind == "command"
        assert event.chat_id == "-100200300"
        assert event.external_actor_id == "111"
        assert result is event

    def test_malformed_update_is_ignored(self, monkeypatch):
        received: list[object] = []
        adapter, _client = self._enabled_adapter(monkeypatch, received.append)
        assert adapter.dispatch_inbound({"message": "no ids"}) is None
        assert received == []

    def test_outbound_send_respects_rate_limit(self, monkeypatch):
        received: list[object] = []
        adapter, client = self._enabled_adapter(monkeypatch, received.append)
        for i in range(30):
            adapter.send_message(chat_id="123", text=f"m{i}")
        assert len(client.sent) == 30
        from zero.adapters.user_session import AdapterRateLimitError

        with pytest.raises(AdapterRateLimitError):
            adapter.send_message(chat_id="123", text="one too many")

    def test_send_without_connect_raises(self, monkeypatch):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        monkeypatch.setattr(us, "telethon_available", lambda: True)
        adapter = UserSessionTelegramAdapter(
            lambda e: e,
            api_id=1,
            api_hash="h",
            session_string="s",
            client_factory=lambda s, a, h: FakeTelethonClient(),
        )
        with pytest.raises(AdapterError, match="not connected"):
            adapter.send_message(chat_id="1", text="x")


class TestSessionLogin:
    def test_disclaimer_requires_exact_confirmation(self, monkeypatch):
        monkeypatch.setenv("ZERO_TELEGRAM_MODE", "user_session")
        seen_prompts: list[str] = []
        with pytest.raises(AdapterError, match="aborted"):
            us.run_session_login(
                api_id=1,
                api_hash="h",
                input_prompt=lambda p: "+15550001111",
                secret_prompt=lambda p: "otp",
                confirm_prompt=lambda p: seen_prompts.append(p) or "no",
            )
        assert any(SESSION_LOGIN_DISCLAIMER in p for p in seen_prompts)

    def test_otp_and_password_never_persisted(self, monkeypatch, tmp_path):
        """The login flow returns only the session string; OTP stays local."""
        captured: dict[str, str] = {}

        class FakeLoginClient:
            def __init__(self, *args, **kwargs):
                pass

            def connect(self):
                pass

            def send_code_request(self, phone):
                captured["phone"] = phone
                return type("R", (), {"phone_code_hash": "hash123"})()

            def sign_in(self, phone=None, code=None, password=None, phone_code_hash=None):
                if password is not None:
                    captured["twofa"] = password
                    return
                # This account always demands the 2FA step.
                captured["otp_seen"] = code
                raise RuntimeError("need password")

            def disconnect(self):
                pass

            class session:
                @staticmethod
                def save():
                    return "SESSION_BLOB"

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "telethon":
                module = type(sys)("telethon")
                module.TelegramClient = FakeLoginClient
                sessions = type(sys)("telethon.sessions")
                sessions.StringSession = lambda blob="": object()
                sys.modules["telethon.sessions"] = sessions
                module.sessions = sessions
                sys.modules["telethon"] = module
                return module
            return real_import(name, *args, **kwargs)

        builtins_mod = __import__("builtins")
        original_import = builtins_mod.__import__
        builtins_mod.__import__ = fake_import
        try:
            secrets_given: list[str] = []

            def secret_prompt(prompt_text):
                secrets_given.append("otp-value" if "code" in prompt_text else "pw-value")
                return secrets_given[-1]

            blob = us.run_session_login(
                api_id=1,
                api_hash="h",
                phone="+15550001111",
                input_prompt=lambda p: "+15550001111",
                secret_prompt=secret_prompt,
                confirm_prompt=lambda p: "I UNDERSTAND",
            )
        finally:
            builtins_mod.__import__ = original_import

        assert blob == "SESSION_BLOB"
        # OTP/2FA were consumed by the flow but appear nowhere persistent.
        assert captured["otp_seen"] == "otp-value"
        assert captured["twofa"] == "pw-value"
