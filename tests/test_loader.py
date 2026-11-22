# ruff: noqa: D103, SLF001
"""Import and reload tests for the error-notifier hooks module."""

from __future__ import annotations

import asyncio
import importlib
import logging

import pytest
from conftest import FakeHookContext, remove_installed_handlers
from error_notifier_test_import import hooks
from error_notifier_test_import import log_handler as log_handler_module
from error_notifier_test_import import worker as worker_module
from error_notifier_test_import.log_handler import HANDLER_NAME
from mindroom.hooks.decorators import get_hook_metadata, iter_module_hooks
from mindroom.plugin_api import EVENT_BOT_READY


def test_importing_hooks_buffers_errors_until_the_worker_starts() -> None:
    worker_module.cancel_worker()
    module = importlib.reload(hooks)
    module._buffer.clear()
    try:
        logging.getLogger("mindroom.orchestrator").error("config_update_failed")

        assert [record.event for record in module._buffer] == ["config_update_failed"]
        assert sum(handler.get_name() == HANDLER_NAME for handler in logging.getLogger().handlers) == 1
    finally:
        remove_installed_handlers()


def test_ready_is_the_only_hook_and_is_explicitly_service_scoped() -> None:
    [callback] = iter_module_hooks(hooks)
    metadata = get_hook_metadata(callback)

    assert metadata is not None
    assert metadata.event_name == EVENT_BOT_READY
    assert metadata.hook_name == "error-notifier-ready"
    assert metadata.scope == "service"
    assert metadata.agents is None


@pytest.mark.asyncio
async def test_alerts_and_in_send_suppression_survive_a_plugin_hot_reload() -> None:
    worker_module.cancel_worker()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def _log_during_send(room_id: str) -> None:
        if not send_started.is_set():
            send_started.set()
            await release_send.wait()
        logging.getLogger("mindroom.matrix.client_delivery").error(
            {
                "event": "matrix_message_send_failed",
                "room_id": room_id,
                "error": "502 Bad Gateway",
                "cache_bypass": False,
            },
        )

    ctx = FakeHookContext(
        settings={"ops_room": "!ops:localhost", "extra_events": ["hot_reload_probe"]},
        on_send=_log_during_send,
    )
    module = importlib.reload(hooks)
    old_worker = None
    try:
        await module.handle_bot_ready(ctx)
        old_worker = worker_module.current_worker()
        assert old_worker is not None

        logging.getLogger("mindroom.orchestrator").error({"event": "first_probe"})
        await asyncio.wait_for(send_started.wait(), timeout=1)

        # Hot reload imports fresh modules without bot:ready while old_worker is still mid-send.
        importlib.reload(log_handler_module)
        importlib.reload(worker_module)
        reloaded_hooks = importlib.reload(hooks)

        release_send.set()
        await asyncio.wait_for(old_worker.queue.join(), timeout=1)
        assert list(reloaded_hooks._buffer) == []

        old_worker.cancel()
        await old_worker.wait_closed()
        old_worker = None

        # Resuming after reload still delivers alerts and shares the handed-over ContextVar.
        logging.getLogger("mindroom.orchestrator").error("hot_reload_probe")
        for _ in range(100):
            if len(ctx.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        resumed_worker = worker_module.current_worker()
        assert resumed_worker is not None
        await asyncio.wait_for(resumed_worker.queue.join(), timeout=1)

        assert [message["room_id"] for message in ctx.sent] == ["!ops:localhost", "!ops:localhost"]
        assert "first_probe" in ctx.sent[0]["text"]
        assert "hot_reload_probe" in ctx.sent[1]["text"]
        assert all("matrix_message_send_failed" not in msg["text"] for msg in ctx.sent)
    finally:
        release_send.set()
        if old_worker is not None:
            old_worker.cancel()
            await old_worker.wait_closed()
        worker = worker_module.current_worker()
        worker_module.cancel_worker()
        if worker is not None:
            await worker.wait_closed()
        remove_installed_handlers()
