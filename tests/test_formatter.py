# ruff: noqa: D103
"""Tests for error-notifier Matrix message formatting and structured fingerprinting."""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest
from error_notifier_test_import.formatter import (
    _MAX_STACK_TRACE_CHARS,
    _code_span,
    _is_mindroom_frame,
    format_alert,
    format_related_alert,
    format_thread_followup,
    traceback_summary,
)
from error_notifier_test_import.models import ErrorFingerprint, ObservedError


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
        "🚨 `mindroom.orchestrator`: Configuration update failed; will retry if a new change is queued\n"
        "time:    2026-05-20T21:29:40.123456+00:00\n"
        "logger:  mindroom.orchestrator\n"
        "event:   Configuration update failed; will retry if a new change is queued\n"
        "error:   Provisioning credentials are invalid or revoked.\n"
        "action:  Run `mindroom connect --pair-code <code>` from a paired session."
    )


def test_alert_body_with_subkey_context_error_type_and_percent_encoded_reoccurred_link() -> None:
    record = _record(
        logger="mindroom.hooks",
        event="hook_execution_failed",
        event_dict={
            "plugin_name": "thread-goal",
            "hook_name": "thread-goal-context",
            "agent": "assistant",
            "error_type": "TimeoutError",
            "error": "TimeoutError: timed out after 15s",
        },
    )
    body = format_alert(record, None, previous_thread=("!ops:localhost", "$prev1"), recent_occurrences=5)
    assert "\noccurrences: 5 in last window\n" in body
    assert "\noccurrences:" not in format_alert(record, None, recent_occurrences=1)
    body = format_alert(record, None, previous_thread=("!ops:localhost", "$prev1"))

    assert body == (
        "🚨 `mindroom.hooks (thread-goal/thread-goal-context)`: `TimeoutError`\n"
        "time:    2026-05-20T21:29:40.123456+00:00\n"
        "logger:  mindroom.hooks\n"
        "event:   hook_execution_failed\n"
        "context: `plugin=thread-goal` `hook=thread-goal-context` `agent=assistant`\n"
        "error:   TimeoutError: timed out after 15s\n"
        "reoccurred: [previous resolved thread](https://matrix.to/#/%21ops%3Alocalhost/%24prev1)"
    )


def test_alert_title_uses_code_spans_short_class_name_and_escaped_prose_event() -> None:
    record = _record(
        logger="mindroom.error_handling",
        event="provider_request_failed",
        event_dict={"provider": "open_router", "agent": "*bot*", "error": "ProviderUnavailable('Connection error.')"},
    )
    body = format_alert(record, "Run `mindroom reload`.")

    assert body.startswith("🚨 `mindroom.error_handling (open_router)`: `ProviderUnavailable`\n")
    assert "\ncontext: `provider=open_router` `agent=*bot*`\n" in body
    assert "\naction:  Run `mindroom reload`." in body

    dotted_tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/mindroom/memory/_file_backend.py", line 12, in embed\n    raise err\n'
        "mindroom.embeddings.errors.EmbedderRequestError: 502 Bad Gateway\n"
    )
    dotted_rec = _record(
        logger="mindroom.memory._file_backend",
        event="embed_failed",
        event_dict={},
        exception=dotted_tb,
        exception_summary=traceback_summary(dotted_tb),
    )
    dotted_body = format_alert(dotted_rec, None)
    assert dotted_rec.fingerprint.error_type == "mindroom.embeddings.errors.EmbedderRequestError"
    assert dotted_body.startswith("🚨 `mindroom.memory._file_backend`: `EmbedderRequestError`\n")
    assert "error:   mindroom.embeddings.errors.EmbedderRequestError: 502 Bad Gateway" in dotted_body

    prose_rec = _record(
        logger="mindroom.matrix.client_delivery",
        event="Failed Matrix media upload response",
        event_dict={"error": "502 Bad Gateway"},
    )
    assert format_alert(prose_rec, None).startswith(
        "🚨 `mindroom.matrix.client_delivery`: Failed Matrix media upload response\n"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mindroom.memory._file_backend", "`mindroom.memory._file_backend`"),
        ("a\nb\r\nc\rd", "`a b c d`"),
        ("foo`bar", "``foo`bar``"),
        ("foo``bar", "```foo``bar```"),
        ("`foo`", "`` `foo` ``"),
        ("`foo", "`` `foo ``"),
        ("foo`", "`` foo` ``"),
        (" padded ", "`  padded  `"),
        ("   ", "`   `"),
        (" leading", "` leading`"),
        ("trailing ", "`trailing `"),
    ],
)
def test_code_span_commonmark_escaping(raw: str, expected: str) -> None:
    assert _code_span(raw) == expected


