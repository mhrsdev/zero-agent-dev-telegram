"""Interactive single-turn chat service (GAP 6).

One ephemeral request/response cycle through the normal provider chain:
no plan, no execution, no task rows. The provider request and its usage
remain durable so accounting stays truthful, but nothing enters the
batch pipeline. Tool calls run through :class:`ToolService.invoke`, so
grants, budgets, redaction, and audit apply unchanged.

Per ``docs/gap-designs/GAP-06-chat-endpoint.md``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from zero.app.authorization_service import AuthorizationService
from zero.app.provider_service import ProviderService
from zero.app.tool_service import ToolInvocationDeniedError, ToolService
from zero.domain.audit import AuditSource
from zero.domain.identity import ProjectId, UserId
from zero.domain.providers import CanonicalMessage, CanonicalRequest, ToolDeclaration
from zero.domain.tools import AgentScope

logger = logging.getLogger(__name__)

CHAT_SYSTEM_MESSAGE = (
    "You are Zero's interactive assistant. Answer directly and concisely. "
    "Tools available to you follow the project's capability grants; never "
    "claim an action you did not perform."
)

_MAX_TOOL_ROUNDS_LIMIT = 8


class ChatRateLimitError(RuntimeError):
    """The caller exceeded the configured chat requests/minute budget."""


class TokenBucketRateLimiter:
    """Thread-safe per-key token bucket (capacity = rate per minute)."""

    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self._rate = float(requests_per_minute)
        self._capacity = float(requests_per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = threading.Lock()

    @property
    def per_minute(self) -> int:
        return int(self._rate)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, current))
            elapsed = max(0.0, current - last)
            tokens = min(self._capacity, tokens + elapsed * (self._rate / 60.0))
            if tokens >= 1.0:
                tokens -= 1.0
                self._buckets[key] = (tokens, current)
                return True
            self._buckets[key] = (tokens, current)
            return False


@dataclass(frozen=True)
class ChatTurnResult:
    content: str
    tool_calls_executed: tuple[dict[str, Any], ...]
    usage: dict[str, int] | None
    provider_request_id: str


class ChatService:
    """Ephemeral single-turn conversations without the batch pipeline."""

    def __init__(
        self,
        *,
        providers: ProviderService,
        authorization: AuthorizationService,
        tools: ToolService | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        default_model_max_tokens: int = 1024,
    ) -> None:
        self._providers = providers
        self._authz = authorization
        self._tools = tools
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(10)
        self._default_model_max_tokens = default_model_max_tokens

    def complete(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        message: str,
        agent_scope: AgentScope = "main_worker",
        max_tool_rounds: int = 3,
        provider: str,
        model_name: str,
        source: AuditSource = "web",
    ) -> ChatTurnResult:
        """Run one user message through the provider chain.

        Identical repeat messages deduplicate through the standard
        request-hash path (the stored response is returned without a new
        provider call) — the same contract as every other request.
        """
        if not message.strip():
            raise ValueError("message must not be empty")
        if not 0 <= max_tool_rounds <= _MAX_TOOL_ROUNDS_LIMIT:
            raise ValueError(f"max_tool_rounds must be between 0 and {_MAX_TOOL_ROUNDS_LIMIT}")
        self._authz.require_permission(
            actor_id=actor_id,
            project_id=project_id,
            permission="project.view",
            source=source,
        )
        if not self._rate_limiter.allow(f"{project_id.value}:{actor_id.value}"):
            raise ChatRateLimitError("chat rate limit exceeded")

        granted_tools = self._granted_tool_names(project_id, agent_scope)
        messages: list[CanonicalMessage] = [
            CanonicalMessage(role="user", content=message),
        ]
        executed: list[dict[str, Any]] = []

        response = None
        provider_request = None
        for round_index in range(max_tool_rounds + 1):
            is_final_round = round_index == max_tool_rounds
            request = CanonicalRequest(
                provider=provider,
                model_name=model_name,
                messages=tuple(messages),
                tools=()
                if is_final_round
                else tuple(self._declaration(name) for name in granted_tools),
                system_message=CHAT_SYSTEM_MESSAGE,
                max_tokens=self._default_model_max_tokens,
            )
            provider_request, response = self._providers.send_request_with_fallback(
                project_id=project_id,
                actor_id=actor_id,
                execution_id=None,
                request=request,
                source=source,
                agent_scope=agent_scope,
            )
            if not response.tool_calls:
                break
            if is_final_round:
                break
            messages.append(
                CanonicalMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=tuple(
                        (call.tool_name, call.tool_call_id, call.arguments)
                        for call in response.tool_calls
                    ),
                )
            )
            if self._tools is None:
                logger.warning("provider requested tools but chat has no tool service")
                break
            for call in response.tool_calls:
                result_payload = self._invoke_tool(
                    project_id=project_id,
                    actor_id=actor_id,
                    agent_scope=agent_scope,
                    tool_name=call.tool_name,
                    arguments_text=call.arguments,
                    source=source,
                )
                executed.append(result_payload)
                messages.append(
                    CanonicalMessage(
                        role="tool",
                        content=result_payload["result"],
                        tool_call_id=call.tool_call_id,
                    )
                )

        assert response is not None and provider_request is not None
        usage_dict = (
            {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
                "cache_read_input_tokens": response.usage.cache_read_input_tokens,
            }
            if response.usage is not None
            else None
        )
        return ChatTurnResult(
            content=response.content,
            tool_calls_executed=tuple(executed),
            usage=usage_dict,
            provider_request_id=provider_request.id.value,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _granted_tool_names(
        self, project_id: ProjectId, agent_scope: AgentScope
    ) -> tuple[str, ...]:
        """Only tools already granted to this scope may be declared.

        Reads the repository directly: listing grants via the service
        would require ``tool.manage``, which an interactive caller does
        not need to *use* granted tools.
        """
        if self._tools is None:
            return ()
        try:
            grants = self._tools._tool_repo.list_grants_for_project(project_id)
        except Exception:  # noqa: BLE001 - degraded: run toolless
            return ()
        names: list[str] = []
        for grant in grants:
            if grant.agent_scope != agent_scope:
                continue
            try:
                # Bug fix (real run, 2026-08-28): this called
                # ``get_tool`` — a method the repository has never had
                # (the real accessor is ``get_tool_by_id``) — so the
                # lookup raised, the degraded path swallowed it, and the
                # interactive chat silently ran TOOLLESS no matter what
                # capabilities were granted.
                tool = self._tools._tool_repo.get_tool_by_id(grant.tool_id)
            except Exception as grant_exc:  # noqa: BLE001
                logger.debug("granted tool %s unavailable: %s", grant.tool_id, grant_exc)
                continue
            names.append(tool.name)
        return tuple(sorted(set(names)))

    def _declaration(self, tool_name: str) -> ToolDeclaration | str:
        try:
            tool = self._tools._tool_repo.get_tool_by_name(tool_name)  # type: ignore[union-attr]
            return ToolDeclaration(
                name=tool.name,
                description=tool.description or "",
                parameters=dict(tool.input_schema or {}) or None,
            )
        except Exception:  # noqa: BLE001
            return tool_name

    def _invoke_tool(
        self,
        *,
        project_id: ProjectId,
        actor_id: UserId,
        agent_scope: AgentScope,
        tool_name: str,
        arguments_text: str,
        source: AuditSource,
    ) -> dict[str, Any]:
        # Hermes parity (audit 2026-08-28): unparseable arguments are
        # never executed with guessed (empty) inputs — the model receives
        # a structured error so it can re-issue the call.
        try:
            input_data = json.loads(arguments_text) if arguments_text.strip() else {}
        except json.JSONDecodeError as decode_exc:
            return {
                "tool_name": tool_name,
                "arguments": {},
                "result": json.dumps(
                    {
                        "error": "invalid_tool_arguments",
                        "detail": f"arguments are not valid JSON: {decode_exc.msg}",
                        "hint": "Re-issue this tool call with a JSON object "
                        "whose keys match the declared schema.",
                    },
                    ensure_ascii=False,
                ),
            }
        if not isinstance(input_data, dict):
            return {
                "tool_name": tool_name,
                "arguments": {},
                "result": json.dumps(
                    {
                        "error": "invalid_tool_arguments",
                        "detail": "arguments must decode to a JSON object",
                    },
                    ensure_ascii=False,
                ),
            }
        try:
            result = self._tools.invoke(  # type: ignore[union-attr]
                project_id=project_id,
                actor_id=actor_id,
                agent_scope=agent_scope,
                tool_name=tool_name,
                input_data=input_data,
                source=source,
            )
        except ToolInvocationDeniedError as exc:
            return {"tool_name": tool_name, "arguments": input_data, "result": f"denied: {exc}"}
        except Exception as exc:  # noqa: BLE001 - tool failure is a result, not a crash
            # Hermes parity (audit 2026-08-28): the failure REASON (bounded,
            # redacted) reaches the model instead of a bare class name.
            from zero.domain.audit import redact_sensitive_text

            detail = redact_sensitive_text(str(exc) or type(exc).__name__)[:512]
            return {
                "tool_name": tool_name,
                "arguments": input_data,
                "result": f"error executing tool {tool_name!r}: {type(exc).__name__}: {detail}",
            }
        return {
            "tool_name": tool_name,
            "arguments": input_data,
            "result": result.model_facing,
            "status": result.status,
        }


__all__ = [
    "ChatRateLimitError",
    "ChatService",
    "ChatTurnResult",
    "TokenBucketRateLimiter",
]
