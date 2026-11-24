# ruff: noqa: D103
"""Tests for error-notifier Matrix message formatting and structured fingerprinting."""

from __future__ import annotations

import asyncio
import traceback

import pytest
from error_notifier_test_import.formatter import (
    extra_content_for,
    format_alert,
    format_related_alert,
    format_thread_followup,
    traceback_summary,
)
from error_notifier_test_import.models import ErrorFingerprint, ObservedError
from mindroom.matrix.message_extras import MINDROOM_MESSAGE_EXTRAS_KEY


def _record(**overrides: object) -> ObservedError:
    values: dict[str, object] = {
        "logger": "mindroom.orchestrator",
        "level": "error",
        "event": "Configuration update failed; will retry if a new change is queued",
        "timestamp": "2026-05-20T21:29:40.123456+00:00",
        "event_dict": {"error": "Provisioning credentials are invalid or revoked."},
        "exception": None,
    }
    values.update(overrides)
    return ObservedError(**values)  # type: ignore[arg-type]


def test_alert_body_shape_without_error_type() -> None:
    body = format_alert(_record(), "Run `mindroom connect --pair-code <code>` from a paired session.")

    assert body == (
        "🚨 mindroom.orchestrator: Configuration update failed; will retry if a new change is queued\n"
        "time:    2026-05-20T21:29:40.123456+00:00\n"
        "logger:  mindroom.orchestrator\n"
        "event:   Configuration update failed; will retry if a new change is queued\n"
        "error:   Provisioning credentials are invalid or revoked.\n"
        "action:  Run `mindroom connect --pair-code <code>` from a paired session."
    )


def test_alert_body_with_subkey_context_error_type_and_reoccurred_link() -> None:
    record = _record(
        logger="mindroom.hooks",
        event="hook_execution_failed",
        event_dict={
            "plugin_name": "voice-enrich",
            "hook_name": "enrich",
            "agent": "assistant",
            "error": "TimeoutError: timed out after 15s",
        },
    )
    body = format_alert(record, None, previous_thread=("!ops:localhost", "$prev1"))

    assert body == (
        "🚨 mindroom.hooks (voice-enrich/enrich): TimeoutError\n"
        "time:    2026-05-20T21:29:40.123456+00:00\n"
        "logger:  mindroom.hooks\n"
        "event:   hook_execution_failed\n"
        "context: plugin=voice-enrich hook=enrich agent=assistant\n"
        "error:   TimeoutError: timed out after 15s\n"
        "reoccurred: [previous resolved thread](https://matrix.to/#/!ops:localhost/$prev1)"
    )


def test_alert_title_uses_provider_subkey() -> None:
    record = _record(
        logger="mindroom.error_handling",
        event="provider_request_failed",
        event_dict={"provider": "openrouter", "error": "ProviderUnavailable('Connection error.')"},
    )
    body = format_alert(record, None)

    assert body.startswith("🚨 mindroom.error_handling (openrouter): ProviderUnavailable\n")
    assert "\ncontext: provider=openrouter\n" in body


def test_multiline_error_indentation() -> None:
    body = format_alert(_record(event_dict={"error": "first\nsecond"}), None)

    assert "error:   first\n         second" in body


def test_unknown_action_is_omitted() -> None:
    body = format_alert(_record(), None)

    assert "\naction:" not in body


def test_stack_trace_extras_populated_when_enabled() -> None:
    extras = extra_content_for(_record(exception="Traceback\nRuntimeError: boom"), True)

    assert extras is not None
    assert extras[MINDROOM_MESSAGE_EXTRAS_KEY]["sections"][0]["title"] == "Stack trace"


def test_stack_trace_extras_absent_when_disabled() -> None:
    assert extra_content_for(_record(exception="Traceback\nRuntimeError: boom"), False) is None


def test_stack_trace_extras_absent_without_exception() -> None:
    assert extra_content_for(_record(exception=None), True) is None


