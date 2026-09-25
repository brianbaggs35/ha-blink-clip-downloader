"""Tests for blink_downloader.tracker."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from blink_downloader.tracker import (
    _MAX_TRACKED_IDS,
    FAILURE_RECORD_TTL,
    GIVE_UP_AFTER,
    GIVE_UP_AFTER_ATTEMPTS,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_MAX,
    ClipTracker,
    FailedDownload,
)


def make_tracker(tmp_path: Path) -> ClipTracker:
    return ClipTracker(tmp_path / "tracker.json")


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_empty(tmp_path):
    t = make_tracker(tmp_path)
    assert not t.is_downloaded("abc")
    assert t.list_cursor is None
    assert t.given_up_count == 0
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


def test_mark_downloaded_leaves_the_list_cursor_alone(tmp_path):
    """Regression test: mark_downloaded() used to move the cursor to "now",
    carrying it past every clip in the same poll that had failed or been
    held back, so none of them were ever requested again."""
    t = make_tracker(tmp_path)
    held = datetime(2024, 1, 1, tzinfo=UTC)
    t.set_list_cursor(held)
    t.mark_downloaded("clip_x")
    assert t.list_cursor == held


def test_mark_downloaded_on_a_fresh_tracker_sets_no_cursor(tmp_path):
    t = make_tracker(tmp_path)
    t.mark_downloaded("clip_x")
    assert t.list_cursor is None


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


def test_list_cursor_persisted(tmp_path):
    f = tmp_path / "tracker.json"
    t1 = ClipTracker(f)
    cursor = datetime(2024, 6, 1, 8, 20, tzinfo=UTC)
    t1.set_list_cursor(cursor)
    t1.save()

    assert ClipTracker(f).list_cursor == cursor


def test_list_cursor_keeps_its_old_key_on_disk(tmp_path):
    """The cursor is stored under the key it had when it was the time of
    the last download, so a file from before this change carries its
    cursor forward, and a downgrade reads this one's."""
    f = tmp_path / "tracker.json"
    f.write_text(
        json.dumps(
            {"downloaded_ids": ["a"], "last_download_time": "2024-06-01T08:20:00+00:00"}
        )
    )
    t = ClipTracker(f)
    assert t.list_cursor == datetime(2024, 6, 1, 8, 20, tzinfo=UTC)

    t.set_list_cursor(datetime(2024, 6, 2, tzinfo=UTC))
    t.save()
    saved = json.loads(f.read_text())
    assert saved["last_download_time"] == "2024-06-02T00:00:00+00:00"


def test_list_cursor_without_an_offset_is_read_as_utc(tmp_path):
    f = tmp_path / "tracker.json"
    f.write_text(json.dumps({"last_download_time": "2024-06-01T08:20:00"}))
    assert ClipTracker(f).list_cursor == datetime(2024, 6, 1, 8, 20, tzinfo=UTC)


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


# ---------------------------------------------------------------------------
# Failed downloads
# ---------------------------------------------------------------------------


def _age(t: ClipTracker, clip_id: str, by: timedelta) -> None:
    """Move a clip's first failure *by* into the past."""
    t._failures[clip_id].first_failed -= by


def test_record_failed_attempt_counts_polls(tmp_path):
    t = make_tracker(tmp_path)
    before = datetime.now(UTC)
    first = t.record_failed_attempt("c1")
    second = t.record_failed_attempt("c1")
    assert first is second
    assert second.attempts == 2
    assert before <= second.first_failed <= second.last_failed <= datetime.now(UTC)
    assert not second.given_up
    assert not t.has_given_up("c1")
    assert t.is_retrying("c1")
    assert not t.is_retrying("c2")


def test_retry_backoff_doubles_up_to_its_cap():
    start = datetime(2024, 6, 1, tzinfo=UTC)
    delays = [
        FailedDownload(attempts=n, first_failed=start, last_failed=start).next_try
        - start
        for n in range(1, 10)
    ]
    assert delays[:4] == [
        RETRY_BACKOFF_BASE,
        RETRY_BACKOFF_BASE * 2,
        RETRY_BACKOFF_BASE * 4,
        RETRY_BACKOFF_BASE * 8,
    ]
    assert max(delays) == RETRY_BACKOFF_MAX
    assert delays[-1] == RETRY_BACKOFF_MAX


def test_retry_due_waits_out_the_backoff(tmp_path):
    t = make_tracker(tmp_path)
    assert t.retry_due("never_failed")
    t.record_failed_attempt("c1")
    assert not t.retry_due("c1")
    t._failures["c1"].last_failed -= RETRY_BACKOFF_BASE
    assert t.retry_due("c1")


def test_many_quick_failures_alone_do_not_give_up(tmp_path):
    """Fast polling can fail a clip five times in a minute during a short
    outage; that is not long enough to give up on it."""
    t = make_tracker(tmp_path)
    records = [t.record_failed_attempt("c1") for _ in range(GIVE_UP_AFTER_ATTEMPTS * 3)]
    assert not any(record.given_up for record in records)


def test_one_old_failure_alone_does_not_give_up(tmp_path):
    """An add-on stopped for a day must still retry a clip it failed on
    before the stop, rather than give up on the first poll back."""
    t = make_tracker(tmp_path)
    t.record_failed_attempt("c1")
    _age(t, "c1", GIVE_UP_AFTER * 4)
    assert not t.record_failed_attempt("c1").given_up


