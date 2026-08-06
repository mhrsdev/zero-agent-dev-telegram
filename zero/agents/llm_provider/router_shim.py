"""In-process HTTP server that exposes an LLMProvider as an OpenAI-compatible
Router endpoint.

Why this exists (ADR 0004):
    Zero's RouterClient talks to "the Router" via the OpenAI protocol. In a
    production deployment, the Router is a separate service (model gateway,
    load balancer, billing layer). For single-instance deployments, dev, and
    tests, we ship a tiny local Router shim that:

        1. Listens on 127.0.0.1:<port>
        2. Accepts POST /v1/chat/completions (OpenAI protocol)
        3. Translates to ProviderMessage + ProviderToolDef
        4. Calls the configured LLMProvider (Gemini, OpenAI, OpenRouter, ...)
        5. Translates the LLMProviderResponse back to OpenAI format
        6. Adds the Router headers (x-zero-cost-usd, x-zero-request-id, ...)

This keeps the architectural boundary intact: Zero code only ever talks to
"the Router", never to a specific LLM provider directly. The shim is the
Router.

The shim is implemented with ``aiohttp`` (already a dependency via aiogram).
No external dependencies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import web

from zero.agents.llm_provider.base import (
    LLMProvider,
    LLMProviderError,
    LLMProviderTimeoutError,
    ProviderMessage,
    ProviderToolDef,
    scope_headers,
)
from zero.core.scope import Mode, Scope

if TYPE_CHECKING:
    from zero.core.secret import SecretResolver

__all__ = [
    "RouterShim",
    "RouterShimConfig",
]

_log = logging.getLogger("zero.agents.llm_provider.router_shim")


# ---------------------------------------------------------------------- config

class RouterShimConfig:
    """Configuration for the RouterShim server."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,  # 0 = pick a free port
        api_key_ref: str = "secret://env/ZERO_ROUTER_API_KEY",
        allowed_api_key: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.api_key_ref = api_key_ref
        # If set, requests must send Authorization: Bearer <allowed_api_key>.
        # If None, the shim accepts any bearer token (local trusted use only).
        self.allowed_api_key = allowed_api_key

    @property
    def base_url(self) -> str:
        """The base URL Zero should use to talk to this shim."""
        if self.port == 0:
            return f"http://{self.host}:<dynamic>/v1"
        return f"http://{self.host}:{self.port}/v1"


# ---------------------------------------------------------------------- shim

