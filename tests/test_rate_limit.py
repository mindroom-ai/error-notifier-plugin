# ruff: noqa: D103
"""Tests for error-notifier rate limiting and exponential backoff."""

from __future__ import annotations

from error_notifier_test_import.models import ErrorFingerprint, ObservedError
from error_notifier_test_import.rate_limit import DecisionKind, RateLimiter, SweepEntry


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _fp(event: str = "event") -> ErrorFingerprint:
    return ErrorFingerprint("logger", event)


def _record(event: str = "event", error: str = "boom") -> ObservedError:
    return ObservedError(
        logger="logger",
        level="error",
        event=event,
        timestamp="2026-05-20T21:29:40+00:00",
        event_dict={"error": error},
    )


def test_first_occurrence_emits_fresh() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)

    assert limiter.gate(_fp()).kind is DecisionKind.EMIT_FRESH


def test_within_window_suppresses_and_counts() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    limiter.gate(_fp(), _record(error="first"))
    clock.now = 2
    decision1 = limiter.gate(_fp(), _record(error="second"))
    clock.now = 5
    decision2 = limiter.gate(_fp(), _record(error="third"))

    assert decision1.kind is DecisionKind.SUPPRESS
    assert decision1.suppressed_count == 1
    assert decision2.suppressed_count == 2


def test_window_expiry_with_prior_suppression_emits_resume() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    limiter.gate(_fp())
    clock.now = 3
    limiter.gate(_fp(), _record(error="suppressed"))
    clock.now = 10

    decision = limiter.gate(_fp(), _record(error="resuming"))

    assert decision.kind is DecisionKind.EMIT_RESUME
    assert decision.suppressed_count == 1


def test_distinct_keys_are_independent() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    limiter.gate(_fp("one"))

    assert limiter.gate(_fp("two")).kind is DecisionKind.EMIT_FRESH


def test_same_clock_value_does_not_crash() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)

    assert limiter.gate(_fp()).kind is DecisionKind.EMIT_FRESH
    assert limiter.gate(_fp()).kind is DecisionKind.SUPPRESS


def test_sweep_returns_expired_suppressed_entries_with_latest_record() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    latest = _record(error="latest")
    limiter.gate(_fp())
    clock.now = 2
    limiter.gate(_fp(), latest)
    clock.now = 10

    assert limiter.sweep() == [SweepEntry(_fp(), 1, last_record=latest)]
    assert limiter.sweep() == []


def test_sweep_skips_entries_within_window() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    limiter.gate(_fp())
    limiter.gate(_fp())
    clock.now = 9

    assert limiter.sweep() == []


def test_quiet_window_resets_backoff_with_or_without_intervening_sweep() -> None:
    # With intervening sweep:
    clock1 = Clock()
    limiter1: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock1)
    limiter1.gate(_fp())
    limiter1.gate(_fp())
    clock1.now = 10
    assert len(limiter1.sweep()) == 1  # step 1 (30s)
    clock1.now = 40
    assert limiter1.sweep() == []  # quiet 30s window -> pruned
    assert limiter1.gate(_fp()).kind is DecisionKind.EMIT_FRESH
    limiter1.gate(_fp())
    clock1.now = 50  # back at 10s base window
    assert len(limiter1.sweep()) == 1

    # Without any intervening sweep:
    clock2 = Clock()
    limiter2: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock2)
    limiter2.gate(_fp())
    limiter2.gate(_fp())
    clock2.now = 10
    assert limiter2.gate(_fp()).kind is DecisionKind.EMIT_RESUME  # step 1 (30s)
    clock2.now = 40  # quiet 30s window, no sweep called!
    assert limiter2.gate(_fp()).kind is DecisionKind.EMIT_FRESH  # resets to step 0 (10s)
    limiter2.gate(_fp())
    clock2.now = 49
    assert limiter2.gate(_fp()).kind is DecisionKind.SUPPRESS
    clock2.now = 50  # expires at 10s base window, proving step reset to 0!
    assert limiter2.gate(_fp()).kind is DecisionKind.EMIT_RESUME


def test_configure_window_preserves_state() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    limiter.gate(_fp())
    limiter.gate(_fp())
    limiter.configure_window(5)
    clock.now = 5

    assert limiter.sweep() == [SweepEntry(_fp(), 1, last_record=None)]


def test_exponential_backoff_schedule_and_cap() -> None:
    # Default 300s -> 300s (5m) -> 900s (15m) -> 3600s (1h) -> 21600s (6h cap) -> stays 21600s.
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(300, clock=clock)
    key = _fp()
    assert limiter.gate(key).kind is DecisionKind.EMIT_FRESH

    for expected_window in (300, 900, 3600, 21600, 21600):
        limiter.gate(key)
        clock.now += expected_window - 1
        assert limiter.sweep() == []
        clock.now += 1
        assert len(limiter.sweep()) == 1


def test_backoff_scales_for_small_and_large_base_windows() -> None:
    clock = Clock()
    small: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    key = _fp()
    small.gate(key)
    for expected_window in (10, 30, 120, 720, 720):
        small.gate(key)
        clock.now += expected_window - 1
        assert small.gate(key).kind is DecisionKind.SUPPRESS
        clock.now += 1
        assert small.gate(key).kind is DecisionKind.EMIT_RESUME

    clock_large = Clock()
    large: RateLimiter[ErrorFingerprint] = RateLimiter(30000, clock=clock_large)
    large.gate(key)
    large.gate(key)
    clock_large.now = 29999
    assert large.sweep() == []
    clock_large.now = 30000
    assert len(large.sweep()) == 1


def test_reset_returns_bucket_to_base_window() -> None:
    clock = Clock()
    limiter: RateLimiter[ErrorFingerprint] = RateLimiter(10, clock=clock)
    key = _fp()
    limiter.gate(key)
    limiter.gate(key)
    clock.now = 10
    assert len(limiter.sweep()) == 1  # step 1 (30s)

    limiter.reset(key)
    assert limiter.gate(key).kind is DecisionKind.EMIT_FRESH
    limiter.gate(key)
    clock.now = 20  # 10s base window expires
    assert len(limiter.sweep()) == 1
