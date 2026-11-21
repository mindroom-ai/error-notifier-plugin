# ruff: noqa: D103, SLF001
"""Import and reload tests for the error-notifier hooks module."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path

import pytest
from conftest import PLUGIN_ROOT, FakeHookContext
from error_notifier_test_import import hooks
from error_notifier_test_import.log_handler import HANDLER_NAME
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import EVENT_BOT_READY
from mindroom.hooks.decorators import get_hook_metadata, iter_module_hooks
from mindroom.plugin_lifecycle import PluginLifespans
from mindroom.tool_system.plugins import config_plugins, prepare_plugins, publish_plugins


def _remove_installed_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == HANDLER_NAME:
            root.removeHandler(handler)


def test_ready_is_the_only_hook_and_is_explicitly_service_scoped() -> None:
    [callback] = iter_module_hooks(hooks)
    metadata = get_hook_metadata(callback)

    assert metadata is not None
    assert metadata.event_name == EVENT_BOT_READY
    assert metadata.hook_name == "error-notifier-ready"
    assert metadata.scope == "service"
    assert metadata.agents is None


class _PublishedRuntime:
    """Publish real plugin generations without starting agents or contacting Matrix."""

    def __init__(self, tmp_path: Path) -> None:
        self.paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
        self.config = Config()
        self.lifespans = PluginLifespans(self.paths, lambda: self.config)

    async def publish(self, *, ops_room: str | None = "!startup:localhost", enabled: bool = True) -> None:
        entries = []
        if ops_room is not None:
            entries.append({"path": str(PLUGIN_ROOT), "enabled": enabled, "settings": {"ops_room": ops_room}})
        self.config = Config.model_validate({"plugins": entries})
        result = publish_plugins(prepare_plugins(self.config, self.paths))
        assert not result.skipped
        await self.lifespans.apply_published()

    async def ready(self, ctx: FakeHookContext) -> None:
        [ready] = config_plugins(self.config, self.paths).hooks.hooks_for(EVENT_BOT_READY)
        await ready.callback(ctx)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[_PublishedRuntime]:
    _remove_installed_handlers()
    published = _PublishedRuntime(tmp_path)
    try:
        yield published
    finally:
        await published.lifespans.stop()
        publish_plugins(prepare_plugins(Config(), published.paths))
        _remove_installed_handlers()
        # The old implementation could orphan a worker started after its generation was removed.
        workers = [task for task in asyncio.all_tasks() if task.get_name() == "error-notifier-worker"]
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_only_published_lifespan_attaches_handler_and_buffers_startup_errors(
    runtime: _PublishedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.model_validate({"plugins": [{"path": str(PLUGIN_ROOT)}]})
    staged = prepare_plugins(config, runtime.paths)
    assert not staged.skipped
    assert all(handler.get_name() != HANDLER_NAME for handler in logging.getLogger().handlers)

    await runtime.publish()
    logging.getLogger("mindroom.orchestrator").error("before_ready")
    assert sum(handler.get_name() == HANDLER_NAME for handler in logging.getLogger().handlers) == 1
    posted = asyncio.Event()
    alerts: list[str] = []

    async def send_message(*, text: str, **_kwargs: object) -> str:
        alerts.append(text)
        posted.set()
        return "$alert"

    ctx = FakeHookContext(settings={"ops_room": "!startup:localhost"})
    monkeypatch.setattr(ctx, "send_message", send_message)
    await runtime.ready(ctx)
    await asyncio.wait_for(posted.wait(), timeout=2)
    [alert] = alerts
    assert "before_ready" in alert


@pytest.mark.asyncio
async def test_published_settings_change_preserves_pending_alerts(
    runtime: _PublishedRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_flight = asyncio.Event()
    release = asyncio.Event()
    delivered_after_swap = asyncio.Event()
    sent: list[tuple[str, str]] = []

    async def send_message(*, room_id: str, text: str, **_kwargs: object) -> str:
        event = next(line.removeprefix("event: ") for line in text.splitlines() if line.startswith("event: ")).strip()
        if event == "in_flight":
            in_flight.set()
            await release.wait()
        sent.append((event, room_id))
        if event == "after_swap":
            delivered_after_swap.set()
        return "$alert"

    await runtime.publish()
    ctx = FakeHookContext(settings={"ops_room": "!startup:localhost"})
    monkeypatch.setattr(ctx, "send_message", send_message)
    await runtime.ready(ctx)
    logger = logging.getLogger("mindroom.orchestrator")
    logger.error("in_flight")
    await asyncio.wait_for(in_flight.wait(), timeout=2)
    logger.error("queued_one")
    logger.error("queued_two")
    await asyncio.sleep(0)  # Let log callbacks enqueue both records while the first send is blocked.

    await runtime.publish(ops_room="!edited:localhost")
    release.set()
    logger.error("after_swap")
    await asyncio.wait_for(delivered_after_swap.wait(), timeout=2)

    assert sent == [(event, "!startup:localhost") for event in ("in_flight", "queued_one", "queued_two", "after_swap")]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["disable", "remove", "shutdown"])
async def test_notifier_stops_posting_and_detaches_its_handler(
    runtime: _PublishedRuntime,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    posted = asyncio.Event()

    async def send_message(**_kwargs: object) -> str:
        posted.set()
        return "$alert"

    await runtime.publish()
    ctx = FakeHookContext(settings={"ops_room": "!startup:localhost"})
    monkeypatch.setattr(ctx, "send_message", send_message)
    await runtime.ready(ctx)
    logger = logging.getLogger("mindroom.orchestrator")
    logger.error("before_stop")
    await asyncio.wait_for(posted.wait(), timeout=2)
    posted.clear()
    # The old handler on this generation can resurrect a worker after publication cancels the first one.
    await runtime.publish(ops_room="!edited:localhost")

    if stop == "shutdown":
        await runtime.lifespans.stop()
    else:
        await runtime.publish(ops_room="!edited:localhost" if stop == "disable" else None, enabled=False)
    logger.error("after_stop")
    with suppress(TimeoutError):
        await asyncio.wait_for(posted.wait(), timeout=0.1)

    assert not posted.is_set(), f"posted after {stop}"
    assert all(handler.get_name() != HANDLER_NAME for handler in logging.getLogger().handlers)
    assert all(task.get_name() != "error-notifier-worker" for task in asyncio.all_tasks())
