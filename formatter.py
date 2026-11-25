"""Matrix message formatting for MindRoom error notifications."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import quote

from .models import ObservedError, non_empty_str, parse_traceback_tail

_FRAME_PATH_RE = re.compile(r'^File "([^"]+)"')
_BACKTICK_RUN_RE = re.compile(r"`+")
_MAX_SUMMARY_CHARS = 300
_MAX_STACK_TRACE_CHARS = 16 * 1024
_MARKDOWN_ESCAPES = str.maketrans({char: f"\\{char}" for char in r"\*_[]()#`"})


def format_alert(
    record: ObservedError,
    action: str | None,
    *,
    previous_thread: tuple[str, str] | None = None,
    recent_occurrences: int | None = None,
    include_stack_traces: bool = False,
) -> str:
    """Format one top-level root alert body."""
    component, headline = _title_parts(record)
    lines = [f"🚨 {component}: {headline}", f"time:    {timestamp_for(record)}", *_detail_lines(record, action)]
    if recent_occurrences is not None and recent_occurrences > 1:
        lines.append(f"occurrences: {recent_occurrences} in last window")
    if previous_thread is not None:
        room_id, prev_thread_id = previous_thread
        url = f"https://matrix.to/#/{quote(room_id, safe='')}/{quote(prev_thread_id, safe='')}"
        lines.append(f"reoccurred: [previous resolved thread]({url})")
    if include_stack_traces and record.exception and record.exception.strip():
        lines.extend(("", _format_stack_trace(record.exception)))
    return "\n".join(lines)


def format_thread_followup(
    record: ObservedError | None,
    *,
    count: int = 1,
    is_rollup: bool = False,
    last_error: str | None = None,
    include_stack_traces: bool = False,
) -> str:
    """Format one compact in-thread follow-up or rollup reply."""
    latest_ts = timestamp_for(record)
    if is_rollup or count > 1:
        noun = "occurrence" if count == 1 else "occurrences"
        header = f"🔁 +{count} {noun} (latest: {latest_ts})"
    else:
        header = f"🔁 Recurred — {latest_ts}"
    lines = [header]
    new_error = raw_error_text(record) if record is not None else None
    if new_error and (not last_error or new_error != last_error):
        lines.extend(_format_error_lines(_escape_markdown(new_error)))
    if include_stack_traces and record is not None and record.exception and record.exception.strip():
        lines.extend(("", _format_stack_trace(record.exception)))
    return "\n".join(lines)


def format_related_alert(
    record: ObservedError,
    action: str | None,
    *,
    include_stack_traces: bool = False,
) -> str:
    """Format one same-turn related cascade error reply."""
    component, headline = _title_parts(record)
    lines = [f"🔗 Related error (same turn) — {component}: {headline}", *_detail_lines(record, action)]
    if include_stack_traces and record.exception and record.exception.strip():
        lines.extend(("", _format_stack_trace(record.exception)))
    return "\n".join(lines)


def _format_stack_trace(exception: str) -> str:
    bounded = _cap(exception.rstrip("\r\n"), _MAX_STACK_TRACE_CHARS)
    fence = "`" * max(3, 1 + max(map(len, _BACKTICK_RUN_RE.findall(bounded)), default=0))
    return f"<details><summary>Stack trace</summary>\n\n{fence}\n{bounded}\n{fence}\n\n</details>"


def timestamp_for(record: ObservedError | None) -> str:
    """Return the record timestamp or current UTC ISO timestamp."""
    return record.timestamp if record is not None and record.timestamp else datetime.now(UTC).isoformat()


def raw_error_text(record: ObservedError) -> str | None:
    """Return the unescaped display error text for one record, if available."""
    if (error := record.event_dict.get("error")) is not None and (text := str(error)):
        return text
    return record.exception_summary or None


def traceback_summary(exception: str) -> str | None:
    """Return a short summary (final exception line and preferred frame) of a traceback."""
    frames, exc_line, section = parse_traceback_tail(exception)
    if not section:
        return None
    if not frames:
        return _cap(section[0].strip(), _MAX_SUMMARY_CHARS)
    frame = next((candidate for _, candidate in reversed(frames) if _is_mindroom_frame(candidate)), frames[-1][1])
    if not exc_line:
        return _cap(frame, _MAX_SUMMARY_CHARS)
    budget = _MAX_SUMMARY_CHARS - len(frame) - 1
    if budget >= 40:
        return f"{_cap(exc_line, budget)}\n{frame}"
    return _cap(f"{exc_line}\n{frame}", _MAX_SUMMARY_CHARS)


def _is_mindroom_frame(frame: str) -> bool:
    match = _FRAME_PATH_RE.match(frame)
    if match is None:
        return False
    path = match.group(1).replace("\\", "/")
    for pkg_marker in ("/site-packages/", "/dist-packages/"):
        if pkg_marker in path:
            return path.rsplit(pkg_marker, 1)[1].startswith("mindroom/")
    if "/.venv/" in path or "/python3." in path:
        return False
    return "/mindroom/" in path or path.startswith("mindroom/")


def _escape_markdown(text: str) -> str:
    return text.translate(_MARKDOWN_ESCAPES)


def _title_parts(record: ObservedError) -> tuple[str, str]:
    fp = record.fingerprint
    subkey_display = (
        ", ".join(_escape_markdown(part.split("=", 1)[-1]) for part in fp.subkey.split(",")) if fp.subkey else ""
    )
    logger_md = _escape_markdown(record.logger)
    component = f"{logger_md} ({subkey_display})" if subkey_display else logger_md
    return component, _escape_markdown(fp.error_type or record.event)


def _detail_lines(record: ObservedError, action: str | None) -> list[str]:
    lines = [f"logger:  {record.logger}", f"event:   {record.event}"]
    if context_line := _format_context(record):
        lines.append(f"context: {context_line}")
    if raw_error := raw_error_text(record):
        lines.extend(_format_error_lines(_escape_markdown(raw_error)))
    if action:
        lines.append(f"action:  {action}")
    return lines


def _format_context(record: ObservedError) -> str:
    event_dict = record.event_dict
    fields = (
        ("plugin", non_empty_str(event_dict.get("plugin_name"))),
        ("hook", non_empty_str(event_dict.get("hook_name"))),
        ("tool", non_empty_str(event_dict.get("tool_name")) or non_empty_str(event_dict.get("tool"))),
        ("provider", non_empty_str(event_dict.get("provider"))),
        ("agent", non_empty_str(event_dict.get("agent"))),
    )
    return " ".join(f"{label}={_escape_markdown(value)}" for label, value in fields if value)


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _format_error_lines(error_text: str) -> list[str]:
    first, *rest = error_text.splitlines()
    return [f"error:   {first}", *(f"         {line}" for line in rest)]
