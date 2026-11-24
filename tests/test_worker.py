# ruff: noqa: D103
"""Tests for the error-notifier async worker."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeHookContext, FakeMatrixAdmin, remove_installed_handlers
from error_notifier_test_import import worker as worker_module
from error_notifier_test_import.log_handler import install_error_log_handler
from error_notifier_test_import.models import NotifierSettings, ObservedError
from error_notifier_test_import.worker import (
    _SEND_RETRY_BACKOFF_SECONDS,
    ErrorNotifierWorker,
    WorkerDeps,
    resolve_ops_room,
)


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
    assert r"🚨 mindroom.orchestrator: test\_burst\_error" in ctx.sent[0]["text"]
    assert ctx.sent[0]["thread_id"] is None


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
    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert "🔁 +1 occurrence (latest:" in ctx.sent[1]["text"]


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

    assert len(ctx.sent) == 2
    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert "🔁 +2 occurrences (latest:" in ctx.sent[1]["text"]


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
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(extra_events=("test_burst_error",)),
        deps=WorkerDeps(sleep=_sleep),
    )

    await worker._handle_record(_record())
    await worker.wait_closed()

    assert sleeps == [_SEND_RETRY_BACKOFF_SECONDS]
    assert len(ctx.sent) == 2
    assert "send returned no event id" in caplog.text


@pytest.mark.asyncio
async def test_sender_returns_none_retries_once_without_blocking_other_alerts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    retry_sleeping = asyncio.Event()
    release_retry = asyncio.Event()
    ctx = _ctx()
    ctx.send_result = None

    async def _sleep(seconds: float) -> None:
        assert seconds == _SEND_RETRY_BACKOFF_SECONDS
        retry_sleeping.set()
        await release_retry.wait()

    async def _recover_after_first(_room_id: str) -> None:
        if ctx.sent:
            ctx.send_result = "$event"

    ctx.on_send = _recover_after_first
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(extra_events=("first_blip", "second_alert")),
        deps=WorkerDeps(sleep=_sleep),
    )
    worker.start()
    try:
        worker.submit(_record(event="first_blip"))
        await asyncio.wait_for(retry_sleeping.wait(), timeout=1)
        assert len(ctx.sent) == 1

        worker.submit(_record(event="second_alert"))
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        assert len(ctx.sent) == 2
        assert "second_alert" in ctx.sent[1]["text"]

        release_retry.set()
        await asyncio.wait_for(asyncio.gather(*list(worker_module._retry_tasks)), timeout=1)
        assert len(ctx.sent) == 3
        assert "first_blip" in ctx.sent[2]["text"]
        assert "send returned no event id" not in caplog.text
    finally:
        release_retry.set()
        worker.cancel()
        await worker.wait_closed()


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

    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert "🔁 +1 occurrence (latest:" in ctx.sent[1]["text"]


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


@pytest.mark.asyncio
async def test_errors_logged_during_retry_send_are_dropped() -> None:
    send_calls = 0

    async def _log_every_send(room_id: str) -> None:
        nonlocal send_calls
        send_calls += 1
        logging.getLogger("mindroom.matrix.client_delivery").error(
            {"event": "matrix_message_send_failed", "room_id": room_id, "error": "502 Bad Gateway"},
        )

    async def _no_sleep(_seconds: float) -> None:
        return None

    ctx = FakeHookContext(
        matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops:localhost"}),
        send_result=None,
        on_send=_log_every_send,
    )
    received: list[ObservedError] = []
    worker = ErrorNotifierWorker(ctx, NotifierSettings(), deps=WorkerDeps(sleep=_no_sleep))

    def _on_error(record: ObservedError) -> None:
        received.append(record)
        worker.submit(record)

    install_error_log_handler(_on_error)
    worker.start()
    try:
        logging.getLogger("mindroom.orchestrator").error({"event": "config_update_failed", "error": "boom"})
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        await asyncio.wait_for(asyncio.gather(*list(worker_module._retry_tasks)), timeout=1)

        assert send_calls == 2
        assert len(ctx.sent) == 2
        assert [record.event for record in received] == ["config_update_failed"]
    finally:
        worker.cancel()
        await worker.wait_closed()
        remove_installed_handlers()


@pytest.mark.asyncio
async def test_open_thread_persists_across_restart_and_deduplicates_error(tmp_path: Path) -> None:
    clock = Clock()
    ctx = FakeHookContext(matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops:localhost"}), state_root=tmp_path)
    settings = NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",))
    worker1 = ErrorNotifierWorker(ctx, settings, deps=WorkerDeps(clock=clock))

    await worker1._handle_record(_record(event_dict={"error": "502 Bad Gateway"}))
    assert (tmp_path / "open_threads.json").exists()
    assert ctx.sent[0]["thread_id"] is None

    # Same error inside window -> suppressed; sweep sends compact 1-line rollup without repeating error:.
    await worker1._handle_record(_record(event_dict={"error": "502 Bad Gateway"}))
    clock.now = 10
    await worker1._emit_sweep_summaries()
    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert ctx.sent[1]["text"] == "🔁 +1 occurrence (latest: 2026-05-20T21:29:40+00:00)"

    # Restart worker from same state_root; error recurs with changed message -> replies in same thread with error:.
    worker2 = ErrorNotifierWorker(ctx, settings, deps=WorkerDeps(clock=clock))
    await worker2._handle_record(
        _record(timestamp="2026-05-20T22:00:00+00:00", event_dict={"error": "503 Service Unavailable"})
    )
    assert len(ctx.sent) == 3
    assert ctx.sent[2]["thread_id"] == "$event-1"
    assert ctx.sent[2]["text"] == "🔁 Recurred — 2026-05-20T22:00:00+00:00\nerror:   503 Service Unavailable"


@pytest.mark.asyncio
async def test_distinct_fingerprints_open_separate_threads() -> None:
    ctx = _ctx()
    worker = ErrorNotifierWorker(ctx, NotifierSettings(rate_limit_seconds=60))

    await worker._handle_record(
        _record(
            logger="mindroom.hooks",
            event="hook_failed",
            event_dict={"plugin_name": "p1", "error_type": "TimeoutError", "error": "timed out"},
        )
    )
    await worker._handle_record(
        _record(
            logger="mindroom.hooks",
            event="hook_failed",
            event_dict={"plugin_name": "p2", "error_type": "TimeoutError", "error": "timed out"},
        )
    )
    await worker._handle_record(
        _record(
            logger="mindroom.hooks",
            event="hook_failed",
            event_dict={"plugin_name": "p1", "error_type": "ValueError", "error": "bad value"},
        )
    )

    assert len(ctx.sent) == 3
    assert [msg["thread_id"] for msg in ctx.sent] == [None, None, None]
    assert "mindroom.hooks (p1): TimeoutError" in ctx.sent[0]["text"]
    assert "mindroom.hooks (p2): TimeoutError" in ctx.sent[1]["text"]
    assert "mindroom.hooks (p1): ValueError" in ctx.sent[2]["text"]


@pytest.mark.asyncio
async def test_resolved_thread_reopens_with_link_and_resets_backoff(tmp_path: Path) -> None:
    clock = Clock()
    ctx = FakeHookContext(matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops:localhost"}), state_root=tmp_path)
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )

    # Open $event-1, suppress one, sweep -> advances backoff to step 1 (30s window).
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    clock.now = 10
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 2

    # Cleared tag ({}) is still open -> stays in $event-1. Suppress one and resume -> step 2 (120s window).
    ctx.set_thread_resolved("!ops:localhost", "$event-1", cleared=True)
    await worker._handle_record(_record())
    clock.now = 40
    await worker._handle_record(_record())
    assert ctx.sent[2]["thread_id"] == "$event-1"

    # Mark $event-1 resolved -> next error after window forgets old thread, resets backoff, opens new root with link.
    ctx.set_thread_resolved("!ops:localhost", "$event-1", resolved=True)
    clock.now = 160
    await worker._handle_record(_record())
    assert len(ctx.sent) == 4
    assert ctx.sent[3]["thread_id"] is None
    assert (
        "reoccurred: [previous resolved thread](https://matrix.to/#/%21ops%3Alocalhost/%24event-1)"
        in ctx.sent[3]["text"]
    )

    # Verify backoff was reset to base (10s), NOT 120s/720s!
    await worker._handle_record(_record())
    clock.now = 170
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 5
    assert ctx.sent[4]["thread_id"] == "$event-4"


@pytest.mark.asyncio
async def test_sweep_resolution_ordering_drops_pre_resolve_and_reopens_post_resolve() -> None:
    # (a) Repeat BEFORE resolve -> sweep drops stale summary, keeps existing, next occurrence reopens WITH backlink.
    clock_a = Clock()
    ctx_a = _ctx()
    worker_a = ErrorNotifierWorker(
        ctx_a,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock_a),
    )
    await worker_a._handle_record(_record(timestamp="2026-05-20T21:00:00+00:00"))
    await worker_a._handle_record(_record(timestamp="2026-05-20T21:00:02+00:00"))
    ctx_a.set_thread_resolved("!ops:localhost", "$event-1", resolved=True, set_at="2026-05-20T21:00:05+00:00")
    clock_a.now = 10
    await worker_a._emit_sweep_summaries()
    assert len(ctx_a.sent) == 1  # stale pre-resolution summary dropped

    # Next post-resolve occurrence reopens immediately WITH the reoccurred: link!
    await worker_a._handle_record(_record(timestamp="2026-05-20T21:00:12+00:00"))
    assert len(ctx_a.sent) == 2
    assert ctx_a.sent[1]["thread_id"] is None
    assert (
        "reoccurred: [previous resolved thread](https://matrix.to/#/%21ops%3Alocalhost/%24event-1)"
        in ctx_a.sent[1]["text"]
    )

    # (b) Resolve FIRST, then recurrence INSIDE backoff window -> sweep reopens a new root WITH backlink!
    clock_b = Clock()
    ctx_b = _ctx()
    worker_b = ErrorNotifierWorker(
        ctx_b,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock_b),
    )
    await worker_b._handle_record(_record(timestamp="2026-05-20T21:00:00+00:00"))
    ctx_b.set_thread_resolved("!ops:localhost", "$event-1", resolved=True, set_at="2026-05-20T21:00:03+00:00")
    await worker_b._handle_record(_record(timestamp="2026-05-20T21:00:05+00:00"))  # suppressed inside window
    clock_b.now = 10
    await worker_b._emit_sweep_summaries()
    assert len(ctx_b.sent) == 2
    assert ctx_b.sent[1]["thread_id"] is None
    assert (
        "reoccurred: [previous resolved thread](https://matrix.to/#/%21ops%3Alocalhost/%24event-1)"
        in ctx_b.sent[1]["text"]
    )

    # Subsequent repeat bundles into the newly reopened $event-2 (not old $event-1).
    await worker_b._handle_record(_record(timestamp="2026-05-20T21:00:15+00:00"))
    clock_b.now = 20
    await worker_b._emit_sweep_summaries()
    assert len(ctx_b.sent) == 3
    assert ctx_b.sent[2]["thread_id"] == "$event-2"

    # Missing/unparseable set_at on resolved tag also fails safe and reopens instead of dropping.
    ctx_b.set_thread_resolved("!ops:localhost", "$event-2", resolved=True, set_at=None)
    await worker_b._handle_record(_record(timestamp="2026-05-20T21:00:25+00:00"))
    clock_b.now = 50
    await worker_b._emit_sweep_summaries()
    assert len(ctx_b.sent) == 4
    assert ctx_b.sent[3]["thread_id"] is None
    assert "%24event-2" in ctx_b.sent[3]["text"]


@pytest.mark.asyncio
async def test_sweep_skips_orphan_summary_when_root_send_failed() -> None:
    clock = Clock()
    ctx = _ctx()
    ctx.send_raises = RuntimeError("homeserver down")
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    ctx.send_raises = None
    clock.now = 10
    await worker._emit_sweep_summaries()
    assert ctx.sent == []


@pytest.mark.asyncio
async def test_resolved_check_error_fails_safe_without_feedback_loop(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()

    async def _log_during_query(_room: str, _ev: str, _key: str | None) -> None:
        logging.getLogger("mindroom.matrix.client").error({"event": "state_query_failed", "error": "502"})

    ctx = _ctx()
    ctx.query_state_raises = RuntimeError("502 Bad Gateway")
    ctx.on_query_state = _log_during_query
    received: list[ObservedError] = []
    install_error_log_handler(received.append)
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=5, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    try:
        await worker._handle_record(_record())
        clock.now = 5
        await worker._handle_record(_record())

        assert [msg["thread_id"] for msg in ctx.sent] == [None, "$event-1"]
        assert received == []
        assert "failed to query resolved state" in caplog.text
    finally:
        remove_installed_handlers()


@pytest.mark.asyncio
async def test_context_without_state_root_or_query_room_state_works_gracefully() -> None:
    sent: list[dict[str, object]] = []

    async def _send_message(
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, object] | None = None,
        trigger_dispatch: bool = False,
    ) -> str:
        del trigger_dispatch
        sent.append({"room_id": room_id, "text": text, "thread_id": thread_id, "extra_content": extra_content})
        return f"$min-{len(sent)}"

    minimal_ctx = SimpleNamespace(
        event_name="bot:ready",
        matrix_admin=FakeMatrixAdmin({"#ops:localhost": "!ops:localhost"}),
        matrix_user_id="@router:localhost",
        send_message=_send_message,
    )
    clock = Clock()
    worker = ErrorNotifierWorker(
        minimal_ctx,  # type: ignore[arg-type]
        NotifierSettings(rate_limit_seconds=5, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    await worker._handle_record(_record())
    clock.now = 5
    await worker._handle_record(_record())
    assert [msg["thread_id"] for msg in sent] == [None, "$min-1"]


@pytest.mark.asyncio
async def test_async_retry_records_open_thread_for_subsequent_rollups() -> None:
    clock = Clock()
    ctx = _ctx()
    ctx.send_result = None

    async def _recover(_room_id: str) -> None:
        if ctx.sent:
            ctx.send_result = "$retried-root"

    async def _no_sleep(_seconds: float) -> None:
        return None

    ctx.on_send = _recover
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock, sleep=_no_sleep),
    )

    await worker._handle_record(_record())
    await asyncio.wait_for(asyncio.gather(*list(worker_module._retry_tasks)), timeout=1)
    assert len(ctx.sent) == 2

    await worker._handle_record(_record())
    clock.now = 10
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 3
    assert ctx.sent[2]["thread_id"] == "$retried-root"


@pytest.mark.asyncio
async def test_correlation_cascade_across_turns_and_immediate_standalone_secondary() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, include_stack_traces=True),
        deps=WorkerDeps(clock=clock),
    )

    def _primary(corr_id: str) -> ObservedError:
        return _record(
            logger="mindroom.hooks",
            event="hook_failed",
            event_dict={
                "plugin_name": "voice",
                "correlation_id": corr_id,
                "error_type": "TimeoutError",
                "error": "slow",
            },
        )

    def _secondary(corr_id: str | None) -> ObservedError:
        event_dict: dict[str, object] = {"provider": "openrouter", "error": "ProviderUnavailable('cascade')"}
        if corr_id is not None:
            event_dict["correlation_id"] = corr_id
        return _record(
            logger="mindroom.ai",
            event="model_invocation_failed",
            event_dict=event_dict,
            exception=(
                "Traceback (most recent call last):\n"
                '  File "/app/mindroom/ai.py", line 1, in run\n'
                "    raise x\nProviderUnavailable: cascade\n"
            ),
        )

    # Turn 1 (t=0): Primary A opens $event-1; secondary B posts 🔗 inside $event-1 with stack extra.
    await worker._handle_record(_primary("turn-1"))
    clock.now = 1
    await worker._handle_record(_secondary("turn-1"))
    assert len(ctx.sent) == 2
    assert ctx.sent[0]["thread_id"] is None
    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert "🔗 Related error (same turn) — mindroom.ai (openrouter): ProviderUnavailable" in ctx.sent[1]["text"]
    assert ctx.sent[1]["extra_content"] is not None

    # Turn 2 (t=2, inside A's backoff window): A hits SUPPRESS yet records turn-2 -> $event-1;
    # B is throttled in $event-1 (no duplicate 🔗 AND no orphan top-level root for B!).
    clock.now = 2
    await worker._handle_record(_primary("turn-2"))
    await worker._handle_record(_secondary("turn-2"))
    assert len(ctx.sent) == 2

    # Turn 3 (t=12, after window): A posts rollup in $event-1 and B posts one new 🔗 in $event-1.
    clock.now = 12
    await worker._handle_record(_primary("turn-3"))
    await worker._handle_record(_secondary("turn-3"))
    assert len(ctx.sent) == 4
    assert ctx.sent[2]["thread_id"] == "$event-1"
    assert ctx.sent[3]["thread_id"] == "$event-1"
    assert "🔗 Related error (same turn)" in ctx.sent[3]["text"]

    # Standalone B IMMEDIATELY after (t=13, no correlation_id) has an untouched standalone bucket -> opens own root!
    clock.now = 13
    await worker._handle_record(_secondary(None))
    assert len(ctx.sent) == 5
    assert ctx.sent[4]["thread_id"] is None
    assert "🚨 mindroom.ai (openrouter): ProviderUnavailable" in ctx.sent[4]["text"]

    # Expired correlation_id (>=60s) and resolved parent thread both open their own top-level threads.
    clock.now = 80
    await worker._handle_record(
        _record(
            logger="mindroom.tools",
            event="tool_failed",
            event_dict={"tool_name": "shell", "correlation_id": "turn-3", "error_type": "OSError", "error": "late"},
        )
    )
    assert ctx.sent[5]["thread_id"] is None

    ctx.set_thread_resolved("!ops:localhost", "$event-6", resolved=True)
    clock.now = 85
    await worker._handle_record(
        _record(
            logger="mindroom.storage",
            event="write_failed",
            event_dict={"correlation_id": "turn-3", "error_type": "OSError", "error": "disk"},
        )
    )
    assert ctx.sent[6]["thread_id"] is None


@pytest.mark.asyncio
async def test_run_loop_sweeps_under_continuous_traffic_and_quiet_window_resets_backoff() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10),
        deps=WorkerDeps(clock=clock),
    )
    worker.start()
    try:
        # Error A opens $event-1 at t=0, then suppresses a repeat at t=1.
        worker.submit(_record(logger="mindroom.a", event="err_a"))
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        clock.now = 1
        worker.submit(_record(logger="mindroom.a", event="err_a"))
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        assert len(ctx.sent) == 1

        # Advance clock past A's window and immediately submit traffic on another logger (no wait_for timeout!).
        clock.now = 10
        worker.submit(_record(logger="mindroom.b", event="err_b"))
        await asyncio.wait_for(worker.queue.join(), timeout=1)

        # Steady traffic on logger B must NOT starve A's sweep rollup!
        assert len(ctx.sent) == 3
        assert ctx.sent[1]["thread_id"] is None  # err_b root
        assert ctx.sent[2]["thread_id"] == "$event-1"  # err_a sweep rollup (A now at step 1 -> 30s)

        # Quiet 30s window for A WITHOUT any sweep -> recurrence resets backoff to step 0 (10s base)!
        clock.now = 40
        await worker._handle_record(_record(logger="mindroom.a", event="err_a"))
        assert ctx.sent[3]["thread_id"] == "$event-1"
        assert "🔁 Recurred —" in ctx.sent[3]["text"]
        await worker._handle_record(_record(logger="mindroom.a", event="err_a"))
        clock.now = 50  # expires after 10s base window, proving backoff reset!
        await worker._emit_sweep_summaries()
        assert len(ctx.sent) == 5
        assert ctx.sent[4]["thread_id"] == "$event-1"
    finally:
        worker.cancel()
        await worker.wait_closed()
