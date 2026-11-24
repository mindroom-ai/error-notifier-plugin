"""Room-scoped open thread persistence and same-turn correlation tracking."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import ErrorFingerprint, non_empty_str

OPEN_THREADS_FILENAME = "open_threads.json"
MAX_THREADS_PER_ROOM = 500
CORRELATION_TTL_SECONDS = 60.0
CORRELATION_MAX_ENTRIES = 500
_WARNING_LOGGER = logging.getLogger("mindroom.plugins.error_notifier.worker")


@dataclass(frozen=True, slots=True)
class OpenThread:
    """One tracked open alert thread for a room and error fingerprint."""

    thread_id: str
    last_error: str | None = None
    last_seen_at: str = ""


@dataclass(frozen=True, slots=True)
class CorrelationEntry:
    """One active same-turn correlation mapping to a primary alert thread."""

    thread_id: str
    primary_fingerprint: ErrorFingerprint
    recorded_at: float


class ThreadStore:
    """Persist `room_id -> canonical_fingerprint -> OpenThread` and track recent correlations."""

    def __init__(
        self,
        state_root: Path | None = None,
        *,
        max_per_room: int = MAX_THREADS_PER_ROOM,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = None if state_root is None else state_root / OPEN_THREADS_FILENAME
        self._max_per_room = max(1, max_per_room)
        self._clock = clock
        self._rooms: dict[str, dict[str, OpenThread]] = {}
        self._correlations: dict[tuple[str, str], CorrelationEntry] = {}
        self._cascade_emits: dict[tuple[str, ErrorFingerprint], float] = {}
        if self._path is not None:
            self._load()

    @classmethod
    def from_context(cls, ctx: object, *, clock: Callable[[], float] = time.monotonic) -> ThreadStore:
        """Build a store using `ctx.state_root` when available, else in-memory."""
        try:
            raw = getattr(ctx, "state_root", None)
        except Exception as exc:
            _WARNING_LOGGER.warning("error-notifier failed to read ctx.state_root: %s", exc)
            raw = None
        root = raw if isinstance(raw, Path) else (Path(raw) if isinstance(raw, str) and raw.strip() else None)
        return cls(root, clock=clock)

    def get(self, room_id: str, fingerprint: ErrorFingerprint) -> OpenThread | None:
        """Return the open thread recorded for `(room_id, fingerprint)`, if any."""
        return self._rooms.get(room_id, {}).get(fingerprint.canonical())

    def record(
        self, room_id: str, fingerprint: ErrorFingerprint, thread_id: str, last_error: str | None, last_seen_at: str
    ) -> OpenThread:
        """Record or update the open thread for `(room_id, fingerprint)` and persist."""
        room_map = self._rooms.setdefault(room_id, {})
        key = fingerprint.canonical()
        room_map.pop(key, None)
        entry = OpenThread(thread_id, last_error, last_seen_at)
        room_map[key] = entry
        self._prune_room(room_map)
        self._save()
        return entry

    def forget(self, room_id: str, fingerprint: ErrorFingerprint) -> OpenThread | None:
        """Remove a tracked thread for `(room_id, fingerprint)` and persist if changed."""
        room_map = self._rooms.get(room_id)
        if not room_map or (removed := room_map.pop(fingerprint.canonical(), None)) is None:
            return None
        if not room_map:
            self._rooms.pop(room_id, None)
        self._save()
        return removed

    def record_correlation(
        self, room_id: str, corr_id: str | None, thread_id: str, primary_fingerprint: ErrorFingerprint
    ) -> None:
        """Record or refresh a same-turn correlation mapping."""
        if not corr_id:
            return
        now = self._clock()
        self._prune_correlations(now)
        key = (room_id, corr_id)
        prev = self._correlations.pop(key, None)
        if prev is None or prev.primary_fingerprint == primary_fingerprint:
            self._correlations[key] = CorrelationEntry(thread_id, primary_fingerprint, now)
        else:
            self._correlations[key] = CorrelationEntry(prev.thread_id, prev.primary_fingerprint, now)
        while len(self._correlations) > CORRELATION_MAX_ENTRIES:
            self._correlations.pop(next(iter(self._correlations)))

    def lookup_correlation(self, room_id: str, corr_id: str | None) -> CorrelationEntry | None:
        """Return an unexpired correlation entry for `(room_id, corr_id)`, if any."""
        if not corr_id:
            return None
        self._prune_correlations(self._clock())
        return self._correlations.get((room_id, corr_id))

    def forget_correlation(self, room_id: str, corr_id: str) -> None:
        """Forget one correlation mapping."""
        self._correlations.pop((room_id, corr_id), None)

    def is_cascade_throttled(self, thread_id: str, fingerprint: ErrorFingerprint, window_seconds: int) -> bool:
        """Return whether a cascade reply for `(thread_id, fingerprint)` was already sent in this window."""
        now = self._clock()
        self._prune_cascade_emits(now, window_seconds)
        last = self._cascade_emits.get((thread_id, fingerprint))
        return last is not None and now - last < max(1, window_seconds)

    def record_cascade_emit(self, thread_id: str, fingerprint: ErrorFingerprint, window_seconds: int) -> None:
        """Record a cascade reply emission timestamp for `(thread_id, fingerprint)`."""
        now = self._clock()
        self._prune_cascade_emits(now, window_seconds)
        key = (thread_id, fingerprint)
        self._cascade_emits.pop(key, None)
        self._cascade_emits[key] = now
        while len(self._cascade_emits) > CORRELATION_MAX_ENTRIES:
            self._cascade_emits.pop(next(iter(self._cascade_emits)))

    def _prune_correlations(self, now: float) -> None:
        for key in [k for k, e in self._correlations.items() if now - e.recorded_at >= CORRELATION_TTL_SECONDS]:
            self._correlations.pop(key, None)

    def _prune_cascade_emits(self, now: float, window_seconds: int) -> None:
        ttl = max(1, window_seconds)
        for key in [k for k, ts in self._cascade_emits.items() if now - ts >= ttl]:
            self._cascade_emits.pop(key, None)

    def _prune_room(self, room_map: dict[str, OpenThread]) -> None:
        excess = len(room_map) - self._max_per_room
        if excess <= 0:
            return
        oldest = sorted(enumerate(room_map.items()), key=lambda item: (item[1][1].last_seen_at, item[0]))[:excess]
        for _, (fp_key, _) in oldest:
            room_map.pop(fp_key, None)

    def _load(self) -> None:
        assert self._path is not None
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _WARNING_LOGGER.warning("error-notifier failed to load %s; starting fresh: %s", self._path, exc)
            return
        if not isinstance(raw, dict):
            _WARNING_LOGGER.warning("error-notifier found invalid %s format; starting fresh", OPEN_THREADS_FILENAME)
            return

        loaded: dict[str, dict[str, OpenThread]] = {}
        corrupt = 0
        for room_id, entries in raw.items():
            if not isinstance(room_id, str) or not isinstance(entries, dict):
                corrupt += 1
                continue
            parsed: dict[str, OpenThread] = {}
            for fp_key, item in entries.items():
                if isinstance(fp_key, str) and (entry := _parse_open_thread(item)) is not None:
                    parsed[fp_key] = entry
                else:
                    corrupt += 1
            if parsed:
                self._prune_room(parsed)
                loaded[room_id] = parsed
        if corrupt:
            _WARNING_LOGGER.warning("error-notifier skipped %d corrupt entries while loading %s", corrupt, self._path)
        self._rooms = loaded

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {
            room_id: {
                k: {"thread_id": e.thread_id, "last_error": e.last_error, "last_seen_at": e.last_seen_at}
                for k, e in entries.items()
            }
            for room_id, entries in self._rooms.items()
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".open_threads.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self._path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            _WARNING_LOGGER.warning("error-notifier failed to persist open threads: %s", exc)


def _parse_open_thread(item: object) -> OpenThread | None:
    if not isinstance(item, dict) or (thread_id := non_empty_str(item.get("thread_id"))) is None:
        return None
    last_error = item.get("last_error") if isinstance(item.get("last_error"), str) else None
    last_seen = item.get("last_seen_at") if isinstance(item.get("last_seen_at"), str) else ""
    return OpenThread(thread_id, last_error, last_seen)