def test_thread_followup_formats_rollup_and_recurrence_and_deduplicates_error() -> None:
    rec = _record(event_dict={"error": "502 Bad Gateway"})

    assert format_thread_followup(rec, count=1, is_rollup=True, last_error="502 Bad Gateway") == (
        "🔁 +1 occurrence (latest: 2026-05-20T21:29:40.123456+00:00)"
    )
    assert format_thread_followup(rec, count=9, is_rollup=True, last_error="502 Bad Gateway") == (
        "🔁 +9 occurrences (latest: 2026-05-20T21:29:40.123456+00:00)"
    )
    assert format_thread_followup(rec, count=1, is_rollup=False, last_error="502 Bad Gateway") == (
        "🔁 Recurred — 2026-05-20T21:29:40.123456+00:00"
    )
    # Changed error or no prior error includes error: line.
    assert format_thread_followup(rec, count=3, is_rollup=True, last_error="503 Service Unavailable") == (
        "🔁 +3 occurrences (latest: 2026-05-20T21:29:40.123456+00:00)\nerror:   502 Bad Gateway"
    )
    assert format_thread_followup(rec, count=1, is_rollup=False, last_error=None) == (
        "🔁 Recurred — 2026-05-20T21:29:40.123456+00:00\nerror:   502 Bad Gateway"
    )


def test_related_alert_shape() -> None:
    record = _record(
        logger="mindroom.tools",
        event="tool_call_failed",
        event_dict={"tool_name": "web_search", "error": "TimeoutError: read timed out"},
    )
    body = format_related_alert(record, "Retry the request.")

    assert body == (
        "🔗 Related error (same turn) — mindroom.tools (web_search): TimeoutError\n"
        "logger:  mindroom.tools\n"
        "event:   tool_call_failed\n"
        "context: tool=web_search\n"
        "error:   TimeoutError: read timed out\n"
        "action:  Retry the request."
    )


def test_markdown_special_characters_are_escaped_in_error() -> None:
    body = format_alert(_record(event_dict={"error": "*boom* [x](y) `z`"}), None)

    assert "\\*boom\\* \\[x\\]\\(y\\) \\`z\\`" in body


def test_timestamp_falls_back_when_missing() -> None:
    body = format_alert(_record(timestamp=None), None)

    assert "\ntime:    20" in body


def test_exception_summary_used_when_error_missing() -> None:
    summary = traceback_summary("RuntimeError: *boom*\ntrace")
    body = format_alert(_record(event_dict={}, exception_summary=summary), None)

    assert "error:   RuntimeError: \\*boom\\*" in body


def test_error_line_omitted_without_error_or_exception() -> None:
    body = format_alert(_record(event_dict={}, exception=None), None)

    assert "\nerror:" not in body


def test_traceback_prefers_last_mindroom_frame_over_stdlib_and_third_party() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/srv/mindroom/src/mindroom/ai.py", line 42, in stream_response\n    await send()\n'
        '  File "/srv/mindroom/.venv/lib/python3.13/site-packages/nio/client.py", line 812, in room_send\n'
        '    raise RoomSendError("unknown error")\n'
        '  File "/usr/lib/python3.13/asyncio/timeouts.py", line 115, in __aexit__\n    raise TimeoutError\n'
        "TimeoutError: timed out\n"
    )
    body = format_alert(_record(event_dict={}, exception=tb, exception_summary=traceback_summary(tb)), None)

    assert (
        "error:   TimeoutError: timed out\n"
        '         File "/srv/mindroom/src/mindroom/ai.py", line 42, in stream\\_response'
    ) in body
    assert "timeouts.py" not in body
    assert "nio/client.py" not in body
    assert "Traceback" not in body


