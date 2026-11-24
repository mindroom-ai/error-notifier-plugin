# ruff: noqa: D103
"""Tests for the error-notifier async worker."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeHookContext, FakeMatrixAdmin, remove_installed_handlers
from error_notifier_test_import import worker as worker_module
from error_notifier_test_import.log_handler import install_error_log_handler
from error_notifier_test_import.models import ErrorFingerprint, NotifierSettings, ObservedError
from error_notifier_test_import.thread_store import ThreadStore
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
    assert "🚨 mindroom.orchestrator: test_burst_error" in ctx.sent[0]["text"]
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

    # 1. Root alert opens thread and writes open_threads.json.
    await worker1._handle_record(_record(event_dict={"error": "502 Bad Gateway"}))
    assert (tmp_path / "open_threads.json").exists()
    assert ctx.sent[0]["thread_id"] is None

    # 2. Same error inside window -> suppressed; sweep sends compact 1-line rollup without repeating error:.
    await worker1._handle_record(_record(event_dict={"error": "502 Bad Gateway"}))
    clock.now = 10
    await worker1._emit_sweep_summaries()
    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert ctx.sent[1]["text"] == "🔁 +1 occurrence (latest: 2026-05-20T21:29:40+00:00)"

    # 3. Restart worker from same state_root; error recurs with changed message -> replies in same thread with error:.
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
            logger="mindroom.hooks", event="hook_failed", event_dict={"plugin_name": "p1", "error": "TimeoutError: a"}
        )
    )
    await worker._handle_record(
        _record(
            logger="mindroom.hooks", event="hook_failed", event_dict={"plugin_name": "p2", "error": "TimeoutError: b"}
        )
    )
    await worker._handle_record(
        _record(
            logger="mindroom.hooks", event="hook_failed", event_dict={"plugin_name": "p1", "error": "ValueError: c"}
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

    # Cleared tag ({}) is still open -> stays in $event-1.
    ctx.set_thread_resolved("!ops:localhost", "$event-1", cleared=True)
    clock.now = 40
    await worker._handle_record(_record())
    assert ctx.sent[2]["thread_id"] == "$event-1"  # now step 2 (120s window)

    # Mark $event-1 resolved -> next error after window forgets old thread, resets backoff, opens new root with link.
    ctx.set_thread_resolved("!ops:localhost", "$event-1", resolved=True)
    clock.now = 160
    await worker._handle_record(_record())
    assert len(ctx.sent) == 4
    assert ctx.sent[3]["thread_id"] is None
    assert "reoccurred: [previous resolved thread](https://matrix.to/#/!ops:localhost/$event-1)" in ctx.sent[3]["text"]

    # Verify backoff was reset to base (10s), NOT 120s/720s!
    await worker._handle_record(_record())
    clock.now = 170
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 5
    assert ctx.sent[4]["thread_id"] == "$event-4"


@pytest.mark.asyncio
async def test_sweep_drops_stale_summary_when_thread_resolved_or_missing() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )

    # 1. Resolved before sweep -> drop stale suppression summary completely.
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    ctx.set_thread_resolved("!ops:localhost", "$event-1", resolved=True)
    clock.now = 10
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 1

    # Next error immediately opens a fresh root because sweep cleared bucket & thread.
    await worker._handle_record(_record())
    assert len(ctx.sent) == 2
    assert ctx.sent[1]["thread_id"] is None

    # 2. Root send fails permanently -> sweep skips emitting an orphan summary.
    ctx_fail = _ctx()
    ctx_fail.send_raises = RuntimeError("homeserver down")
    worker_fail = ErrorNotifierWorker(
        ctx_fail,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )
    await worker_fail._handle_record(_record())
    await worker_fail._handle_record(_record())
    ctx_fail.send_raises = None
    clock.now = 20
    await worker_fail._emit_sweep_summaries()
    assert ctx_fail.sent == []


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

        # Treated as still open (replies in $event-1, no duplicate root) and internal log was suppressed!
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

    # Repeat and sweep now bundle into $retried-root!
    await worker._handle_record(_record())
    clock.now = 10
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 3
    assert ctx.sent[2]["thread_id"] == "$retried-root"


@pytest.mark.asyncio
async def test_correlation_id_bundles_same_turn_cascade_without_overwriting_secondary_root() -> None:
    clock = Clock()
    ctx = _ctx()
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, include_stack_traces=True),
        deps=WorkerDeps(clock=clock),
    )

    primary = _record(
        logger="mindroom.hooks",
        event="hook_failed",
        event_dict={"plugin_name": "voice", "correlation_id": "turn-1", "error": "TimeoutError: slow"},
    )
    secondary = _record(
        logger="mindroom.ai",
        event="model_invocation_failed",
        event_dict={"provider": "openrouter", "correlation_id": "turn-1", "error": "RuntimeError: cascade"},
        exception=(
            "Traceback (most recent call last):\n"
            '  File "/app/mindroom/ai.py", line 1, in run\n'
            "    raise x\nRuntimeError: cascade\n"
        ),
    )

    # Primary opens $event-1; secondary with same correlation_id within 60s posts 🔗 inside $event-1 with stack extra.
    await worker._handle_record(primary)
    clock.now = 2
    await worker._handle_record(secondary)
    assert len(ctx.sent) == 2
    assert ctx.sent[0]["thread_id"] is None
    assert ctx.sent[1]["thread_id"] == "$event-1"
    assert "🔗 Related error (same turn) — mindroom.ai (openrouter): RuntimeError" in ctx.sent[1]["text"]
    assert ctx.sent[1]["extra_content"] is not None

    # Repeat of secondary in same turn is rate-limited (no spam).
    await worker._handle_record(secondary)
    assert len(ctx.sent) == 2

    # Later in another turn (after rate-limit window, no correlation_id), secondary gets its OWN top-level thread!
    clock.now = 15
    standalone_secondary = _record(
        logger="mindroom.ai",
        event="model_invocation_failed",
        event_dict={"provider": "openrouter", "error": "RuntimeError: standalone"},
    )
    await worker._handle_record(standalone_secondary)
    assert len(ctx.sent) == 3
    assert ctx.sent[2]["thread_id"] is None
    assert "🚨 mindroom.ai (openrouter): RuntimeError" in ctx.sent[2]["text"]

    # Expired correlation_id (>=60s) or resolved parent thread opens a new top-level thread instead of bundling.
    clock.now = 70
    expired_related = _record(
        logger="mindroom.tools",
        event="tool_failed",
        event_dict={"tool_name": "shell", "correlation_id": "turn-1", "error": "OSError: late"},
    )
    await worker._handle_record(expired_related)
    assert ctx.sent[3]["thread_id"] is None

    # Resolved primary thread during 60s window also prevents posting into the resolved thread.
    ctx.set_thread_resolved("!ops:localhost", "$event-4", resolved=True)
    clock.now = 75
    after_resolved = _record(
        logger="mindroom.storage",
        event="write_failed",
        event_dict={"correlation_id": "turn-1", "error": "OSError: disk"},
    )
    await worker._handle_record(after_resolved)
    assert ctx.sent[4]["thread_id"] is None


def test_thread_store_corrupt_json_and_pruning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Corrupt JSON starts fresh and warns.
    (tmp_path / "open_threads.json").write_text("{not valid json", encoding="utf-8")
    store = ThreadStore(tmp_path, max_per_room=2)
    assert "failed to load" in caplog.text

    # Prunes oldest by last_seen_at beyond max_per_room and persists atomically.
    fp1 = ErrorFingerprint("mindroom.a", "e1")
    fp2 = ErrorFingerprint("mindroom.a", "e2")
    fp3 = ErrorFingerprint("mindroom.a", "e3")
    store.record("!ops:localhost", fp1, "$1", "err1", "2026-05-20T01:00:00+00:00")
    store.record("!ops:localhost", fp2, "$2", "err2", "2026-05-20T02:00:00+00:00")
    store.record("!ops:localhost", fp3, "$3", "err3", "2026-05-20T03:00:00+00:00")

    reloaded = ThreadStore(tmp_path, max_per_room=2)
    assert reloaded.get("!ops:localhost", fp1) is None
    assert reloaded.get("!ops:localhost", fp2) is not None
    assert reloaded.get("!ops:localhost", fp3) is not None

    # Partially corrupt entries are skipped with warning while preserving valid ones.
    data = json.loads((tmp_path / "open_threads.json").read_text(encoding="utf-8"))
    data["!ops:localhost"]["bad_entry"] = {"thread_id": ""}
    (tmp_path / "open_threads.json").write_text(json.dumps(data), encoding="utf-8")
    partial = ThreadStore(tmp_path, max_per_room=2)
    assert partial.get("!ops:localhost", fp2) is not None
    assert "skipped 1 corrupt" in caplog.text
