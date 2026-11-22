# ruff: noqa: D103
"""Tests for the error-notifier async worker."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from conftest import FakeHookContext, FakeMatrixAdmin, remove_installed_handlers
from error_notifier_test_import import worker as worker_module
from error_notifier_test_import.log_handler import install_error_log_handler
from error_notifier_test_import.models import NotifierSettings, ObservedError
from error_notifier_test_import.worker import ErrorNotifierWorker, WorkerDeps, resolve_ops_room


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _record(**overrides: object) -> ObservedError:
    values = {
        "logger": "mindroom.orchestrator",
        "level": "error",
        "event": "test_burst_error",
        "timestamp": "2026-05-20T21:29:40+00:00",
        "event_dict": {"error": "boom"},
        "exception": None,
    }
    values.update(overrides)
    return ObservedError(**values)


def _ctx(*, room_id: str | None = "!ops:localhost") -> FakeHookContext:
    return FakeHookContext(
        matrix_admin=FakeMatrixAdmin({"#ops:localhost": room_id}),
    )


@pytest.mark.asyncio
async def test_queue_draining_delivers_single_event() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))

    await worker._handle_record(_record())

    assert len(ctx.sent) == 1
    assert ctx.sent[0]["room_id"] == "!ops:localhost"
    assert "🚨 MindRoom error" in ctx.sent[0]["text"]


@pytest.mark.asyncio
async def test_submit_from_another_thread_queues_on_the_worker_loop() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    put_threads: list[int] = []
    put_nowait = worker.queue.put_nowait

    def recording_put_nowait(record: ObservedError) -> None:
        put_threads.append(threading.get_ident())
        put_nowait(record)

    worker.queue.put_nowait = recording_put_nowait  # type: ignore[method-assign]
    worker.start()

    submitted = await asyncio.to_thread(worker.submit, _record())
    for _ in range(100):
        if ctx.sent:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    await worker.wait_closed()

    assert submitted
    assert put_threads == [threading.get_ident()]
    assert len(ctx.sent) == 1


@pytest.mark.asyncio
async def test_actual_run_loop_drains_submitted_record() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    worker.start()
    assert worker.submit(_record())

    await asyncio.wait_for(worker.queue.join(), timeout=1)
    worker.cancel()
    await worker.wait_closed()

    assert len(ctx.sent) == 1


@pytest.mark.asyncio
async def test_periodic_sweep_emits_suppression_after_quiet_window() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=5, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    clock.now = 5

    await worker._emit_sweep_summaries()

    assert len(ctx.sent) == 2
    assert "suppressed 1 further occurrences" in ctx.sent[1]["text"]


@pytest.mark.asyncio
async def test_fresh_event_after_suppression_emits_summary_then_alert() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=5, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    clock.now = 5

    await worker._handle_record(_record())

    assert "suppressed 1 further occurrences" in ctx.sent[1]["text"]
    assert "🚨 MindRoom error" in ctx.sent[2]["text"]


@pytest.mark.asyncio
async def test_non_matching_record_is_ignored() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))

    await worker._handle_record(_record(level="info"))

    assert ctx.sent == []


@pytest.mark.asyncio
async def test_sender_returns_none_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    ctx = _ctx()
    ctx.send_result = None
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))

    await worker._handle_record(_record())

    assert "send returned no event id" in caplog.text


@pytest.mark.asyncio
async def test_sender_raises_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    ctx = _ctx()
    ctx.send_raises = RuntimeError("send failed")
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))

    await worker._handle_record(_record())

    assert "failed to send alert" in caplog.text


@pytest.mark.asyncio
async def test_unresolved_ops_room_warns_once_and_noops(caplog: pytest.LogCaptureFixture) -> None:
    ctx = _ctx(room_id=None)
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))

    await worker._handle_record(_record())
    await worker._handle_record(_record(event="second_event"))

    assert ctx.sent == []
    assert caplog.text.count("ops room could not be resolved") == 1


def test_submit_queue_full_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    worker.queue.put_nowait(_record())
    worker.queue._maxsize = 1

    assert not worker.submit(_record())
    assert "queue full" in caplog.text


@pytest.mark.asyncio
async def test_full_room_id_passes_through_resolver() -> None:
    ctx = FakeHookContext(matrix_admin=None)
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(ops_room="!abc:server", extra_events=("test_burst_error",)),
    )

    await worker._handle_record(_record())

    assert ctx.sent[0]["room_id"] == "!abc:server"


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    worker.start()
    first_task = worker.task
    worker.start()

    assert worker.task is first_task
    worker.cancel()
    await worker.wait_closed()


@pytest.mark.asyncio
async def test_reconfigure_updates_rate_limit_without_losing_queue() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=300, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    worker.update_settings(NotifierSettings(rate_limit_seconds=5, extra_events=("test_burst_error",)))
    clock.now = 5

    await worker._emit_sweep_summaries()

    assert "suppressed 1 further occurrences" in ctx.sent[1]["text"]


@pytest.mark.asyncio
async def test_reconfigure_changed_room_resolves_again() -> None:
    ctx = FakeHookContext(matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops", "#dev:localhost": "!dev"}))
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    await worker._handle_record(_record())

    worker.update_settings(NotifierSettings(ops_room="dev", extra_events=("test_burst_error",)))
    await worker._handle_record(_record(event="agent_start_failed"))

    assert [item["room_id"] for item in ctx.sent] == ["!ops", "!dev"]


@pytest.mark.asyncio
async def test_task_cancel_shuts_down_cleanly() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    worker.start()
    worker.cancel()

    await asyncio.wait_for(worker.wait_closed(), timeout=1)


@pytest.mark.asyncio
async def test_wait_closed_without_task_is_noop() -> None:
    worker = ErrorNotifierWorker(_ctx(), NotifierSettings())

    await worker.wait_closed()


@pytest.mark.asyncio
async def test_resolve_ops_room_failure_modes_and_aliases() -> None:
    assert await resolve_ops_room(_ctx(), NotifierSettings(ops_room="")) is None
    assert await resolve_ops_room(FakeHookContext(matrix_admin=None), NotifierSettings()) is None
    bad_user = FakeHookContext(matrix_admin=FakeMatrixAdmin(), matrix_user_id="@bad")
    assert await resolve_ops_room(bad_user, NotifierSettings()) is None

    admin = FakeMatrixAdmin({"#ops:localhost": "!ops"})
    resolved = await resolve_ops_room(FakeHookContext(matrix_admin=admin), NotifierSettings(ops_room="#ops:localhost"))

    assert resolved is not None
    assert resolved.room_id == "!ops"


@pytest.mark.asyncio
async def test_global_worker_lifecycle_functions() -> None:
    worker_module.cancel_worker()
    assert not worker_module.enqueue_record(_record())
    assert worker_module.current_worker() is None

    ctx = _ctx()
    worker = worker_module.start_worker(ctx, NotifierSettings(extra_events=("test_burst_error",)))
    same_worker = worker_module.start_worker(ctx, NotifierSettings(rate_limit_seconds=5))

    assert same_worker is worker
    assert worker_module.current_worker() is worker
    worker_module.cancel_worker()
    assert worker_module.current_worker() is None
    await worker.wait_closed()


@pytest.mark.asyncio
async def test_errors_logged_during_send_are_dropped() -> None:
    async def _log_send_failure(room_id: str) -> None:
        logging.getLogger("mindroom.matrix.client_delivery").error(
            {
                "event": "matrix_message_send_failed",
                "room_id": room_id,
                "error": "502 Bad Gateway",
                "cache_bypass": False,
            },
        )
        logging.getLogger("mindroom.bot").error(
            {"event": "Failed to send hook message", "room_id": room_id, "source_hook": "error-notifier:bot:ready"},
        )

    ctx = FakeHookContext(
        matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops:localhost"}),
        send_result=None,
        on_send=_log_send_failure,
    )
    received: list[ObservedError] = []
    worker = ErrorNotifierWorker(ctx, NotifierSettings())

    def _on_error(record: ObservedError) -> None:
        received.append(record)
        worker.submit(record)

    install_error_log_handler(_on_error)
    worker.start()
    try:
        logging.getLogger("mindroom.orchestrator").error({"event": "config_update_failed", "error": "boom"})
        await asyncio.wait_for(worker.queue.join(), timeout=1)

        assert [record.event for record in received] == ["config_update_failed"]
        assert len(ctx.sent) == 1

        # Probe in the SAME task that runs a raising _send_text to verify finally resets the ContextVar.
        ctx.send_raises = RuntimeError("502")
        await worker._send_text("probe", None)
        ctx.send_raises = None
        ctx.send_result = "$event"

        logging.getLogger("mindroom.matrix.client_delivery").error(
            {
                "event": "matrix_message_send_failed",
                "room_id": "!other:localhost",
                "error": "502 Bad Gateway",
                "cache_bypass": False,
            },
        )
        await asyncio.wait_for(worker.queue.join(), timeout=1)

        assert [record.event for record in received] == ["config_update_failed", "matrix_message_send_failed"]
        assert len(ctx.sent) == 2
        assert "matrix_message_send_failed" in ctx.sent[1]["text"]
    finally:
        worker.cancel()
        await worker.wait_closed()
        remove_installed_handlers()


@pytest.mark.asyncio
async def test_concurrent_task_error_during_send_is_still_received() -> None:
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def _pause_first_send(_room_id: str) -> None:
        if not send_started.is_set():
            send_started.set()
            await release_send.wait()

    ctx = FakeHookContext(
        matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops:localhost"}),
        on_send=_pause_first_send,
    )
    received: list[ObservedError] = []
    worker = ErrorNotifierWorker(ctx, NotifierSettings())

    def _on_error(record: ObservedError) -> None:
        received.append(record)
        worker.submit(record)

    install_error_log_handler(_on_error)
    worker.start()
    try:
        logging.getLogger("mindroom.orchestrator").error({"event": "config_update_failed"})
        await asyncio.wait_for(send_started.wait(), timeout=1)

        # Another task's send failure while the notifier is awaiting its own send must still be captured.
        logging.getLogger("mindroom.matrix.client_delivery").error(
            {"event": "matrix_message_send_failed", "room_id": "!agent:localhost", "error": "502"},
        )
        release_send.set()
        await asyncio.wait_for(worker.queue.join(), timeout=1)

        assert [record.event for record in received] == ["config_update_failed", "matrix_message_send_failed"]
        assert len(ctx.sent) == 2
    finally:
        release_send.set()
        worker.cancel()
        await worker.wait_closed()
        remove_installed_handlers()
