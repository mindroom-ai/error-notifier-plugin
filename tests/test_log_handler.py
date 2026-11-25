# ruff: noqa: D103
"""Tests for the error-notifier root logging handler."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from conftest import remove_installed_handlers
from error_notifier_test_import.formatter import _MAX_SUMMARY_CHARS, format_alert
from error_notifier_test_import.log_handler import (
    _MAX_EXCEPTION_CHARS,
    HANDLER_NAME,
    _truncate,
    install_error_log_handler,
    observed_error_from_record,
)
from error_notifier_test_import.models import ObservedError
from mindroom.logging_config import setup_logging
from mindroom.plugin_api import RuntimePaths, get_logger


@pytest.fixture
def received(tmp_path: Path) -> Iterator[list[ObservedError]]:
    setup_logging(
        level="INFO",
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path / "mindroom_data",
        ),
    )
    records: list[ObservedError] = []
    install_error_log_handler(records.append)
    yield records
    remove_installed_handlers()


def test_structlog_error_arrives_redacted(received: list[ObservedError]) -> None:
    get_logger("mindroom.orchestrator").error("config_update_failed", api_key="sk-live-0123456789abcdefghij")

    [record] = received
    assert (record.logger, record.level, record.event) == ("mindroom.orchestrator", "error", "config_update_failed")
    assert record.timestamp
    assert "sk-live-0123456789abcdefghij" not in str(record.event_dict)


def test_exception_is_rendered_and_redacted(received: list[ObservedError]) -> None:
    try:
        raise RuntimeError("login failed with api_key=sk-live-0123456789abcdefghij")
    except RuntimeError:
        get_logger("mindroom.orchestrator").exception("agent_start_failed")

    [record] = received
    assert record.exception is not None
    assert "RuntimeError" in record.exception
    assert "sk-live-0123456789abcdefghij" not in record.exception


def test_stdlib_errors_arrive_and_lower_levels_do_not(received: list[ObservedError]) -> None:
    log = logging.getLogger("mindroom.matrix.users")
    log.warning("not an error")
    log.error("sync failed: %s", "timeout")

    [record] = received
    assert (record.logger, record.level, record.event) == ("mindroom.matrix.users", "error", "sync failed: %s")
    assert record.event_dict["error"] == "sync failed: timeout"


def test_own_loggers_are_skipped(received: list[ObservedError]) -> None:
    logging.getLogger("mindroom.plugins.error_notifier.worker").error("send failed")

    assert received == []


def test_reinstalling_replaces_the_previous_handler(received: list[ObservedError]) -> None:
    later: list[ObservedError] = []
    install_error_log_handler(later.append)
    get_logger("mindroom.orchestrator").error("config_update_failed")

    assert sum(handler.get_name() == HANDLER_NAME for handler in logging.getLogger().handlers) == 1
    assert received == []
    assert len(later) == 1


def test_truncate_keeps_head_and_tail_within_cap() -> None:
    exact = "x" * _MAX_EXCEPTION_CHARS
    assert _truncate(exact) == exact

    head = 'Traceback (most recent call last):\n  File "/app/top.py", line 1, in entry\n    run()\n'
    middle = '  File "/app/deep.py", line 10, in step\n    await next_step()\n' * 200
    tail = (
        '  File "/app/nio/client.py", line 812, in room_send\n'
        "    raise RoomSendError()\n"
        "nio.exceptions.RoomSendError: unknown error\n"
    )
    truncated = _truncate(head + middle + tail)

    assert len(truncated) == _MAX_EXCEPTION_CHARS
    assert truncated.startswith(head)
    assert "\n…\n" in truncated
    assert truncated.endswith(tail)


def test_long_exception_summarized_before_truncation(received: list[ObservedError]) -> None:
    def _raise_bad_gateway() -> None:
        raise RuntimeError(f"Bad gateway (502): {'upstream body; ' * 650}")

    try:
        _raise_bad_gateway()
    except RuntimeError:
        get_logger("mindroom.matrix.client_delivery").exception("matrix_message_send_failed")

    [exc_record] = received
    assert exc_record.exception is not None and len(exc_record.exception) == _MAX_EXCEPTION_CHARS
    assert exc_record.exception_summary is not None and len(exc_record.exception_summary) <= _MAX_SUMMARY_CHARS
    body = format_alert(exc_record, None)
    assert "RuntimeError: Bad gateway \\(502\\):" in body
    assert "…\n" in body
    assert "in \\_raise\\_bad\\_gateway" in body

    preformatted = (
        'Traceback (most recent call last):\n  File "/app/client.py", line 9, in send\n'
        f"    raise ValueError()\nValueError: {'Y' * 12000}\n"
    )
    raw = logging.LogRecord(
        "mindroom.ai", logging.ERROR, __file__, 1, {"event": "x", "exception": preformatted}, (), None
    )
    observed = observed_error_from_record(raw)
    assert observed.exception_summary is not None and len(observed.exception_summary) <= _MAX_SUMMARY_CHARS
    assert "error:   ValueError: YYY" in format_alert(observed, None)
    assert 'File "/app/client.py", line 9, in send' in format_alert(observed, None)


def test_stdlib_percent_args_do_not_leak_into_error_type(received: list[ObservedError]) -> None:
    log = logging.getLogger("mindroom.matrix.users")
    log.error("%s: sync failed", "WorkerA")
    log.error("%s: sync failed", "WorkerB")
    get_logger("mindroom.ai").error("provider_failed", error="ProviderUnavailable('Connection error.')")

    first, second, third = received
    assert first.error_from_args and second.error_from_args
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.error_type == ""
    assert not third.error_from_args
    assert third.fingerprint.error_type == "ProviderUnavailable"


def test_stdlib_extra_fields_are_forwarded_and_redacted(received: list[ObservedError]) -> None:
    log = logging.getLogger("livekit.agents")
    log.error(
        "room join failed: %s",
        "WorkerA",
        extra={"correlation_id": "turn-42", "provider": "livekit", "agent": "voice"},
    )

    [record] = received
    assert record.event == "room join failed: %s"
    assert record.error_from_args
    assert record.event_dict["correlation_id"] == "turn-42"
    assert record.event_dict["agent"] == "voice"
    assert record.fingerprint.subkey == "provider=livekit"
    assert record.fingerprint.error_type == ""


def test_stdlib_records_inherit_structlog_contextvars_with_extra_precedence(received: list[ObservedError]) -> None:
    log = logging.getLogger("livekit.agents")
    with structlog.contextvars.bound_contextvars(correlation_id="turn-1", provider="openrouter"):
        log.error("audio track failed")
        log.error("audio track failed", extra={"provider": "livekit"})

    inherited, overridden = received
    assert inherited.event_dict["correlation_id"] == "turn-1"
    assert inherited.fingerprint.subkey == "provider=openrouter"
    assert overridden.event_dict["correlation_id"] == "turn-1"
    assert overridden.fingerprint.subkey == "provider=livekit"
