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

#: Periodic getMe heartbeat budget (Hermes parity, round 5): Hermes
#: probes ``get_me`` every 90s so a wedged TCP socket (CLOSE-WAIT) or a
#: silently blocked long-poll is detected even when no error fires.
_POLLING_HEARTBEAT_SECONDS = 90.0
#: Warn after this long without ANY successful Telegram round trip
#: (poll or heartbeat probe) — the operator sees "the gateway went
#: deaf at about this time" instead of an unbounded silent stall.
_POLLING_STALL_SECONDS = 300.0


@dataclass
class WorkerHostStatus:
    """Observable status of the managed worker host."""

    running: bool = False
    scheduler_ticks: int = 0
    delivery_drains: int = 0
    polling_iterations: int = 0
    polling_conflicts: int = 0
    polling_locked_out: int = 0
    last_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "scheduler_ticks": self.scheduler_ticks,
            "delivery_drains": self.delivery_drains,
            "polling_iterations": self.polling_iterations,
            "polling_conflicts": self.polling_conflicts,
            "polling_locked_out": self.polling_locked_out,
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

        Bug fixes (2026-08-29, dead-bot session):
        - a cross-process lock (``TokenPollLock``) makes a second engine
          (``zero start`` + ``zero-develop serve`` running side by side)
          SKIP polling the same bot token instead of fighting the first
          engine with HTTP 409 conflicts and splitting its updates;
        - a genuine 409 (foreign poller) is recognized as the typed
          ``TelegramConflictError`` and answered with an exponential
          backoff per binding (5s doubling to 60s) plus a one-time,
          actionable log line — instead of a full-speed error loop.

        Bug fixes (2026-08-29, flaky-network session — the operator saw
        ``polling:ib_…: TransportError`` every ~4s for 8+ minutes while
        api.telegram.org was unreachable through a filtered network):
        - EVERY polling error now earns an exponential per-binding
          backoff (2s doubling to 60s, reset on success) — transport
          failures used to hot-loop at the 1s polling interval;
        - the first error of a streak is logged WITH its sanitized
          underlying cause (``TransportError: provider transport failed
          after retries — ConnectError: ...``) instead of the bare class
          name; repeats are compact DEBUG lines;
        - after 3 consecutive failures a one-time actionable hint points
          at ZERO_TELEGRAM_PROXY_URL / HTTPS_PROXY for filtered networks;
        - the first successful poll verifies the bot identity via getMe
          once per token and logs ``@username (id=...)`` so an operator
          can tell a healthy gateway from a dead one at a glance.
        """
        from zero.adapters.telegram import TelegramConflictError
        from zero.app.poll_lock import TokenPollLock

        interval = max(0.1, float(self._settings.polling_interval_seconds))
        cursor_store = _InMemoryCursorStore()
        poll_lock = TokenPollLock()
        backoff_until: dict[str, float] = {}
        backoff_step: dict[str, int] = {}
        conflict_reported: set[str] = set()
        lock_reported: set[str] = set()
        lock_state: dict[str, bool] = {}  # token -> acquired?
        error_until: dict[str, float] = {}
        error_step: dict[str, int] = {}
        error_reported: set[str] = set()
        hint_reported: set[str] = set()
        identity_verified: set[str] = set()
        # Heartbeat + stall watchdog state (Hermes parity, round 5):
        # the loop start counts as provisional success so a quiet but
        # healthy start is not flagged before the first window elapses.
        last_heartbeat = _loop_monotonic()
        last_success = last_heartbeat
        stall_reported = False
        try:
            while not self._stop.is_set():
                polled_any = False
                now = _loop_monotonic()
                poll_targets = self._telegram_poll_targets()
                # ---- periodic getMe heartbeat (runs between polling
                # ---- cycles; never interrupts an in-flight poll).
                if poll_targets and now - last_heartbeat >= _POLLING_HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    try:
                        _hb_project, _hb_binding, hb_token = poll_targets[0]
                        hb_adapter = _build_binding_adapter(
                            services=self._services,
                            chat_token=hb_token,
                            cursor_store=cursor_store,
                        )
                        me = await asyncio.to_thread(hb_adapter.get_me)
                        last_success = _loop_monotonic()
                        stall_reported = False
                        logger.info(
                            "polling heartbeat: bot @%s alive (id=%s)",
                            me.get("username"),
                            me.get("id"),
                        )
                    except Exception as exc:  # noqa: BLE001 - probe is diagnostic
                        # A failed heartbeat must NOT touch per-binding
                        # backoff state: polling itself may still work.
                        logger.debug(
                            "polling heartbeat probe failed: %s", type(exc).__name__
                        )
                if (
                    poll_targets
                    and not stall_reported
                    and now - last_success >= _POLLING_STALL_SECONDS
                ):
                    stall_reported = True
                    logger.warning(
                        "polling: no successful Telegram round trip for %.0fs — "
                        "the long-poll path may be silently stalled (dead TCP "
                        "socket or filtered network); if this persists, check "
                        "ZERO_TELEGRAM_PROXY_URL / HTTPS_PROXY",
                        now - last_success,
                    )
                for binding_poll in poll_targets:
                    if self._stop.is_set():
                        break
                    _project, binding, token = binding_poll
                    bid = binding.id.value
                    if now < backoff_until.get(bid, 0.0) or now < error_until.get(bid, 0.0):
                        continue
                    # Acquire ONCE per token and hold it for the loop's
                    # lifetime — re-acquiring every iteration would race
                    # against our own lock file.
                    if token not in lock_state:
                        acquired, holder = poll_lock.try_acquire(token)
                        lock_state[token] = acquired
                        if not acquired:
                            self.status.polling_locked_out += 1
                            if bid not in lock_reported:
                                lock_reported.add(bid)
                                logger.warning(
                                    "polling:%s: another Zero process (pid %s) is "
                                    "already long-polling this bot token — this "
                                    "instance skips Telegram polling to avoid 409 "
                                    "conflicts; stop the other instance if this one "
                                    "should own the bot",
                                    bid,
                                    holder,
                                )
                    if not lock_state[token]:
                        continue
                    try:
                        adapter = _build_binding_adapter(
                            services=self._services,
                            chat_token=token,
                            cursor_store=cursor_store,
                        )
                        updates = await asyncio.to_thread(
                            adapter.poll_once, scope_key=binding.id.value
                        )
                        polled_any = polled_any or bool(updates)
                        self.status.polling_iterations += 1
                        # A successful poll IS a successful round trip:
                        # it clears the stall watchdog.
                        last_success = _loop_monotonic()
                        stall_reported = False
                        backoff_step.pop(bid, None)
                        conflict_reported.discard(bid)
                        had_error_streak = bid in error_reported or bid in hint_reported
                        error_step.pop(bid, None)
                        error_until.pop(bid, None)
                        error_reported.discard(bid)
                        hint_reported.discard(bid)
                        if had_error_streak:
                            logger.info(
                                "polling:%s: recovered — Telegram is reachable again",
                                bid,
                            )
                        if token not in identity_verified:
                            # One-time identity check (best-effort, short):
                            # proves the token works and names the bot in the
                            # log, so "is my bot actually online?" has a
                            # definitive answer in `zero logs`.
                            try:
                                me = await asyncio.to_thread(adapter.get_me)
                            except Exception:  # noqa: BLE001 - cosmetic probe
                                logger.debug(
                                    "polling:%s: getMe identity probe failed "
                                    "(will retry after the next successful poll)",
                                    bid,
                                )
                            else:
                                identity_verified.add(token)
                                logger.info(
                                    "polling:%s: Telegram bot online: @%s (id=%s)",
                                    bid,
                                    me.get("username"),
                                    me.get("id"),
                                )
                    except asyncio.CancelledError:
                        raise
                    except TelegramConflictError as exc:
                        self.status.polling_conflicts += 1
                        step = min(6, backoff_step.get(bid, 0) + 1)
                        backoff_step[bid] = step
                        delay = min(60.0, 5.0 * (2 ** (step - 1)))
                        backoff_until[bid] = _loop_monotonic() + delay
                        if bid not in conflict_reported:
                            conflict_reported.add(bid)
                            logger.warning(
                                "polling:%s: Telegram reports ANOTHER getUpdates "
                                "consumer for this bot token (409) — backing off "
                                "%.0fs before retrying; if another Zero instance "
                                "is running, stop it or let it own the bot",
                                bid,
                                delay,
                            )
                        else:
                            logger.debug("polling:%s: conflict backoff %.0fs", bid, delay)
                        self._record_error(f"polling:{bid}: {type(exc).__name__}")
                    except Exception as exc:  # noqa: BLE001 - per-binding isolation
                        step = min(6, error_step.get(bid, 0) + 1)
                        error_step[bid] = step
                        delay = min(60.0, 2.0 * (2 ** (step - 1)))
                        error_until[bid] = _loop_monotonic() + delay
                        if bid not in error_reported:
                            error_reported.add(bid)
                            # First failure of the streak: full sanitized
                            # detail (cause type + message, bot token
                            # redacted) so the operator can act on facts.
                            logger.warning(
                                "polling:%s: %s (retrying in %.0fs)", bid, exc, delay
                            )
                            self._record_error(f"polling:{bid}: {exc}")
                        else:
                            logger.debug(
                                "polling:%s: still failing (%s) — backoff %.0fs",
                                bid,
                                type(exc).__name__,
                                delay,
                            )
                            self._record_error(f"polling:{bid}: {type(exc).__name__}")
                        if step >= 3 and bid not in hint_reported:
                            hint_reported.add(bid)
                            logger.warning(
                                "polling:%s: Telegram has failed %s times in a row — "
                                "if your network blocks or throttles api.telegram.org "
                                "(common on filtered networks), set "
                                "ZERO_TELEGRAM_PROXY_URL (e.g. socks5://127.0.0.1:1080 "
                                "or http://127.0.0.1:8080) in your environment or "
                                "ZERO_HOME/.env and restart; standard HTTPS_PROXY / "
                                "ALL_PROXY variables are honored too",
                                bid,
                                step,
                            )
                if not polled_any:
                    await self._wait(interval)
        finally:
            poll_lock.release_all()

    # ------------------------------------------------------------------
    # Tick bodies (run in worker threads; synchronous service boundary)
    # ------------------------------------------------------------------

    def _scheduler_tick(self) -> None:
        services = self._services
        provider_names = services.providers.registered_provider_names
        provider = provider_names[0] if provider_names else "openai-compatible"
        model_name = self._settings.openai_model
        # Round-7 routing alignment (live fix): config_sync pins the tick
        # to ``routing.primary_model`` — the scheduler's LLM calls
        # (decomposition + task execution) must call the configured
        # model, not the ``settings.openai_model`` default. On the
        # operator's gateway the gpt-4o-mini default stopped being served
        # outright (every task call edge-403'd) while the aligned planner
        # and chat bridge kept working — this override makes the tasks
        # follow the SAME routing truth.
        try:
            tick_provider, tick_model = services.scheduler.tick_routing_override()
        except Exception:  # noqa: BLE001 - scheduler optional in some compositions
            tick_provider, tick_model = None, None
        if tick_provider:
            provider = tick_provider
        if tick_model:
            model_name = tick_model
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


def _loop_monotonic() -> float:
    """Monotonic seconds for polling backoff timing (test-seamable)."""
    import time

    return time.monotonic()


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

    2026-08-29: the API base now honors ``ZERO_TELEGRAM_API_BASE``
    (same escape hatch the setup/doctor probes always had), so a
    self-hosted Bot API gateway or a test server works uniformly.
    """
    import os as _os

    from zero.adapters.messaging import RetryPolicy
    from zero.adapters.telegram import TelegramAdapter

    poll_timeout = 25
    transports = services.interface_transports
    transport = transports.http_transport if transports is not None else None
    api_base = _os.environ.get(
        "ZERO_TELEGRAM_API_BASE", "https://api.telegram.org"
    ).rstrip("/")
    return TelegramAdapter(
        event_handler=services.interfaces.process_inbound_event,
        transport=transport,
        bot_token=chat_token,
        cursor_store=cursor_store,
        api_base_url=api_base,
        poll_timeout_seconds=poll_timeout,
        retry_policy=RetryPolicy(
            attempts=1,
            backoff_seconds=0.25,
            timeout_seconds=float(poll_timeout) + 10.0,
        ),
    )


__all__ = ["BackgroundWorkerHost", "WorkerHostStatus"]
