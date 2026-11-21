"""Feed MindRoom's ERROR log records to the notifier through a stdlib logging handler.

MindRoom routes structlog and stdlib records through the root logger's handlers, so one
handler on the root logger sees every error without any MindRoom-side observer API.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from mindroom.redaction import redact_log_event, redact_sensitive_text

from .models import ObservedError
from .rules import FEEDBACK_LOOP_LOGGER_PREFIX

HANDLER_NAME = "mindroom-error-notifier"
_MAX_EXCEPTION_CHARS = 8192

_ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


class ErrorLogHandler(logging.Handler):
    """Pass each ERROR record to a callback as a redacted `ObservedError`; never raise into the caller.

    The handler also keeps what the plugin learned at `bot:ready` (the service hook context, the plugin settings,
    and the event loop), because a plugin hot reload imports fresh modules without firing `bot:ready` again.
    """

    def __init__(self, callback: Callable[[ObservedError], None]) -> None:
        super().__init__(level=logging.ERROR)
        self.set_name(HANDLER_NAME)
        self._callback = callback
        self.context: Any = None
        self.raw_settings: dict[str, Any] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def emit(self, record: logging.LogRecord) -> None:
        """Convert and hand over one record."""
        if record.name.startswith(FEEDBACK_LOOP_LOGGER_PREFIX):
            return
        try:
            self._callback(observed_error_from_record(record))
        except Exception:  # noqa: BLE001, S110 - a log handler that logs its own failure would recurse.
            pass


def install_error_log_handler(callback: Callable[[ObservedError], None]) -> ErrorLogHandler:
    """Attach a handler to the root logger, taking over from one an earlier import of this plugin left behind."""
    root = logging.getLogger()
    handler = ErrorLogHandler(callback)
    for previous in list(root.handlers):
        if previous.get_name() == HANDLER_NAME:
            if isinstance(getattr(previous, "raw_settings", None), dict):
                handler.context = getattr(previous, "context", None)
                handler.raw_settings = previous.raw_settings
                handler.loop = getattr(previous, "loop", None)
            root.removeHandler(previous)
    root.addHandler(handler)
    return handler


def observed_error_from_record(record: logging.LogRecord) -> ObservedError:
    """Build a redacted snapshot of one log record."""
    if isinstance(record.msg, dict):
        # structlog's `wrap_for_formatter` hands the event dict to stdlib logging as the message.
        event_dict: dict[str, Any] = dict(record.msg)
        exc_info: object = event_dict.pop("exc_info", None)
    else:
        # The unformatted template names the error, so its rate limit holds across differing arguments.
        event_dict = {"event": str(record.msg)}
        if record.args:
            event_dict["error"] = record.getMessage()
        exc_info = record.exc_info
    event_dict.setdefault("logger", record.name)
    event_dict.setdefault("level", record.levelname.lower())
    event_dict.setdefault("timestamp", datetime.fromtimestamp(record.created, UTC).isoformat())

    redacted = redact_log_event(None, record.levelname.lower(), event_dict)
    exception = _format_exc_info(exc_info)
    if exception is None and redacted.get("exception") is not None:
        exception = _truncate(redact_sensitive_text(str(redacted["exception"])))
    return ObservedError(
        logger=str(redacted.get("logger") or record.name),
        level=str(redacted.get("level") or record.levelname.lower()),
        event=str(redacted.get("event") or ""),
        timestamp=None if redacted.get("timestamp") is None else str(redacted["timestamp"]),
        event_dict=redacted,
        exception=exception,
    )


def _format_exc_info(exc_info: object) -> str | None:
    normalized = _normalize_exc_info(exc_info)
    if normalized is None:
        return None
    rendered = "".join(traceback.format_exception(*normalized))
    return _truncate(redact_sensitive_text(rendered))


def _normalize_exc_info(exc_info: object) -> _ExcInfo | None:
    if exc_info is True:
        exc_type, exc, tb = sys.exc_info()
        if exc_type is None or exc is None:
            return None
        return exc_type, exc, tb
    if isinstance(exc_info, BaseException):
        return type(exc_info), exc_info, exc_info.__traceback__
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc_type, exc, tb = exc_info
        if isinstance(exc_type, type) and issubclass(exc_type, BaseException) and isinstance(exc, BaseException):
            return exc_type, exc, tb if isinstance(tb, TracebackType) else None
    return None


def _truncate(value: str) -> str:
    if len(value) <= _MAX_EXCEPTION_CHARS:
        return value
    return value[: _MAX_EXCEPTION_CHARS - 15] + "... [truncated]"