def test_multiline_error_indentation() -> None:
    body = format_alert(_record(event_dict={"error": "first\nsecond"}), None)

    assert "error:   first\n         second" in body


def test_unknown_action_is_omitted() -> None:
    body = format_alert(_record(), None)

    assert "\naction:" not in body


def test_stack_trace_included_inline_when_enabled() -> None:
    body = format_alert(_record(exception="Traceback\nRuntimeError: boom\n"), None, include_stack_traces=True)

    assert body.endswith(
        "\n\n<details><summary>Stack trace</summary>\n\n```\nTraceback\nRuntimeError: boom\n```\n\n</details>",
    )


def test_stack_trace_omitted_when_disabled_or_missing() -> None:
    assert "<details>" not in format_alert(
        _record(exception="Traceback\nRuntimeError: boom"), None, include_stack_traces=False
    )
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
    assert format_thread_followup(rec, count=3, is_rollup=True, last_error="503 Service Unavailable") == (
        "🔁 +3 occurrences (latest: 2026-05-20T21:29:40.123456+00:00)\nerror:   502 Bad Gateway"
    )
    assert format_thread_followup(rec, count=1, is_rollup=False, last_error=None) == (
        "🔁 Recurred — 2026-05-20T21:29:40.123456+00:00\nerror:   502 Bad Gateway"
    )


def test_related_alert_shape() -> None:
    record = _record(
        logger="mindroom.error_handling",
        event="provider_request_failed",
        event_dict={
            "provider": "openrouter",
            "error_type": "ProviderUnavailable",
            "error": "ProviderUnavailable: Connection error.",
        },
    )
    body = format_related_alert(record, "Retry the request.")

    assert body == (
        "🔗 Related error (same turn) — `mindroom.error_handling (openrouter)`: `ProviderUnavailable`\n"
        "logger:  mindroom.error_handling\n"
        "event:   provider_request_failed\n"
        "context: `provider=openrouter`\n"
        "error:   ProviderUnavailable: Connection error.\n"
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
    ("path", "expected"),
    [
        ("/home/runner/work/mindroom/mindroom/plugin/tests/test_formatter.py", False),
        ("/home/mindroom/plugins/custom/tools.py", False),
        ("/srv/mindroom/tests/test_ai.py", False),
        ("/srv/mindroom/plugins/error-notifier/formatter.py", False),
        ("src/mindroom/ai.py", True),
        ("/srv/mindroom/src/mindroom/ai.py", True),
        ("/srv/mindroom/.venv/lib/python3.13/site-packages/mindroom/hooks.py", True),
        ("/usr/lib/python3/dist-packages/mindroom/hooks.py", True),
    ],
)
def test_is_mindroom_frame_matches_only_package_paths(path: str, expected: bool) -> None:
    assert _is_mindroom_frame(f'File "{path}", line 12, in run') is expected


def test_is_mindroom_frame_matches_loaded_package_root_and_rejects_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkg_dir = tmp_path / "custom_pkg"
    pkg_dir.mkdir()
    link_dir = tmp_path / "pkg_link"
    link_dir.symlink_to(pkg_dir)
    monkeypatch.setitem(sys.modules, "mindroom", SimpleNamespace(__file__=str(link_dir / "__init__.py")))

    assert _is_mindroom_frame(f'File "{link_dir / "ai.py"}", line 7, in run') is True
    assert _is_mindroom_frame(f'File "{pkg_dir.resolve() / "ai.py"}", line 7, in run') is True
    assert _is_mindroom_frame(f'File "{tmp_path / "pkg_link_data" / "ai.py"}", line 7, in run') is False
    assert _is_mindroom_frame(f'File "{tmp_path / "custom_pkg_data" / "ai.py"}", line 7, in run') is False


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


