# ruff: noqa: D103
"""Tests for error-notifier rule matching."""

from __future__ import annotations

from error_notifier_test_import.models import NotifierSettings, ObservedError
from error_notifier_test_import.rules import action_for, matches, settings_from_mapping


def _record(**overrides: object) -> ObservedError:
    values = {
        "logger": "mindroom.orchestrator",
        "level": "error",
        "event": "config_update_failed",
        "timestamp": "2026-05-20T21:29:40+00:00",
        "event_dict": {},
        "exception": None,
    }
    values.update(overrides)
    return ObservedError(**values)


def test_default_loggers_match_error_level() -> None:
    assert matches(_record(event="arbitrary_event"), NotifierSettings())


def test_non_error_levels_are_rejected() -> None:
    assert not matches(_record(level="warning"), NotifierSettings())


def test_every_mindroom_and_livekit_logger_matches() -> None:
    for logger in ("mindroom.orchestration.runtime", "mindroom.tool_system.plugins", "livekit.agents"):
        assert matches(_record(logger=logger, event="sync_loop_failed"), NotifierSettings())


def test_other_libraries_do_not_match() -> None:
    assert not matches(_record(logger="agno", event="1 validation error for SkillTools"), NotifierSettings())
    assert not matches(_record(logger="nio.client", event="sync failed"), NotifierSettings())
    assert not matches(_record(logger="mindroomx", event="sync failed"), NotifierSettings())


def test_event_loop_stalls_are_ignored() -> None:
    record = _record(logger="mindroom.event_loop_stall", event="event_loop_stall_detected")
    assert not matches(record, NotifierSettings())


def test_ignore_loggers_extend_the_ignored_set() -> None:
    settings = NotifierSettings(ignore_loggers=("mindroom.voice",))

    assert not matches(_record(logger="mindroom.voice.handler"), settings)
    assert matches(_record(logger="mindroom.voice_other"), settings)


def test_extra_loggers_extend_rule_set() -> None:
    settings = NotifierSettings(extra_loggers=("custom.logger",))

    assert matches(_record(logger="custom.logger.child", event="custom_event"), settings)


def test_extra_events_extend_rule_set() -> None:
    settings = NotifierSettings(extra_events=("custom_event",))

    assert matches(_record(logger="other.module", event="custom_event"), settings)


def test_feedback_loop_guard_rejects_plugin_logger() -> None:
    assert not matches(_record(logger="mindroom.plugins.error_notifier"), NotifierSettings())


def test_feedback_loop_guard_rejects_worker_logger() -> None:
    assert not matches(_record(logger="mindroom.plugins.error_notifier.worker"), NotifierSettings())


def test_feedback_loop_guard_keeps_other_plugin_hook_failures() -> None:
    record = _record(
        logger="mindroom.bot",
        event="Failed to send hook message",
        event_dict={"source_hook": "other-plugin:bot:ready"},
    )
    assert matches(record, NotifierSettings())


def test_missing_logger_or_event_is_rejected() -> None:
    assert not matches(_record(logger=""), NotifierSettings())
    assert not matches(_record(event=""), NotifierSettings())


def test_disabled_env_rejects_records(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MINDROOM_ERROR_NOTIFIER_DISABLED", "1")

    assert not matches(_record(), NotifierSettings())


def test_action_for_event_takes_priority_over_substring() -> None:
    record = _record(
        event="plugin_activation_failed",
        event_dict={"error": "Provisioning credentials are invalid or revoked"},
    )

    assert action_for(record) and "plugin manifest" in action_for(record)


def test_action_for_substring_match() -> None:
    record = _record(event="unknown", event_dict={"error": "invalid_api_key from provider"})

    assert action_for(record) == "Refresh the model provider API key in credentials and retry the request."


def test_action_for_unknown_record_returns_none() -> None:
    assert action_for(_record(event="unknown", event_dict={"error": "plain failure"})) is None


def test_settings_from_mapping_parses_supported_keys() -> None:
    settings = settings_from_mapping(
        {
            "ops_room": " dev ",
            "rate_limit_seconds": "5",
            "extra_loggers": ["x", "", 1],
            "extra_events": "custom_event",
            "ignore_loggers": ["mindroom.voice"],
            "include_stack_traces": "true",
        },
    )

    assert settings.ops_room == "dev"
    assert settings.rate_limit_seconds == 5
    assert settings.extra_loggers == ("x",)
    assert settings.extra_events == ("custom_event",)
    assert settings.ignore_loggers == ("mindroom.voice",)
    assert settings.include_stack_traces


def test_settings_from_mapping_uses_defaults_for_invalid_values() -> None:
    settings = settings_from_mapping(
        {
            "ops_room": "",
            "rate_limit_seconds": "not-an-int",
            "extra_loggers": 123,
            "include_stack_traces": "unknown",
        },
    )

    assert settings == NotifierSettings()


def test_settings_from_mapping_handles_bool_and_false_string() -> None:
    settings = settings_from_mapping(
        {
            "rate_limit_seconds": True,
            "extra_events": ["x", 1, ""],
            "include_stack_traces": "false",
        },
    )

    assert settings.rate_limit_seconds == 300
    assert settings.extra_events == ("x",)
    assert not settings.include_stack_traces


def test_action_for_uses_exception_when_error_field_missing() -> None:
    record = _record(event="unknown", event_dict={}, exception="Provisioning credentials are invalid or revoked")

    assert action_for(record) == "Run `mindroom connect --pair-code <code>` from a paired session."


def test_action_for_without_error_or_exception_returns_none() -> None:
    assert action_for(_record(event="unknown", event_dict={}, exception=None)) is None
