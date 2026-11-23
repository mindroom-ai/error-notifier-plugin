"""Matrix message formatting for MindRoom error notifications."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from mindroom.matrix.message_extras import (
    MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS,
    MatrixMessageExtra,
    build_message_extras_content,
)

from .models import ObservedError

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
_MAX_SUMMARY_CHARS = 300


def format_alert(record: ObservedError, action: str | None) -> str:
    """Format one alert body."""
    lines = [
        f"🚨 MindRoom error — {_timestamp(record)}",
        f"logger: {record.logger}",
        f"event:  {record.event}",
    ]
    error_text = _display_error(record)
    if error_text:
        lines.extend(_format_error_lines(error_text))
    if action:
        lines.append(f"action: {action}")
    return "\n".join(lines)


def format_suppression_summary(key: tuple[str, str], count: int) -> str:
    """Format a suppression summary body."""
    logger_name, event = key
    return "\n".join(
        [
            f"🔕 MindRoom error — suppressed {count} further occurrences of:",
            f"logger: {logger_name}",
            f"event:  {event}",
        ],
    )


def extra_content_for(record: ObservedError, include_stack_traces: bool) -> dict[str, object] | None:
    """Build collapsed traceback extras when stack traces are enabled."""
    if not include_stack_traces or not record.exception:
        return None
    content = record.exception[:MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS]
    return build_message_extras_content(
        [
            MatrixMessageExtra(
                title="Stack trace",
                content=content,
                content_type="text/markdown",
                collapsed=True,
            ),
        ],
    )


def _timestamp(record: ObservedError) -> str:
    if record.timestamp:
        return record.timestamp
    return datetime.now(UTC).isoformat()


def _display_error(record: ObservedError) -> str | None:
    error = record.event_dict.get("error")
    if error is not None:
        return _escape_markdown_text(str(error))
    if record.exception:
        summary = _traceback_summary(record.exception)
        if summary:
            return _escape_markdown_text(summary)
    return None


def _is_border(line: str) -> bool:
    stripped = line.lstrip(" |").rstrip()
    return stripped.startswith("+-") and "---" in stripped and set(stripped) <= _BORDER_CHARS


def _traceback_summary(exception: str) -> str | None:
    truncated = "\n…\n" in exception
    if truncated:
        exception = exception.rsplit("\n…\n", maxsplit=1)[1]
    lines = [
        cleaned
        for raw in exception.splitlines()
        if (cleaned := _GUTTER_RE.sub("", raw)).strip() and not _is_border(raw)
    ]
    if not lines:
        return "…" if truncated else None
    start = max((i + 1 for i, line in enumerate(lines) if line.strip().startswith(_CHAIN_MARKERS)), default=0)
    section = lines[start:] or lines
    frames = [(i, match.group(1)) for i, line in enumerate(section) if (match := _FRAME_RE.match(line))]
    if not frames:
        line = section[-1] if truncated else section[0]
        return _cap(line.strip(), _MAX_SUMMARY_CHARS)
    frame_idx, frame = frames[-1]
    exc_line = next((line.strip() for line in section[frame_idx + 1 :] if not line[0].isspace()), None)
    if not exc_line:
        return _cap(frame, _MAX_SUMMARY_CHARS)
    budget = _MAX_SUMMARY_CHARS - len(frame) - 1
    if budget >= 40:
        return f"{_cap(exc_line, budget)}\n{frame}"
    return _cap(f"{exc_line}\n{frame}", _MAX_SUMMARY_CHARS)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _format_error_lines(error_text: str) -> list[str]:
    first, *rest = error_text.splitlines()
    lines = [f"error:  {first}"]
    lines.extend(f"        {line}" for line in rest)
    return lines


def _escape_markdown_text(value: str) -> str:
    replacements = {
        "\\": "\\\\",
        "*": "\\*",
        "_": "\\_",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        "#": "\\#",
        "`": "\\`",
    }
    return "".join(replacements.get(char, char) for char in value)