def test_gives_up_after_enough_polls_over_enough_time(tmp_path):
    t = make_tracker(tmp_path)
    for _ in range(GIVE_UP_AFTER_ATTEMPTS - 1):
        t.record_failed_attempt("c1")
    _age(t, "c1", GIVE_UP_AFTER)
    assert t.record_failed_attempt("c1").given_up
    assert t.has_given_up("c1")
    assert not t.has_given_up("c2")
    assert not t.is_retrying("c1")
    assert t.given_up_count == 1


def test_given_up_count_covers_only_the_last_week_without_a_save(tmp_path):
    """Records past the TTL are pruned only on save(), which runs only
    after a poll that downloaded something; the count must not wait."""
    t = make_tracker(tmp_path)
    for clip_id in ("recent", "old"):
        for _ in range(GIVE_UP_AFTER_ATTEMPTS - 1):
            t.record_failed_attempt(clip_id)
        _age(t, clip_id, GIVE_UP_AFTER)
        t.record_failed_attempt(clip_id)
    _age(t, "old", FAILURE_RECORD_TTL)
    assert t.given_up_count == 1


def test_mark_downloaded_clears_the_failure_record(tmp_path):
    t = make_tracker(tmp_path)
    t.record_failed_attempt("c1")
    t.mark_downloaded("c1")
    assert "c1" not in t._failures
    # A later failure starts a fresh run rather than continuing the old one.
    assert t.record_failed_attempt("c1").attempts == 1


def test_failure_records_persist(tmp_path):
    f = tmp_path / "tracker.json"
    t1 = ClipTracker(f)
    for _ in range(GIVE_UP_AFTER_ATTEMPTS - 1):
        t1.record_failed_attempt("given_up")
    _age(t1, "given_up", GIVE_UP_AFTER)
    t1.record_failed_attempt("given_up")
    t1.record_failed_attempt("retrying")
    t1.save()

    t2 = ClipTracker(f)
    assert t2.has_given_up("given_up")
    assert t2._failures["given_up"].attempts == GIVE_UP_AFTER_ATTEMPTS
    assert not t2.has_given_up("retrying")
    assert t2._failures["retrying"].attempts == 1
    assert (
        t2._failures["retrying"].first_failed == t1._failures["retrying"].first_failed
    )
    assert t2._failures["retrying"].last_failed == t1._failures["retrying"].last_failed


def test_save_prunes_failure_records_past_their_ttl(tmp_path):
    f = tmp_path / "tracker.json"
    t = ClipTracker(f)
    t.record_failed_attempt("old")
    t.record_failed_attempt("recent")
    _age(t, "old", FAILURE_RECORD_TTL + timedelta(minutes=1))
    t.save()

    assert set(ClipTracker(f)._failures) == {"recent"}


def test_malformed_failure_records_cost_only_themselves(tmp_path, caplog):
    """One bad record must not discard the downloaded IDs or cursor that
    the rest of the file holds, which the file-level fallback would do."""
    f = tmp_path / "tracker.json"
    f.write_text(
        json.dumps(
            {
                "downloaded_ids": ["a"],
                "last_download_time": "2024-06-01T00:00:00+00:00",
                "failed_downloads": {
                    "good": {
                        "attempts": 2,
                        "first_failed": "2024-06-01T00:00:00",
                        "given_up": True,
                    },
                    "with_last": {
                        "attempts": 1,
                        "first_failed": "2024-06-01T00:00:00+00:00",
                        "last_failed": "2024-06-02T00:00:00",
                    },
                    "bad_last": {
                        "attempts": 1,
                        "first_failed": "2024-06-01T00:00:00+00:00",
                        "last_failed": "never",
                    },
                    "no_time": {"attempts": 1},
                    "bad_time": {"attempts": 1, "first_failed": "soon"},
                    "not_a_dict": [1, 2],
                    "bad_count": {"attempts": "many", "first_failed": "2024-06-01"},
                },
            }
        )
    )
    with caplog.at_level(logging.DEBUG, logger="blink_downloader.tracker"):
        t = ClipTracker(f)

    assert t.is_downloaded("a")
    assert t.list_cursor == datetime(2024, 6, 1, tzinfo=UTC)
    assert set(t._failures) == {"good", "with_last"}
    assert t._failures["good"].first_failed == datetime(2024, 6, 1, tzinfo=UTC)
    # A record with no last failure falls back to its first one.
    assert t._failures["good"].last_failed == datetime(2024, 6, 1, tzinfo=UTC)
    assert t._failures["with_last"].last_failed == datetime(2024, 6, 2, tzinfo=UTC)
    assert t.has_given_up("good")
    assert "Ignoring malformed failure record for no_time" in caplog.text


def test_failure_records_of_the_wrong_shape_are_ignored(tmp_path):
    f = tmp_path / "tracker.json"
    f.write_text(json.dumps({"downloaded_ids": ["a"], "failed_downloads": ["c1"]}))
    t = ClipTracker(f)
    assert t.is_downloaded("a")
    assert t._failures == {}
def test_has_history_reflects_an_earlier_run(tmp_path):
    path = tmp_path / "tracker.json"
    assert ClipTracker(path).has_history is False
    tracker = ClipTracker(path)
    tracker.mark_downloaded("clip-1")
    tracker.save()
    assert ClipTracker(path).has_history is True
