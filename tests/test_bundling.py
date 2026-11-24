# ruff: noqa: D103
"""End-to-end tests for error-notifier thread bundling, resolution, recovery, and cascades."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeHookContext, FakeMatrixAdmin, remove_installed_handlers
from error_notifier_test_import import worker as worker_module
from error_notifier_test_import.log_handler import install_error_log_handler
from error_notifier_test_import.models import NotifierSettings, ObservedError
from error_notifier_test_import.worker import ErrorNotifierWorker, WorkerDeps


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _record(**overrides: object) -> ObservedError:
    values: dict[str, object] = {
        "logger": "mindroom.orchestrator",
        "level": "error",
        "event": "test_burst_error",
        "timestamp": "2026-05-20T21:29:40+00:00",
        "event_dict": {"error": "boom"},
        "exception": None,
    }
    values.update(overrides)
    return ObservedError(**values)  # type: ignore[arg-type]


def _ctx(*, room_id: str | None = "!ops:localhost", state_root: Path | None = None) -> FakeHookContext:
    return FakeHookContext(
        matrix_admin=FakeMatrixAdmin({"#ops:localhost": room_id}),
        state_root=state_root,
    )


@pytest.mark.asyncio
async def test_open_thread_persists_across_restart_and_deduplicates_error(tmp_path: Path) -> None:
    clock = Clock()
    ctx = _ctx(state_root=tmp_path)
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
            event_dict={"plugin_name": "p1", "error": "TimeoutError: slow"},
        )
    )
    await worker._handle_record(
        _record(
            logger="mindroom.hooks",
            event="hook_failed",
            event_dict={"plugin_name": "p2", "error": "TimeoutError: slow"},
        )
    )
    await worker._handle_record(
        _record(
            logger="mindroom.hooks",
            event="hook_failed",
            event_dict={"plugin_name": "p1", "error": "ValueError: bad value"},
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
    ctx = _ctx(state_root=tmp_path)
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

    # Mark $event-1 resolved -> next error after window resets backoff and opens new root with link.
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
async def test_sweep_resolution_ordering_drops_pre_resolve_and_reopens_post_resolve_with_count() -> None:
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

    await worker_a._handle_record(_record(timestamp="2026-05-20T21:00:12+00:00"))
    assert len(ctx_a.sent) == 2
    assert ctx_a.sent[1]["thread_id"] is None
    assert (
        "reoccurred: [previous resolved thread](https://matrix.to/#/%21ops%3Alocalhost/%24event-1)"
        in ctx_a.sent[1]["text"]
    )

    # (b) Resolve FIRST, then multi-error burst INSIDE backoff window -> sweep reopens WITH occurrences & backlink!
    clock_b = Clock()
    ctx_b = _ctx()
    worker_b = ErrorNotifierWorker(
        ctx_b,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock_b),
    )
    await worker_b._handle_record(_record(timestamp="2026-05-20T21:00:00+00:00"))
    ctx_b.set_thread_resolved("!ops:localhost", "$event-1", resolved=True, set_at="2026-05-20T21:00:03+00:00")
    for sec in ("05", "06", "07"):
        await worker_b._handle_record(_record(timestamp=f"2026-05-20T21:00:{sec}+00:00"))
    clock_b.now = 10
    await worker_b._emit_sweep_summaries()
    assert len(ctx_b.sent) == 2
    assert ctx_b.sent[1]["thread_id"] is None
    assert "occurrences: 3 in last window" in ctx_b.sent[1]["text"]
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
    assert "occurrences:" not in ctx_b.sent[3]["text"]  # single recurrence omits occurrences line


@pytest.mark.asyncio
async def test_sweep_recovers_failed_root_burst_when_matrix_recovers() -> None:
    clock = Clock()
    ctx = _ctx()
    ctx.send_raises = RuntimeError("homeserver down")
    worker = ErrorNotifierWorker(
        ctx,
        NotifierSettings(rate_limit_seconds=10, extra_events=("test_burst_error",)),
        deps=WorkerDeps(clock=clock),
    )

    # Initial root fails during Matrix outage, and 2 more errors arrive in the window.
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    await worker._handle_record(_record())

    # If Matrix is STILL down at sweep, emits nothing cleanly.
    clock.now = 10
    await worker._emit_sweep_summaries()
    assert ctx.sent == []

    # Another burst arrives, then Matrix recovers before sweep -> opens a real 🚨 root alert with occurrences count!
    await worker._handle_record(_record())
    await worker._handle_record(_record())
    ctx.send_raises = None
    clock.now = 20
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 1
    assert ctx.sent[0]["thread_id"] is None
    assert ctx.sent[0]["text"].startswith("🚨 ")
    assert "occurrences: 2 in last window" in ctx.sent[0]["text"]

    # Subsequent repeat now rolls up inside the recovered root thread $event-1.
    await worker._handle_record(_record())
    clock.now = 30
    await worker._emit_sweep_summaries()
    assert len(ctx.sent) == 2
    assert ctx.sent[1]["thread_id"] == "$event-1"


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
            event_dict={"plugin_name": "voice", "correlation_id": corr_id, "error": "TimeoutError: slow"},
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
            event_dict={"tool_name": "shell", "correlation_id": "turn-3", "error": "OSError: late"},
        )
    )
    assert ctx.sent[5]["thread_id"] is None

    ctx.set_thread_resolved("!ops:localhost", "$event-6", resolved=True)
    clock.now = 85
    await worker._handle_record(
        _record(
            logger="mindroom.storage",
            event="write_failed",
            event_dict={"correlation_id": "turn-3", "error": "OSError: disk"},
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
