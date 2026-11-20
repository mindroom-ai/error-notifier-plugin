# ruff: noqa: D103
"""Tests for error-notifier Matrix message formatting."""

from __future__ import annotations

from mindroom.matrix.message_extras import MINDROOM_MESSAGE_EXTRAS_KEY

from error_notifier_test_import.formatter import extra_content_for, format_alert, format_suppression_summary
from error_notifier_test_import.models import ObservedError


def _record(**overrides: object) -> ObservedError:
    values = {
        "logger": "mindroom.orchestrator",
        "level": "error",
        "event": "Configuration update failed; will retry if a new change is queued",
        "timestamp": "2026-05-20T21:29:40.123456+00:00",
        "event_dict": {"error": "Provisioning credentials are invalid or revoked."},
        "exception": "Traceback\nRuntimeError: boom",
    }
    values.update(overrides)
    return ObservedError(**values)


def test_alert_body_shape() -> None:
    body = format_alert(_record(), "Run `mindroom connect --pair-code <code>` from a paired session.")

    assert body == (
        "🚨 MindRoom error — 2026-05-20T21:29:40.123456+00:00\n"
        "logger: mindroom.orchestrator\n"
        "event:  Configuration update failed; will retry if a new change is queued\n"
        "error:  Provisioning credentials are invalid or revoked.\n"
        "action: Run `mindroom connect --pair-code <code>` from a paired session."
    )


def test_multiline_error_indentation() -> None:
    body = format_alert(_record(event_dict={"error": "first\nsecond"}), None)

    assert "error:  first\n        second" in body


def test_unknown_action_is_omitted() -> None:
    body = format_alert(_record(), None)

    assert "\naction:" not in body


def test_stack_trace_extras_populated_when_enabled() -> None:
    extras = extra_content_for(_record(), True)

    assert extras is not None
    assert extras[MINDROOM_MESSAGE_EXTRAS_KEY]["sections"][0]["title"] == "Stack trace"


def test_stack_trace_extras_absent_when_disabled() -> None:
    assert extra_content_for(_record(), False) is None


def test_stack_trace_extras_absent_without_exception() -> None:
    assert extra_content_for(_record(exception=None), True) is None


def test_suppression_summary_shape() -> None:
    body = format_suppression_summary(("mindroom.orchestrator", "test_burst_error"), 9)

    assert body == (
        "🔕 MindRoom error — suppressed 9 further occurrences of:\n"
        "logger: mindroom.orchestrator\n"
        "event:  test_burst_error"
    )


def test_markdown_special_characters_are_escaped_in_error() -> None:
    body = format_alert(_record(event_dict={"error": "*boom* [x](y) `z`"}), None)

    assert "\\*boom\\* \\[x\\]\\(y\\) \\`z\\`" in body


def test_timestamp_falls_back_when_missing() -> None:
    body = format_alert(_record(timestamp=None), None)

    assert body.startswith("🚨 MindRoom error — 20")


def test_exception_first_line_used_when_error_missing() -> None:
    body = format_alert(_record(event_dict={}, exception="RuntimeError: *boom*\ntrace"), None)

    assert "error:  RuntimeError: \\*boom\\*" in body


def test_error_line_omitted_without_error_or_exception() -> None:
    body = format_alert(_record(event_dict={}, exception=None), None)

    assert "\nerror:" not in body
