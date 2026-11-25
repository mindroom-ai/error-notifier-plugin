# ruff: noqa: D103
"""Tests for error-notifier Matrix message formatting."""

from __future__ import annotations

import asyncio
import traceback

import pytest
from error_notifier_test_import.formatter import (
    _MAX_STACK_TRACE_CHARS,
    format_alert,
    format_suppression_summary,
    traceback_summary,
)
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


def test_stack_trace_included_inline_when_enabled() -> None:
    body = format_alert(_record(exception="Traceback\nRuntimeError: boom\n"), None, include_stack_traces=True)

    assert body.endswith(
        "\n\n<details><summary>Stack trace</summary>\n\n```\nTraceback\nRuntimeError: boom\n```\n\n</details>",
    )


def test_stack_trace_omitted_when_disabled_or_missing() -> None:
    assert "<details>" not in format_alert(_record(), None, include_stack_traces=False)
    assert "<details>" not in format_alert(_record(exception=None), None, include_stack_traces=True)
    assert "<details>" not in format_alert(_record(exception="  \n"), None, include_stack_traces=True)


def test_stack_trace_truncates_at_cap() -> None:
    exact = "x" * _MAX_STACK_TRACE_CHARS
    exact_body = format_alert(_record(exception=f"{exact}\n"), None, include_stack_traces=True)
    assert f"```\n{exact}\n```" in exact_body

    over = "y" * (_MAX_STACK_TRACE_CHARS + 50)
    over_body = format_alert(_record(exception=over), None, include_stack_traces=True)
    expected = f"{'y' * (_MAX_STACK_TRACE_CHARS - 1)}…"
    assert f"```\n{expected}\n```" in over_body


@pytest.mark.parametrize(
    ("exception_text", "expected_fence"),
    [
        ("ValueError: bad `arg` and ``pair``", "```"),
        ("RuntimeError: ```\n</details>\n```", "````"),
        ("RuntimeError: `````five`````", "``````"),
    ],
)
def test_stack_trace_picks_fence_longer_than_inner_backticks(exception_text: str, expected_fence: str) -> None:
    body = format_alert(_record(exception=exception_text), None, include_stack_traces=True)

    assert body.endswith(
        f"\n\n<details><summary>Stack trace</summary>\n\n"
        f"{expected_fence}\n{exception_text}\n{expected_fence}\n\n</details>",
    )


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


def test_exception_summary_used_when_error_missing() -> None:
    summary = traceback_summary("RuntimeError: *boom*\ntrace")
    body = format_alert(_record(event_dict={}, exception_summary=summary), None)

    assert "error:  RuntimeError: \\*boom\\*" in body


def test_error_line_omitted_without_error_or_exception() -> None:
    body = format_alert(_record(event_dict={}, exception=None), None)

    assert "\nerror:" not in body


def test_traceback_shows_final_exception_line_and_raising_frame() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/mindroom/ai.py", line 42, in stream_response\n    await send()\n'
        '  File "/app/nio/client.py", line 812, in room_send\n    raise RoomSendError("unknown error")\n'
        "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        "nio.exceptions.RoomSendError: unknown error\n"
    )
    body = format_alert(_record(event_dict={}, exception_summary=traceback_summary(tb)), None)

    assert (
        "error:  nio.exceptions.RoomSendError: unknown error\n"
        '        File "/app/nio/client.py", line 812, in room\\_send'
    ) in body
    assert "Traceback" not in body


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (
            "During handling of the above exception, another exception occurred:\n\n"
            "Traceback (most recent call last):\n"
            '  File "/app/b.py", line 20, in outer_fn\n    raise RateLimited()\n'
            "RateLimited: openai/gpt-4o is temporarily rate-limited\ndetail line\n",
            "error:  RateLimited: openai/gpt-4o is temporarily rate-limited\n"
            '        File "/app/b.py", line 20, in outer\\_fn',
        ),
        (
            "The above exception was the direct cause of the following exception:\n\nRuntimeError: outer failure\n",
            "error:  RuntimeError: outer failure",
        ),
    ],
)
def test_chained_traceback_uses_final_exception(tail: str, expected: str) -> None:
    head = 'Traceback (most recent call last):\n  File "/app/a.py", line 10, in inner\n    1 / 0\nZeroDivisionError\n\n'
    body = format_alert(_record(event_dict={}, exception_summary=traceback_summary(head + tail)), None)

    assert expected in body
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

    body = format_alert(_record(event_dict={}, exception_summary=traceback_summary(tb)), None)

    assert "error:  RuntimeError: task child boom\n" in body
    assert "in \\_failing\\_child" in body
    assert "Exception Group Traceback" not in body


def test_pathological_divider_line_does_not_hang() -> None:
    summary = traceback_summary(f"+{'-' * 8192}x\nRuntimeError: boom\n")
    assert summary is not None and summary.endswith("…")


def test_syntax_error_traceback_shows_exception_and_file_line() -> None:
    try:
        compile("def bad(:\n  pass", "/app/bad.py", "exec")
    except SyntaxError:
        tb = traceback.format_exc()
    body = format_alert(_record(event_dict={}, exception_summary=traceback_summary(tb)), None)

    assert "error:  SyntaxError: invalid syntax\n" in body
    assert 'File "/app/bad.py", line 1' in body
