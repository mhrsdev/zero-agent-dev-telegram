"""Unit tests for zero.core.secret — ADR 0007."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from zero.core.secret import (
    CompositeSecretResolver,
    EnvSecretBackend,
    FileSecretBackend,
    SecretError,
    SecretNotFoundError,
    SecretPermissionError,
    SecretValue,
    is_secret_ref,
    parse_secret_ref,
    redact_text,
)

# ---------------------------------------------------------------------- SecretValue

class TestSecretValue:
    def test_repr_does_not_leak_value(self) -> None:
        sv = SecretValue("super-secret-token-12345")
        r = repr(sv)
        assert "super-secret-token-12345" not in r
        assert "***" in r

    def test_str_does_not_leak_value(self) -> None:
        sv = SecretValue("super-secret-token-12345")
        assert str(sv) == "***"

    def test_format_does_not_leak_value(self) -> None:
        sv = SecretValue("super-secret-token-12345")
        assert f"{sv}" == "***"
        assert f"{sv:>{10}}" == "***"

    def test_reveal_returns_value(self) -> None:
        sv = SecretValue("super-secret-token-12345")
        assert sv.reveal() == "super-secret-token-12345"

    def test_json_dumps_does_not_leak_value(self) -> None:
        sv = SecretValue("super-secret-token-12345")
        # When put in a dict, json.dumps uses __repr__ for unknown types...
        # actually no, it uses default=str which falls back to __str__.
        # Let's verify str() is "***".
        d = {"token": sv}
        out = json.dumps(d, default=str)
        assert "super-secret-token-12345" not in out

    def test_length_property(self) -> None:
        sv = SecretValue("hello")
        assert sv.length == 5
        assert len(sv) == 5

    def test_equality(self) -> None:
        a = SecretValue("foo")
        b = SecretValue("foo")
        c = SecretValue("bar")
        assert a == b
        assert a != c


# ---------------------------------------------------------------------- parse_secret_ref

class TestParseSecretRef:
    def test_env_ref(self) -> None:
        provider, path, key = parse_secret_ref("secret://env/MY_TOKEN")
        assert provider == "env"
        assert path == "MY_TOKEN"
        assert key is None

    def test_file_ref_with_key(self) -> None:
        provider, path, key = parse_secret_ref("secret://file/~/.zero/secrets.yaml#telegram_token")
        assert provider == "file"
        assert path == "~/.zero/secrets.yaml"
        assert key == "telegram_token"

    def test_vault_ref(self) -> None:
        provider, path, key = parse_secret_ref("secret://vault/kv/data/zero/prod#bot_token")
        assert provider == "vault"
        assert path == "kv/data/zero/prod"
        assert key == "bot_token"

    def test_invalid_ref_no_provider(self) -> None:
        with pytest.raises(SecretError):
            parse_secret_ref("secret:///just/path")

    def test_invalid_ref_no_path(self) -> None:
        with pytest.raises(SecretError):
            parse_secret_ref("secret://env")

    def test_is_secret_ref(self) -> None:
        assert is_secret_ref("secret://env/FOO") is True
        assert is_secret_ref("plain_value") is False
        assert is_secret_ref("sk-abc123") is False


# ---------------------------------------------------------------------- EnvSecretBackend

class TestEnvSecretBackend:
    def test_resolve_existing_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET_FOO", "value123")
        backend = EnvSecretBackend()
        sv = backend.resolve("secret://env/TEST_SECRET_FOO")
        assert sv.reveal() == "value123"

    def test_resolve_missing_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_SECRET_MISSING", raising=False)
        backend = EnvSecretBackend()
        with pytest.raises(SecretNotFoundError):
            backend.resolve("secret://env/TEST_SECRET_MISSING")

    def test_resolve_empty_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET_EMPTY", "")
        backend = EnvSecretBackend()
        with pytest.raises(SecretNotFoundError):
            backend.resolve("secret://env/TEST_SECRET_EMPTY")

    def test_env_does_not_support_key_fragment(self) -> None:
        backend = EnvSecretBackend()
        with pytest.raises(SecretError):
            backend.resolve("secret://env/FOO#key")

    def test_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET_EXISTS", "value")
        backend = EnvSecretBackend()
        assert backend.exists("secret://env/TEST_SECRET_EXISTS") is True
        assert backend.exists("secret://env/TEST_SECRET_MISSING") is False

    def test_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_SECRET_META", "value")
        backend = EnvSecretBackend()
        meta = backend.metadata("secret://env/TEST_SECRET_META")
        assert meta.configured is True
        assert meta.length_hint == 5


# ---------------------------------------------------------------------- FileSecretBackend

class TestFileSecretBackend:
    def test_resolve_whole_file(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("my-secret-value\n")
        f.chmod(0o600)
        backend = FileSecretBackend()
        sv = backend.resolve(f"secret://file/{f}")
        assert sv.reveal() == "my-secret-value"

    def test_resolve_with_yaml_key(self, tmp_path: Path) -> None:
        f = tmp_path / "secrets.yaml"
        f.write_text("telegram_token: abc123\ngithub_token: def456\n")
        f.chmod(0o600)
        backend = FileSecretBackend()
        sv = backend.resolve(f"secret://file/{f}#telegram_token")
        assert sv.reveal() == "abc123"

    def test_permission_too_open_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("my-secret-value\n")
        f.chmod(0o644)  # group/other read — too open
        backend = FileSecretBackend()
        with pytest.raises(SecretPermissionError):
            backend.resolve(f"secret://file/{f}")

    def test_permission_0400_ok(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("my-secret-value\n")
        f.chmod(0o400)  # owner read only — OK
        backend = FileSecretBackend()
        sv = backend.resolve(f"secret://file/{f}")
        assert sv.reveal() == "my-secret-value"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        backend = FileSecretBackend()
        with pytest.raises(SecretNotFoundError):
            backend.resolve(f"secret://file/{tmp_path / 'nonexistent.txt'}")

    def test_missing_key_in_yaml_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "secrets.yaml"
        f.write_text("foo: bar\n")
        f.chmod(0o600)
        backend = FileSecretBackend()
        with pytest.raises(SecretNotFoundError):
            backend.resolve(f"secret://file/{f}#missing_key")


# ---------------------------------------------------------------------- CompositeSecretResolver

class TestCompositeSecretResolver:
    def test_routes_to_env_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_COMP_FOO", "value123")
        r = CompositeSecretResolver()
        sv = r.resolve("secret://env/TEST_COMP_FOO")
        assert sv.reveal() == "value123"

    def test_routes_to_file_backend(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("file-value\n")
        f.chmod(0o600)
        r = CompositeSecretResolver()
        sv = r.resolve(f"secret://file/{f}")
        assert sv.reveal() == "file-value"

    def test_unknown_provider_raises(self) -> None:
        r = CompositeSecretResolver()
        with pytest.raises(SecretNotFoundError):
            r.resolve("secret://unknown_provider/foo")


# ---------------------------------------------------------------------- redaction patterns

class TestRedactionPatterns:
    def test_telegram_bot_token_redacted(self) -> None:
        token = "1234567890:" + "ABCDEF1234567890abcdefghijklmnopqrstuv"
        text = f"token={token}"
        redacted = redact_text(text)
        assert token not in redacted
        assert "***" in redacted

    def test_openai_key_redacted(self) -> None:
        text = "api_key=sk-abcdefghijklmnopqrstuvwxyz123456"
        redacted = redact_text(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in redacted

    def test_github_token_redacted(self) -> None:
        text = "token=ghp_abcdefghijklmnopqrstuvwxyz1234567890ABCD"
        redacted = redact_text(text)
        assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890ABCD" not in redacted

    def test_bearer_redacted(self) -> None:
        text = "Authorization: Bearer abcdef1234567890abcdef1234567890"
        redacted = redact_text(text)
        assert "Bearer abcdef1234567890abcdef1234567890" not in redacted

    def test_no_false_positive_on_short_strings(self) -> None:
        # Short strings that don't match any pattern should pass through.
        assert redact_text("hello world") == "hello world"
        assert redact_text("") == ""

    def test_url_password_redacted(self) -> None:
        text = "https://user:secretpassword123@example.com/path"
        redacted = redact_text(text)
        assert "secretpassword123" not in redacted
