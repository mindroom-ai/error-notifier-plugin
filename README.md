# Error Notifier

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/error-notifier-plugin/actions/workflows/plugin-ci.yml/badge.svg)](https://github.com/mindroom-ai/error-notifier-plugin/actions/workflows/plugin-ci.yml)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

A [MindRoom](https://github.com/mindroom-ai/mindroom) hook plugin that posts scannable, Sentry-style bundled alerts to one Matrix room whenever MindRoom logs an error, with a suggested fix when it knows one.

```text
🚨 mindroom.orchestrator: Configuration update failed; will retry if a new change is queued
time:    2026-05-20T21:29:40.123456+00:00
logger:  mindroom.orchestrator
event:   Configuration update failed; will retry if a new change is queued
error:   Provisioning credentials are invalid or revoked.
action:  Run `mindroom connect --pair-code <code>` from a paired session.
```

Repeats and same-turn cascades stay inside that alert's Matrix thread instead of flooding the room:

```text
🔁 +9 occurrences (latest: 2026-05-20T21:34:40.123456+00:00)
```

```text
🔗 Related error (same turn) — mindroom.ai (openrouter): ProviderUnavailable
logger:  mindroom.ai
event:   model_invocation_failed
context: provider=openrouter
error:   ProviderUnavailable('Connection error.')
```

- **One open thread per error class:** errors are fingerprinted by `(logger, event, subkey, error_type)` (including plugin/hook, tool, provider, and exception class without variable message text). Repeats reply inside the open thread and persist across restarts in `open_threads.json`.
- **Resolved-thread reopening:** before replying into a tracked thread, the plugin checks MindRoom's `resolved` thread tag (`com.mindroom.thread.tags`). If resolved, a new occurrence opens a fresh top-level alert with a link to the previous resolved thread and resets backoff; stale sweep-only summaries are dropped.
- **Same-turn cascade bundling:** secondary errors sharing a `correlation_id` within 60 seconds post as compact `🔗 Related error (same turn)` replies in the primary thread without overwriting their own standalone thread mapping.
- **Exponential backoff:** repeats are batched starting at `rate_limit_seconds` and backing off by `1× → 3× → 12× → 72×` (capped at 6 hours, e.g. 5m → 15m → 1h → 6h), resetting when resolved or after a quiet window.
- **Redacted & no Matrix setup:** every record goes through MindRoom's redactor before leaving the log handler, and alerts post through MindRoom into an existing room.

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
| `rate_limit_seconds` | `300` | Initial rollup window per open thread; backs off exponentially (`1×`, `3×`, `12×`, `72×`, capped at 6h). |
| `include_stack_traces` | `false` | Include the traceback as a collapsed `<details>` section of the alert. |
| `ignore_loggers` | none | Extra logger prefixes to ignore. |
| `extra_loggers` | none | Extra logger prefixes to watch, beyond `mindroom` and `livekit`. |
| `extra_events` | none | Extra structured event names to watch, from any logger. |

Set `MINDROOM_ERROR_NOTIFIER_DISABLED=1` to turn it off without editing the config.

<details>
<summary><b>What counts as an error</b></summary>

- Only records at `error` level.
- Every logger under `mindroom`, and `livekit` for calls.
- Not `mindroom.event_loop_stall`, which reports slowness at error level, not the plugin's own logger, and not errors logged while the plugin itself is sending an alert or checking thread state.
- Not Agno's loggers: their errors are a model's bad tool arguments and failed web fetches, which the agent already sees.

A plain stdlib record is keyed by its unformatted message, so `sync failed: %s` is one error whatever its arguments; the formatted text appears on the `error:` line.

</details>

<details>
<summary><b>How it works</b></summary>

1. Importing the plugin installs a `logging.Handler` at `ERROR` on the root logger, where MindRoom routes both structlog and stdlib records. A reload replaces the earlier handler.
2. Each matching record is redacted and queued. Records logged before MindRoom is ready are buffered, at most 1000.
3. On the service-scoped `bot:ready`, a worker starts, loads persisted open threads from `ctx.state_root / "open_threads.json"`, and drains the buffer.
4. Each record is fingerprinted as `(logger, event, subkey, error_type)`. The first occurrence opens a top-level alert with a scannable `🚨 {component}: {headline}` title; traceback summaries prefer the last MindRoom frame over stdlib or third-party internals.
5. Repeats inside the active backoff window are counted and rolled up into the open thread as compact `🔁` replies (repeating `error:` only when the error text changes). After a quiet window with no repeats, the rate-limit bucket is pruned while the persisted thread stays open.
6. Before posting a follow-up or rollup, the worker queries MindRoom's `com.mindroom.thread.tags` state for `resolved`. Resolved threads are replaced when a new top-level alert opens, linking back to the previous resolved thread; stale pre-resolution sweep summaries are dropped. If the state query fails or is unavailable, the plugin fails safe and keeps replying in the existing thread.
7. Different fingerprints sharing a `correlation_id` within 60 seconds are still rate-limited and posted as `🔗 Related error (same turn)` replies inside the primary open thread without taking over their own root mapping.
8. The `action:` line comes from a small table of known events and error texts (startup, agent start, config update, provisioning, plugin activation, model keys).

If the room cannot be resolved, the plugin logs one warning and drops alerts. A plugin reload retries the same destination on the next alert, but applying changed plugin settings requires restarting MindRoom. Tracebacks are omitted unless `include_stack_traces` is enabled, in which case they are included in a collapsed `<details>` block.

</details>

<details>
<summary><b>Limits</b></summary>

- It sees only errors MindRoom logs; a crash that kills the process before logging sends nothing.
- Alerts go to a room that a running MindRoom bot can post in.
- If the homeserver returns no event id, an alert may be retried once after a short delay (a rare duplicate is possible).
- `mindroom plugins check` warns that the plugin imports MindRoom's redaction helper outside the stable plugin API; re-run it after each upgrade.

</details>

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `error-notifier-ready` | `bot:ready` (service) | Start the alert worker and send buffered errors |
