"""GAP 12 tests: rate-limit-aware task retry backoff."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from zero.app.retry_backoff import (
    RETRY_BASE_DELAY_SECONDS,
    RETRY_MAX_DELAY_SECONDS,
    compute_retry_delay,
    parse_retry_after_seconds,
)
from zero.app.scheduler_service import SchedulerService


class TestParseRetryAfter:
    def test_parses_provider_detail_format(self):
        text = "provider HTTP request failed with status 429 (retry_after=17)"
        assert parse_retry_after_seconds(text) == 17

    def test_case_insensitive(self):
        assert parse_retry_after_seconds("Retry_After = 42") == 42

    def test_missing_or_malformed_returns_none(self):
        assert parse_retry_after_seconds("some other failure") is None
        assert parse_retry_after_seconds("") is None
        assert parse_retry_after_seconds(None) is None
        assert parse_retry_after_seconds("retry_after=") is None

    def test_negative_rejected_as_none(self):
        # The regex only captures digits; a leading minus means no match.
        assert parse_retry_after_seconds("retry_after=-5") is None


class TestComputeRetryDelay:
    def test_exponential_growth_from_base(self):
        seeded = random.Random(1234)
        delay1 = compute_retry_delay(1, "", rng=seeded)
        # Attempt 1: base 60 + jitter [0, 30].
        assert RETRY_BASE_DELAY_SECONDS <= delay1 <= RETRY_BASE_DELAY_SECONDS * 3 // 2

    def test_attempt_two_doubles_the_base(self):
        seeded = random.Random(99)
        delay2 = compute_retry_delay(2, "", rng=seeded)
        assert 120 <= delay2 <= 180

    def test_caps_at_one_hour(self):
        seeded = random.Random(7)
        for attempt in (10, 16, 40):
            assert compute_retry_delay(attempt, "", rng=seeded) <= RETRY_MAX_DELAY_SECONDS

    def test_retry_after_wins_over_formula(self):
        assert compute_retry_delay(5, "status 429 (retry_after=90)") == 90

    def test_retry_after_is_capped(self):
        assert compute_retry_delay(1, "(retry_after=999999)") == RETRY_MAX_DELAY_SECONDS

    def test_delay_never_negative(self):
        seeded = random.Random(3)
        assert compute_retry_delay(1, "", rng=seeded) >= 0

    def test_jitter_stays_within_half_base_bounds(self):
        seeded = random.Random(4242)
        values = [compute_retry_delay(1, "", rng=seeded) for _ in range(50)]
        assert all(60 <= v <= 90 for v in values)
        assert len(set(values)) > 1  # decorrelated


class TestSchedulerRetryGating:
    @staticmethod
    def _task(next_retry_at):
        from zero.domain.execution import ExecutionId, Task, TaskId
        from zero.domain.identity import ProjectId

        return Task(
            id=TaskId("task_1"),
            execution_id=ExecutionId("exec_1"),
            project_id=ProjectId("p_1"),
            objective="obj",
            permitted_scope=("scope",),
            expected_evidence=("provider_response",),
            state="failed",
            next_retry_at=next_retry_at,
        )

    def test_null_next_retry_at_is_immediately_eligible(self):
        assert SchedulerService._retry_delay_elapsed(self._task(None)) is True

    def test_future_timestamp_blocks(self):
        future = (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert SchedulerService._retry_delay_elapsed(self._task(future)) is False

    def test_past_timestamp_eligible(self):
        past = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert SchedulerService._retry_delay_elapsed(self._task(past)) is True

    def test_unparsable_timestamp_fails_open(self):
        # A corrupt stamp must never wedge a task forever.
        assert SchedulerService._retry_delay_elapsed(self._task("not-a-date")) is True
