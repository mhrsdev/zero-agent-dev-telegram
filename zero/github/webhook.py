"""GitHub webhook handler — Phase 5 T-5.8.

Per ADR T-5.8:
    - Signature verified (HMAC-SHA256 of payload with webhook secret)
    - Only Zero input port — must be explicitly enabled
    - Events: push, pull_request, issue, check_run

The webhook secret is stored as ``secret://`` reference (never raw).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from zero.core.logging import get_logger
from zero.core.secret import SecretResolver
from zero.security.net_guard import check_url

__all__ = [
    "GitHubWebhookEvent",
    "GitHubWebhookPayload",
    "GitHubWebhookHandler",
    "WebhookSignatureError",
]

_log = get_logger("zero.github.webhook")


class WebhookSignatureError(RuntimeError):
    """Raised when webhook signature verification fails."""


class GitHubWebhookEvent(str, Enum):
    PUSH = "push"
    PULL_REQUEST = "pull_request"
    ISSUE = "issues"
    CHECK_RUN = "check_run"
    CHECK_SUITE = "check_suite"
    CREATE = "create"
    DELETE = "delete"
    FORK = "fork"
    PING = "ping"


@dataclass(frozen=True, slots=True)
class GitHubWebhookPayload:
    """Parsed GitHub webhook payload."""

    event: GitHubWebhookEvent
    action: str | None  # e.g. "opened", "closed", "synchronize"
    repo_full_name: str
    sender_login: str
    raw: dict[str, Any]
    delivery_id: str

    # Event-specific data (populated based on event type).
    push_data: dict[str, Any] | None = None
    pr_data: dict[str, Any] | None = None
    issue_data: dict[str, Any] | None = None
    check_data: dict[str, Any] | None = None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.value,
            "action": self.action,
            "repo": self.repo_full_name,
            "sender": self.sender_login,
            "delivery_id": self.delivery_id,
        }


class GitHubWebhookHandler:
    """Handles GitHub webhook verification + parsing.

    Usage:
        >>> handler = GitHubWebhookHandler(
        ...     webhook_secret_ref="secret://env/GITHUB_WEBHOOK_SECRET",
        ...     resolver=resolver,
        ... )
        >>> payload = await handler.verify_and_parse(
        ...     signature="sha256=abc123...",
        ...     body=raw_body_bytes,
        ...     event_header="pull_request",
        ...     delivery_id="12345-67890",
        ... )
    """

    def __init__(
        self,
        *,
        webhook_secret_ref: str,
        resolver: SecretResolver,
    ) -> None:
        self._secret_ref = webhook_secret_ref
        self._resolver = resolver

    async def verify_and_parse(
        self,
        *,
        signature: str,
        body: bytes,
        event_header: str,
        delivery_id: str,
    ) -> GitHubWebhookPayload:
        """Verify the webhook signature and parse the payload.

        Raises :class:`WebhookSignatureError` if signature is invalid.
        """
        # Resolve the webhook secret.
        secret = self._resolver.resolve(self._secret_ref)
        secret_bytes = secret.reveal().encode("utf-8")

        # Verify signature using constant-time comparison.
        if not self._verify_signature(signature, body, secret_bytes):
            raise WebhookSignatureError(
                f"invalid webhook signature (delivery_id={delivery_id!r})"
            )

        # Parse the JSON payload.
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise WebhookSignatureError(f"invalid JSON payload: {e}") from e

        # Map event header to enum.
        try:
            event = GitHubWebhookEvent(event_header)
        except ValueError:
            _log.warning(f"unhandled GitHub event type: {event_header!r}")
            event = GitHubWebhookEvent.PING  # fallback

        # Extract common fields.
        repo = data.get("repository", {})
        sender = data.get("sender", {})
        action = data.get("action")

        # Extract event-specific data.
        push_data = data if event is GitHubWebhookEvent.PUSH else None
        pr_data = data.get("pull_request") if event is GitHubWebhookEvent.PULL_REQUEST else None
        issue_data = data.get("issue") if event is GitHubWebhookEvent.ISSUE else None
        check_data = data if event in (GitHubWebhookEvent.CHECK_RUN, GitHubWebhookEvent.CHECK_SUITE) else None

        return GitHubWebhookPayload(
            event=event,
            action=action,
            repo_full_name=repo.get("full_name", ""),
            sender_login=sender.get("login", ""),
            raw=data,
            delivery_id=delivery_id,
            push_data=push_data,
            pr_data=pr_data,
            issue_data=issue_data,
            check_data=check_data,
        )

    @staticmethod
    def _verify_signature(signature: str, body: bytes, secret: bytes) -> bool:
        """Verify HMAC-SHA256 signature.

        GitHub sends: ``sha256=<hex_digest>``
        We compute: ``hmac_sha256(secret, body)`` and compare in constant time.
        """
        if not signature.startswith("sha256="):
            return False

        expected = signature[len("sha256="):]
        computed = hmac.new(secret, body, hashlib.sha256).hexdigest()

        # Constant-time comparison.
        return hmac.compare_digest(expected, computed)

    async def handle_payload(
        self,
        payload: GitHubWebhookPayload,
        *,
        callback: Any = None,
    ) -> dict[str, Any]:
        """Handle a parsed payload by dispatching to a callback.

        The callback receives the payload and returns a result dict.
        """
        _log.info(f"handling webhook: {payload.to_log_dict()}")

        if callback is not None:
            try:
                result = await callback(payload) if callable(callback) else None
                return {"status": "ok", "result": result}
            except Exception as e:
                _log.error(f"webhook callback failed: {e}", exc=e)
                return {"status": "error", "error": str(e)}

        # Default handling: just log.
        return {"status": "ok", "handled": False}
