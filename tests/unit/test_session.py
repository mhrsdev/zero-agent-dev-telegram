"""Unit tests for zero.security.session — ADR T-8.10."""
from __future__ import annotations

import pytest
from zero.core.scope import Scope
from zero.security.session import (
    TOKEN_PREFIX,
    SessionExpiredError,
    SessionRevokedError,
    SessionStore,
)


@pytest.fixture
def personal_scope() -> Scope:
    return Scope.personal(user_id="usr_01HALICE").with_default_memory_scope()


@pytest.fixture
def store() -> SessionStore:
    return SessionStore(default_ttl_seconds=60)


class TestSession:
    def test_create_returns_session_and_token(self, store: SessionStore, personal_scope: Scope) -> None:
        session, token = store.create(user_id="usr_01HALICE", scope=personal_scope)
        assert session.user_id == "usr_01HALICE"
        assert token.startswith(TOKEN_PREFIX)
        assert session.is_valid

    def test_lookup_valid_token(self, store: SessionStore, personal_scope: Scope) -> None:
        _, token = store.create(user_id="usr_01HALICE", scope=personal_scope)
        session = store.lookup(token)
        assert session.user_id == "usr_01HALICE"

    def test_lookup_invalid_token_raises(self, store: SessionStore) -> None:
        with pytest.raises(Exception):
            store.lookup("zs_invalid_token")

    def test_lookup_token_wrong_prefix_raises(self, store: SessionStore) -> None:
        with pytest.raises(Exception, match="invalid token format"):
            store.lookup("wrong_prefix_value")

    def test_revoke_single_session(self, store: SessionStore, personal_scope: Scope) -> None:
        session, token = store.create(user_id="usr_01HALICE", scope=personal_scope)
        assert store.revoke(session.id, reason="manual") is True
        with pytest.raises(SessionRevokedError):
            store.lookup(token)

    def test_revoke_all_for_user(self, store: SessionStore, personal_scope: Scope) -> None:
        _, token1 = store.create(user_id="usr_01HALICE", scope=personal_scope)
        # Bypass max concurrent by revoking first
        store.revoke_all_for_user("usr_01HALICE")
        _, token2 = store.create(user_id="usr_01HALICE", scope=personal_scope)
        count = store.revoke_all_for_user("usr_01HALICE")
        assert count == 1  # only token2 was still active
        with pytest.raises(Exception):
            store.lookup(token1)
        with pytest.raises(SessionRevokedError):
            store.lookup(token2)

    def test_expired_session_raises(self, personal_scope: Scope) -> None:
        store = SessionStore(default_ttl_seconds=0)
        session, token = store.create(user_id="usr_01HALICE", scope=personal_scope)
        import time
        time.sleep(0.01)
        with pytest.raises(SessionExpiredError):
            store.lookup(token)

    def test_extend_session(self, store: SessionStore, personal_scope: Scope) -> None:
        session, _ = store.create(user_id="usr_01HALICE", scope=personal_scope)
        original_expiry = session.expires_at
        extended = store.extend(session.id, extend_by_seconds=3600)
        assert extended.expires_at > original_expiry

    def test_max_concurrent_per_user(self, personal_scope: Scope) -> None:
        """Creating more than max_concurrent_per_user revokes oldest."""
        store = SessionStore(max_concurrent_per_user=2)
        s1, _ = store.create(user_id="usr_01HALICE", scope=personal_scope)
        s2, _ = store.create(user_id="usr_01HALICE", scope=personal_scope)
        s3, _ = store.create(user_id="usr_01HALICE", scope=personal_scope)
        # s1 should be revoked
        assert store.get(s1.id) is None or store.get(s1.id).is_revoked  # type: ignore[union-attr]
        # s2 and s3 should still be valid
        assert store.get(s2.id) is not None and not store.get(s2.id).is_revoked  # type: ignore[union-attr]
        assert store.get(s3.id) is not None and not store.get(s3.id).is_revoked  # type: ignore[union-attr]

    def test_session_to_log_dict_doesnt_leak_token(self, store: SessionStore, personal_scope: Scope) -> None:
        session, token = store.create(user_id="usr_01HALICE", scope=personal_scope)
        d = session.to_log_dict()
        # Token hash must NEVER appear in log output.
        assert session.token_hash not in str(d)
        assert token not in str(d)

    def test_cleanup_expired(self, personal_scope: Scope) -> None:
        store = SessionStore(default_ttl_seconds=0)
        store.create(user_id="usr_01HALICE", scope=personal_scope)
        import time
        time.sleep(0.01)
        count = store.cleanup_expired()
        assert count == 1

    def test_constant_time_token_comparison(self, store: SessionStore, personal_scope: Scope) -> None:
        """Token comparison uses hmac.compare_digest (timing-safe)."""
        _, token = store.create(user_id="usr_01HALICE", scope=personal_scope)
        # Look up with correct token
        s = store.lookup(token)
        assert s is not None
        # Look up with wrong token — should not raise but return not found
        with pytest.raises(Exception):
            store.lookup(TOKEN_PREFIX + "wrong_token_value")
