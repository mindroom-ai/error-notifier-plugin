# ruff: noqa: INP001
"""Hooks for the MindRoom error notifier plugin."""

from __future__ import annotations

import asyncio
import os
from collections import deque

import structlog
from mindroom.hooks import EVENT_BOT_READY, AgentLifecycleContext, hook

from .log_handler import install_error_log_handler
from .models import ObservedError
from .rules import settings_from_mapping
from .worker import ErrorNotifierWorker, enqueue_record, start_worker

PLUGIN_NAME = "error-notifier"
_DISABLED = os.getenv("MINDROOM_ERROR_NOTIFIER_DISABLED") == "1"
_logger = structlog.get_logger("mindroom.plugins.error_notifier")
_buffer: deque[ObservedError] = deque(maxlen=1000)


def _observer(observed: ObservedError) -> None:
    if enqueue_record(observed):
        return
    _buffer.append(observed)
    # A plugin hot reload imports fresh modules and cancels the old worker without firing bot:ready again, so the
    # first record afterwards restarts the worker from what the handler kept.
    if _handler is not None and _handler.context is not None and _handler.loop is not None:
        _handler.loop.call_soon_threadsafe(_resume_worker)


def _start(ctx: object, raw_settings: dict[str, object]) -> ErrorNotifierWorker:
    worker = start_worker(ctx, settings_from_mapping(raw_settings))
    buffered = len(_buffer)
    while _buffer:
        worker.submit(_buffer.popleft())
    _logger.info("error-notifier worker ready", buffered_records=buffered)
    return worker


def _resume_worker() -> None:
    if _handler is not None and _handler.context is not None:
        _start(_handler.context, _handler.raw_settings)


_handler = None if _DISABLED else install_error_log_handler(_observer)


@hook(EVENT_BOT_READY, name="error-notifier-ready", scope="service", priority=50, timeout_ms=30000)
async def handle_bot_ready(ctx: AgentLifecycleContext) -> None:
    """Start the notifier worker after MindRoom is ready."""
    if _handler is None:
        return
    _handler.context, _handler.raw_settings, _handler.loop = ctx, dict(ctx.settings), asyncio.get_running_loop()
    _start(ctx, _handler.raw_settings)