def test_syntax_error_from_mindroom_caller_keeps_syntax_error_frame() -> None:
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/srv/mindroom/src/mindroom/tool_system/plugins.py", line 123, in load_plugin\n'
        '    compile(source, "/app/bad.py", "exec")\n'
        '  File "/app/bad.py", line 1\n'
        "    def bad(:\n"
        "            ^\n"
        "SyntaxError: invalid syntax\n"
    )
    assert traceback_summary(tb) == 'SyntaxError: invalid syntax\nFile "/app/bad.py", line 1'


def test_structured_fingerprint_subkey_error_type_and_canonical() -> None:
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
        '["mindroom.hooks","hook_failed",'
        '"hook=voice-enrich/voice-enrich-message,tool=transcribe,provider=openai","TimeoutError"]'
    )

    # Single subkey variants + fallback tool key.
    assert _record(event_dict={"plugin_name": "p1", "tool": "search"}).fingerprint.subkey == "plugin=p1,tool=search"
    assert _record(event_dict={"hook_name": "h1", "provider": ""}).fingerprint.subkey == "hook=h1"

    # Error type priority: explicit > exception traceback > ClassName(...) or pkg.ClassName: > "".
    tb_dotted = (
        "Traceback (most recent call last):\n"
        '  File "/app/mindroom/emb.py", line 5, in run\n    raise err\n'
        "mindroom.embeddings.errors.EmbedderRequestError: 502 from https://example.com\n"
    )
    assert (
        _record(event_dict={"exception_type": "CustomErr"}, exception=tb_dotted).fingerprint.error_type == "CustomErr"
    )
    assert _record(event_dict={"error": "TimeoutError()"}, exception=tb_dotted).fingerprint.error_type == (
        "mindroom.embeddings.errors.EmbedderRequestError"
    )
    assert _record(event_dict={"error": "ProviderUnavailable('Connection error.')"}).fingerprint.error_type == (
        "ProviderUnavailable"
    )
    assert _record(event_dict={"error": "httpx.ReadTimeout: slow"}).fingerprint.error_type == "httpx.ReadTimeout"
    timeout_fp = _record(event_dict={"error": "TimeoutError: slow"}).fingerprint
    value_fp = _record(event_dict={"error": "ValueError: bad"}).fingerprint
    assert timeout_fp.error_type == "TimeoutError"
    assert value_fp.error_type == "ValueError"
    assert timeout_fp != value_fp
    assert _record(event_dict={"error": "ProviderUnavailable('x')"}, error_from_args=True).fingerprint.error_type == ""

    # Leading-underscore private exception classes extract the full dotted class name and display short name.
    private_exc_line = (
        "mindroom.history.summary_call._CompactionSummaryUsageError: Could not persist compaction summary usage"
    )
    private_tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/mindroom/history/summary_call.py", line 88, in persist\n    raise err\n'
        f"{private_exc_line}\n"
    )
    from_tb = _record(
        logger="mindroom.history.summary_call",
        event="compaction_summary_usage_failed",
        event_dict={},
        exception=private_tb,
        exception_summary=traceback_summary(private_tb),
    )
    from_error = _record(event_dict={"error": private_exc_line})
    assert from_tb.fingerprint.error_type == "mindroom.history.summary_call._CompactionSummaryUsageError"
    assert from_error.fingerprint.error_type == "mindroom.history.summary_call._CompactionSummaryUsageError"
    assert (
        _record(event_dict={"error": "_CompactionSummaryUsageError: failed"}).fingerprint.error_type
        == "_CompactionSummaryUsageError"
    )
    private_body = format_alert(from_tb, None)
    assert private_body.startswith("🚨 `mindroom.history.summary_call`: `_CompactionSummaryUsageError`\n")
    assert "mindroom.history.summary\\_call.\\_CompactionSummaryUsageError:" in private_body

    # Never leaks bare words + colon, private vars, raw messages, URLs, timestamps, or all-caps prefixes.
    for raw_error in (
        "WorkerA: sync failed",
        "WorkerB: sync failed",
        "_WorkerA: failed",
        "_private_var: foo",
        "mindroom.history._private_var: foo",
        "boom",
        "502 Bad Gateway",
        "https://api.example.com/v1/chat",
        "2026-05-20T21:29:40Z timeout",
        "ERROR: transient failure",
        "Provisioning credentials are invalid or revoked.",
    ):
        assert _record(event_dict={"error": raw_error}).fingerprint.error_type == ""
