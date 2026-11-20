"""Matrix message formatting for MindRoom error notifications."""

from __future__ import annotations

from datetime import UTC, datetime

from mindroom.matrix.message_extras import (
    MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS,
    MatrixMessageExtra,
    build_message_extras_content,
)

from .models import ObservedError


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
        lines = record.exception.splitlines()
        if lines:
            return _escape_markdown_text(lines[0])
    return None


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