def test_traceback_matches_mindroom_in_site_packages_and_falls_back_when_absent() -> None:
    wheel_tb = (
        "Traceback (most recent call last):\n"
        '  File "/srv/mindroom/.venv/lib/python3.13/site-packages/mindroom/hooks.py", line 88, in invoke\n'
        "    await cb()\n"
        '  File "/srv/mindroom/.venv/lib/python3.13/site-packages/httpx/_client.py", line 99, in send\n'
        "    raise ReadTimeout()\n"
        "httpx.ReadTimeout: slow\n"
    )
    assert 'site-packages/mindroom/hooks.py", line 88, in invoke' in (traceback_summary(wheel_tb) or "")

    external_tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/caller.py", line 10, in outer\n    inner()\n'
        '  File "/app/nio/client.py", line 812, in room_send\n    raise RoomSendError("unknown error")\n'
        "nio.exceptions.RoomSendError: unknown error\n"
    )
    summary = traceback_summary(external_tb)
    assert summary == 'nio.exceptions.RoomSendError: unknown error\nFile "/app/nio/client.py", line 812, in room_send'


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (
            "During handling of the above exception, another exception occurred:\n\n"
            "Traceback (most recent call last):\n"
            '  File "/app/b.py", line 20, in outer_fn\n    raise RateLimited()\n'
            "RateLimited: openai/gpt-4o is temporarily rate-limited\ndetail line\n",
            "error:   RateLimited: openai/gpt-4o is temporarily rate-limited\n"
            '         File "/app/b.py", line 20, in outer\\_fn',
        ),
        (
            "The above exception was the direct cause of the following exception:\n\nRuntimeError: outer failure\n",
            "error:   RuntimeError: outer failure",
        ),
    ],
)
def test_chained_traceback_uses_final_exception(tail: str, expected: str) -> None:
    head = 'Traceback (most recent call last):\n  File "/app/a.py", line 10, in inner\n    1 / 0\nZeroDivisionError\n\n'
    body = format_alert(
        _record(event_dict={}, exception=head + tail, exception_summary=traceback_summary(head + tail)), None
    )

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

    body = format_alert(_record(event_dict={}, exception=tb, exception_summary=traceback_summary(tb)), None)

    assert "error:   RuntimeError: task child boom\n" in body
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
    body = format_alert(_record(event_dict={}, exception=tb, exception_summary=traceback_summary(tb)), None)

    assert "error:   SyntaxError: invalid syntax\n" in body
    assert 'File "/app/bad.py", line 1' in body


def test_structured_fingerprint_subkey_error_type_and_canonical() -> None:
    # Subkey combines plugin/hook, tool, provider in deterministic order and excludes agent / non-strings.
    rec = _record(
        logger="mindroom.hooks",
        event="hook_failed",
        event_dict={
            "plugin_name": "voice-enrich",
            "hook_name": "voice-enrich-message",
            "tool_name": "transcribe",
            "provider": "openai",
            "agent": "helper",
            "error_type": "TimeoutError",
        },
    )
    assert rec.fingerprint == ErrorFingerprint(
        "mindroom.hooks",
        "hook_failed",
        "hook=voice-enrich/voice-enrich-message,tool=transcribe,provider=openai",
        "TimeoutError",
    )
    assert rec.fingerprint.canonical() == (
        '["mindroom.hooks","hook_failed","hook=voice-enrich/voice-enrich-message,tool=transcribe,provider=openai","TimeoutError"]'
    )

    # Single subkey variants + fallback tool key.
    assert _record(event_dict={"plugin_name": "p1", "tool": "search"}).fingerprint.subkey == "plugin=p1,tool=search"
    assert _record(event_dict={"hook_name": "h1", "provider": ""}).fingerprint.subkey == "hook=h1"

    # Error type priority: explicit > exception traceback > leading class in error > "".
    tb_dotted = (
        "Traceback (most recent call last):\n"
        '  File "/app/mindroom/emb.py", line 5, in run\n    raise err\n'
        "mindroom.embeddings.errors.EmbedderRequestError: 502 from https://example.com\n"
    )
    assert (
        _record(event_dict={"exception_type": "CustomErr"}, exception=tb_dotted).fingerprint.error_type == "CustomErr"
    )
    assert _record(event_dict={"error": "TimeoutError: x"}, exception=tb_dotted).fingerprint.error_type == (
        "mindroom.embeddings.errors.EmbedderRequestError"
    )
    assert _record(event_dict={"error": "ProviderUnavailable('Connection error.')"}).fingerprint.error_type == (
        "ProviderUnavailable"
    )
    assert _record(event_dict={"error": "TimeoutError"}).fingerprint.error_type == "TimeoutError"

    # Never leaks raw messages, URLs, timestamps, or all-caps prefixes into error_type.
    for raw_error in (
        "boom",
        "502 Bad Gateway",
        "https://api.example.com/v1/chat",
        "2026-05-20T21:29:40Z timeout",
        "ERROR: transient failure",
        "Provisioning credentials are invalid or revoked.",
    ):
        assert _record(event_dict={"error": raw_error}).fingerprint.error_type == ""
