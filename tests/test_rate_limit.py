# ruff: noqa: D103
"""Tests for error-notifier rate limiting."""

from __future__ import annotations

from error_notifier_test_import.rate_limit import DecisionKind, RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_first_occurrence_emits_fresh() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)

    assert limiter.gate(("logger", "event")).kind is DecisionKind.EMIT_FRESH


def test_within_window_suppresses_and_counts() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "event"))
    clock.now = 1

    decision = limiter.gate(("logger", "event"))

    assert decision.kind is DecisionKind.SUPPRESS
    assert decision.suppressed_count == 1


def test_window_expiry_with_prior_suppression_emits_resume() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "event"))
    limiter.gate(("logger", "event"))
    clock.now = 10

    decision = limiter.gate(("logger", "event"))

    assert decision.kind is DecisionKind.EMIT_RESUME
    assert decision.suppressed_count == 1


def test_distinct_keys_are_independent() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "one"))

    assert limiter.gate(("logger", "two")).kind is DecisionKind.EMIT_FRESH


def test_same_clock_value_does_not_crash() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)

    assert limiter.gate(("logger", "event")).kind is DecisionKind.EMIT_FRESH
    assert limiter.gate(("logger", "event")).kind is DecisionKind.SUPPRESS


def test_sweep_returns_expired_suppressed_entries_and_resets() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "event"))
    limiter.gate(("logger", "event"))
    clock.now = 10

    assert limiter.sweep() == [(("logger", "event"), 1)]
    assert limiter.sweep() == []


def test_sweep_skips_entries_within_window() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "event"))
    limiter.gate(("logger", "event"))
    clock.now = 9

    assert limiter.sweep() == []


def test_sweep_prunes_expired_unsuppressed_entries() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "event"))
    clock.now = 10

    assert limiter.sweep() == []
    assert limiter.gate(("logger", "event")).kind is DecisionKind.EMIT_FRESH


def test_configure_window_preserves_state() -> None:
    clock = Clock()
    limiter = RateLimiter(10, clock=clock)
    limiter.gate(("logger", "event"))
    limiter.gate(("logger", "event"))
    limiter.configure_window(5)
    clock.now = 5

    assert limiter.sweep() == [(("logger", "event"), 1)]
