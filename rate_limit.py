"""Monotonic-clock rate limiting with exponential backoff for repeated error notifications."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ObservedError

BACKOFF_MULTIPLIERS = (1, 3, 12, 72)
MAX_BACKOFF_SECONDS = 21600
_MAX_STEP = len(BACKOFF_MULTIPLIERS) - 1


class DecisionKind(StrEnum):
    """Rate-limit decision kinds."""

    EMIT_FRESH = "emit_fresh"
    SUPPRESS = "suppress"
    EMIT_RESUME = "emit_resume"


@dataclass(frozen=True, slots=True)
class Decision:
    """Decision returned by the rate limiter."""

    kind: DecisionKind
    suppressed_count: int = 0


@dataclass(frozen=True, slots=True)
class SweepEntry[K]:
    """One expired suppressed bucket returned by `RateLimiter.sweep`."""

    key: K
    suppressed_count: int
    last_record: ObservedError | None = None


@dataclass(slots=True)
class _Bucket:
    last_emit: float
    step: int = 0
    suppressed_count: int = 0
    last_record: ObservedError | None = None

    def advance(self, now: float) -> tuple[int, ObservedError | None]:
        result = (self.suppressed_count, self.last_record)
        self.last_emit = now
        self.step = min(self.step + 1, _MAX_STEP)
        self.suppressed_count = 0
        self.last_record = None
        return result


class RateLimiter[K]:
    """Track repeated error keys over an exponentially backing-off monotonic window."""

    def __init__(self, window_seconds: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_seconds = max(1, window_seconds)
        self._clock = clock
        self._buckets: dict[K, _Bucket] = {}

    def configure_window(self, window_seconds: int) -> None:
        """Update the base rate-limit window without dropping suppression state."""
        self.window_seconds = max(1, window_seconds)

    def reset(self, key: K) -> None:
        """Clear a key's rate-limit bucket so the next alert starts fresh at step 0."""
        self._buckets.pop(key, None)

    def _window_for_step(self, step: int) -> int:
        multiplier = BACKOFF_MULTIPLIERS[min(step, _MAX_STEP)]
        return min(self.window_seconds * multiplier, max(self.window_seconds, MAX_BACKOFF_SECONDS))

    def gate(self, key: K, record: ObservedError | None = None) -> Decision:
        """Return whether an event for key should emit or suppress."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(last_emit=now)
            return Decision(DecisionKind.EMIT_FRESH)

        if now - bucket.last_emit < self._window_for_step(bucket.step):
            bucket.suppressed_count += 1
            if record is not None:
                bucket.last_record = record
            return Decision(DecisionKind.SUPPRESS, bucket.suppressed_count)

        if bucket.suppressed_count:
            suppressed_count, _ = bucket.advance(now)
            return Decision(DecisionKind.EMIT_RESUME, suppressed_count)

        bucket.last_emit = now
        bucket.step = 0
        return Decision(DecisionKind.EMIT_FRESH)

    def sweep(self) -> list[SweepEntry[K]]:
        """Return expired suppressed buckets that need rollup emission and prune quiet ones."""
        now = self._clock()
        summaries: list[SweepEntry[K]] = []
        for key, bucket in tuple(self._buckets.items()):
            if now - bucket.last_emit < self._window_for_step(bucket.step):
                continue
            if bucket.suppressed_count:
                count, last_record = bucket.advance(now)
                summaries.append(SweepEntry(key=key, suppressed_count=count, last_record=last_record))
                continue
            self._buckets.pop(key, None)
        return summaries
