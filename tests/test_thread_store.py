# ruff: noqa: D103
"""Tests for error-notifier ThreadStore persistence, pruning, and correlation tracking."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from error_notifier_test_import.models import ErrorFingerprint
from error_notifier_test_import.thread_store import ThreadStore


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_thread_store_persists_forgets_prunes_and_tolerates_corrupt_json(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Corrupt JSON starts fresh and warns.
    (tmp_path / "open_threads.json").write_text("{not valid json", encoding="utf-8")
    store = ThreadStore(tmp_path, max_per_room=2)
    assert "failed to load" in caplog.text

    # Prunes oldest by last_seen_at beyond max_per_room and persists atomically.
    fp1 = ErrorFingerprint("mindroom.a", "e1")
    fp2 = ErrorFingerprint("mindroom.a", "e2")
    fp3 = ErrorFingerprint("mindroom.a", "e3")
    store.record("!ops:localhost", fp1, "$1", "err1", "2026-05-20T01:00:00+00:00")
    store.record("!ops:localhost", fp2, "$2", "err2", "2026-05-20T02:00:00+00:00")
    store.record("!ops:localhost", fp3, "$3", "err3", "2026-05-20T03:00:00+00:00")

    reloaded = ThreadStore(tmp_path, max_per_room=2)
    assert reloaded.get("!ops:localhost", fp1) is None
    assert reloaded.get("!ops:localhost", fp2) is not None
    assert reloaded.get("!ops:localhost", fp3) is not None

    # Forgetting removes entry from disk.
    assert reloaded.forget("!ops:localhost", fp2) is not None
    assert ThreadStore(tmp_path, max_per_room=2).get("!ops:localhost", fp2) is None

    # Partially corrupt entries are skipped with warning while preserving valid ones.
    data = json.loads((tmp_path / "open_threads.json").read_text(encoding="utf-8"))
    data["!ops:localhost"]["bad_entry"] = {"thread_id": ""}
    (tmp_path / "open_threads.json").write_text(json.dumps(data), encoding="utf-8")
    partial = ThreadStore(tmp_path, max_per_room=2)
    assert partial.get("!ops:localhost", fp3) is not None
    assert "skipped 1 corrupt" in caplog.text


def test_thread_store_from_context_handles_missing_and_raising_state_root(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    in_mem = ThreadStore.from_context(SimpleNamespace())
    fp = ErrorFingerprint("mindroom.a", "e1")
    in_mem.record("!ops:localhost", fp, "$1", None, "2026-05-20T01:00:00+00:00")
    assert in_mem.get("!ops:localhost", fp) is not None

    class _BrokenCtx:
        @property
        def state_root(self) -> Path:
            raise RuntimeError("no storage")

    broken = ThreadStore.from_context(_BrokenCtx())
    assert broken.get("!ops:localhost", fp) is None
    assert "failed to read ctx.state_root" in caplog.text

    persisted = ThreadStore.from_context(SimpleNamespace(state_root=tmp_path))
    persisted.record("!ops:localhost", fp, "$2", "boom", "2026-05-20T01:00:00+00:00")
    assert (tmp_path / "open_threads.json").exists()


def test_thread_store_correlation_ttl_and_cascade_throttling() -> None:
    clock = Clock()
    store = ThreadStore(None, clock=clock)
    fp_a = ErrorFingerprint("mindroom.a", "primary")
    fp_b = ErrorFingerprint("mindroom.b", "secondary")

    store.record_correlation("!ops:localhost", "turn-1", "$root-1", fp_a)
    assert store.lookup_correlation("!ops:localhost", "turn-1") is not None
    clock.now = 60
    assert store.lookup_correlation("!ops:localhost", "turn-1") is None

    assert not store.is_cascade_throttled("$root-1", fp_b, window_seconds=10)
    store.record_cascade_emit("$root-1", fp_b, window_seconds=10)
    assert store.is_cascade_throttled("$root-1", fp_b, window_seconds=10)
    clock.now = 70
    assert not store.is_cascade_throttled("$root-1", fp_b, window_seconds=10)
