# ruff: noqa: D103, SLF001
"""Import and reload tests for the error-notifier hooks module."""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeHookContext
from error_notifier_test_import import hooks
from error_notifier_test_import import worker as worker_module
from error_notifier_test_import.log_handler import HANDLER_NAME
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import EVENT_CONFIG_RELOADED, ConfigReloadedContext, HookRegistry, emit


def _remove_installed_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == HANDLER_NAME:
            root.removeHandler(handler)


def test_importing_hooks_buffers_errors_until_the_worker_starts() -> None:
    worker_module.cancel_worker()
    module = importlib.reload(hooks)
    module._buffer.clear()
    try:
        logging.getLogger("mindroom.orchestrator").error("config_update_failed")

        assert [record.event for record in module._buffer] == ["config_update_failed"]
        assert sum(handler.get_name() == HANDLER_NAME for handler in logging.getLogger().handlers) == 1
    finally:
        _remove_installed_handlers()


@pytest.mark.asyncio
async def test_config_reload_redirects_alerts_to_new_ops_room(tmp_path: Path) -> None:
    worker_module.cancel_worker()
    module = importlib.reload(hooks)
    ctx = FakeHookContext(settings={"ops_room": "!old:localhost"})
    plugin = SimpleNamespace(
        name="error-notifier",
        entry_config=PluginEntryConfig(path=".", settings={"ops_room": "!new:localhost"}),
        plugin_order=0,
        discovered_hooks=(module.handle_config_reloaded,),
        discovered_automations=(),
    )
    registry = HookRegistry.from_plugins([plugin])
    reload_context = ConfigReloadedContext(
        event_name=EVENT_CONFIG_RELOADED,
        config=Config(),
        runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={}),
        correlation_id="test-reload",
        changed_entities=(),
        added_entities=(),
        removed_entities=(),
        plugin_changes=(),
    )
    worker = None
    try:
        await module.handle_bot_ready(ctx)
        worker = worker_module.current_worker()
        assert worker is not None
        logger = logging.getLogger("mindroom.orchestrator")
        logger.error("before_reload")
        await asyncio.wait_for(worker.queue.join(), timeout=1)
        await emit(registry, EVENT_CONFIG_RELOADED, reload_context)
        logger.error("after_reload")
        await asyncio.wait_for(worker.queue.join(), timeout=1)

        assert [message["room_id"] for message in ctx.sent] == ["!old:localhost", "!new:localhost"]
    finally:
        worker_module.cancel_worker()
        if worker is not None:
            await worker.wait_closed()
        _remove_installed_handlers()


@pytest.mark.asyncio
async def test_alerts_continue_after_a_plugin_hot_reload() -> None:
    worker_module.cancel_worker()
    ctx = FakeHookContext(settings={"ops_room": "!ops:localhost", "extra_events": ["hot_reload_probe"]})
    module = importlib.reload(hooks)
    try:
        await module.handle_bot_ready(ctx)
        old_worker = worker_module.current_worker()
        assert old_worker is not None

        # A hot reload imports the plugin's modules fresh, without bot:ready, and cancels the tasks the old ones held.
        importlib.reload(worker_module)
        importlib.reload(hooks)
        old_worker.cancel()
        await old_worker.wait_closed()

        logging.getLogger("mindroom.orchestrator").error("hot_reload_probe")
        for _ in range(100):
            if ctx.sent:
                break
            await asyncio.sleep(0.01)

        assert [message["room_id"] for message in ctx.sent] == ["!ops:localhost"]
        assert "hot_reload_probe" in ctx.sent[0]["text"]
    finally:
        worker = worker_module.current_worker()
        worker_module.cancel_worker()
        if worker is not None:
            await worker.wait_closed()
        _remove_installed_handlers()
