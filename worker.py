"""Async worker for the MindRoom error notifier plugin."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .formatter import (
    extra_content_for,
    format_alert,
    format_related_alert,
    format_thread_followup,
    raw_error_text,
    timestamp_for,
)
from .log_handler import installed_sending_var
from .models import ErrorFingerprint, NotifierSettings, ObservedError, ResolvedOpsRoom, non_empty_str
from .rate_limit import DecisionKind, RateLimiter
from .rules import action_for, matches
from .thread_store import OpenThread, ThreadStore

QUEUE_MAX_SIZE = 1000
THREAD_TAGS_EVENT_TYPE = "com.mindroom.thread.tags"
_SEND_RETRY_BACKOFF_SECONDS = 4.0
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
    """Drain observed errors, apply rules and rate limits, and send bundled thread alerts."""

    def __init__(self, ctx: _SendContext, settings: NotifierSettings, *, deps: WorkerDeps | None = None) -> None:
        self._ctx = ctx
        self._settings = settings
        self._deps = deps or WorkerDeps()
        self._clock = self._deps.clock or time.monotonic
        self._sending = installed_sending_var()
        self._queue: asyncio.Queue[ObservedError] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._rate_limiter: RateLimiter[ErrorFingerprint] = RateLimiter(settings.rate_limit_seconds, clock=self._clock)
        self._thread_store = ThreadStore.from_context(ctx, clock=self._clock)
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
        """Cancel the worker task and pending retries when running."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        for task in list(_retry_tasks):
            task.cancel()

    async def wait_closed(self) -> None:
        """Wait for worker and retry tasks to finish."""
        for task in [t for t in (self._task, *list(_retry_tasks)) if t is not None]:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        last_sweep = self._clock()
        while True:
            try:
                record = await asyncio.wait_for(self._queue.get(), timeout=self._sweep_interval())
            except TimeoutError:
                await self._emit_sweep_summaries()
                last_sweep = self._clock()
                continue

            try:
                await self._handle_record(record)
                if self._clock() - last_sweep >= self._sweep_interval():
                    await self._emit_sweep_summaries()
                    last_sweep = self._clock()
            finally:
                self._queue.task_done()

    def _sweep_interval(self) -> float:
        return min(60.0, max(1.0, self._settings.rate_limit_seconds / 4))

    async def _handle_record(self, record: ObservedError) -> None:
        if not matches(record, self._settings):
            return

        room = await self._resolve_ops_room()
        if room is None:
            return

        fp = record.fingerprint
        corr_id = non_empty_str(record.event_dict.get("correlation_id"))
        if corr_id is not None and await self._try_send_related_cascade(room, record, fp, corr_id):
            return

        decision = self._rate_limiter.gate(fp, record)
        if decision.kind is DecisionKind.SUPPRESS:
            if corr_id is not None and (existing := self._thread_store.get(room.room_id, fp)) is not None:
                self._thread_store.record_correlation(room.room_id, corr_id, existing.thread_id, fp)
            return

        existing = self._thread_store.get(room.room_id, fp)
        previous_thread: tuple[str, str] | None = None
        if existing is not None:
            resolved, _ = await self._resolved_at(room.room_id, existing.thread_id)
            if resolved:
                previous_thread = (room.room_id, existing.thread_id)
            else:
                is_rollup = decision.kind is DecisionKind.EMIT_RESUME
                count = decision.suppressed_count + 1 if is_rollup else 1
                self._thread_store.record_correlation(room.room_id, corr_id, existing.thread_id, fp)
                await self._send_followup(room, fp, existing, record, count=count, is_rollup=is_rollup)
                return

        self._rate_limiter.reset(fp)
        self._rate_limiter.gate(fp, record)
        await self._send_root_alert(room, record, fp, previous_thread=previous_thread)

    async def _try_send_related_cascade(
        self, room: ResolvedOpsRoom, record: ObservedError, fp: ErrorFingerprint, corr_id: str
    ) -> bool:
        entry = self._thread_store.lookup_correlation(room.room_id, corr_id)
        if entry is None or entry.primary_fingerprint == fp:
            return False
        window = self._settings.rate_limit_seconds
        if self._thread_store.is_cascade_throttled(entry.thread_id, fp, window):
            return True
        resolved, _ = await self._resolved_at(room.room_id, entry.thread_id)
        if resolved:
            self._thread_store.forget_correlation(room.room_id, corr_id)
            return False
        self._thread_store.record_cascade_emit(entry.thread_id, fp, window)
        await self._send_text(
            format_related_alert(record, action_for(record)),
            extra_content_for(record, self._settings.include_stack_traces),
            thread_id=entry.thread_id,
            room=room,
        )
        return True

    async def _send_root_alert(
        self,
        room: ResolvedOpsRoom,
        record: ObservedError,
        fp: ErrorFingerprint,
        *,
        previous_thread: tuple[str, str] | None = None,
        recent_occurrences: int | None = None,
    ) -> None:
        last_error, seen_at = raw_error_text(record), timestamp_for(record)
        corr_id = non_empty_str(record.event_dict.get("correlation_id"))
        replaced_id = previous_thread[1] if previous_thread is not None else None

        def _on_delivered(event_id: str) -> None:
            current = self._thread_store.get(room.room_id, fp)
            if current is None or (replaced_id is not None and current.thread_id == replaced_id):
                self._thread_store.record(room.room_id, fp, event_id, last_error, seen_at)
            self._thread_store.record_correlation(room.room_id, corr_id, event_id, fp)

        await self._send_text(
            format_alert(
                record,
                action_for(record),
                previous_thread=previous_thread,
                recent_occurrences=recent_occurrences,
            ),
            extra_content_for(record, self._settings.include_stack_traces),
            room=room,
            on_delivered=_on_delivered,
        )

    async def _send_followup(
        self,
        room: ResolvedOpsRoom,
        fp: ErrorFingerprint,
        existing: OpenThread,
        record: ObservedError | None,
        *,
        count: int,
        is_rollup: bool,
    ) -> None:
        new_error = raw_error_text(record) if record is not None else None
        seen_at = timestamp_for(record)

        def _on_delivered(_event_id: str) -> None:
            current = self._thread_store.get(room.room_id, fp)
            if current is not None and current.thread_id == existing.thread_id:
                updated_error = new_error if new_error is not None else current.last_error
                self._thread_store.record(room.room_id, fp, existing.thread_id, updated_error, seen_at)

        await self._send_text(
            format_thread_followup(record, count=count, is_rollup=is_rollup, last_error=existing.last_error),
            extra_content_for(record, self._settings.include_stack_traces) if record is not None else None,
            thread_id=existing.thread_id,
            room=room,
            on_delivered=_on_delivered,
        )

    async def _emit_sweep_summaries(self) -> None:
        entries = self._rate_limiter.sweep()
        if not entries or (room := await self._resolve_ops_room()) is None:
            return
        for entry in entries:
            existing = self._thread_store.get(room.room_id, entry.key)
            if existing is None:
                self._rate_limiter.reset(entry.key)
                if entry.last_record is not None:
                    self._rate_limiter.gate(entry.key, entry.last_record)
                    await self._send_root_alert(
                        room, entry.last_record, entry.key, recent_occurrences=entry.suppressed_count
                    )
                continue
            resolved, resolved_at = await self._resolved_at(room.room_id, existing.thread_id)
            if resolved:
                self._rate_limiter.reset(entry.key)
                record = entry.last_record
                record_dt = _parse_iso_datetime(record.timestamp) if record is not None else None
                if record is not None and (resolved_at is None or record_dt is None or record_dt > resolved_at):
                    self._rate_limiter.gate(entry.key, record)
                    await self._send_root_alert(
                        room,
                        record,
                        entry.key,
                        previous_thread=(room.room_id, existing.thread_id),
                        recent_occurrences=entry.suppressed_count,
                    )
                continue
            await self._send_followup(
                room, entry.key, existing, entry.last_record, count=entry.suppressed_count, is_rollup=True
            )

    async def _resolved_at(self, room_id: str, thread_id: str) -> tuple[bool, datetime | None]:
        query = getattr(self._ctx, "query_room_state", None)
        if not callable(query):
            return False, None
        state_key = json.dumps([thread_id, "resolved"], separators=(",", ":"))
        token = self._sending.set(True)
        try:
            result = await query(room_id, THREAD_TAGS_EVENT_TYPE, state_key)
        except Exception as exc:
            _WARNING_LOGGER.warning("error-notifier failed to query resolved state for thread %s: %s", thread_id, exc)
            return False, None
        finally:
            self._sending.reset(token)
        if not (isinstance(result, dict) and bool(result)):
            return False, None
        return True, _parse_iso_datetime(result.get("set_at"))

    async def _send_text(
        self,
        body: str,
        extra_content: dict[str, Any] | None,
        *,
        thread_id: str | None = None,
        room: ResolvedOpsRoom | None = None,
        on_delivered: Callable[[str], None] | None = None,
    ) -> str | None:
        target = room if room is not None else await self._resolve_ops_room()
        if target is None:
            return None
        handled, event_id = await self._attempt_send(target, body, extra_content, thread_id)
        if event_id is not None and on_delivered is not None:
            on_delivered(event_id)
        if not handled:
            task = asyncio.create_task(
                self._retry_send(target, body, extra_content, thread_id, on_delivered),
                name="error-notifier-retry",
            )
            _retry_tasks.add(task)
            task.add_done_callback(_retry_tasks.discard)
        return event_id

    async def _retry_send(
        self,
        room: ResolvedOpsRoom,
        body: str,
        extra_content: dict[str, Any] | None,
        thread_id: str | None,
        on_delivered: Callable[[str], None] | None,
    ) -> None:
        await self._deps.sleep(_SEND_RETRY_BACKOFF_SECONDS)
        handled, event_id = await self._attempt_send(room, body, extra_content, thread_id)
        if event_id is not None and on_delivered is not None:
            on_delivered(event_id)
        if not handled:
            _WARNING_LOGGER.warning("error-notifier send returned no event id for room %s", room.room_id)

    async def _attempt_send(
        self, room: ResolvedOpsRoom, body: str, extra_content: dict[str, Any] | None, thread_id: str | None
    ) -> tuple[bool, str | None]:
        """Send one message; return `(handled, event_id)` where `handled=False` requests a retry."""
        try:
            token = self._sending.set(True)
            try:
                event_id = await self._ctx.send_message(
                    room_id=room.room_id, text=body, thread_id=thread_id, extra_content=extra_content
                )
            finally:
                self._sending.reset(token)
        except Exception as exc:
            _WARNING_LOGGER.warning("error-notifier failed to send alert", exc_info=(type(exc), exc, exc.__traceback__))
            return True, None
        return event_id is not None, event_id

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


def _parse_iso_datetime(value: object) -> datetime | None:
    text = non_empty_str(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


_worker: ErrorNotifierWorker | None = None
_worker_task: asyncio.Task[None] | None = None
_retry_tasks: set[asyncio.Task[None]] = set()


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
    return False if _worker is None else _worker.submit(record)


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
    return None if room_id is None else ResolvedOpsRoom(room_id=room_id, label=alias)


def _alias_from_localpart(ctx: _SendContext, localpart: str) -> str | None:
    matrix_user_id = ctx.matrix_user_id
    if ":" not in matrix_user_id:
        return None
    server_name = matrix_user_id.rsplit(":", maxsplit=1)[1]
    return f"#{localpart}:{server_name}" if server_name else None
