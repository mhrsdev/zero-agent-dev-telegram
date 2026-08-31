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
from typing import Any

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
        # Live-run fix (2026-08-31): an engine kill between a task claim
        # and its terminal transition used to poison every LATER run:
        # ``agent_instances`` rows stayed ``running`` (every new task
        # failed with "agent type concurrency limit reached") and
        # ``worktrees`` rows stayed in the partial-unique states (every
        # re-attempt died with "UNIQUE constraint failed:
        # worktrees.task_id"). Recovery previously required an operator
        # to POST /recover per execution; it now runs automatically at
        # boot, before the first tick can claim anything.
        try:
            await asyncio.to_thread(self._startup_recovery)
        except Exception as exc:  # noqa: BLE001 - boot recovery must not kill the host
            logger.warning("startup recovery failed: %s", type(exc).__name__)
        tasks = []
        # Mega-scale fix (2026-08-31, live-found, M15b): with
        # ZERO_TICK_PROJECT_PARALLELISM > 1 each project gets its OWN
        # recurring scheduler loop (Hermes-style per-scope loops) + a
        # bounded concurrency semaphore. A pool-per-cycle starved fast
        # projects behind the slowest tick of the cycle; independent
        # loops let every project tick at its own cadence and pick up
        # projects created AFTER boot (dynamic discovery below).
        if max(1, min(8, int(self._settings.tick_project_parallelism))) > 1:
            tasks.append(
                asyncio.create_task(
                    self._project_scheduler_coordinator(),
                    name="zero-scheduler-coordinator",
                )
            )
        else:
            tasks.append(
                asyncio.create_task(self._scheduler_loop(), name="zero-scheduler-worker")
            )
        tasks.append(asyncio.create_task(self._delivery_loop(), name="zero-delivery-worker"))
        tasks.append(asyncio.create_task(self._polling_loop(), name="zero-polling-worker"))
        # GAP 4 wiring fix (2026-08-31): ``ZERO_TELEGRAM_MODE=user_session``
        # used to validate in Settings but changed NOTHING at runtime — the
        # adapter existed and was never constructed. The mode now starts a
        # real MTProto loop that feeds the SAME durable intake, and its
        # outbound path is attached to the transport service.
        if self._settings.telegram_mode == "user_session":
            tasks.append(
                asyncio.create_task(
                    self._user_session_loop(), name="zero-user-session-worker"
                )
            )
        self._tasks = tasks
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

    def _startup_recovery(self) -> None:
        """Reconcile state a killed process left behind (boot, pre-tick).

        1. Release stale ``running`` agent-instance leases. A lease is
           valid only while its task is ``running``; anything else is a
           leak that permanently consumes the type's concurrency budget.
        2. Reconcile every non-terminal execution: tasks ``running``
           under dead/expired leases go back to ``ready`` (their attempt
           marked ``unknown``), readiness is recomputed, and graphs with
           nothing left to run are paused — the same contract as the
           explicit ``POST /recover`` endpoint, applied to all projects.
        3. Abandon worktrees stuck in the partial-unique states
           (``allocated``/``active``/``interrupted``) whose task is no
           longer running, so the task's next attempt can create a fresh
           worktree instead of dying on
           ``UNIQUE constraint failed: worktrees.task_id``.
        """
        services = self._services
        released = 0
        agent_type_repo = getattr(services.worker, "_agent_type_repo", None)
        if agent_type_repo is not None:
            try:
                released = agent_type_repo.release_stale_running_instances()
            except Exception as exc:  # noqa: BLE001 - bookkeeping is advisory
                logger.warning("stale instance sweep failed: %s", type(exc).__name__)
        recovered = 0
        try:
            projects = services.identity.list_projects()
        except Exception as exc:  # noqa: BLE001 - recovery is best-effort at boot
            logger.warning("startup recovery could not list projects: %s", type(exc).__name__)
            return
        for project in projects:
            try:
                executions = services.worker.list_project_executions(
                    project_id=project.id,
                    actor_id=project.owner_user_id,
                    source="system",
                )
            except Exception as exc:  # noqa: BLE001 - per-project isolation
                logger.debug(
                    "startup recovery: executions of project %s unavailable: %s",
                    project.id.value,
                    type(exc).__name__,
                )
                continue
            for execution in executions:
                if execution.state in {"completed", "failed", "cancelled"}:
                    continue
                try:
                    services.worker.recover_after_restart(
                        execution_id=execution.id,
                        project_id=project.id,
                        actor_id=project.owner_user_id,
                        source="system",
                    )
                    recovered += 1
                except Exception as exc:  # noqa: BLE001 - per-execution isolation
                    logger.debug(
                        "startup recovery: execution %s recover failed: %s",
                        execution.id.value,
                        type(exc).__name__,
                    )
        abandoned = 0
        worktree_service = getattr(services, "worktree", None)
        if worktree_service is not None:
            try:
                abandoned = worktree_service.abandon_stale_worktrees()
            except Exception as exc:  # noqa: BLE001 - bookkeeping is advisory
                logger.warning("stale worktree sweep failed: %s", type(exc).__name__)
        if released or recovered or abandoned:
            logger.info(
                "startup recovery: %d stale agent instance lease(s) released, "
                "%d execution(s) reconciled, %d stale worktree(s) abandoned",
                released,
                recovered,
                abandoned,
            )

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
        # Hermes parity (gap E): message events dispatch OFF the polling
        # loop, serialized per chat, so one slow agent turn cannot stall
        # other chats, the heartbeat, or the stall watchdog.
        dispatcher = _ChatSerialDispatcher()
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
        # Hermes-parity mention gating inputs (2026-08-31): the bot's own
        # identity is resolved ONCE per token (getMe) BEFORE the first
        # poll and fed into every adapter rebuild, so @mention /
        # reply-to-bot detection works from the very first update.
        bot_identity: dict[str, dict[str, Any]] = {}
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
                        if token not in bot_identity:
                            # Resolve the bot identity BEFORE the first
                            # poll so mention gating is correct from the
                            # first update. A probe failure stays
                            # non-fatal: the adapter fails OPEN on
                            # mention detection (commands still route).
                            probe = _build_binding_adapter(
                                services=self._services,
                                chat_token=token,
                                cursor_store=cursor_store,
                            )
                            try:
                                me = await asyncio.to_thread(probe.get_me)
                                bot_identity[token] = {
                                    "username": str(me.get("username") or ""),
                                    "id": str(me.get("id") or ""),
                                }
                                identity_verified.add(token)
                                logger.info(
                                    "polling:%s: Telegram bot online: @%s (id=%s)",
                                    bid,
                                    me.get("username"),
                                    me.get("id"),
                                )
                            except Exception:  # noqa: BLE001 - identity is best-effort
                                bot_identity[token] = {}
                                logger.warning(
                                    "polling:%s: getMe identity probe failed — "
                                    "mention gating runs fail-open until it "
                                    "succeeds",
                                    bid,
                                )
                        adapter = _build_binding_adapter(
                            services=self._services,
                            chat_token=token,
                            cursor_store=cursor_store,
                            bot_username=bot_identity.get(token, {}).get("username"),
                            bot_id=bot_identity.get(token, {}).get("id"),
                            # getattr: test doubles use SimpleNamespace
                            # bindings without the full InterfaceBinding
                            # attribute surface.
                            group_chat_id=getattr(binding, "chat_id", None),
                        )
                        poll_kwargs: dict[str, Any] = {"scope_key": binding.id.value}
                        try:
                            import inspect as _inspect

                            if (
                                "background_dispatch"
                                in _inspect.signature(adapter.poll_once).parameters
                            ):
                                poll_kwargs["background_dispatch"] = dispatcher
                        except (TypeError, ValueError):  # noqa: BLE001
                            pass
                        updates = await asyncio.to_thread(adapter.poll_once, **poll_kwargs)
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

    async def _user_session_loop(self) -> None:
        """Host the MTProto user-session adapter (GAP 4 wiring).

        ``ZERO_TELEGRAM_MODE=user_session`` used to be validated but never
        acted upon. This loop resolves the three session secrets from the
        encrypted store, connects the personal account, registers the
        inbound handler on Telethon's private loop, and attaches the
        adapter to the outbound transport service. Any missing piece
        degrades with a clear log instead of leaving a zombie mode.
        """
        secrets = _resolve_user_session_secrets(self._services)
        if secrets is None:
            logger.warning(
                "ZERO_TELEGRAM_MODE=user_session but the MTProto session "
                "secrets (telegram_session_api_id / telegram_session_api_hash "
                "/ telegram_session_string) are not in the management "
                "project's secret store - user-session mode stays idle; "
                "Bot API bindings are unaffected"
            )
            return
        host = _UserSessionHost(services=self._services, secrets=secrets)
        try:
            await asyncio.to_thread(host.start)
        except Exception as exc:  # noqa: BLE001 - the session must not kill the host
            self._record_error(f"user-session: {type(exc).__name__}")
            return
        try:
            while not self._stop.is_set():
                await self._wait(1.0)
        finally:
            try:
                await asyncio.to_thread(host.stop)
            except Exception:  # noqa: BLE001 - teardown best-effort
                pass

    # ------------------------------------------------------------------
    # Tick bodies (run in worker threads; synchronous service boundary)
    # ------------------------------------------------------------------

    def _scheduler_tick(self) -> None:
        """Serial mode (parallelism=1): tick every project once, in order."""
        for project in self._services.identity.list_projects():
            self._tick_single_project(project)

    async def _project_scheduler_coordinator(self) -> None:
        """M15b: discover projects dynamically and run one scheduler loop
        per project, bounded by ZERO_TICK_PROJECT_PARALLELISM.

        A project created after boot joins on the next scan without an
        engine restart; a loop that dies is restarted on the next scan.
        """
        interval = max(0.1, float(self._settings.scheduler_interval_seconds))
        parallelism = max(1, min(8, int(self._settings.tick_project_parallelism)))
        semaphore = asyncio.Semaphore(parallelism)
        loops: dict[str, asyncio.Task] = {}
        try:
            while not self._stop.is_set():
                try:
                    projects = await asyncio.to_thread(
                        self._services.identity.list_projects
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - coordinator must survive
                    self._record_error(
                        f"coordinator:list_projects: {type(exc).__name__}"
                    )
                    projects = []
                for project in projects:
                    key = project.id.value
                    existing = loops.get(key)
                    if existing is not None and not existing.done():
                        continue
                    if existing is not None and existing.done():
                        exc = existing.exception()
                        if exc is not None and not isinstance(
                            exc, asyncio.CancelledError
                        ):
                            self._record_error(
                                f"scheduler-project:{key}: {type(exc).__name__}"
                            )
                    loops[key] = asyncio.create_task(
                        self._project_scheduler_loop(project, semaphore),
                        name=f"zero-scheduler:{key}",
                    )
                await self._wait(interval)
        finally:
            for task in loops.values():
                task.cancel()

    async def _project_scheduler_loop(
        self, project, semaphore: asyncio.Semaphore
    ) -> None:
        """Tick ONE project forever at its own cadence (M15b)."""
        interval = max(0.1, float(self._settings.scheduler_interval_seconds))
        while not self._stop.is_set():
            await semaphore.acquire()
            try:
                await asyncio.to_thread(self._tick_single_project, project)
                self.status.scheduler_ticks += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - per-project isolation
                self._record_error(
                    f"scheduler-project:{project.id.value}: {type(exc).__name__}"
                )
            finally:
                semaphore.release()
            await self._wait(interval)

    def _tick_single_project(self, project) -> None:
        """Tick ONE project: routing resolution + lease reconciliation +
        scheduler run_once.

        Extracted from the historical serial loop so projects can tick
        independently (serial mode, or one dedicated loop per project
        under ZERO_TICK_PROJECT_PARALLELISM) without changing the
        per-project behavior or error isolation.
        """
        services = self._services
        provider_names = services.providers.registered_provider_names
        provider = provider_names[0] if provider_names else "openai-compatible"
        model_name = self._settings.openai_model
        # Round-7 routing alignment (live fix): config_sync pins the tick
        # to ``routing.primary_model`` — the scheduler's LLM calls
        # (decomposition + task execution) must call the configured
        # model, not the ``settings.openai_model`` default.
        try:
            tick_provider, tick_model = services.scheduler.tick_routing_override()
        except Exception:  # noqa: BLE001 - scheduler optional in some compositions
            tick_provider, tick_model = None, None
        if tick_provider:
            provider = tick_provider
        if tick_model:
            model_name = tick_model
        combined_command = self._settings.combined_test_command
        try:
            owner = project.owner_user_id
            # Live-run fix (2026-08-31): reconcile running tasks whose
            # lease expired (dead owner) BEFORE claiming — boot-only
            # recovery left dead-lease tasks blocking their graph
            # (and their agent-type slot) forever when the lease was
            # still live at boot but the owner died afterwards.
            try:
                reconciled = services.worker.reconcile_expired_leases(
                    project_id=project.id,
                    actor_id=owner,
                    source="system",
                )
                if reconciled:
                    logger.info(
                        "tick reconciliation: %d execution(s) recovered from expired leases",
                        reconciled,
                    )
            except Exception as exc:  # noqa: BLE001 - advisory
                self._record_error(
                    f"reconcile:{project.id.value}: {type(exc).__name__}"
                )
            # Hermes live-report parity (gap C): task executions
            # stream live progress into every enabled Telegram
            # binding of the project (lazy: quiet ticks send
            # nothing). Any sink failure degrades to silent — the
            # durable scheduler outcome never depends on it.
            stream_cb, task_event_cb = self._build_live_progress_callbacks(
                project_id=project.id, actor_id=owner
            )
            services.scheduler.run_once(
                project_id=project.id,
                actor_id=owner,
                lease_owner="managed-worker-host",
                provider=provider,
                model_name=model_name,
                combined_test_command=(combined_command[0] if combined_command else None),
                combined_test_args=tuple(combined_command[1:]),
                combined_test_timeout_seconds=self._settings.combined_test_timeout_seconds,
                stream_callback=stream_cb,
                task_event_callback=task_event_cb,
            )
        except Exception as exc:  # noqa: BLE001 - per-project isolation
            self._record_error(f"scheduler:{project.id.value}: {type(exc).__name__}")

    def _build_live_progress_callbacks(self, *, project_id, actor_id):
        """Build (stream_callback, task_event_callback) fanning out to
        every enabled Telegram binding of the project.

        Returns ``(None, None)`` when no Telegram binding is enabled —
        the common case for web-only projects — so the hot path stays
        allocation-free. All adapter construction is lazy (first event),
        and every delivery failure is swallowed: progress is
        presentation, never authority.
        """
        try:
            transports = self._services.interface_transports
            if transports is None:
                return None, None
            bindings = self._services.result_delivery.list_enabled_bindings(project_id)
            targets = [b for b in bindings if b.platform == "telegram"]
            if not targets:
                return None, None
            from zero.app.telegram_live import TelegramExecutionProgress

            progress: dict[str, list[Any]] = {}

            def _progress_for(execution_id: str) -> list[Any]:
                views = progress.get(execution_id)
                if views is not None:
                    return views
                views = []
                for binding in targets:
                    try:
                        adapter = transports.build_telegram_adapter(
                            project_id=project_id,
                            binding_id=binding.id,
                            actor_id=actor_id,
                        )
                        views.append(
                            TelegramExecutionProgress(
                                adapter=adapter,
                                chat_id=str(binding.chat_id),
                                topic_id=(
                                    str(binding.topic_id)
                                    if getattr(binding, "topic_id", None)
                                    else None
                                ),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - per-binding isolation
                        logger.debug(
                            "live progress adapter for %s unavailable: %s",
                            binding.id.value,
                            type(exc).__name__,
                        )
                progress[execution_id] = views
                return views

            def stream_callback(execution_id_value: str, payload: dict) -> None:
                for view in _progress_for(execution_id_value):
                    view.on_stream_event(payload)

            def task_event_callback(payload: dict) -> None:
                execution_id_value = str(payload.get("execution_id") or "")
                kind = payload.get("type")
                for view in _progress_for(execution_id_value):
                    if kind == "task_started":
                        view.on_task_started(
                            str(payload.get("task_id") or ""),
                            str(payload.get("objective") or ""),
                        )
                    elif kind in ("task_completed", "task_failed"):
                        view.on_task_finished(
                            str(payload.get("task_id") or ""),
                            "completed" if kind == "task_completed" else "failed",
                            str(payload.get("detail") or ""),
                        )

            return stream_callback, task_event_callback
        except Exception:  # noqa: BLE001 - progress must never break the tick
            return None, None

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


class _ChatSerialDispatcher:
    """Per-chat serialized background dispatch for polled messages.

    Hermes parity (gap E): a conversational agent turn can run for
    MINUTES; dispatching it inline in the polling worker stalled every
    other chat, the heartbeat, and the stall watchdog behind one slow
    LLM call. Message events are therefore handed to single-thread
    executors keyed by chat id — per-chat ORDER is preserved, different
    chats proceed in parallel, and the polling loop returns immediately.

    Bounded by design: at most ``max_workers`` chat lanes exist at once
    and each lane queues at most ``max_queue`` events. When the queue is
    full the submission raises — the adapter logs and drops, the durable
    claim stays open, and recovery replays the event.
    """

    def __init__(self, *, max_workers: int = 8, max_queue: int = 16) -> None:
        import queue as _queue
        import threading as _threading

        if max_workers < 1 or max_queue < 1:
            raise ValueError("max_workers and max_queue must be positive")
        self._max_workers = max_workers
        self._max_queue = max_queue
        self._lanes: dict[str, tuple[_threading.Thread, "_queue.SimpleQueue"]] = {}
        self._lock = _threading.Lock()

    def _lane(self, chat_id: str):
        import queue as _queue
        import threading as _threading

        with self._lock:
            existing = self._lanes.get(chat_id)
            if existing is not None:
                thread, q = existing
                if thread.is_alive():
                    return thread, q
                del self._lanes[chat_id]
            if len(self._lanes) >= self._max_workers:
                raise RuntimeError(
                    f"all {self._max_workers} dispatch lanes are busy"
                )
            q: "_queue.SimpleQueue" = _queue.SimpleQueue()
            thread = _threading.Thread(
                target=self._drain_lane, args=(q,), daemon=True,
                name=f"zero-telegram-dispatch-{chat_id}",
            )
            thread.start()
            self._lanes[chat_id] = (thread, q)
            return thread, q

    @staticmethod
    def _drain_lane(q) -> None:
        while True:
            job = q.get()
            if job is None:
                return
            fn = job[0]
            try:
                fn()
            except Exception:  # noqa: BLE001 - lane must survive job crashes
                logger.debug("dispatch lane job failed", exc_info=True)

    def submit_for_chat(self, chat_id: str, fn) -> None:
        """Queue ``fn`` on the chat's lane; raises when saturated."""
        thread, q = self._lane(str(chat_id))
        if q.qsize() >= self._max_queue:
            raise RuntimeError(f"dispatch lane for chat {chat_id} is saturated")
        q.put((fn,))

    def submit(self, fn) -> None:
        """Adapter contract: serialize by the event's chat id when the
        callable carries one (``_run`` closes over the event), else run
        on the anonymous pool lane."""
        chat_id = getattr(fn, "chat_id", None) or "_pool"
        self.submit_for_chat(chat_id, fn)

    def __call__(self, fn) -> None:
        """Live-run fix (2026-08-31): the adapter's dispatch contract is
        ``background_dispatch(_run)`` — a CALLABLE sink. The dispatcher
        only exposed ``submit``, so every polled group message died with
        "'_ChatSerialDispatcher' object is not callable" and the bot
        never processed any message. Alias the call operator to submit
        so both conventions work."""
        self.submit(fn)


def _group_require_mention_override(chat_id: str) -> bool | None:
    """Per-group ``require_mention`` override from ``config.yaml``.

    Reads ``access.groups[].require_mention`` (added 2026-08-31). None
    keeps the global default; True/False overrides it for this chat.
    """
    try:
        from zero.manage.core.config import ConfigService, zero_home

        cfgsvc = ConfigService(zero_home())
        if not cfgsvc.exists():
            return None
        cfg = cfgsvc.load()
        for group in cfg.access.groups:
            if str(group.chat_id) == str(chat_id):
                return group.require_mention
    except Exception as exc:  # noqa: BLE001 - policy lookup must never break polling
        logger.debug("group mention override lookup failed: %s", type(exc).__name__)
    return None


def _build_binding_adapter(
    *,
    services,
    chat_token: str,
    cursor_store,
    bot_username: str | None = None,
    bot_id: str | None = None,
    group_chat_id: str | None = None,
):
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

    2026-08-31 (Hermes parity): the adapter receives the bot's own
    identity (username + id from getMe) and the per-group mention
    override so unaddressed group messages and other bots' messages are
    skipped at the transport boundary instead of spawning agent turns.
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

    require_mention_override: bool | None = None
    if group_chat_id is not None:
        require_mention_override = _group_require_mention_override(group_chat_id)

    return TelegramAdapter(
        event_handler=services.interfaces.process_inbound_event,
        transport=transport,
        bot_token=chat_token,
        cursor_store=cursor_store,
        api_base_url=api_base,
        poll_timeout_seconds=poll_timeout,
        bot_username=bot_username,
        bot_id=bot_id,
        require_mention=require_mention_override,
        retry_policy=RetryPolicy(
            attempts=1,
            backoff_seconds=0.25,
            timeout_seconds=float(poll_timeout) + 10.0,
        ),
    )


def _resolve_user_session_secrets(services) -> dict[str, str] | None:
    """Resolve the MTProto session credentials from the encrypted store.

    GAP 4 (2026-08-31): the user-session mode is only real when all
    three secrets exist in the management project's secret store:
    ``telegram_session_api_id``, ``telegram_session_api_hash``, and
    ``telegram_session_string`` (written by ``zero telegram session-login``).
    Missing entries return None — the mode degrades with a clear log.
    """
    try:
        from zero.domain.secrets import SecretError

        project = None
        for p in services.identity.list_projects():
            if p.name == "Zero Management":
                project = p
                break
        if project is None:
            return None
        owner_id = project.owner_user_id
        resolved: dict[str, str] = {}
        for name in (
            "telegram_session_api_id",
            "telegram_session_api_hash",
            "telegram_session_string",
        ):
            ref = services.secrets.get_reference_by_name(
                project_id=project.id,
                name=name,
                actor_id=owner_id,
                source="system",
            )
            if ref is None or ref.is_revoked:
                return None
            resolved[name] = services.secrets.resolve_value(
                project_id=project.id,
                secret_id=ref.id,
                actor_id=owner_id,
                source="system",
            )
        return resolved
    except SecretError:
        return None
    except Exception as exc:  # noqa: BLE001 - resolution must not crash the host
        logger.debug("user-session secret resolution failed: %s", type(exc).__name__)
        return None


class _UserSessionHost:
    """Drives one Telethon user-session client on its private loop.

    Inbound: new messages (excluding the account's own outgoing ones —
    the MTProto analog of the Bot API bot-sender filter) are converted
    to the shared ``NormalizedEvent`` intake mapping and dispatched
    through the per-chat serial dispatcher, exactly like polled Bot API
    updates. Outbound: the adapter is attached to the
    ``InterfaceTransportService`` so command replies, chat-bridge turns,
    plan cards, and result deliveries flow through the personal account
    with its rate limiter.
    """

    def __init__(self, *, services, secrets: dict[str, str]) -> None:
        self._services = services
        self._secrets = secrets
        self._adapter = None
        self._dispatcher = _ChatSerialDispatcher(max_workers=4, max_queue=16)
        self._thread: Any = None
        self._stop_event: Any = None

    def start(self) -> None:
        import threading

        from zero.adapters.user_session import (
            UserSessionTelegramAdapter,
            user_session_mode_enabled,
        )
        from zero.domain.interfaces import NormalizedEvent

        if not user_session_mode_enabled():
            logger.warning(
                "ZERO_TELEGRAM_MODE=user_session requires the [session] extra "
                "(pip install 'zero-develop[session]'); staying on Bot API"
            )
            return

        adapter = UserSessionTelegramAdapter(
            self._services.interfaces.process_inbound_event,
            api_id=int(self._secrets["telegram_session_api_id"]),
            api_hash=self._secrets["telegram_session_api_hash"],
            session_string=self._secrets["telegram_session_string"],
        )
        adapter.connect()
        self._adapter = adapter

        transports = self._services.interface_transports
        if transports is not None and hasattr(transports, "attach_session_adapter"):
            transports.attach_session_adapter(adapter)
            logger.info(
                "user-session outbound attached to the interface transport "
                "service (replies flow through the personal account)"
            )

        def _on_message(update: dict[str, Any]) -> None:
            event = adapter.normalize_update(update)
            if event is None:
                return
            try:
                self._dispatcher.submit_for_chat(
                    str(event.chat_id),
                    lambda ev=event: adapter.dispatch_inbound(_as_mapping(ev)),
                )
            except Exception as exc:  # noqa: BLE001 - intake must not crash the loop
                logger.warning(
                    "user-session dispatch rejected: %s: %s",
                    type(exc).__name__,
                    str(exc)[:200],
                )

        def _as_mapping(event: NormalizedEvent) -> dict[str, Any]:
            return {
                "sender_id": event.external_actor_id,
                "chat_id": event.chat_id,
                "message": event.content,
                "id": event.external_event_id,
                "event_id": event.external_event_id,
            }

        stop_event = threading.Event()
        self._stop_event = stop_event

        def _run() -> None:
            client = getattr(adapter, "_client", None)
            registered = False
            if client is not None:
                try:
                    from telethon import events

                    def _telethon_handler(telethon_event):  # pragma: no cover - MTProto I/O
                        try:
                            message = telethon_event.message
                            if message is None or message.out:
                                return
                            sender_id = message.sender_id
                            chat_id = message.chat_id
                            if sender_id is None or chat_id is None:
                                return
                            _on_message(
                                {
                                    "sender_id": sender_id,
                                    "chat_id": chat_id,
                                    "message": message.message or "",
                                    "id": message.id,
                                    "event_id": f"us_{chat_id}_{message.id}",
                                }
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug("telethon handler failed", exc_info=True)

                    client.add_event_handler(
                        _telethon_handler, events.NewMessage()
                    )
                    registered = True
                    logger.info(
                        "user-session worker online: personal account is now "
                        "connected (MTProto); messages dispatch through the "
                        "durable intake"
                    )
                    while not stop_event.is_set():
                        stop_event.wait(timeout=1.0)
                except ImportError:
                    logger.warning(
                        "telethon is not importable — user-session mode stays "
                        "on Bot API"
                    )
            if not registered:
                logger.warning(
                    "user-session worker could not register its Telethon "
                    "handler; no inbound events will be processed"
                )
            try:
                adapter.close()
            except Exception:  # noqa: BLE001 - teardown best-effort
                pass

        self._thread = threading.Thread(target=_run, daemon=True, name="zero-user-session")
        self._thread.start()

    def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def _user_session_secrets_available(services) -> dict[str, str] | None:
    return _resolve_user_session_secrets(services)


__all__ = [
    "BackgroundWorkerHost",
    "WorkerHostStatus",
    "_build_binding_adapter",
    "_resolve_user_session_secrets",
]
