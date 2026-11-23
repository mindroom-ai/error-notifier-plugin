# ruff: noqa: D103
"""Tests for the error-notifier root logging handler."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import remove_installed_handlers
from error_notifier_test_import.formatter import format_alert
from error_notifier_test_import.log_handler import (
    _MAX_EXCEPTION_CHARS,
    HANDLER_NAME,
    _truncate,
    install_error_log_handler,
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

    body = format_alert(
        ObservedError(
            logger="mindroom.matrix.client_delivery",
            level="error",
            event="matrix_message_send_failed",
            timestamp="2026-11-22T20:45:32+00:00",
            exception=truncated,
        ),
        None,
    )
    assert "error:  nio.exceptions.RoomSendError: unknown error" in body
    assert 'File "/app/nio/client.py", line 812, in room\\_send' in body
