# ruff: noqa: D103
"""Test scaffolding for the error-notifier plugin."""

from __future__ import annotations

import importlib.util
import sys
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
    sent: list[dict[str, Any]] = field(default_factory=list)
    send_result: str | None = "$event"
    send_raises: BaseException | None = None

    async def send_message(
        self,
        room_id: str,
        text: str,
        *,
        thread_id: str | None = None,
        extra_content: dict[str, Any] | None = None,
        trigger_dispatch: bool = False,
    ) -> str | None:
        del trigger_dispatch
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append(
            {
                "room_id": room_id,
                "text": text,
                "thread_id": thread_id,
                "extra_content": extra_content,
            },
        )
        return self.send_result
