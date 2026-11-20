"""Data models for the MindRoom error notifier plugin."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_RATE_LIMIT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class NotifierSettings:
    """Runtime settings for the error notifier."""

    ops_room: str = "ops"
    rate_limit_seconds: int = DEFAULT_RATE_LIMIT_SECONDS
    extra_loggers: tuple[str, ...] = ()
    extra_events: tuple[str, ...] = ()
    ignore_loggers: tuple[str, ...] = ()
    include_stack_traces: bool = False


@dataclass(frozen=True, slots=True)
class ObservedError:
    """One redacted structured error log event observed by the plugin."""

    logger: str
    level: str
    event: str
    timestamp: str | None
    event_dict: dict[str, Any] = field(default_factory=dict)
    exception: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Return the rate-limit key."""
        return (self.logger, self.event)


@dataclass(frozen=True, slots=True)
class ResolvedOpsRoom:
    """Resolved destination room for notifier alerts."""

    room_id: str
    label: str
