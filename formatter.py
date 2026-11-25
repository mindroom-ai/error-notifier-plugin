"""Matrix message formatting for MindRoom error notifications."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import ObservedError

_FRAME_RE = re.compile(r'^\s*(File "[^"]+", line \d+(?:, in .+)?)$')
_GUTTER_RE = re.compile(r"^(?:[ ]+\|)+(?: |$)")
_BACKTICK_RUN_RE = re.compile(r"`+")
_BORDER_CHARS = frozenset(" +-0123456789")
_CHAIN_MARKERS = (
    "Traceback (most recent call last):",
    "+ Exception Group Traceback (most recent call last):",
    "Exception Group Traceback (most recent call last):",
    "During handling of the above exception,",
    "The above exception was the direct cause",
)
_MAX_SUMMARY_CHARS = 300
_MAX_STACK_TRACE_CHARS = 16 * 1024


def format_alert(
    record: ObservedError,
    action: str | None,
    *,
    include_stack_traces: bool = False,
) -> str:
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
    body = "\n".join(lines)
    if include_stack_traces and record.exception:
        stack_trace = _format_stack_trace(record.exception)
        if stack_trace:
            return f"{body}\n\n{stack_trace}"
    return body


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


def _format_stack_trace(exception: str) -> str | None:
    content = exception.rstrip("\r\n")
    if not content.strip():
        return None
    bounded = _cap(content, _MAX_STACK_TRACE_CHARS)
    fence = _backtick_fence_for(bounded)
    return f"<details><summary>Stack trace</summary>\n\n{fence}\n{bounded}\n{fence}\n\n</details>"


def _backtick_fence_for(text: str) -> str:
    longest_run = max((len(match.group(0)) for match in _BACKTICK_RUN_RE.finditer(text)), default=0)
    return "`" * max(3, longest_run + 1)


def _timestamp(record: ObservedError) -> str:
    if record.timestamp:
        return record.timestamp
    return datetime.now(UTC).isoformat()


def _display_error(record: ObservedError) -> str | None:
    error = record.event_dict.get("error")
    if error is not None:
        return _escape_markdown_text(str(error))
    if record.exception_summary:
        return _escape_markdown_text(record.exception_summary)
    return None


def _is_border(line: str) -> bool:
    stripped = line.lstrip(" |").rstrip()
    return stripped.startswith("+-") and "---" in stripped and set(stripped) <= _BORDER_CHARS


def traceback_summary(exception: str) -> str | None:
    """Return a short summary (final exception line and raising frame) of a traceback."""
    lines = [
        cleaned
        for raw in exception.splitlines()
        if (cleaned := _GUTTER_RE.sub("", raw)).strip() and not _is_border(raw)
    ]
    if not lines:
        return None
    start = max((i + 1 for i, line in enumerate(lines) if line.strip().startswith(_CHAIN_MARKERS)), default=0)
    section = lines[start:] or lines
    frames = [(i, match.group(1)) for i, line in enumerate(section) if (match := _FRAME_RE.match(line))]
    if not frames:
        return _cap(section[0].strip(), _MAX_SUMMARY_CHARS)
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
