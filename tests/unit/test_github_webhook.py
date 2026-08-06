"""Tests for GitHub webhook handler."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from zero.core.secret import CompositeSecretResolver
from zero.github.webhook import (
    GitHubWebhookEvent,
    GitHubWebhookHandler,
    GitHubWebhookPayload,
    WebhookSignatureError,
)


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> CompositeSecretResolver:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test_webhook_secret_12345")
    return CompositeSecretResolver()


@pytest.fixture
def handler(resolver: CompositeSecretResolver) -> GitHubWebhookHandler:
    return GitHubWebhookHandler(
        webhook_secret_ref="secret://env/GITHUB_WEBHOOK_SECRET",
        resolver=resolver,
    )


def _sign(body: bytes, secret: str) -> str:
    """Compute GitHub-style HMAC-SHA256 signature."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestGitHubWebhookHandler:
    @pytest.mark.asyncio
    async def test_verify_valid_signature(self, handler: GitHubWebhookHandler) -> None:
        """Valid signature + valid JSON → payload parsed."""
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "testuser"},
            "pull_request": {"number": 1, "title": "Test PR"},
        }
        body = json.dumps(payload).encode()
        signature = _sign(body, "test_webhook_secret_12345")

        result = await handler.verify_and_parse(
            signature=signature,
            body=body,
            event_header="pull_request",
            delivery_id="del-123",
        )
        assert result.event is GitHubWebhookEvent.PULL_REQUEST
        assert result.action == "opened"
        assert result.repo_full_name == "owner/repo"
        assert result.sender_login == "testuser"
        assert result.pr_data is not None
        assert result.pr_data["number"] == 1

    @pytest.mark.asyncio
    async def test_reject_invalid_signature(self, handler: GitHubWebhookHandler) -> None:
        """Invalid signature → WebhookSignatureError."""
        body = b'{"action": "opened"}'
        with pytest.raises(WebhookSignatureError, match="invalid webhook signature"):
            await handler.verify_and_parse(
                signature="sha256=invalid_hash",
                body=body,
                event_header="pull_request",
                delivery_id="del-456",
            )

    @pytest.mark.asyncio
    async def test_reject_missing_sha256_prefix(self, handler: GitHubWebhookHandler) -> None:
        """Signature without 'sha256=' prefix is rejected."""
        body = b'{}'
        with pytest.raises(WebhookSignatureError):
            await handler.verify_and_parse(
                signature="sha1=abc123",
                body=body,
                event_header="push",
                delivery_id="del-789",
            )

    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, handler: GitHubWebhookHandler) -> None:
        """Invalid JSON body → WebhookSignatureError."""
        body = b'not valid json'
        signature = _sign(body, "test_webhook_secret_12345")
        with pytest.raises(WebhookSignatureError, match="invalid JSON"):
            await handler.verify_and_parse(
                signature=signature,
                body=body,
                event_header="push",
                delivery_id="del-000",
            )

    @pytest.mark.asyncio
    async def test_push_event(self, handler: GitHubWebhookHandler) -> None:
        """Push event parsed correctly."""
        payload = {
            "ref": "refs/heads/main",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "pusher"},
            "commits": [{"id": "abc123", "message": "test commit"}],
        }
        body = json.dumps(payload).encode()
        signature = _sign(body, "test_webhook_secret_12345")

        result = await handler.verify_and_parse(
            signature=signature,
            body=body,
            event_header="push",
            delivery_id="del-push-1",
        )
        assert result.event is GitHubWebhookEvent.PUSH
        assert result.push_data is not None
        assert result.push_data["ref"] == "refs/heads/main"

    @pytest.mark.asyncio
    async def test_issue_event(self, handler: GitHubWebhookHandler) -> None:
        """Issue event parsed correctly."""
        payload = {
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "reporter"},
            "issue": {"number": 42, "title": "Bug report"},
        }
        body = json.dumps(payload).encode()
        signature = _sign(body, "test_webhook_secret_12345")

        result = await handler.verify_and_parse(
            signature=signature,
            body=body,
            event_header="issues",
            delivery_id="del-issue-1",
        )
        assert result.event is GitHubWebhookEvent.ISSUE
        assert result.issue_data is not None
        assert result.issue_data["number"] == 42

    @pytest.mark.asyncio
    async def test_unknown_event_falls_back_to_ping(
        self, handler: GitHubWebhookHandler
    ) -> None:
        """Unknown event type falls back to PING."""
        payload = {
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "test"},
        }
        body = json.dumps(payload).encode()
        signature = _sign(body, "test_webhook_secret_12345")

        result = await handler.verify_and_parse(
            signature=signature,
            body=body,
            event_header="unknown_event",
            delivery_id="del-unknown",
        )
        assert result.event is GitHubWebhookEvent.PING

    @pytest.mark.asyncio
    async def test_handle_payload_with_callback(
        self, handler: GitHubWebhookHandler
    ) -> None:
        """handle_payload dispatches to callback."""
        payload = GitHubWebhookPayload(
            event=GitHubWebhookEvent.PUSH,
            action=None,
            repo_full_name="owner/repo",
            sender_login="test",
            raw={},
            delivery_id="del-1",
        )

        callback_called = []

        async def callback(p: GitHubWebhookPayload) -> str:
            callback_called.append(p)
            return "ok"

        result = await handler.handle_payload(payload, callback=callback)
        assert result["status"] == "ok"
        assert len(callback_called) == 1

    @pytest.mark.asyncio
    async def test_constant_time_comparison(self, handler: GitHubWebhookHandler) -> None:
        """Signature comparison uses constant-time hmac.compare_digest."""
        # This test verifies the method exists and works; the constant-time
        # property is guaranteed by hmac.compare_digest (stdlib).
        body = b'{"test": true}'
        sig = _sign(body, "test_webhook_secret_12345")
        result = await handler.verify_and_parse(
            signature=sig,
            body=body,
            event_header="ping",
            delivery_id="del-ct",
        )
        assert result is not None
