"""Monotonic-clock rate limiting for repeated error notifications."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


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


@dataclass(slots=True)
class _Bucket:
    last_emit: float
    suppressed_count: int = 0


class RateLimiter:
    """Track repeated `(logger, event)` keys over a monotonic time window."""

    def __init__(
        self,
        window_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window_seconds = max(1, window_seconds)
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def configure_window(self, window_seconds: int) -> None:
        """Update the rate-limit window without dropping suppression state."""
        self.window_seconds = max(1, window_seconds)

    def gate(self, key: tuple[str, str]) -> Decision:
        """Return whether a fresh event for key should emit or suppress."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(last_emit=now)
            return Decision(DecisionKind.EMIT_FRESH)

        if now - bucket.last_emit < self.window_seconds:
            bucket.suppressed_count += 1
            return Decision(DecisionKind.SUPPRESS, bucket.suppressed_count)

        suppressed_count = bucket.suppressed_count
        bucket.last_emit = now
        bucket.suppressed_count = 0
        if suppressed_count:
            return Decision(DecisionKind.EMIT_RESUME, suppressed_count)
        return Decision(DecisionKind.EMIT_FRESH)

    def sweep(self) -> list[tuple[tuple[str, str], int]]:
        """Return expired suppressed buckets that need summary emission."""
        now = self._clock()
        summaries: list[tuple[tuple[str, str], int]] = []
        for key, bucket in tuple(self._buckets.items()):
            if now - bucket.last_emit < self.window_seconds:
                continue
            if bucket.suppressed_count:
                summaries.append((key, bucket.suppressed_count))
                bucket.last_emit = now
                bucket.suppressed_count = 0
                continue
            self._buckets.pop(key, None)
        return summaries
