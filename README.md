# Error Notifier

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

A [MindRoom](https://github.com/mindroom-ai/mindroom) hook plugin that posts a short alert to one Matrix room whenever MindRoom logs an error, with a suggested fix when it knows one.

```text
🚨 MindRoom error — 2026-05-20T21:29:40.123456+00:00
logger: mindroom.orchestrator
event:  Configuration update failed; will retry if a new change is queued
error:  Provisioning credentials are invalid or revoked.
action: Run `mindroom connect --pair-code <code>` from a paired session.
```

- **One alert per kind of error:** repeats inside the rate-limit window are counted and reported once, as `🔕 MindRoom error — suppressed 9 further occurrences of: …`.
- **Redacted:** every record goes through MindRoom's redactor before it leaves the log handler.
- **No setup in Matrix:** it sends through MindRoom into a room that already exists, and creates no rooms or clients.

## Quick start

```bash
mindroom plugins install error-notifier-plugin
```

```yaml
plugins:
  - path: plugins/error-notifier-plugin
    settings:
      ops_room: ops  # a room name, #alias:server or !id:server
```

Update later with `mindroom plugins update error-notifier-plugin`.
Needs MindRoom v2026.11.491 or later (plugin API 1.33).

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `ops_room` | `ops` | Where alerts go: a plain name (an alias on MindRoom's homeserver), `#alias:server`, or `!id:server`. |
| `rate_limit_seconds` | `300` | Window in which repeats of the same logger and event are suppressed. |
| `include_stack_traces` | `false` | Attach the traceback as a collapsed section of the alert. |
| `ignore_loggers` | none | Extra logger prefixes to ignore. |
| `extra_loggers` | none | Extra logger prefixes to watch, beyond `mindroom` and `livekit`. |
| `extra_events` | none | Extra structured event names to watch, from any logger. |

Set `MINDROOM_ERROR_NOTIFIER_DISABLED=1` to turn it off without editing the config.

<details>
<summary><b>What counts as an error</b></summary>

- Only records at `error` level.
- Every logger under `mindroom`, and `livekit` for calls.
- Not `mindroom.event_loop_stall`, which reports slowness at error level, and not the plugin's own logger.
- Not Agno's loggers: their errors are a model's bad tool arguments and failed web fetches, which the agent already sees.

A plain stdlib record is keyed by its unformatted message, so `sync failed: %s` is one error whatever its arguments; the formatted text appears on the `error:` line.

</details>

<details>
<summary><b>How it works</b></summary>

1. Importing the plugin installs a `logging.Handler` at `ERROR` on the root logger, where MindRoom routes both structlog and stdlib records. A reload replaces the earlier handler.
2. Each matching record is redacted and queued. Records logged before MindRoom is ready are buffered, at most 1000.
3. On the service-scoped `bot:ready`, a worker starts and sends the buffer, then each new record.
4. The first record of a `(logger, event)` pair is sent at once. Repeats inside `rate_limit_seconds` are counted; when the window ends, one summary is sent. A new occurrence after a quiet window sends the summary, then the fresh alert.
5. The `action:` line comes from a small table of known events and error texts (startup, agent start, config update, provisioning, plugin activation, model keys).

Restart MindRoom to apply changed plugin settings.

If the room cannot be resolved, the plugin logs one warning and drops alerts. A plugin reload retries the same destination on the next alert; it does not apply changed settings. Tracebacks are never inlined in the alert body.

</details>

<details>
<summary><b>Limits</b></summary>

- It sees only errors MindRoom logs; a crash that kills the process before logging sends nothing.
- Alerts go to a room that a running MindRoom bot can post in.
- `mindroom plugins check` warns that the plugin imports MindRoom helpers outside the stable plugin API (hooks, redaction, message extras); re-run it after each upgrade.

</details>

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `error-notifier-ready` | `bot:ready` (service) | Start the alert worker and send buffered errors |
