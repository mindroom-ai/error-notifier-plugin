# ruff: noqa: INP001
"""Own the notifier for the process and give it its Matrix bindings at bot:ready."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import deque
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from mindroom.hooks import EVENT_BOT_READY, AgentLifecycleContext, hook

from .log_handler import HANDLER_NAME, install_error_log_handler
from .models import ObservedError
from .rules import settings_from_mapping
from .worker import ErrorNotifierWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.plugin_api import PluginLifecycle

_logger = structlog.get_logger("mindroom.plugins.error_notifier")


class _NotifierRuntime:
    def __init__(self) -> None:
        self._buffer: deque[ObservedError] = deque(maxlen=1000)
        self._worker: ErrorNotifierWorker | None = None
        self._active = True
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()

        def observe(record: ObservedError) -> None:
            if threading.get_ident() == loop_thread:
                self._receive(record)
            else:
                loop.call_soon_threadsafe(self._receive, record)

        self._handler = install_error_log_handler(observe)

    def _receive(self, record: ObservedError) -> None:
        if not self._active:  # A logging thread may have scheduled this before stop detached the handler.
            return
        if self._worker is None:
            self._buffer.append(record)
        else:
            self._worker.submit(record)

    def ready(self, ctx: AgentLifecycleContext | None) -> None:
        if not self._active:
            return
        if self._worker is not None:
            self._worker.start()  # Preserve the queue and settings even if the consumer exited.
            return
        if ctx is None:
            return
        worker = self._worker = ErrorNotifierWorker(ctx, settings_from_mapping(ctx.settings))
        worker.start()
        buffered = len(self._buffer)
        while self._buffer:
            worker.submit(self._buffer.popleft())
        _logger.info("error-notifier worker ready", buffered_records=buffered)

    async def stop(self) -> None:
        self._active = False
        logging.getLogger().removeHandler(self._handler)
        self._handler.close()
        self._buffer.clear()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await worker.wait_closed()


_runtime: _NotifierRuntime | None = None
_pending_ready: AgentLifecycleContext | None = None


@asynccontextmanager
async def lifespan(lifecycle: PluginLifecycle) -> AsyncIterator[None]:
    """Keep the same handler, settings, and queue until disable or shutdown."""
    global _runtime, _pending_ready
    state = lifecycle.process_state
    if os.getenv("MINDROOM_ERROR_NOTIFIER_DISABLED") == "1":
        if (previous := state.pop("runtime", None)) is not None:
            await previous.stop()
        yield
        return

    runtime = state.get("runtime")
    ctx, _pending_ready = _pending_ready, None
    if runtime is None:
        # Upgrading a pre-lifespan notifier after startup does not emit bot:ready again.
        if ctx is None:
            ctx = next(
                (getattr(h, "context", None) for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME),
                None,
            )
        runtime = state["runtime"] = _NotifierRuntime()
    _runtime = runtime
    try:
        runtime.ready(ctx)
        yield
    finally:
        if lifecycle.stop_reason != "reload":
            state.pop("runtime", None)
            await runtime.stop()


@hook(EVENT_BOT_READY, name="error-notifier-ready", scope="service", priority=50, timeout_ms=30000)
async def handle_bot_ready(ctx: AgentLifecycleContext) -> None:
    """Start the notifier worker after MindRoom is ready."""
    global _pending_ready
    if _runtime is None:
        _pending_ready = ctx  # Publication can expose this hook before its lifespan starts.
    else:
        _runtime.ready(ctx)
