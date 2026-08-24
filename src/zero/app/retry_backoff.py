"""Rate-limit-aware retry delay computation for failed tasks (GAP 12).

Per ``docs/gap-designs/GAP-12-retry-backoff.md``:

- base delay doubles per attempt, capped at one hour;
- uniform jitter decorrelates concurrent retries;
- an explicit provider Retry-After hint embedded in the failure text is
  honored (capped) ahead of the formula — Hermes parity via
  ``parse_retry_after_seconds``.
"""

from __future__ import annotations

import random
import re
from typing import Final

RETRY_BASE_DELAY_SECONDS: Final[int] = 60
RETRY_MAX_DELAY_SECONDS: Final[int] = 3600

#: Matches the detail the provider layer embeds into rate-limit errors:
#: ``"provider HTTP request failed with status 429 (retry_after=17)"``.
_RETRY_AFTER_RE = re.compile(r"retry_after\s*=\s*(\d+)", re.IGNORECASE)


def parse_retry_after_seconds(error_text: str | None) -> int | None:
    """Extract a Retry-After seconds value from a failure message.

    Returns ``None`` when the text carries no parsable hint or the
    value is implausible.
    """
    if not error_text:
        return None
    match = _RETRY_AFTER_RE.search(error_text)
    if match is None:
        return None
    try:
        value = int(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees digits
        return None
    if value < 0:
        return None
    return value


def compute_retry_delay(
    attempt_number: int,
    error_text: str = "",
    *,
    rng: random.Random | None = None,
) -> int:
    """Compute the seconds a failed task must wait before requeueing.

    - attempt 1 → 60 s base, attempt 2 → 120 s, … capped at 3600 s;
    - jitter adds a uniform random [0, 50% of base];
    - a provider Retry-After hint wins outright (capped at 3600 s).
    """
    rng = rng or random.Random()
    honored = parse_retry_after_seconds(error_text)
    if honored is not None:
        return min(honored, RETRY_MAX_DELAY_SECONDS)
    exponent = max(0, int(attempt_number) - 1)
    capped_exponent = min(exponent, 16)
    base = min(RETRY_BASE_DELAY_SECONDS * (2**capped_exponent), RETRY_MAX_DELAY_SECONDS)
    jitter = rng.uniform(0, base / 2)
    delay = int(base + jitter)
    return max(0, min(delay, RETRY_MAX_DELAY_SECONDS))


__all__ = [
    "RETRY_BASE_DELAY_SECONDS",
    "RETRY_MAX_DELAY_SECONDS",
    "compute_retry_delay",
    "parse_retry_after_seconds",
]
