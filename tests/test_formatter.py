# ruff: noqa: D103
"""Tests for error-notifier Matrix message formatting."""

from __future__ import annotations

import asyncio
import time
import traceback

import pytest
from error_notifier_test_import.formatter import (
    _MAX_SUMMARY_CHARS,
    extra_content_for,
    format_alert,
    format_suppression_summary,
)
from error_notifier_test_import.models import ObservedError
from mindroom.matrix.message_extras import MINDROOM_MESSAGE_EXTRAS_KEY


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


def test_traceback_shows_final_exception_line_and_raising_frame() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/mindroom/ai.py", line 42, in stream_response\n'
        "    await send()\n"
        '  File "/app/nio/client.py", line 812, in room_send\n'
        '    raise RoomSendError("unknown error")\n'
        "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        "nio.exceptions.RoomSendError: unknown error\n"
    )
    body = format_alert(_record(event_dict={}, exception=tb), None)

    assert (
        "error:  nio.exceptions.RoomSendError: unknown error\n"
        '        File "/app/nio/client.py", line 812, in room\\_send'
    ) in body
    assert "Traceback" not in body


def test_chained_traceback_uses_final_exception_and_frame() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 10, in inner\n'
        "    1 / 0\n"
        "ZeroDivisionError: division by zero\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\n"
        '  File "/app/b.py", line 20, in outer_fn\n'
        '    raise RateLimited("openai/gpt-4o is temporarily rate-limited")\n'
        "RateLimited: openai/gpt-4o is temporarily rate-limited\n"
        "detail line\n"
    )
    body = format_alert(_record(event_dict={}, exception=tb), None)

    assert (
        "error:  RateLimited: openai/gpt-4o is temporarily rate-limited\n"
        '        File "/app/b.py", line 20, in outer\\_fn'
    ) in body
    assert "ZeroDivisionError" not in body


def test_direct_cause_chained_traceback_without_outer_frame() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/a.py", line 10, in inner\n'
        "    1 / 0\n"
        "ZeroDivisionError: division by zero\n\n"
        "The above exception was the direct cause of the following exception:\n\n"
        "RuntimeError: outer failure\n"
    )
    body = format_alert(_record(event_dict={}, exception=tb), None)

    assert "error:  RuntimeError: outer failure" in body
    assert "ZeroDivisionError" not in body


@pytest.mark.asyncio
async def test_real_taskgroup_traceback_shows_child_exception_and_frame() -> None:
    async def _failing_child() -> None:
        raise RuntimeError("task child boom")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_failing_child())
    except BaseException:
        tb = traceback.format_exc()

    body = format_alert(_record(event_dict={}, exception=tb), None)

    assert "error:  RuntimeError: task child boom\n" in body
    assert "in \\_failing\\_child" in body
    assert "Exception Group Traceback" not in body


def test_long_traceback_summary_is_capped() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/client.py", line 12, in send\n'
        "    raise RuntimeError()\n"
        f"RuntimeError: {'x' * 500}\n"
    )
    body = format_alert(_record(event_dict={}, exception=tb), None)
    error_lines = [line for line in body.splitlines() if line.startswith(("error:", "        "))]
    summary_len = sum(len(line) - 8 for line in error_lines) + len(error_lines) - 1

    assert summary_len <= _MAX_SUMMARY_CHARS
    assert "…\n" in body
    assert 'File "/app/client.py", line 12, in send' in body


def test_pathological_divider_line_formats_quickly() -> None:
    tb = f"+{'-' * 8192}x\nRuntimeError: boom\n"
    start = time.perf_counter()
    body = format_alert(_record(event_dict={}, exception=tb), None)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05
    assert "error:" in body


def test_truncated_exception_without_tail_frame_never_uses_head_frame() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/head_only.py", line 1, in outer_entry\n'
        "    run()\n"
        "\n…\n"
        "mid-word fragment of huge payload\n"
        "final tail detail\n"
    )
    body = format_alert(_record(event_dict={}, exception=tb), None)

    assert "error:  final tail detail" in body
    assert "head\\_only.py" not in body
    assert "mid-word fragment" not in body
