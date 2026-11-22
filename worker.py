"""Async worker for the MindRoom error notifier plugin."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .formatter import extra_content_for, format_alert, format_suppression_summary
from .log_handler import installed_sending_var
from .models import NotifierSettings, ObservedError, ResolvedOpsRoom
from .rate_limit import DecisionKind, RateLimiter
from .rules import action_for, matches

QUEUE_MAX_SIZE = 1000
_WARNING_LOGGER = logging.getLogger("mindroom.plugins.error_notifier.worker")


class _MatrixAdmin(Protocol):
    async def resolve_alias(self, alias: str) -> str | None:
        """Resolve one Matrix room alias."""


class _SendContext(Protocol):
    event_name: str
    matrix_admin: _MatrixAdmin | None
    matrix_user_id: str

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None:
        """Send one Matrix message."""


@dataclass(frozen=True, slots=True)
class WorkerDeps:
    """Worker dependencies that are injectable in tests."""

    clock: Callable[[], float] | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


class ErrorNotifierWorker:
    """Drain observed errors, apply rules and rate limits, and send alerts."""

    def __init__(
        self,
        ctx: _SendContext,
        settings: NotifierSettings,
        *,
        deps: WorkerDeps | None = None,
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._deps = deps or WorkerDeps()
        self._sending = installed_sending_var()
        self._queue: asyncio.Queue[ObservedError] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._rate_limiter = RateLimiter(settings.rate_limit_seconds, clock=self._deps.clock or time.monotonic)
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._resolved_room: ResolvedOpsRoom | None = None
        self._resolution_attempted = False
        self._resolution_warning_emitted = False

    @property
    def task(self) -> asyncio.Task[None] | None:
        """Return the active worker task."""
        return self._task

    @property
    def queue(self) -> asyncio.Queue[ObservedError]:
        """Return the worker queue."""
        return self._queue

    def start(self) -> None:
        """Start the worker task once."""
        global _worker_task
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name="error-notifier-worker")
        _worker_task = self._task

    def update_settings(self, settings: NotifierSettings) -> None:
        """Apply updated settings without replacing the queue."""
        room_changed = settings.ops_room != self._settings.ops_room
        self._settings = settings
        self._rate_limiter.configure_window(settings.rate_limit_seconds)
        if room_changed:
            self._resolved_room = None
            self._resolution_attempted = False
            self._resolution_warning_emitted = False

    def submit(self, record: ObservedError) -> bool:
        """Queue one record without blocking, from any thread; MindRoom logs from more than one."""
        loop = self._loop
        if loop is not None and _running_loop() is not loop:
            try:
                loop.call_soon_threadsafe(self._put, record)
            except RuntimeError:  # The worker's loop closed.
                return False
            return True
        return self._put(record)

    def _put(self, record: ObservedError) -> bool:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            _WARNING_LOGGER.warning(
                "error-notifier queue full; dropping newest error from %s event %s",
                record.logger,
                record.event,
            )
            return False
        return True

    def cancel(self) -> None:
        """Cancel the worker task when running."""
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def wait_closed(self) -> None:
        """Wait for worker cancellation to finish."""
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            return

    async def _run(self) -> None:
        while True:
            try:
                record = await asyncio.wait_for(self._queue.get(), timeout=self._sweep_interval())
            except TimeoutError:
                await self._emit_sweep_summaries()
                continue

            try:
                await self._handle_record(record)
            finally:
                self._queue.task_done()

    def _sweep_interval(self) -> float:
        return min(60.0, max(1.0, self._settings.rate_limit_seconds / 4))

    async def _handle_record(self, record: ObservedError) -> None:
        if not matches(record, self._settings):
            return

        decision = self._rate_limiter.gate(record.key)
        if decision.kind is DecisionKind.SUPPRESS:
            return
        if decision.kind is DecisionKind.EMIT_RESUME:
            await self._send_text(format_suppression_summary(record.key, decision.suppressed_count), None)

        await self._send_text(
            format_alert(record, action_for(record)),
            extra_content_for(record, self._settings.include_stack_traces),
        )

    async def _emit_sweep_summaries(self) -> None:
        for key, count in self._rate_limiter.sweep():
            await self._send_text(format_suppression_summary(key, count), None)

    async def _send_text(self, body: str, extra_content: dict[str, Any] | None) -> None:
        room = await self._resolve_ops_room()
        if room is None:
            return
        try:
            token = self._sending.set(True)
            try:
                event_id = await self._ctx.send_message(
                    room_id=room.room_id,
                    text=body,
                    thread_id=None,
                    extra_content=extra_content,
                )
            finally:
                self._sending.reset(token)
        except Exception as exc:
            _WARNING_LOGGER.warning(
                "error-notifier failed to send alert",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        if event_id is None:
            _WARNING_LOGGER.warning("error-notifier send returned no event id for room %s", room.room_id)

    async def _resolve_ops_room(self) -> ResolvedOpsRoom | None:
        if self._resolved_room is not None:
            return self._resolved_room
        if self._resolution_attempted:
            return None

        self._resolution_attempted = True
        resolved = await resolve_ops_room(self._ctx, self._settings)
        if resolved is None:
            if not self._resolution_warning_emitted:
                _WARNING_LOGGER.warning("error-notifier ops room could not be resolved: %s", self._settings.ops_room)
                self._resolution_warning_emitted = True
            return None
        self._resolved_room = resolved
        return resolved


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


_worker: ErrorNotifierWorker | None = None
_worker_task: asyncio.Task[None] | None = None


def start_worker(
    ctx: _SendContext,
    settings: NotifierSettings,
    *,
    deps: WorkerDeps | None = None,
) -> ErrorNotifierWorker:
    """Start or reconfigure the module-global worker."""
    global _worker
    if _worker is None:
        _worker = ErrorNotifierWorker(ctx, settings, deps=deps)
    else:
        _worker.update_settings(settings)
    _worker.start()
    return _worker


def enqueue_record(record: ObservedError) -> bool:
    """Queue one record on the running worker."""
    if _worker is None:
        return False
    return _worker.submit(record)


def current_worker() -> ErrorNotifierWorker | None:
    """Return the module-global worker for tests."""
    return _worker


def cancel_worker() -> None:
    """Cancel the module-global worker and forget it."""
    global _worker, _worker_task
    if _worker is not None:
        _worker.cancel()
    _worker = None
    _worker_task = None


async def resolve_ops_room(ctx: _SendContext, settings: NotifierSettings) -> ResolvedOpsRoom | None:
    """Resolve the configured ops room without creating rooms."""
    ops_room = settings.ops_room.strip()
    if not ops_room:
        return None
    if ops_room.startswith("!"):
        return ResolvedOpsRoom(room_id=ops_room, label=ops_room)

    admin = ctx.matrix_admin
    if admin is None:
        return None

    alias = ops_room if ops_room.startswith("#") else _alias_from_localpart(ctx, ops_room)
    if alias is None:
        return None
    room_id = await admin.resolve_alias(alias)
    if room_id is None:
        return None
    return ResolvedOpsRoom(room_id=room_id, label=alias)


def _alias_from_localpart(ctx: _SendContext, localpart: str) -> str | None:
    matrix_user_id = ctx.matrix_user_id
    if ":" not in matrix_user_id:
        return None
    server_name = matrix_user_id.rsplit(":", maxsplit=1)[1]
    if not server_name:
        return None
    return f"#{localpart}:{server_name}"
