"""Filtering rules and remediation actions for MindRoom error notifications."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .models import DEFAULT_RATE_LIMIT_SECONDS, NotifierSettings, ObservedError

FEEDBACK_LOOP_LOGGER_PREFIX = "mindroom.plugins.error_notifier"
# Every MindRoom logger, and LiveKit's for calls. Agno is left out: its errors are a model's bad tool arguments and
# failed web fetches, which the agent already sees, worded per call.
DEFAULT_LOGGERS = ("mindroom", "livekit")
# MindRoom logs event-loop stalls at error level, but they report slowness, not a failure.
DEFAULT_IGNORED_LOGGERS = ("mindroom.event_loop_stall",)

ACTIONS_BY_EVENT = {
    "startup_step_failed_permanently": (
        "Inspect the permanent startup error above; fix configuration or credentials, then restart MindRoom."
    ),
    "agent_start_failed": (
        "Check the named agent's model, Matrix account, tools, and room configuration, then trigger a config reload."
    ),
    "agent_start_failed_permanently": (
        "Permanent agent startup failure - credentials, model, or Matrix account are invalid; fix configuration and restart."
    ),
    "config_update_failed": (
        "Inspect the latest config diff in ~/.mindroom-chat/config.yaml; previous valid config is still active."
    ),
    "model_invocation_failed_permanent": "Refresh the model provider API key in credentials and retry the request.",
    "knowledge_base_index_failed": (
        "Check the knowledge-base path and file contents, then touch the file or reload the knowledge base."
    ),
    "plugin_activation_failed": (
        "Inspect the plugin manifest at the indicated path; disable the failing plugin or re-run with --log-level DEBUG to see import errors."
    ),
    "fatal_orchestrator_exit": (
        "MindRoom orchestrator exited. Inspect the recent logs in mindroom_data/logs/ and restart the service."
    ),
    "fatal_embedded_api_server_exit": "Embedded API server died. Check for port conflicts on 8765 and re-run.",
}

ACTIONS_BY_ERROR_SUBSTRING = (
    (
        "Provisioning credentials are invalid or revoked",
        "Run `mindroom connect --pair-code <code>` from a paired session.",
    ),
    (
        "MINDROOM_PROVISIONING_URL is set but local client credentials are missing",
        "Run `mindroom connect --pair-code <code>` before provisioning hosted Matrix accounts.",
    ),
    (
        "Provisioning credentials are incomplete",
        "Check `MINDROOM_LOCAL_CLIENT_ID` and `MINDROOM_LOCAL_CLIENT_SECRET`, or run `mindroom connect --pair-code <code>` again.",
    ),
    (
        "Plugin module execution failed",
        "Disable or fix the failing plugin, then run `!reload-plugins` or restart MindRoom.",
    ),
    (
        "Failed to index knowledge file",
        "Check the knowledge-base path and file contents, then touch the file or reload the knowledge base.",
    ),
    ("invalid_api_key", "Refresh the model provider API key in credentials and retry the request."),
)


def settings_from_mapping(raw: Mapping[str, object] | None) -> NotifierSettings:
    """Build notifier settings from hook settings."""
    data = dict(raw or {})
    return NotifierSettings(
        ops_room=_string_setting(data, "ops_room", "ops"),
        rate_limit_seconds=max(1, _int_setting(data, "rate_limit_seconds", DEFAULT_RATE_LIMIT_SECONDS)),
        extra_loggers=_string_tuple_setting(data.get("extra_loggers")),
        extra_events=_string_tuple_setting(data.get("extra_events")),
        ignore_loggers=_string_tuple_setting(data.get("ignore_loggers")),
        include_stack_traces=_bool_setting(data.get("include_stack_traces"), False),
    )


def matches(record: ObservedError, settings: NotifierSettings) -> bool:
    """Return whether one observed record should be considered for notification."""
    if os.getenv("MINDROOM_ERROR_NOTIFIER_DISABLED") == "1":
        return False
    if record.level != "error":
        return False
    if not record.logger or not record.event:
        return False
    ignored = (FEEDBACK_LOOP_LOGGER_PREFIX, *DEFAULT_IGNORED_LOGGERS, *settings.ignore_loggers)
    if any(_logger_matches(record.logger, logger_name) for logger_name in ignored):
        return False

    loggers = (*DEFAULT_LOGGERS, *settings.extra_loggers)
    return any(_logger_matches(record.logger, logger_name) for logger_name in loggers) or record.event in settings.extra_events


def action_for(record: ObservedError) -> str | None:
    """Return the remediation action for one observed record."""
    action = ACTIONS_BY_EVENT.get(record.event)
    if action is not None:
        return action

    error_text = _error_text(record)
    if not error_text:
        return None
    for needle, candidate in ACTIONS_BY_ERROR_SUBSTRING:
        if needle in error_text:
            return candidate
    return None


def _error_text(record: ObservedError) -> str | None:
    error = record.event_dict.get("error")
    if error is not None:
        return str(error)
    return record.exception


def _logger_matches(logger_name: str, prefix: str) -> bool:
    normalized = prefix.strip()
    return bool(normalized) and (logger_name == normalized or logger_name.startswith(f"{normalized}."))


def _string_setting(data: Mapping[str, object], key: str, default: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        return default
    normalized = value.strip()
    return normalized or default


def _int_setting(data: Mapping[str, object], key: str, default: int) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _string_tuple_setting(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, list | tuple | set):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _bool_setting(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default