class RouterShim:
    """In-process OpenAI-compatible Router shim.

    Construction:
        >>> provider = GeminiProvider(
        ...     api_key_ref="secret://env/GEMINI_API_KEY",
        ...     resolver=resolver,
        ... )
        >>> shim = RouterShim(provider=provider, config=RouterShimConfig())
        >>> await shim.start()  # starts the HTTP server
        >>> print(shim.base_url)  # http://127.0.0.1:54321/v1
        >>> # Configure Zero's RouterClient to use this base_url.
        >>> await shim.stop()  # graceful shutdown

    The shim is a single-threaded asyncio server. It handles one request at a
    time per worker (aiohttp's default). For higher throughput, run multiple
    shims behind a load balancer — or use a real Router service.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        config: RouterShimConfig,
    ) -> None:
        self._provider = provider
        self._config = config
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._actual_port: int = 0
        self._started_at: float | None = None
        self._stop_event = asyncio.Event()

    @property
    def base_url(self) -> str:
        """The base URL Zero should use to talk to this shim."""
        return f"http://{self._config.host}:{self._actual_port}/v1"

    @property
    def actual_port(self) -> int:
        return self._actual_port

    @property
    def is_running(self) -> bool:
        return self._runner is not None and self._actual_port > 0

    async def start(self) -> None:
        """Start the HTTP server. Returns when the server is ready to accept."""
        import time as _time  # noqa: PLC0415

        self._started_at = _time.time()
        app = web.Application(client_max_size=10 * 1024 * 1024)  # 10 MB
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        app.router.add_post("/chat/completions", self._handle_chat_completions)
        app.router.add_get("/v1/models", self._handle_list_models)
        app.router.add_get("/v1/health", self._handle_health)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await self._site.start()

        # Get the actual bound port (in case config.port was 0).
        server = self._site._server  # type: ignore[attr-defined]
        sockets = server.sockets if server is not None else None
        if sockets:
            self._actual_port = sockets[0].getsockname()[1]
        else:
            self._actual_port = self._config.port

        _log.info(
            "RouterShim listening on http://%s:%d (provider=%s, default_model=%s)",
            self._config.host,
            self._actual_port,
            self._provider.provider_name,
            self._provider.default_model,
        )

    async def stop(self) -> None:
        """Stop the HTTP server gracefully."""
        self._stop_event.set()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._site = None

    async def serve_forever(self) -> None:
        """Start the server and block until cancelled."""
        await self.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # ------------------------------------------------------------------ handlers

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({
            "status": "ok",
            "provider": self._provider.provider_name,
            "default_model": self._provider.default_model,
            "uptime_seconds": time.time() - (self._started_at or time.time()),
        })

    async def _handle_list_models(self, request: web.Request) -> web.Response:
        """List models endpoint (minimal — just the default)."""
        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": self._provider.default_model,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": self._provider.provider_name,
                }
            ],
        })

    async def _handle_chat_completions(self, request: web.Request) -> web.Response:
        """Main chat completions endpoint — OpenAI protocol."""
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        start = time.monotonic()

        # Auth check (if configured).
        if self._config.allowed_api_key is not None:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return web.json_response(
                    {"error": {"message": "missing bearer token", "type": "auth_error"}},
                    status=401,
                )
            token = auth[len("Bearer "):]
            import hmac  # noqa: PLC0415

            if not hmac.compare_digest(token, self._config.allowed_api_key):
                return web.json_response(
                    {"error": {"message": "invalid api key", "type": "auth_error"}},
                    status=401,
                )

        # Parse body.
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response(
                {"error": {"message": f"invalid JSON: {e}", "type": "invalid_request_error"}},
                status=400,
            )

        # Translate OpenAI body → ProviderMessage list.
        try:
            messages = self._parse_messages(body.get("messages", []))
            tools = self._parse_tools(body.get("tools"))
        except ValueError as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "invalid_request_error"}},
                status=400,
            )

        # Build Scope from X-Zero-Scope-* headers.
        scope = self._scope_from_headers(request.headers)

        # Determine model.
        model = body.get("model") or self._provider.default_model
        temperature = float(body.get("temperature", 0.0))
        max_tokens = body.get("max_tokens")
        effort_tier = body.get("effort_tier")
        stream = bool(body.get("stream", False))

        if stream:
            return await self._handle_stream(
                request=request,
                request_id=request_id,
                messages=messages,
                tools=tools,
                scope=scope,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                effort_tier=effort_tier,
            )

        # Non-streaming call.
        try:
            resp = await self._provider.complete(
                messages=messages,
                tools=tools,
                scope=scope,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                effort_tier=effort_tier,
            )
        except LLMProviderTimeoutError as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "timeout_error"}},
                status=504,
                headers={"x-zero-request-id": request_id},
            )
        except LLMProviderError as e:
            status = getattr(e, "status_code", None) or 502
            return web.json_response(
                {"error": {"message": str(e), "type": "provider_error"}},
                status=status,
                headers={"x-zero-request-id": request_id},
            )

        latency_ms = (time.monotonic() - start) * 1000.0

        # Build OpenAI-format response.
        response_body = self._build_openai_response(
            resp=resp,
            model=model,
            request_id=request_id,
        )

        # Headers: cost + request_id + cache tokens (per Router contract).
        headers = {
            "x-zero-request-id": request_id,
            "x-zero-cost-usd": f"{resp.cost_usd:.8f}",
            "x-zero-cache-read-tokens": str(resp.cache_read_tokens),
            "x-zero-cache-write-tokens": str(resp.cache_write_tokens),
            "x-zero-provider": self._provider.provider_name,
            "x-zero-latency-ms": f"{latency_ms:.2f}",
        }

        return web.json_response(response_body, headers=headers)

    async def _handle_stream(
        self,
        *,
        request: web.Request,
        request_id: str,
        messages: list[ProviderMessage],
        tools: list[ProviderToolDef] | None,
        scope: Scope,
        model: str,
        temperature: float,
        max_tokens: int | None,
        effort_tier: str | None,
    ) -> web.StreamResponse:
        """Handle a streaming request."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "x-zero-request-id": request_id,
                "x-zero-provider": self._provider.provider_name,
            },
        )
        await resp.prepare(request)

        try:
            async for chunk in self._provider.stream(
                messages=messages,
                tools=tools,
                scope=scope,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                effort_tier=effort_tier,
            ):
                # Pass through OpenAI-format chunks.
                await resp.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            await resp.write(b"data: [DONE]\n\n")
        except Exception as e:
            error_payload = json.dumps({
                "error": {"message": str(e), "type": "stream_error"},
            })
            await resp.write(f"data: {error_payload}\n\n".encode("utf-8"))

        await resp.write_eof()
        return resp

    # ------------------------------------------------------------------ parsers

    @staticmethod
    def _parse_messages(raw: list[dict[str, Any]]) -> list[ProviderMessage]:
        """Parse OpenAI-format messages into ProviderMessage list."""
        out: list[ProviderMessage] = []
        for i, m in enumerate(raw):
            role = m.get("role", "user")
            if role not in ("system", "user", "assistant", "tool"):
                raise ValueError(f"message[{i}]: invalid role {role!r}")
            content = m.get("content")
            if content is not None and not isinstance(content, str):
                # Some providers send list-of-parts (vision). We only support text.
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                else:
                    content = str(content)
            tool_calls = m.get("tool_calls")
            tool_call_id = m.get("tool_call_id")
            name = m.get("name")
            out.append(ProviderMessage(
                role=role,  # type: ignore[arg-type]
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                name=name,
            ))
        return out

    @staticmethod
    def _parse_tools(raw: list[dict[str, Any]] | None) -> list[ProviderToolDef] | None:
        """Parse OpenAI-format tools list into ProviderToolDef list."""
        if not raw:
            return None
        out: list[ProviderToolDef] = []
        for t in raw:
            func = t.get("function", {}) or {}
            out.append(ProviderToolDef(
                name=func.get("name", ""),
                description=func.get("description", ""),
                parameters_schema=func.get("parameters", {}) or {"type": "object"},
            ))
        return out

    @staticmethod
    def _scope_from_headers(headers: Any) -> Scope:
        """Build a Scope from X-Zero-Scope-* headers.

        If headers are missing, defaults to a PERSONAL scope (least privilege).
        """
        mode_str = headers.get("X-Zero-Scope-Mode", "personal")
        scope_key = headers.get("X-Zero-Scope-Key", "personal:usr_anonymous")
        project_id = headers.get("X-Zero-Scope-Project")

        try:
            mode = Mode(mode_str)
        except ValueError:
            mode = Mode.PERSONAL

        # Parse the scope_key to extract identifiers.
        # Format: "personal:usr_<id>" | "normal:grp_<id>:<topic>" | "dev:prj_<id>:<topic>"
        parts = scope_key.split(":", 2)
        if mode is Mode.PERSONAL:
            user_id = parts[1] if len(parts) > 1 else "usr_anonymous"
            # Ensure the user_id has the usr_ prefix (required by Scope validator).
            if not user_id.startswith("usr_"):
                user_id = f"usr_{user_id}"
            return Scope.personal(user_id=user_id).with_default_memory_scope()
        if mode is Mode.NORMAL:
            group_id = parts[1] if len(parts) > 1 else "grp_anonymous"
            # Ensure the group_id has the grp_ prefix.
            if not group_id.startswith("grp_"):
                group_id = f"grp_{group_id}"
            topic_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            return Scope.normal(group_id=group_id, topic_id=topic_id).with_default_memory_scope()
        if mode is Mode.DEVELOPMENT:
            prj = project_id or (parts[1] if len(parts) > 1 else "prj_anonymous")
            # Ensure the project_id has the prj_ prefix.
            if not prj.startswith("prj_"):
                prj = f"prj_{prj}"
            topic_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            return Scope.development(
                org_id=f"org_for_{prj}",
                workspace_id=f"ws_for_{prj}",
                project_id=prj,
                group_id="grp_dev",
                topic_id=topic_id,
            ).with_default_memory_scope()
        return Scope.personal(user_id="usr_anonymous").with_default_memory_scope()

    def _build_openai_response(
        self,
        *,
        resp: Any,  # LLMProviderResponse
        model: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Build an OpenAI-format response dict from LLMProviderResponse."""
        # Convert ProviderToolCall list → OpenAI tool_calls list.
        tool_calls_out: list[dict[str, Any]] = []
        for tc in resp.tool_calls:
            tool_calls_out.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            })

        message: dict[str, Any] = {"role": "assistant", "content": resp.content}
        if tool_calls_out:
            message["tool_calls"] = tool_calls_out

        return {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": resp.model or model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": resp.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": resp.input_tokens,
                "completion_tokens": resp.output_tokens,
                "total_tokens": resp.input_tokens + resp.output_tokens,
            },
        }
