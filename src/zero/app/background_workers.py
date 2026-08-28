"""Managed autonomous background workers.

Per the release audit (Phase 0, "Add an actual service/worker deployment
contract"): an HTTP request is not an autonomous scheduler. This module
hosts the durable tick loops inside the ASGI application lifecycle so a
single ``uvicorn zero.main:app`` deployment makes approved work progress
without an external caller:

- the **scheduler worker** claims approved handoffs, drains ready
  tasks through the agent runtime, advances integration reviews, and
  enqueues result deliveries;
- the **delivery worker** continuously drains outbound result
  deliveries through the configured transports;
- the **polling worker** hosts Telegram long-polling for every enabled
  Telegram binding when a Telegram verifier is configured.

All loops are bounded and replay-safe: every iteration goes through the
same durable service boundaries as an explicit API call. Failures are
logged with redacted summaries and retried on the next interval; they
never mark work successful merely because the loop survived.

Workers are disabled in the test environment by default so tests never
run autonomous work implicitly; ``ZERO_WORKERS_ENABLED=0`` disables them
for any environment.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from zero.app.services import Services
from zero.config import Settings

logger = logging.getLogger("zero.workers")


@dataclass
class WorkerHostStatus:
    """Observable status of the managed worker host."""

    running: bool = False
    scheduler_ticks: int = 0
    delivery_drains: int = 0
    polling_iterations: int = 0
    last_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "scheduler_ticks": self.scheduler_ticks,
            "delivery_drains": self.delivery_drains,
            "polling_iterations": self.polling_iterations,
            "recent_error_count": len(self.last_errors),
        }


class BackgroundWorkerHost:
    """Own the lifecycle of the managed worker tasks."""

    def __init__(self, settings: Settings, services: Services) -> None:
        self._settings = settings
        self._services = services
        self.status = WorkerHostStatus()
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Lifecycle (FastAPI lifespan)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            logger.info(
                "background workers disabled (env=%s workers_enabled=%s)",
                self._settings.zero_env,
                self._settings.workers_enabled,
            )
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._scheduler_loop(), name="zero-scheduler-worker"),
            asyncio.create_task(self._delivery_loop(), name="zero-delivery-worker"),
            asyncio.create_task(self._polling_loop(), name="zero-polling-worker"),
        ]
        self.status.running = True
        logger.info("background workers started")

    async def stop(self) -> None:
        if not self._tasks:
            return
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.status.running = False
        logger.info("background workers stopped")

    @property
    def enabled(self) -> bool:
        return self._settings.workers_enabled and not self._settings.is_test

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _record_error(self, message: str) -> None:
        logger.warning("worker error: %s", message)
        self.status.last_errors.append(message)
        del self.status.last_errors[:-20]

    # ------------------------------------------------------------------
    # Loops
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        interval = max(0.1, float(self._settings.scheduler_interval_seconds))
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._scheduler_tick)
                self.status.scheduler_ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker loop must survive
                self._record_error(f"scheduler: {type(exc).__name__}")
            await self._wait(interval)

    async def _delivery_loop(self) -> None:
        interval = max(0.1, float(self._settings.delivery_interval_seconds))
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._delivery_drain)
                self.status.delivery_drains += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker loop must survive
                self._record_error(f"delivery: {type(exc).__name__}")
            await self._wait(interval)

    async def _polling_loop(self) -> None:
        """Host Telegram long-polling for every enabled binding.

        Per the release audit (Phase 4): webhook-only operation is not a
        complete gateway. Each enabled Telegram binding is polled with
        its own resolved bot credential and its own durable offset so
        one malformed or unconfigured binding cannot stall the others.
        """
        interval = max(0.1, float(self._settings.polling_interval_seconds))
        cursor_store = _InMemoryCursorStore()
        while not self._stop.is_set():
            polled_any = False
            for binding_poll in self._telegram_poll_targets():
                if self._stop.is_set():
                    break
                _project, binding, token = binding_poll
                try:
                    adapter = _build_binding_adapter(
                        services=self._services,
                        chat_token=token,
                        cursor_store=cursor_store,
                    )
                    updates = await asyncio.to_thread(adapter.poll_once, scope_key=binding.id.value)
                    polled_any = polled_any or bool(updates)
                    self.status.polling_iterations += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - per-binding isolation
                    self._record_error(f"polling:{binding.id.value}: {type(exc).__name__}")
            if not polled_any:
                await self._wait(interval)

    # ------------------------------------------------------------------
    # Tick bodies (run in worker threads; synchronous service boundary)
    # ------------------------------------------------------------------

    def _scheduler_tick(self) -> None:
        services = self._services
        provider_names = services.providers.registered_provider_names
        provider = provider_names[0] if provider_names else "openai-compatible"
        model_name = self._settings.openai_model
        combined_command = self._settings.combined_test_command
        for project in services.identity.list_projects():
            try:
                owner = project.owner_user_id
                services.scheduler.run_once(
                    project_id=project.id,
                    actor_id=owner,
                    lease_owner="managed-worker-host",
                    provider=provider,
                    model_name=model_name,
                    combined_test_command=(combined_command[0] if combined_command else None),
                    combined_test_args=tuple(combined_command[1:]),
                    combined_test_timeout_seconds=self._settings.combined_test_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - per-project isolation
                self._record_error(f"scheduler:{project.id.value}: {type(exc).__name__}")

    def _delivery_drain(self) -> None:
        services = self._services
        for project in services.identity.list_projects():
            try:
                services.result_delivery.drain_once(project_id=project.id)
            except Exception as exc:  # noqa: BLE001 - per-project isolation
                self._record_error(f"delivery:{project.id.value}: {type(exc).__name__}")

    def _telegram_poll_targets(self) -> list[tuple[object, object, str]]:
        """Resolve (project, binding, bot_token) for every pollable binding.

        A binding is pollable when it is enabled, belongs to the Telegram
        platform, and has a bot credential reference that resolves
        successfully. Bindings whose credential cannot be resolved are
        skipped with a recorded error: polling must never crash because
        one project misconfigured its interface.
        """
        services = self._services
        transports = services.interface_transports
        if transports is None or getattr(transports, "_transport", None) is None:
            return []
        targets: list[tuple[object, object, str]] = []
        for project in services.identity.list_projects():
            try:
                bindings = services.result_delivery.list_enabled_bindings(project.id)
            except Exception as exc:  # noqa: BLE001 - per-project isolation
                self._record_error(f"polling:{project.id.value}: {type(exc).__name__}")
                continue
            for binding in bindings:
                if binding.platform != "telegram" or binding.bot_token_ref is None:
                    continue
                try:
                    from zero.domain.secrets import SecretReferenceId

                    token = services.secrets.resolve_value(
                        project_id=project.id,
                        secret_id=SecretReferenceId(binding.bot_token_ref),
                        actor_id=project.owner_user_id,
                        source="system",
                    )
                except Exception as exc:  # noqa: BLE001 - per-binding isolation
                    self._record_error(
                        f"polling-credential:{binding.id.value}: {type(exc).__name__}"
                    )
                    continue
                targets.append((project, binding, token))
        return targets


class _InMemoryCursorStore:
    """Process-local polling offsets.

    Durable enough for one hosted worker process: offsets only ever
    advance, and a redelivery after a crash is deduplicated by the
    interface service's durable event claims.
    """

    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], str] = {}
        import threading

        self._lock = threading.Lock()

    def get_cursor(self, platform: str, scope_key: str) -> str | None:
        with self._lock:
            return self._cursors.get((platform, scope_key))

    def advance_cursor(self, platform: str, scope_key: str, value: str) -> None:
        with self._lock:
            current = self._cursors.get((platform, scope_key))
            if current is None or int(value) > int(current):
                self._cursors[(platform, scope_key)] = value


def _build_binding_adapter(*, services, chat_token: str, cursor_store):
    """Build a TelegramAdapter bound to one resolved bot credential.

    Bug fix (real server run, 2026-08-28): the adapter was built with the
    default ``RetryPolicy`` whose per-request ``timeout_seconds=10``.
    Long polling asks Telegram to HOLD the request open for
    ``poll_timeout_seconds`` (25s), so httpx aborted every long poll at
    10s with TransportError — then the retry wrapper re-sent it twice
    more, guaranteeing ~30s of doomed requests per polling iteration.
    The gateway could never receive a real message.

    The per-request budget now always exceeds the long-poll hold
    (+10s margin), and attempts=1 because a completed long poll IS the
    wait — the outer polling loop is the retry.
    """
    from zero.adapters.messaging import RetryPolicy
    from zero.adapters.telegram import TelegramAdapter

    poll_timeout = 25
    transports = services.interface_transports
    transport = transports.http_transport if transports is not None else None
    return TelegramAdapter(
        event_handler=services.interfaces.process_inbound_event,
        transport=transport,
        bot_token=chat_token,
        cursor_store=cursor_store,
        poll_timeout_seconds=poll_timeout,
        retry_policy=RetryPolicy(
            attempts=1,
            backoff_seconds=0.25,
            timeout_seconds=float(poll_timeout) + 10.0,
        ),
    )


__all__ = ["BackgroundWorkerHost", "WorkerHostStatus"]
