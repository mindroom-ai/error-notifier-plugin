# MindRoom Error Notifier

The error-notifier plugin watches redacted MindRoom error logs and sends concise operational alerts to one existing Matrix room.

Install it by cloning this repository into your MindRoom plugins directory, for example `plugins/error-notifier`, and listing it under `plugins:` as below.

It does not create rooms.

It does not create Matrix clients.

It does not retry failed MindRoom operations.

It only sends through the router hook context with `ctx.send_message`.

It watches errors with a stdlib `logging.Handler` at ERROR on the root logger, where MindRoom routes every structlog and stdlib record, and redacts each record with `mindroom.redaction` before it leaves the handler.

Importing the plugin installs the handler, replacing one an earlier import left.

Alerts start once the router's `bot:ready` hook runs, which happens at MindRoom start; errors before that are buffered (at most 1000).

## Configuration

Add the plugin through `config:` or `settings:`.

```yaml
plugins:
  - path: plugins/error-notifier
    config:
      ops_room: ops
      rate_limit_seconds: 300
      include_stack_traces: false
      extra_loggers: []
      extra_events: []
      ignore_loggers: []
```

`ops_room` defaults to `ops`.

Plain room names are resolved as aliases on the router homeserver.

Full room IDs such as `!abc:server` are used directly.

Room aliases such as `#ops:server` are resolved through the existing Matrix admin binding.

If the room cannot be resolved, the plugin logs one stdlib warning and drops notifications until the config changes.

`rate_limit_seconds` defaults to `300`.

The first `(logger, event)` pair emits immediately.

Further matching occurrences inside the rate-limit window are suppressed.

When the window expires, the worker emits one suppression summary.

If a new occurrence arrives after a quiet suppressed window, the worker emits the suppression summary and then the fresh alert.

`include_stack_traces` defaults to `false`.

When enabled, tracebacks are attached as one collapsed `text/markdown` message extra.

Tracebacks are never inlined into the main alert body.

`extra_loggers` extends the default logger prefixes.

`extra_events` extends the default structured event names.

Set `MINDROOM_ERROR_NOTIFIER_DISABLED=1` to disable the log handler and hooks.

## Default Matching

The plugin only matches records with `level: error`.

It matches every logger under `mindroom` and `livekit` (calls).

It ignores `mindroom.event_loop_stall` (slowness reported at error level) and its own `mindroom.plugins.error_notifier` loggers.

Agno's loggers are left out: their errors are a model's bad tool arguments and failed web fetches, worded per call.

`ignore_loggers` adds logger prefixes to ignore; `extra_loggers` and `extra_events` add matches outside `mindroom` and `livekit`.

A plain stdlib record is keyed by its unformatted message, so `sync failed: %s` is one rate-limited error whatever its arguments; the formatted text appears on the `error:` line.

## Alert Shape

Alerts use this body shape.

```text
🚨 MindRoom error — 2026-05-20T21:29:40.123456+00:00
logger: mindroom.orchestrator
event:  Configuration update failed; will retry if a new change is queued
error:  Provisioning credentials are invalid or revoked.
        Run `mindroom connect --pair-code ...` again.
action: Run `mindroom connect --pair-code <code>` from a paired session.
```

Suppression summaries use this body shape.

```text
🔕 MindRoom error — suppressed 9 further occurrences of:
logger: mindroom.orchestrator
event:  Configuration update failed; will retry if a new change is queued
```

## Live Test

For burst testing, temporarily install the throwaway `error-notifier-test` plugin.

Configure `rate_limit_seconds: 5` during the live test.

Send `!fire-errors 10` to produce one immediate alert and one suppression summary.

Send `!simulate-provisioning-failure` to verify the provisioning remediation action.

Delete `~/.mindroom-chat/plugins/error-notifier-test/` after the live test.
