"""Data models and structured fingerprinting for the MindRoom error notifier plugin."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

DEFAULT_RATE_LIMIT_SECONDS = 300

_FRAME_RE = re.compile(r'^\s*(File "[^"]+", line \d+(?:, in .+)?)$')
_GUTTER_RE = re.compile(r"^(?:[ ]+\|)+(?: |$)")
_BORDER_CHARS = frozenset(" +-0123456789")
_CHAIN_MARKERS = (
    "Traceback (most recent call last):",
    "+ Exception Group Traceback (most recent call last):",
    "Exception Group Traceback (most recent call last):",
    "During handling of the above exception,",
    "The above exception was the direct cause",
)
_CLASS_SEG = r"[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*"
_TRACEBACK_CLASS_RE = re.compile(rf"^((?:[A-Za-z_][A-Za-z0-9_]*\.)*{_CLASS_SEG})(?=\(|:(?!//)|\Z)")
_ERROR_TEXT_CLASS_RE = re.compile(
    rf"^(?:((?:[A-Za-z_][A-Za-z0-9_]*\.)*{_CLASS_SEG})(?=\()"
    rf"|((?:[A-Za-z_][A-Za-z0-9_]*\.)+{_CLASS_SEG})(?=:(?!//)|\Z))"
)


class ErrorFingerprint(NamedTuple):
    """Deterministic, JSON-serializable fingerprint identifying one error class."""

    logger: str
    event: str
    subkey: str = ""
    error_type: str = ""

    def canonical(self) -> str:
        """Return the canonical JSON representation for persistence."""
        return json.dumps(list(self), separators=(",", ":"))


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
    exception_summary: str | None = None
    error_from_args: bool = False
    _fingerprint: ErrorFingerprint = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        fp = ErrorFingerprint(self.logger, self.event, _extract_subkey(self.event_dict), _extract_error_type(self))
        object.__setattr__(self, "_fingerprint", fp)

    @property
    def fingerprint(self) -> ErrorFingerprint:
        """Return the structured fingerprint for thread bundling and rate limiting."""
        return self._fingerprint


@dataclass(frozen=True, slots=True)
class ResolvedOpsRoom:
    """Resolved destination room for notifier alerts."""

    room_id: str
    label: str


def non_empty_str(value: object) -> str | None:
    """Return stripped text if value is a non-empty string, else None."""
    return stripped if isinstance(value, str) and (stripped := value.strip()) else None


def parse_traceback_tail(exception: str) -> tuple[list[tuple[int, str]], str | None, list[str]]:
    """Parse the final traceback chain section into `(frames, exc_line, section_lines)`."""
    lines = [
        cleaned
        for raw in exception.splitlines()
        if (cleaned := _GUTTER_RE.sub("", raw)).strip() and not _is_border(raw)
    ]
    if not lines:
        return [], None, []
    start = max((i + 1 for i, line in enumerate(lines) if line.strip().startswith(_CHAIN_MARKERS)), default=0)
    section = lines[start:] or lines
    frames = [(i, match.group(1)) for i, line in enumerate(section) if (match := _FRAME_RE.match(line))]
    if not frames:
        return [], None, section
    exc_line = next((line.strip() for line in section[frames[-1][0] + 1 :] if not line[0].isspace()), None)
    return frames, exc_line, section


def _is_border(line: str) -> bool:
    stripped = line.lstrip(" |").rstrip()
    return stripped.startswith("+-") and "---" in stripped and set(stripped) <= _BORDER_CHARS


def _extract_subkey(event_dict: Mapping[str, Any]) -> str:
    parts: list[str] = []
    plugin_name, hook_name = non_empty_str(event_dict.get("plugin_name")), non_empty_str(event_dict.get("hook_name"))
    if plugin_name and hook_name:
        parts.append(f"hook={plugin_name}/{hook_name}")
    elif plugin_name:
        parts.append(f"plugin={plugin_name}")
    elif hook_name:
        parts.append(f"hook={hook_name}")
    if tool := non_empty_str(event_dict.get("tool_name")) or non_empty_str(event_dict.get("tool")):
        parts.append(f"tool={tool}")
    if provider := non_empty_str(event_dict.get("provider")):
        parts.append(f"provider={provider}")
    return ",".join(parts)


def _extract_error_type(record: ObservedError) -> str:
    if explicit := non_empty_str(record.event_dict.get("error_type")) or non_empty_str(
        record.event_dict.get("exception_type")
    ):
        return explicit
    if record.exception:
        frames, exc_line, section = parse_traceback_tail(record.exception)
        candidates = [exc_line] if exc_line else ([] if frames else [line.strip() for line in section])
        for candidate in candidates:
            if matched := _match_traceback_class(candidate):
                return matched
    if record.exception_summary:
        if matched := _match_traceback_class(record.exception_summary.splitlines()[0].strip()):
            return matched
    if not record.error_from_args and isinstance(error := record.event_dict.get("error"), str):
        return _match_error_text_class(error.strip())
    return ""


def _match_traceback_class(text: str) -> str:
    match = _TRACEBACK_CLASS_RE.match(text)
    return "" if match is None or match.group(1) == "Traceback" else match.group(1)


def _match_error_text_class(text: str) -> str:
    match = _ERROR_TEXT_CLASS_RE.match(text)
    if match is None:
        return ""
    candidate = match.group(1) or match.group(2)
    return "" if candidate == "Traceback" else candidate
