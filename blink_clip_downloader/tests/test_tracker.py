"""Tests for blink_downloader.tracker."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from blink_downloader.tracker import ClipTracker, _MAX_TRACKED_IDS


def make_tracker(tmp_path: Path) -> ClipTracker:
    return ClipTracker(tmp_path / "tracker.json")


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_empty(tmp_path):
    t = make_tracker(tmp_path)
    assert not t.is_downloaded("abc")
    assert t.last_download_time is None
    assert t.stats["total_downloaded"] == 0
    assert t.stats["total_bytes"] == 0


# ---------------------------------------------------------------------------
# Marking downloads
# ---------------------------------------------------------------------------


def test_mark_downloaded(tmp_path):
    t = make_tracker(tmp_path)
    t.mark_downloaded("clip_001", size_bytes=2048)
    assert t.is_downloaded("clip_001")
    assert not t.is_downloaded("clip_002")
    assert t.stats["total_downloaded"] == 1
    assert t.stats["total_bytes"] == 2048


def test_mark_multiple(tmp_path):
    t = make_tracker(tmp_path)
    for i in range(5):
        t.mark_downloaded(f"clip_{i}", size_bytes=100)
    assert t.stats["total_downloaded"] == 5
    assert t.stats["total_bytes"] == 500


def test_last_download_time_set_after_mark(tmp_path):
    t = make_tracker(tmp_path)
    before = datetime.now(timezone.utc)
    t.mark_downloaded("clip_x")
    after = datetime.now(timezone.utc)
    assert t.last_download_time is not None
    assert before <= t.last_download_time <= after


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_save_and_reload(tmp_path):
    f = tmp_path / "tracker.json"
    t1 = ClipTracker(f)
    t1.mark_downloaded("a", 100)
    t1.mark_downloaded("b", 200)
    t1.save()

    t2 = ClipTracker(f)
    assert t2.is_downloaded("a")
    assert t2.is_downloaded("b")
    assert not t2.is_downloaded("c")
    assert t2.stats["total_downloaded"] == 2
    assert t2.stats["total_bytes"] == 300


def test_save_does_not_leave_tmp_file_behind(tmp_path):
    f = tmp_path / "tracker.json"
    t = ClipTracker(f)
    t.mark_downloaded("a", 100)
    t.save()

    assert f.exists()
    assert not (tmp_path / "tracker.json.tmp").exists()


def test_last_download_time_persisted(tmp_path):
    f = tmp_path / "tracker.json"
    t1 = ClipTracker(f)
    t1.mark_downloaded("x")
    saved_time = t1.last_download_time
    t1.save()

    t2 = ClipTracker(f)
    assert t2.last_download_time is not None
    assert saved_time is not None
    assert abs((t2.last_download_time - saved_time).total_seconds()) < 1


def test_corrupted_file_starts_fresh(tmp_path):
    f = tmp_path / "tracker.json"
    f.write_text("{{{INVALID JSON}}}")
    t = ClipTracker(f)
    assert t.stats["total_downloaded"] == 0
    assert not t.is_downloaded("any_id")


def test_empty_file_starts_fresh(tmp_path):
    f = tmp_path / "tracker.json"
    f.write_text("")
    t = ClipTracker(f)
    assert t.stats["total_downloaded"] == 0


def test_unreadable_file_starts_fresh_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An existing-but-unreadable file (e.g. permissions/disk I/O error) must
    not crash the app at __init__ — ClipTracker() is constructed directly in
    BlinkClipDownloaderApp.__init__ with no surrounding try/except, so an
    uncaught OSError here would take down the whole add-on at startup."""
    f = tmp_path / "tracker.json"
    f.write_text('{"downloaded_ids": []}')
    with (
        patch.object(Path, "read_text", side_effect=OSError("permission denied")),
        caplog.at_level(logging.WARNING),
    ):
        t = ClipTracker(f)
    assert t.stats["total_downloaded"] == 0
    assert not t.is_downloaded("any_id")
    assert "corrupt or unreadable" in caplog.text


def test_wrong_shape_json_starts_fresh(tmp_path):
    """Valid JSON that isn't the expected object shape (e.g. a bare array) must
    not raise — it should be treated the same as any other corrupt file."""
    f = tmp_path / "tracker.json"
    f.write_text("[1, 2, 3]")
    t = ClipTracker(f)
    assert t.stats["total_downloaded"] == 0
    assert not t.is_downloaded("any_id")


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def test_prune_keeps_max_ids(tmp_path):
    t = make_tracker(tmp_path)
    overflow = 200
    for i in range(_MAX_TRACKED_IDS + overflow):
        t._downloaded[f"id_{i}"] = None

    t.save()

    t2 = ClipTracker(tmp_path / "tracker.json")
    assert len(t2._downloaded) == _MAX_TRACKED_IDS
    # The oldest IDs (lowest indices) must be the ones dropped.
    assert "id_0" not in t2._downloaded
    assert f"id_{overflow}" in t2._downloaded
    assert f"id_{_MAX_TRACKED_IDS + overflow - 1}" in t2._downloaded


# ---------------------------------------------------------------------------
# Stats are a copy
# ---------------------------------------------------------------------------


def test_stats_returns_copy(tmp_path):
    t = make_tracker(tmp_path)
    stats = t.stats
    stats["total_downloaded"] = 9999
    assert t.stats["total_downloaded"] == 0


# ---------------------------------------------------------------------------
# Session count
# ---------------------------------------------------------------------------


def test_increment_session_count(tmp_path):
    t = make_tracker(tmp_path)
    t.increment_session_count()
    t.increment_session_count()
    assert t.stats["session_count"] == 2
