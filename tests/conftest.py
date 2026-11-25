# ruff: noqa: D103
"""Test scaffolding for the error-notifier plugin."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "error_notifier_test_import"


def _load_plugin_test_package() -> None:
    if PACKAGE_NAME in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        msg = "Failed to create error-notifier test package spec"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


_load_plugin_test_package()


def remove_installed_handlers() -> None:
    from error_notifier_test_import.log_handler import HANDLER_NAME

    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == HANDLER_NAME:
            root.removeHandler(handler)


@dataclass
class FakeMatrixAdmin:
    aliases: dict[str, str | None] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def resolve_alias(self, alias: str) -> str | None:
        self.calls.append(alias)
        return self.aliases.get(alias)


@dataclass
class FakeHookContext:
    matrix_admin: FakeMatrixAdmin | None = None
    matrix_user_id: str = "@mindroom_router:localhost"
    event_name: str = "bot:ready"
    settings: dict[str, Any] = field(default_factory=dict)
    state_root: Path | None = None
    sent: list[dict[str, Any]] = field(default_factory=list)
    send_result: str | None = "$event"
    send_raises: BaseException | None = None
    on_send: Callable[[str], Awaitable[None]] | None = None
    room_state: dict[tuple[str, str, str | None], dict[str, Any] | None] = field(default_factory=dict)
    state_queries: list[tuple[str, str, str | None]] = field(default_factory=list)
    query_state_raises: BaseException | None = None
    on_query_state: Callable[[str, str, str | None], Awaitable[None]] | None = None

    def set_thread_resolved(
        self,
        room_id: str,
        thread_id: str,
        *,
        resolved: bool = True,
        cleared: bool = False,
        set_at: str | None = "2026-05-20T21:30:00+00:00",
    ) -> None:
        state_key = json.dumps([thread_id, "resolved"], separators=(",", ":"))
        key = (room_id, "com.mindroom.thread.tags", state_key)
        if cleared:
            self.room_state[key] = {}
        elif resolved:
            payload: dict[str, Any] = {"set_by": "@ops:localhost"}
            if set_at is not None:
                payload["set_at"] = set_at
            self.room_state[key] = payload
        else:
            self.room_state.pop(key, None)

    async def query_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        self.state_queries.append((room_id, event_type, state_key))
        if self.on_query_state is not None:
            await self.on_query_state(room_id, event_type, state_key)
        if self.query_state_raises is not None:
            raise self.query_state_raises
        return self.room_state.get((room_id, event_type, state_key))

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
    ) -> str | None:
        if self.on_send is not None:
            await self.on_send(room_id)
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append(
            {
                "room_id": room_id,
                "text": text,
                "thread_id": thread_id,
            },
        )
        if self.send_result == "$event":
            return f"$event-{len(self.sent)}"
        return self.send_result
