"""Tests for ClipDatabase."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from blink_downloader.analyzer import AnalysisResult
from blink_downloader.database import (
    ClipDatabase,
    ClipFilters,
    _affected,
    _local_day_bounds,
    _row_to_dict,
    _severities_at_or_above,
)
from blink_downloader.security import (
    SecurityEvent,
    SecurityEventType,
    Severity,
    VehicleSignature,
)
from blink_downloader.vision import DetectedObject
from tests.conftest import TEST_DB_DSN


def _make_clip(clip_id: str = "clip1", camera: str = "Front Door", **kwargs) -> dict:
    return {
        "id": clip_id,
        "camera": camera,
        "path": f"/share/blink-clips/{clip_id}.mp4",
        "timestamp": kwargs.get("timestamp", "2024-06-01T08:00:00+00:00"),
        "size_bytes": kwargs.get("size_bytes", 1_048_576),
        "duration": kwargs.get("duration", 5),
        "source": kwargs.get("source", "pir"),
        "network_id": kwargs.get("network_id", 10),
    }


# Two IANA zones with clean, DST-irrelevant nonzero offsets in opposite
# directions — used to force the process's ambient local timezone (see
# _local_timezone) so the local/UTC calendar-day-mismatch regression tests
# below run deterministically on every host, including UTC-configured CI
# runners, instead of skipping themselves the way they used to when the
# *actual* host timezone happened to be UTC.
_WEST_OF_UTC_TZ = "America/New_York"
_EAST_OF_UTC_TZ = "Asia/Tokyo"


@contextlib.contextmanager
def _local_timezone(tz_name: str):
    """Force the process's local timezone for the duration of the block.

    database._local_day_bounds and _local_utc_offset_sql both derive
    "local" purely from ``datetime.now().astimezone()``, which reads this
    process's TZ — the same mechanism HA Supervisor uses to set the
    container's zone. Forcing it here exercises the real local/UTC
    mismatch code path deterministically rather than depending on
    whatever zone the test host itself happens to be configured with.
    """
    original = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    try:
        yield
    finally:
        if original is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = original
        time.tzset()


def _local_boundary_case() -> tuple[str, bool]:
    """A UTC timestamp straddling the local/UTC calendar-day boundary,
    paired with whether it falls on local-*today* (True) or
    local-*yesterday* (False) — for regression-testing "today" bucketing
    against the process's local timezone (see database._local_day_bounds).
    Must be called inside a :func:`_local_timezone` block.
    """
    local_now = datetime.now(UTC).astimezone()
    offset = local_now.utcoffset() or timedelta(0)
    local_midnight_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if offset < timedelta(0):
        # West of UTC: local "yesterday, 5 minutes before midnight" is
        # already far enough into UTC's next day to read as UTC-*today*.
        local_ts = local_midnight_today - timedelta(minutes=5)
        return local_ts.astimezone(UTC).isoformat(), False
    # East of UTC: local "today, 5 minutes after midnight" hasn't caught up
    # to UTC's calendar yet and still reads as UTC-*yesterday*.
    local_ts = local_midnight_today + timedelta(minutes=5)
    return local_ts.astimezone(UTC).isoformat(), True


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


async def test_init_creates_tables(db: ClipDatabase) -> None:
    assert db._pool is not None
    # A functional check, not just an attribute check: querying a freshly
    # created (and truncated, via the `db` fixture) table succeeds and is
    # empty, proving init() actually created the schema.
    assert await db.get_clips() == []


async def test_double_close_is_safe(db: ClipDatabase) -> None:
    await db.close()
    await db.close()  # should not raise


async def test_init_resets_stale_processing_items(db: ClipDatabase) -> None:
    """A crash/restart while a clip is mid-analysis must not strand it forever.

    Items stuck in 'processing' are never retried by the queue (it only
    fetches status='pending'), so init() must reset them back to pending.
    """
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    await db.update_queue_status("c1", "processing")
    await db.close()

    # Reconnect to the same database — simulates an add-on restart, where
    # a fresh ClipDatabase instance opens against data a previous process
    # left behind.
    d2 = ClipDatabase(TEST_DB_DSN)
    await d2.init()
    try:
        counts = await d2.get_queue_counts()
        assert counts["processing"] == 0
        assert counts["pending"] == 1
        pending = await d2.get_pending_analysis()
        assert pending[0]["clip_id"] == "c1"
        assert pending[0]["completed_at"] == ""
        assert pending[0]["error_message"] == ""
    finally:
        await d2.close()


# ------------------------------------------------------------------
# add_clip / get_clip
# ------------------------------------------------------------------


async def test_add_and_get_clip(db: ClipDatabase) -> None:
    clip = _make_clip()
    await db.add_clip(clip)
    result = await db.get_clip("clip1")
    assert result is not None
    assert result["camera"] == "Front Door"
    assert result["size_bytes"] == 1_048_576
    assert result["starred"] is False
    assert result["archived"] is False
    assert result["tags"] == []


async def test_add_clip_idempotent(db: ClipDatabase) -> None:
    clip = _make_clip()
    await db.add_clip(clip)
    await db.add_clip(clip)  # INSERT OR IGNORE — no error
    assert len(await db.get_clips()) == 1


async def test_get_clip_missing_returns_none(db: ClipDatabase) -> None:
    assert await db.get_clip("nonexistent") is None


async def test_add_clip_when_db_not_init() -> None:
    d = ClipDatabase()
    await d.add_clip(_make_clip())  # should silently no-op


async def test_add_clip_with_null_fields(db: ClipDatabase) -> None:
    """Blink API returns null (→ None) for duration/network_id on some clip types.

    database.add_clip must not raise TypeError when these fields are None.
    Regression test for: int() argument must be a string … not 'NoneType'.
    """
    clip = {
        "id": "null-fields-clip",
        "camera": "Front Door",
        "path": "/share/blink-clips/null-fields-clip.mp4",
        "timestamp": "2024-06-01T08:00:00+00:00",
        "size_bytes": None,  # present but null
        "duration": None,  # present but null
        "source": None,  # present but null
        "network_id": None,  # present but null
    }
    await db.add_clip(clip)  # must not raise
    result = await db.get_clip("null-fields-clip")
    assert result is not None
    assert result["duration"] == 0
    assert result["network_id"] == 0
    assert result["size_bytes"] == 0
    assert result["source"] == ""


# ------------------------------------------------------------------
# import_legacy_sqlite_data (see sqlite_migration.py, the real caller —
# these tests exercise the guard clauses directly rather than through it)
# ------------------------------------------------------------------


async def test_import_legacy_sqlite_data_when_db_not_init() -> None:
    d = ClipDatabase()
    assert await d.import_legacy_sqlite_data([], [], None) == 0


async def test_import_legacy_sqlite_data_with_no_clips_is_a_noop(
    db: ClipDatabase,
) -> None:
    assert await db.import_legacy_sqlite_data([], [], None) == 0


# ------------------------------------------------------------------
# star_clip / set_tags
# ------------------------------------------------------------------


async def test_star_and_unstar_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip())
    assert await db.star_clip("clip1", True) is True
    result = await db.get_clip("clip1")
    assert result is not None
    assert result["starred"] is True

    assert await db.star_clip("clip1", False) is True
    result = await db.get_clip("clip1")
    assert result is not None
    assert result["starred"] is False


async def test_star_nonexistent_returns_false(db: ClipDatabase) -> None:
    assert await db.star_clip("ghost", True) is False


async def test_set_tags(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip())
    assert await db.set_tags("clip1", ["important", "night"]) is True
    result = await db.get_clip("clip1")
    assert result is not None
    assert "important" in result["tags"]
    assert "night" in result["tags"]


# ------------------------------------------------------------------
# update_clip_duration / get_clips_missing_duration
# ------------------------------------------------------------------


async def test_update_clip_duration(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip(duration=0))
    assert await db.update_clip_duration("clip1", 42) is True
    result = await db.get_clip("clip1")
    assert result is not None
    assert result["duration"] == 42


async def test_update_clip_duration_nonexistent_returns_false(
    db: ClipDatabase,
) -> None:
    assert await db.update_clip_duration("ghost", 42) is False


async def test_update_clip_duration_without_init_returns_false() -> None:
    d = ClipDatabase()
    assert await d.update_clip_duration("clip1", 42) is False


async def test_get_clips_missing_duration(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", duration=0))
    await db.add_clip(_make_clip("c2", duration=5))
    await db.add_clip(_make_clip("c3", duration=0))
    missing = await db.get_clips_missing_duration()
    assert {c["id"] for c in missing} == {"c1", "c3"}
    assert all("file_path" in c for c in missing)


async def test_get_clips_missing_duration_excludes_backfilled(
    db: ClipDatabase,
) -> None:
    """Self-limiting: once a clip is backfilled it must not keep showing up
    on every future startup's scan."""
    await db.add_clip(_make_clip("c1", duration=0))
    await db.update_clip_duration("c1", 12)
    assert await db.get_clips_missing_duration() == []


async def test_get_clips_missing_duration_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_clips_missing_duration() == []


async def test_set_tags_nonexistent_returns_false(db: ClipDatabase) -> None:
    assert await db.set_tags("ghost", ["foo"]) is False


# ------------------------------------------------------------------
# delete_clip
# ------------------------------------------------------------------


async def test_delete_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip())
    assert await db.delete_clip("clip1") is True
    assert await db.get_clip("clip1") is None


async def test_delete_nonexistent_returns_false(db: ClipDatabase) -> None:
    assert await db.delete_clip("ghost") is False


async def test_delete_clip_by_path(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip())
    assert await db.delete_clip_by_path("/share/blink-clips/clip1.mp4") is True
    assert await db.get_clip("clip1") is None


async def test_delete_clip_by_path_nonexistent_returns_false(db: ClipDatabase) -> None:
    assert await db.delete_clip_by_path("/no/such/file.mp4") is False


async def test_delete_clip_cascades_to_analysis_tables(db: ClipDatabase) -> None:
    """PRAGMA foreign_keys must be ON or ON DELETE CASCADE is a silent no-op."""
    await db.add_clip(_make_clip())
    await db.add_analysis_result(
        {
            "clip_id": "clip1",
            "camera": "Front Door",
            "model": "test-model",
            "analyzed_at": "2024-06-01T08:00:05+00:00",
        }
    )
    await db.enqueue_for_analysis("clip1", "Front Door", "/share/blink-clips/clip1.mp4")

    assert await db.delete_clip("clip1") is True

    assert db._pool is not None
    remaining_results = await db._pool.fetchval(
        "SELECT COUNT(*) FROM analysis_results WHERE clip_id='clip1'"
    )
    assert remaining_results == 0
    remaining_queue = await db._pool.fetchval(
        "SELECT COUNT(*) FROM analysis_queue WHERE clip_id='clip1'"
    )
    assert remaining_queue == 0


# ------------------------------------------------------------------
# get_clips (filtered)
# ------------------------------------------------------------------


async def test_get_clips_all(db: ClipDatabase) -> None:
    for i in range(3):
        await db.add_clip(_make_clip(f"c{i}", camera="Cam A"))
    clips = await db.get_clips()
    assert len(clips) == 3


async def test_get_clips_filter_by_camera(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Back Yard"))
    clips = await db.get_clips(ClipFilters(camera="Back Yard"))
    assert len(clips) == 1
    assert clips[0]["camera"] == "Back Yard"


async def test_get_clips_filter_starred(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.star_clip("c1", True)
    starred = await db.get_clips(ClipFilters(starred=True))
    assert len(starred) == 1
    assert starred[0]["id"] == "c1"


async def test_get_clips_notified_flag_and_filter(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    # c1: suspicious above threshold -> notified
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "test",
            "is_suspicious": True,
            "confidence": 0.9,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    # c2: suspicious but below threshold -> not notified
    await db.add_analysis_result(
        {
            "clip_id": "c2",
            "camera": "Front Door",
            "model": "test",
            "is_suspicious": True,
            "confidence": 0.2,
            "analyzed_at": "2024-06-01T09:00:00+00:00",
        }
    )
    # c3: no analysis at all -> not notified

    all_clips = {
        c["id"]: c for c in await db.get_clips(ClipFilters(min_confidence=0.5))
    }
    assert all_clips["c1"]["notified"] is True
    assert all_clips["c2"]["notified"] is False
    assert all_clips["c3"]["notified"] is False

    notified_only = await db.get_clips(
        ClipFilters(notified_only=True, min_confidence=0.5)
    )
    assert [c["id"] for c in notified_only] == ["c1"]


async def test_get_clips_notified_filter_counts_rows_tied_at_latest_analysis(
    db: ClipDatabase,
) -> None:
    """Two analysis rows can share a clip's newest analyzed_at — an
    re-analysis recorded within the same second, say. "Latest" then means
    all of them, so the badge and the filter must fire if *any* tied row is
    suspicious, not whichever one the database happens to return first.

    This pins the semantics the notified/recognized filters were rewritten
    around: the select-list expression and the WHERE filter ask the same
    question in different SQL, and a tie is where a careless rewrite of
    either one (a LIMIT 1, a DISTINCT ON) would quietly disagree with the
    other.
    """
    await db.add_clip(_make_clip("tie1"))
    await db.add_clip(_make_clip("tie2"))
    same_moment = "2024-06-01T09:00:00+00:00"
    for clip_id, first, second in (
        # Suspicious row is the second one written...
        ("tie1", False, True),
        # ...and the first one for the other clip, so neither ordering can
        # pass this by accident.
        ("tie2", True, False),
    ):
        for suspicious in (first, second):
            await db.add_analysis_result(
                {
                    "clip_id": clip_id,
                    "camera": "Front Door",
                    "model": "test",
                    "is_suspicious": suspicious,
                    "confidence": 0.9 if suspicious else 0.1,
                    "analyzed_at": same_moment,
                }
            )

    flagged = {
        c["id"]: c["notified"]
        for c in await db.get_clips(ClipFilters(min_confidence=0.5))
    }
    assert flagged["tie1"] is True
    assert flagged["tie2"] is True

    filtered = await db.get_clips(ClipFilters(notified_only=True, min_confidence=0.5))
    assert {c["id"] for c in filtered} == {"tie1", "tie2"}


async def test_get_clips_recognized_filter_counts_rows_tied_at_latest_analysis(
    db: ClipDatabase,
) -> None:
    """Same tie rule for approved_faces_seen, which drives the recognized
    filter and was rewritten alongside the notified one."""
    await db.add_clip(_make_clip("face1"))
    same_moment = "2024-06-01T09:00:00+00:00"
    for seen in (False, True):
        await db.add_analysis_result(
            {
                "clip_id": "face1",
                "camera": "Front Door",
                "model": "test",
                "approved_faces_seen": seen,
                "analyzed_at": same_moment,
            }
        )

    rows = {c["id"]: c for c in await db.get_clips()}
    assert rows["face1"]["face_recognized"] is True
    assert [c["id"] for c in await db.get_clips(ClipFilters(recognized_only=True))] == [
        "face1"
    ]


async def test_get_clips_notified_flag_reflects_latest_reanalysis_only(
    db: ClipDatabase,
) -> None:
    """Regression test: add_analysis_result always inserts a new row rather
    than replacing the old one, so a clip re-analyzed (e.g. via the
    Library's "Re-analyze" button) after an earlier suspicious pass has
    *two* analysis_results rows. The 🔔 notified badge must reflect only
    the most recent verdict, not "was this ever suspicious" — otherwise a
    clip correctly cleared on re-analysis keeps showing as notified forever."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis(
            "c1",
            is_suspicious=True,
            confidence=0.9,
            analyzed_at="2024-06-01T09:00:00+00:00",
        )
    )
    await db.add_analysis_result(
        _make_analysis(
            "c1",
            is_suspicious=False,
            confidence=0.9,
            analyzed_at="2024-06-01T10:00:00+00:00",
        )
    )

    clips = {c["id"]: c for c in await db.get_clips(ClipFilters(min_confidence=0.5))}
    assert clips["c1"]["notified"] is False

    # And the reverse: cleared first, then a later re-analysis genuinely
    # does find it suspicious — notified must flip back on.
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(
        _make_analysis(
            "c2",
            is_suspicious=False,
            confidence=0.9,
            analyzed_at="2024-06-01T09:00:00+00:00",
        )
    )
    await db.add_analysis_result(
        _make_analysis(
            "c2",
            is_suspicious=True,
            confidence=0.9,
            analyzed_at="2024-06-01T10:00:00+00:00",
        )
    )
    clips2 = {c["id"]: c for c in await db.get_clips(ClipFilters(min_confidence=0.5))}
    assert clips2["c2"]["notified"] is True


async def test_get_clips_face_recognized_flag(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    # c1: approved household member seen -> face_recognized
    await db.add_analysis_result(_make_analysis("c1", approved_faces_seen=True))
    # c2: analyzed, but no approved match -> not face_recognized
    await db.add_analysis_result(_make_analysis("c2", approved_faces_seen=False))
    # c3: no analysis at all -> not face_recognized

    clips = {c["id"]: c for c in await db.get_clips()}
    assert clips["c1"]["face_recognized"] is True
    assert clips["c2"]["face_recognized"] is False
    assert clips["c3"]["face_recognized"] is False

    recognized_only = await db.get_clips(ClipFilters(recognized_only=True))
    assert [c["id"] for c in recognized_only] == ["c1"]


async def test_get_clips_face_recognized_flag_reflects_approved_faces_seen_not_just_bypass(
    db: ClipDatabase,
) -> None:
    """The overwhelmingly common real case: a household member's own routine,
    already-non-suspicious visit, so the safety bypass is never even
    consulted (face_bypass_applied stays False) yet a face was still
    unambiguously recognized. face_recognized must reflect approved_faces_seen
    here, not face_bypass_applied, or the Library badge would almost never
    show up for the case it exists to cover."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis("c1", face_bypass_applied=False, approved_faces_seen=True)
    )

    clips = {c["id"]: c for c in await db.get_clips()}
    assert clips["c1"]["face_recognized"] is True


async def test_get_clips_face_recognized_flag_reflects_latest_reanalysis_only(
    db: ClipDatabase,
) -> None:
    """Same latest-row-wins scoping as the notified flag — a clip that once
    had an approved match but no longer does after a later re-analysis (e.g.
    the approved person's enrollment was removed) must not keep showing the
    face-recognized badge forever."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis(
            "c1",
            approved_faces_seen=True,
            analyzed_at="2024-06-01T09:00:00+00:00",
        )
    )
    await db.add_analysis_result(
        _make_analysis(
            "c1",
            approved_faces_seen=False,
            analyzed_at="2024-06-01T10:00:00+00:00",
        )
    )

    clips = {c["id"]: c for c in await db.get_clips()}
    assert clips["c1"]["face_recognized"] is False


async def test_get_clips_search(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("abc123", camera="Garage"))
    await db.add_clip(_make_clip("xyz999", camera="Office"))
    results = await db.get_clips(ClipFilters(search="Garage"))
    assert len(results) == 1


async def test_get_clips_pagination(db: ClipDatabase) -> None:
    for i in range(10):
        await db.add_clip(
            _make_clip(f"c{i:02d}", timestamp=f"2024-06-{i + 1:02d}T00:00:00+00:00")
        )
    page1 = await db.get_clips(limit=5, offset=0)
    page2 = await db.get_clips(limit=5, offset=5)
    assert len(page1) == 5
    assert len(page2) == 5
    assert {c["id"] for c in page1}.isdisjoint({c["id"] for c in page2})


# ------------------------------------------------------------------
# mark_archived / get_clips_to_archive
# ------------------------------------------------------------------


async def test_mark_archived(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip())
    await db.mark_archived("clip1", "/archives/2024-06.zip")
    result = await db.get_clip("clip1")
    assert result is not None
    assert result["archived"] is True
    assert result["archive_path"] == "/archives/2024-06.zip"


async def test_get_all_file_paths_returns_paths(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    paths = await db.get_all_file_paths()
    assert paths == {
        "/share/blink-clips/c1.mp4",
        "/share/blink-clips/c2.mp4",
    }


async def test_get_all_file_paths_empty(db: ClipDatabase) -> None:
    assert await db.get_all_file_paths() == set()


async def test_get_clips_to_archive(db: ClipDatabase) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    new_ts = datetime.now(UTC).isoformat()
    await db.add_clip(_make_clip("old", timestamp=old_ts))
    await db.add_clip(_make_clip("new", timestamp=new_ts))
    to_archive = await db.get_clips_to_archive(older_than_days=30)
    ids = [c["id"] for c in to_archive]
    assert "old" in ids
    assert "new" not in ids


async def test_get_clips_archive_path_filter(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.mark_archived("c1", "/archives/2024-06.zip")
    await db.mark_archived("c2", "/archives/2024-07.zip")
    clips = await db.get_clips(
        ClipFilters(archived=True, archive_path="/archives/2024-06.zip")
    )
    assert [c["id"] for c in clips] == ["c1"]


async def test_get_clips_archive_path_filter_no_match(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.mark_archived("c1", "/archives/2024-06.zip")
    clips = await db.get_clips(
        ClipFilters(archived=True, archive_path="/archives/nope.zip")
    )
    assert clips == []


async def test_get_archive_clips_returns_page_and_total(db: ClipDatabase) -> None:
    for i in range(3):
        clip_id = f"archive-{i}"
        await db.add_clip(
            _make_clip(
                clip_id,
                camera="Driveway" if i < 2 else "Front Door",
                timestamp=f"2024-06-{i + 1:02d}T00:00:00+00:00",
            )
        )
        await db.mark_archived(clip_id, "/archives/2024-06.zip")

    result = await db.get_archive_clips(
        "/archives/2024-06.zip",
        camera="Driveway",
        limit=1,
        offset=1,
    )

    assert result["total"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["camera"] == "Driveway"


async def test_get_archive_clips_without_pool_returns_empty_page() -> None:
    db = ClipDatabase()
    assert await db.get_archive_clips("/archives/none.zip") == {
        "items": [],
        "total": 0,
    }


async def test_get_clips_by_archive_path_returns_full_rows(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("a1", camera="Front Door"))
    await db.add_clip(_make_clip("a2", camera="Backyard"))
    await db.add_clip(_make_clip("other"))
    await db.mark_archived("a1", "/archives/2024-06.zip")
    await db.mark_archived("a2", "/archives/2024-06.zip")
    await db.mark_archived("other", "/archives/2024-07.zip")

    clips = await db.get_clips_by_archive_path("/archives/2024-06.zip")

    assert {c["id"] for c in clips} == {"a1", "a2"}
    # Full rows, not just id/camera/file_path -- gdrive_file_id must be
    # present so the caller can trash each clip's Drive backup.
    assert "gdrive_file_id" in clips[0]


async def test_get_clips_by_archive_path_no_match(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("a1"))
    await db.mark_archived("a1", "/archives/2024-06.zip")
    assert await db.get_clips_by_archive_path("/archives/nope.zip") == []


async def test_get_clips_by_archive_path_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_clips_by_archive_path("/archives/2024-06.zip") == []


async def test_delete_clips_by_archive_path_removes_all_matching(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("a1"))
    await db.add_clip(_make_clip("a2"))
    await db.add_clip(_make_clip("keep"))
    await db.mark_archived("a1", "/archives/2024-06.zip")
    await db.mark_archived("a2", "/archives/2024-06.zip")
    await db.mark_archived("keep", "/archives/2024-07.zip")

    removed = await db.delete_clips_by_archive_path("/archives/2024-06.zip")

    assert removed == 2
    assert await db.get_clip("a1") is None
    assert await db.get_clip("a2") is None
    assert await db.get_clip("keep") is not None


async def test_delete_clips_by_archive_path_no_match_returns_zero(
    db: ClipDatabase,
) -> None:
    assert await db.delete_clips_by_archive_path("/archives/nope.zip") == 0


async def test_delete_clips_by_archive_path_without_init() -> None:
    d = ClipDatabase()
    assert await d.delete_clips_by_archive_path("/archives/2024-06.zip") == 0


async def test_get_archive_groups_filters_clips_inside_mixed_date_archive(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("in-range", timestamp="2024-06-10T00:00:00+00:00"))
    await db.add_clip(_make_clip("out-of-range", timestamp="2024-07-10T00:00:00+00:00"))
    await db.mark_archived("in-range", "/archives/mixed.zip")
    await db.mark_archived("out-of-range", "/archives/mixed.zip")

    groups = await db.get_archive_groups(
        since="2024-06-01",
        until="2024-06-30T23:59:59",
    )

    assert len(groups) == 1
    assert groups[0]["archive_path"] == "/archives/mixed.zip"
    assert groups[0]["clip_count"] == 1
    assert groups[0]["latest_timestamp"] == "2024-06-10T00:00:00+00:00"


# ------------------------------------------------------------------
# get_archived_clip_records / get_archive_groups
# ------------------------------------------------------------------


async def test_get_archived_clip_records_empty(db: ClipDatabase) -> None:
    assert await db.get_archived_clip_records() == []


async def test_get_archived_clip_records_excludes_unarchived(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("archived-1"))
    await db.add_clip(_make_clip("plain-1"))
    await db.mark_archived("archived-1", "/archives/2024-06.zip")
    records = await db.get_archived_clip_records()
    assert [r["id"] for r in records] == ["archived-1"]
    assert records[0]["archive_path"] == "/archives/2024-06.zip"


async def test_get_archived_clip_records_includes_camera_and_file_path(
    db: ClipDatabase,
) -> None:
    """archiver.py's orphan-pruning needs these to reconstruct the exact
    archive member name (camera/filename) a row should point at."""
    clip = _make_clip("archived-1")
    await db.add_clip(clip)
    await db.mark_archived("archived-1", "/archives/2024-06.zip")
    records = await db.get_archived_clip_records()
    assert records[0]["camera"] == clip["camera"]
    assert records[0]["file_path"] == clip["path"]


async def test_get_archived_clip_records_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_archived_clip_records() == []


async def test_get_archive_groups_empty(db: ClipDatabase) -> None:
    assert await db.get_archive_groups() == []


async def test_get_archive_groups_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_archive_groups() == []


async def test_get_archive_groups_excludes_unarchived_clips(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("plain-1"))
    assert await db.get_archive_groups() == []


async def test_get_archive_groups_aggregates_by_archive_path(
    db: ClipDatabase,
) -> None:
    await db.add_clip(
        _make_clip("c1", timestamp="2024-06-01T08:00:00+00:00", size_bytes=1000)
    )
    await db.add_clip(
        _make_clip("c2", timestamp="2024-06-15T08:00:00+00:00", size_bytes=2000)
    )
    await db.add_clip(
        _make_clip("c3", timestamp="2024-07-01T08:00:00+00:00", size_bytes=5000)
    )
    await db.mark_archived("c1", "/archives/2024-06.zip")
    await db.mark_archived("c2", "/archives/2024-06.zip")
    await db.mark_archived("c3", "/archives/2024-07.zip")

    groups = await db.get_archive_groups()

    assert len(groups) == 2
    # Newest archive (by latest_timestamp) first.
    assert groups[0]["archive_path"] == "/archives/2024-07.zip"
    assert groups[0]["clip_count"] == 1
    assert groups[0]["total_size"] == 5000
    june = next(g for g in groups if g["archive_path"] == "/archives/2024-06.zip")
    assert june["clip_count"] == 2
    assert june["total_size"] == 3000
    assert june["latest_timestamp"] == "2024-06-15T08:00:00+00:00"


async def test_get_archive_groups_camera_filter(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Driveway"))
    await db.mark_archived("c1", "/archives/2024-06.zip")
    await db.mark_archived("c2", "/archives/2024-06.zip")

    groups = await db.get_archive_groups(camera="Front Door")

    assert len(groups) == 1
    assert groups[0]["clip_count"] == 1


async def test_get_archive_groups_camera_filter_all_is_noop(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Driveway"))
    await db.mark_archived("c1", "/archives/2024-06.zip")
    await db.mark_archived("c2", "/archives/2024-06.zip")

    groups = await db.get_archive_groups(camera="all")

    assert groups[0]["clip_count"] == 2


async def test_get_archive_groups_since_until_filter_by_latest_timestamp(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1", timestamp="2024-06-01T00:00:00+00:00"))
    await db.add_clip(_make_clip("c2", timestamp="2024-07-01T00:00:00+00:00"))
    await db.mark_archived("c1", "/archives/2024-06.zip")
    await db.mark_archived("c2", "/archives/2024-07.zip")

    only_june = await db.get_archive_groups(
        since="2024-06-01T00:00:00+00:00", until="2024-06-30T23:59:59+00:00"
    )
    assert [g["archive_path"] for g in only_june] == ["/archives/2024-06.zip"]

    from_july = await db.get_archive_groups(since="2024-07-01T00:00:00+00:00")
    assert [g["archive_path"] for g in from_july] == ["/archives/2024-07.zip"]


# ------------------------------------------------------------------
# Statistics
# ------------------------------------------------------------------


async def test_get_stats_empty(db: ClipDatabase) -> None:
    stats = await db.get_stats()
    assert stats["total_count"] == 0
    assert stats["starred_count"] == 0
    assert stats["recognized_count"] == 0


async def test_get_stats_counts(db: ClipDatabase) -> None:
    today = datetime.now(UTC).isoformat()
    await db.add_clip(_make_clip("c1", timestamp=today, size_bytes=2_000_000))
    await db.add_clip(_make_clip("c2", timestamp=today))
    await db.star_clip("c1", True)
    stats = await db.get_stats()
    assert stats["total_count"] == 2
    assert stats["today_count"] == 2
    assert stats["starred_count"] == 1
    assert stats["total_size_bytes"] >= 2_000_000


@pytest.mark.parametrize("tz_name", [_WEST_OF_UTC_TZ, _EAST_OF_UTC_TZ])
async def test_get_stats_today_count_uses_local_calendar_day(
    db: ClipDatabase,
    tz_name: str,
) -> None:
    """Regression test: "today"/"yesterday" used to be computed from the
    UTC calendar date compared against UTC-stored timestamps — a clip
    whose local calendar day doesn't match its UTC calendar day (e.g. late
    evening in any timezone behind UTC, which is already "tomorrow" in
    UTC) must be bucketed by the household's actual local day, not roll
    over to the wrong one hours early."""
    with _local_timezone(tz_name):
        ts, is_local_today = _local_boundary_case()
        await db.add_clip(_make_clip("boundary", timestamp=ts))
        stats = await db.get_stats()
    assert stats["today_count"] == (1 if is_local_today else 0)
    assert stats["yesterday_count"] == (0 if is_local_today else 1)


async def test_get_stats_recognized_count(db: ClipDatabase) -> None:
    """Same approved_faces_seen + latest-row-wins semantics as get_clips'
    face_recognized column (see its docstring) — a clip re-analyzed to no
    longer match isn't counted, and an archived clip isn't counted either,
    matching total_count's own archived=FALSE scoping."""
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    await db.add_analysis_result(_make_analysis("c1", approved_faces_seen=True))
    await db.add_analysis_result(_make_analysis("c2", approved_faces_seen=False))
    await db.add_analysis_result(
        _make_analysis(
            "c3", approved_faces_seen=True, analyzed_at="2024-06-01T09:00:00+00:00"
        )
    )
    await db.add_analysis_result(
        _make_analysis(
            "c3", approved_faces_seen=False, analyzed_at="2024-06-01T10:00:00+00:00"
        )
    )
    stats = await db.get_stats()
    assert stats["recognized_count"] == 1


async def test_get_camera_stats(db: ClipDatabase) -> None:
    today = datetime.now(UTC).isoformat()
    await db.add_clip(_make_clip("c1", camera="Front Door", timestamp=today))
    await db.add_clip(_make_clip("c2", camera="Front Door", timestamp=today))
    await db.add_clip(_make_clip("c3", camera="Back Yard", timestamp=today))
    cam_stats = await db.get_camera_stats()
    cameras = {s["camera"]: s for s in cam_stats}
    assert cameras["Front Door"]["total"] == 2
    assert cameras["Back Yard"]["total"] == 1


async def test_get_camera_stats_merges_case_insensitively(db: ClipDatabase) -> None:
    """Regression test: two clips recorded for the same camera under
    different casing (e.g. a Blink camera renamed/retyped over time) must
    fold into a single stats row, matching how get_clips already matches
    camera names case-insensitively elsewhere in this file."""
    today = datetime.now(UTC).isoformat()
    await db.add_clip(_make_clip("c1", camera="Front Door", timestamp=today))
    await db.add_clip(_make_clip("c2", camera="front door", timestamp=today))
    await db.add_clip(_make_clip("c3", camera="FRONT DOOR", timestamp=today))
    cam_stats = await db.get_camera_stats()
    assert len(cam_stats) == 1
    assert cam_stats[0]["total"] == 3


async def test_rename_camera_migrates_library_and_battery_state(
    db: ClipDatabase,
) -> None:
    await db.record_clip_baseline("Front Door", 8, 10.0)
    await db.record_scene_baseline("Front Door", [0.1, 0.2])
    await db.add_clip(_make_clip("rename-1", camera="Front Door"))
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.enqueue_for_analysis("rename-1", "Front Door", "/clips/rename-1.mp4")

    assert await db.rename_camera("Front Door", "Entryway") is True

    clips = await db.get_clips(ClipFilters(camera="Entryway"))
    assert [clip["id"] for clip in clips] == ["rename-1"]
    assert await db.get_clips(ClipFilters(camera="Front Door")) == []
    battery = await db.get_latest_battery_state()
    assert [row["camera"] for row in battery] == ["Entryway"]
    queue = await db.get_pending_analysis()
    assert queue[0]["camera"] == "Entryway"
    assert db._pool is not None
    assert (
        await db._pool.fetchval(
            "SELECT camera FROM camera_baselines WHERE hour = $1", 8
        )
        == "Entryway"
    )
    assert (
        await db._pool.fetchval(
            "SELECT camera FROM camera_duration_stats WHERE camera = $1", "Entryway"
        )
        == "Entryway"
    )
    assert (
        await db._pool.fetchval(
            "SELECT camera FROM camera_scene_baselines WHERE camera = $1", "Entryway"
        )
        == "Entryway"
    )


async def test_rename_camera_skips_reinsert_when_duration_stats_have_no_samples(
    db: ClipDatabase,
) -> None:
    """A camera_duration_stats row can only exist with sample_count >= 1
    through record_clip_baseline(), but the migration's weighted-average
    guard (skip the reinsert if the matched rows sum to zero samples,
    avoiding a division by zero) should still behave safely if a row ever
    is found with no samples -- e.g. a legacy/manually-edited row."""
    assert db._pool is not None
    await db._pool.execute(
        "INSERT INTO camera_duration_stats (camera, avg_duration, sample_count) "
        "VALUES ($1, $2, $3)",
        "Front Door",
        0.0,
        0,
    )

    assert await db.rename_camera("Front Door", "Entryway") is True

    assert (
        await db._pool.fetchval(
            "SELECT COUNT(*) FROM camera_duration_stats WHERE LOWER(camera) = LOWER($1)",
            "Entryway",
        )
        == 0
    )


async def test_rename_camera_noop_without_persisted_state() -> None:
    database = ClipDatabase()
    assert await database.rename_camera("Front Door", "Entryway") is False


async def test_rename_camera_noop_with_no_matching_rows(db: ClipDatabase) -> None:
    assert await db.rename_camera("Unknown", "Entryway") is False


async def test_rename_camera_noop_for_same_name(db: ClipDatabase) -> None:
    assert await db.rename_camera("Front Door", "Front Door") is False


# ----------------------------------------------------------------------
# A camera rename has to reach every table keyed by camera name. Missing
# one is this repository's most-repeated bug — four separate releases have
# shipped a table the rename did not touch, each found only by a user
# whose renamed camera went half-stale. The tests above check the tables
# somebody thought to write a case for, which is exactly the check that
# keeps passing while a thirteenth table sits unmigrated.
#
# This one derives both sides instead: the tables from the live database,
# and the coverage from the rename's own source. Add a table with a
# `camera` column and this fails until the rename handles it.
# ----------------------------------------------------------------------


def _rename_sources() -> str:
    """The source of rename_camera and every helper it delegates to.

    Walks the call graph rather than naming the helpers, so splitting one
    of them in two does not quietly shrink what this test inspects.
    """
    seen: set[str] = set()
    pending = ["rename_camera"]
    chunks: list[str] = []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        method = getattr(ClipDatabase, name, None)
        if method is None:
            continue
        source = inspect.getsource(method)
        chunks.append(source)
        pending.extend(re.findall(r"self\.(_[a-z_]+)\(", source))
    return "\n".join(chunks)


def _mentions(source: str, table: str) -> bool:
    """Whether *source* names *table* at all.

    Deliberately a whole-word search rather than SQL parsing: the eight
    relabelled tables are a plain tuple the UPDATE is formatted against,
    so the table name and the statement never appear together in the text.
    A table named here but somehow not migrated would slip through, which
    is a far less likely mistake than forgetting the table entirely — and
    forgetting it is the one this exists to catch.
    """
    return re.search(rf"\b{re.escape(table)}\b", source) is not None


async def test_rename_camera_touches_every_table_keyed_by_camera(
    db: ClipDatabase,
) -> None:
    """Every table with a `camera` column is handled by the rename path.

    Derived from information_schema rather than a hand-kept list, so a new
    table arrives here on its own. If this fails, the fix is in
    database/cameras.py, not in this test: add the table to
    _rename_camera_rows, or give it its own _migrate_* helper when its
    rows have to be merged rather than relabelled (a per-camera baseline
    that already exists under the new name, for instance).
    """
    assert db._pool is not None
    async with db._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'camera' AND table_schema = current_schema()"
        )
    keyed_by_camera = {row["table_name"] for row in rows}
    # A sanity floor: if the query ever returns nothing the assertion below
    # would pass vacuously and guard nothing at all.
    assert len(keyed_by_camera) >= 12

    source = _rename_sources()
    missing = {table for table in keyed_by_camera if not _mentions(source, table)}
    assert not missing, (
        f"rename_camera does not touch {sorted(missing)} — a camera renamed "
        "in Blink would leave those rows under the old name"
    )


@pytest.mark.parametrize("tz_name", [_WEST_OF_UTC_TZ, _EAST_OF_UTC_TZ])
async def test_get_camera_stats_today_uses_local_calendar_day(
    db: ClipDatabase,
    tz_name: str,
) -> None:
    with _local_timezone(tz_name):
        ts, is_local_today = _local_boundary_case()
        await db.add_clip(_make_clip("boundary", camera="Front Door", timestamp=ts))
        cam_stats = await db.get_camera_stats()
    today = next(s["today"] for s in cam_stats if s["camera"] == "Front Door")
    assert today == (1 if is_local_today else 0)


async def test_get_distinct_tags(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.set_tags("c1", ["cat", "dog"])
    await db.add_clip(_make_clip("c2"))
    await db.set_tags("c2", ["dog", "fish"])
    tags = await db.get_distinct_tags()
    assert "cat" in tags
    assert "dog" in tags
    assert "fish" in tags


# ------------------------------------------------------------------
# No-op when DB not initialised
# ------------------------------------------------------------------


async def test_get_clips_sort_oldest(db: ClipDatabase) -> None:
    for i in range(3):
        ts = f"2024-06-{i + 1:02d}T00:00:00+00:00"
        await db.add_clip(_make_clip(f"c{i}", timestamp=ts))
    clips = await db.get_clips(sort="oldest")
    assert clips[0]["id"] == "c0"
    assert clips[-1]["id"] == "c2"


async def test_get_clips_sort_newest(db: ClipDatabase) -> None:
    for i in range(3):
        ts = f"2024-06-{i + 1:02d}T00:00:00+00:00"
        await db.add_clip(_make_clip(f"c{i}", timestamp=ts))
    clips = await db.get_clips(sort="newest")
    assert clips[0]["id"] == "c2"
    assert clips[-1]["id"] == "c0"


async def test_get_clips_sort_by_camera(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Zebra"))
    await db.add_clip(_make_clip("c2", camera="Alpha"))
    clips = await db.get_clips(sort="camera")
    assert clips[0]["camera"] == "Alpha"


async def test_get_clips_sort_by_size(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", size_bytes=100))
    await db.add_clip(_make_clip("c2", size_bytes=9000))
    clips = await db.get_clips(sort="size")
    assert clips[0]["id"] == "c2"


async def test_operations_without_init_are_safe() -> None:
    d = ClipDatabase()
    assert await d.get_clip("x") is None
    assert await d.get_clips() == []
    assert await d.get_stats() == {}
    assert await d.get_camera_stats() == []
    assert await d.get_clips_to_archive(30) == []
    assert await d.get_all_file_paths() == set()
    assert await d.star_clip("x", True) is False
    assert await d.set_tags("x", []) is False
    assert await d.delete_clip("x") is False
    assert await d.delete_clip_by_path("/tmp/x.mp4") is False
    assert await d.get_scene_deviation("front", [0.1, 0.2]) is None


# ------------------------------------------------------------------
# Activity data
# ------------------------------------------------------------------


async def test_get_activity_data_empty(db: ClipDatabase) -> None:
    data = await db.get_activity_data(days=7)
    assert data == []


async def test_get_activity_data_returns_rows(db: ClipDatabase) -> None:
    now = datetime.now(UTC)
    for i in range(3):
        ts = (now - timedelta(hours=i)).isoformat()
        await db.add_clip(_make_clip(f"act{i}", timestamp=ts))
    data = await db.get_activity_data(days=1)
    assert len(data) >= 1
    row = data[0]
    assert "date" in row
    assert "hour" in row
    assert "count" in row
    assert isinstance(row["count"], int)
    assert row["count"] >= 1


async def test_get_activity_data_excludes_old_clips(db: ClipDatabase) -> None:
    old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.add_clip(_make_clip("old_clip", timestamp=old_ts))
    data = await db.get_activity_data(days=7)
    assert data == []


async def test_get_activity_data_counts_correctly(db: ClipDatabase) -> None:
    # date/hour are bucketed by local time (see database._local_utc_offset_sql),
    # not the UTC fields a bare timestamptz cast would read back under the
    # pinned-UTC session — so the expected bucket must be derived from the
    # local wall clock too, not base's own UTC hour/date, or this test would
    # only pass by coincidence in a UTC-local test environment.
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    local_base = base.astimezone()
    today_str = local_base.date().isoformat()
    hour = local_base.hour
    for i in range(4):
        ts = (base + timedelta(minutes=i)).isoformat()
        await db.add_clip(_make_clip(f"batch{i}", timestamp=ts))
    data = await db.get_activity_data(days=1)
    matching = [r for r in data if r["date"] == today_str and r["hour"] == hour]
    assert len(matching) == 1
    assert matching[0]["count"] == 4


@pytest.mark.parametrize("tz_name", [_WEST_OF_UTC_TZ, _EAST_OF_UTC_TZ])
async def test_get_activity_data_buckets_by_local_calendar_day(
    db: ClipDatabase,
    tz_name: str,
) -> None:
    """Regression test for the Status tab's "Activity — last 7 days" chart
    showing evening clips under tomorrow's date — see
    test_get_stats_today_count_uses_local_calendar_day for the mechanism."""
    with _local_timezone(tz_name):
        ts, is_local_today = _local_boundary_case()
        await db.add_clip(_make_clip("boundary", timestamp=ts))
        local_date = datetime.fromisoformat(ts).astimezone().date().isoformat()
        data = await db.get_activity_data(days=2)
        # Sanity check the fixture actually landed on the day it claims to —
        # if this ever drifted the test below would trivially pass for the
        # wrong reason.
        today_local = datetime.now(UTC).astimezone().date().isoformat()
    matching = [r for r in data if r["date"] == local_date]
    assert len(matching) == 1
    assert matching[0]["count"] == 1
    assert (local_date == today_local) == is_local_today


async def test_get_activity_data_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_activity_data() == []


# ------------------------------------------------------------------
# AI Analysis Results
# ------------------------------------------------------------------


def _make_analysis(clip_id: str = "clip1", **kwargs) -> dict:
    return {
        "clip_id": clip_id,
        "camera": kwargs.get("camera", "Front Door"),
        "model": kwargs.get("model", "llava:7b"),
        "response_text": kwargs.get("response_text", "Person at door"),
        "is_suspicious": kwargs.get("is_suspicious", False),
        "confidence": kwargs.get("confidence", 0.2),
        "summary": kwargs.get("summary", "Normal activity"),
        "frame_count": kwargs.get("frame_count", 3),
        "analysis_duration": kwargs.get("analysis_duration", 4.5),
        "analyzed_at": kwargs.get("analyzed_at", "2024-06-01T09:00:00+00:00"),
        "face_bypass_applied": kwargs.get("face_bypass_applied", False),
        "face_bypass_names": kwargs.get("face_bypass_names", ""),
        "approved_faces_seen": kwargs.get("approved_faces_seen", False),
    }


async def test_add_and_get_analysis_result(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("clip1"))
    await db.add_analysis_result(_make_analysis("clip1"))
    result = await db.get_analysis_for_clip("clip1")
    assert result is not None
    assert result["clip_id"] == "clip1"
    assert result["model"] == "llava:7b"
    assert result["is_suspicious"] is False
    assert result["confidence"] == 0.2


async def test_add_analysis_result_stores_prompt_text(db: ClipDatabase) -> None:
    """v4.0.0 ai_prompt_debug_enabled: prompt_text round-trips through the DB."""
    await db.add_clip(_make_clip("clip1"))
    result = _make_analysis("clip1")
    result["prompt_text"] = "the exact prompt sent to the model"
    await db.add_analysis_result(result)
    stored = await db.get_analysis_for_clip("clip1")
    assert stored is not None
    assert stored["prompt_text"] == "the exact prompt sent to the model"


async def test_add_analysis_result_prompt_text_defaults_empty(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("clip1"))
    await db.add_analysis_result(_make_analysis("clip1"))
    stored = await db.get_analysis_for_clip("clip1")
    assert stored is not None
    assert stored["prompt_text"] == ""


async def test_add_analysis_result_stores_audio_labels(db: ClipDatabase) -> None:
    """6.0.6 ai_audio_analysis_enabled: the recognized sounds round-trip."""
    await db.add_clip(_make_clip("clip1"))
    result = _make_analysis("clip1")
    result["audio_labels"] = '[{"label": "Shout", "score": 0.61}]'
    await db.add_analysis_result(result)
    stored = await db.get_analysis_for_clip("clip1")
    assert stored is not None
    assert stored["audio_labels"] == '[{"label": "Shout", "score": 0.61}]'


async def test_add_analysis_result_audio_labels_default_empty(
    db: ClipDatabase,
) -> None:
    """Every install has the stage off, so this is the usual case."""
    await db.add_clip(_make_clip("clip1"))
    await db.add_analysis_result(_make_analysis("clip1"))
    stored = await db.get_analysis_for_clip("clip1")
    assert stored is not None
    assert stored["audio_labels"] == ""


async def test_get_analysis_for_clip_missing(db: ClipDatabase) -> None:
    assert await db.get_analysis_for_clip("ghost") is None


async def test_get_suspicious_clips(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(
        _make_analysis("c1", is_suspicious=True, confidence=0.9)
    )
    await db.add_analysis_result(
        _make_analysis("c2", is_suspicious=False, confidence=0.1)
    )
    suspicious = await db.get_suspicious_clips()
    assert len(suspicious) == 1
    assert suspicious[0]["clip_id"] == "c1"
    assert suspicious[0]["is_suspicious"] is True


async def test_count_suspicious_clips(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(
        _make_analysis("c1", is_suspicious=True, confidence=0.9)
    )
    await db.add_analysis_result(
        _make_analysis("c2", is_suspicious=False, confidence=0.1)
    )
    assert await db.count_suspicious_clips() == 1


@pytest.mark.parametrize("period", ["today", "week", "month"])
async def test_get_suspicious_clips_period_includes_current(
    db: ClipDatabase, period: str
) -> None:
    """A suspicious clip analyzed right now falls inside every rolling
    period bucket (today/week/month all start at or before local midnight
    today and are open-ended through now)."""
    now = datetime.now(UTC).isoformat()
    await db.add_clip(_make_clip("recent"))
    await db.add_analysis_result(
        _make_analysis("recent", is_suspicious=True, analyzed_at=now)
    )
    await db.add_clip(_make_clip("ancient"))
    await db.add_analysis_result(
        _make_analysis(
            "ancient", is_suspicious=True, analyzed_at="2020-01-01T00:00:00+00:00"
        )
    )

    filtered = await db.get_suspicious_clips(period=period)
    assert [c["clip_id"] for c in filtered] == ["recent"]
    assert await db.count_suspicious_clips(period=period) == 1

    unfiltered = await db.get_suspicious_clips()
    assert len(unfiltered) == 2
    assert await db.count_suspicious_clips() == 2


async def test_get_suspicious_clips_period_yesterday(db: ClipDatabase) -> None:
    yesterday_start, yesterday_end = _local_day_bounds(1)
    # Midpoint of yesterday's local calendar day, safely away from either
    # edge of the [start, end) bucket regardless of the host's timezone.
    mid_yesterday = (
        datetime.fromisoformat(yesterday_start)
        + (
            datetime.fromisoformat(yesterday_end)
            - datetime.fromisoformat(yesterday_start)
        )
        / 2
    ).isoformat()

    await db.add_clip(_make_clip("yday"))
    await db.add_analysis_result(
        _make_analysis("yday", is_suspicious=True, analyzed_at=mid_yesterday)
    )
    await db.add_clip(_make_clip("today"))
    await db.add_analysis_result(
        _make_analysis(
            "today",
            is_suspicious=True,
            analyzed_at=datetime.now(UTC).isoformat(),
        )
    )

    filtered = await db.get_suspicious_clips(period="yesterday")
    assert [c["clip_id"] for c in filtered] == ["yday"]
    assert await db.count_suspicious_clips(period="yesterday") == 1


async def test_get_suspicious_clips_unknown_period_is_unfiltered(
    db: ClipDatabase,
) -> None:
    """An unrecognized period string (defensive — the HTTP layer already
    validates this) must fall back to "all time" rather than matching
    nothing."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis(
            "c1", is_suspicious=True, analyzed_at="2020-01-01T00:00:00+00:00"
        )
    )
    assert len(await db.get_suspicious_clips(period="decade")) == 1
    assert await db.count_suspicious_clips(period="decade") == 1


async def test_get_analysis_stats(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(
        _make_analysis("c1", is_suspicious=True, frame_count=3)
    )
    await db.add_analysis_result(
        _make_analysis("c2", is_suspicious=False, frame_count=5)
    )
    stats = await db.get_analysis_stats()
    assert stats["total_analyzed"] == 2
    assert stats["suspicious_count"] == 1
    assert stats["last_analysis"] is not None
    assert stats["total_frames_analyzed"] == 8
    # Both results use a fixed 2024-06-01 timestamp, not today.
    assert stats["frames_analyzed_today"] == 0


async def test_get_analysis_stats_frames_analyzed_today(db: ClipDatabase) -> None:
    today = datetime.now(UTC).isoformat()
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(_make_analysis("c1", frame_count=4, analyzed_at=today))
    await db.add_analysis_result(
        _make_analysis("c2", frame_count=2, analyzed_at="2024-06-01T09:00:00+00:00")
    )
    stats = await db.get_analysis_stats()
    assert stats["total_frames_analyzed"] == 6
    assert stats["frames_analyzed_today"] == 4


@pytest.mark.parametrize("tz_name", [_WEST_OF_UTC_TZ, _EAST_OF_UTC_TZ])
async def test_get_analysis_stats_frames_analyzed_today_uses_local_calendar_day(
    db: ClipDatabase,
    tz_name: str,
) -> None:
    with _local_timezone(tz_name):
        ts, is_local_today = _local_boundary_case()
        await db.add_clip(_make_clip("c1"))
        await db.add_analysis_result(
            _make_analysis("c1", frame_count=7, analyzed_at=ts)
        )
        stats = await db.get_analysis_stats()
    assert stats["frames_analyzed_today"] == (7 if is_local_today else 0)


async def test_analysis_stats_empty(db: ClipDatabase) -> None:
    stats = await db.get_analysis_stats()
    assert stats["total_analyzed"] == 0
    assert stats["suspicious_count"] == 0
    assert stats["last_analysis"] is None
    assert stats["total_frames_analyzed"] == 0
    assert stats["frames_analyzed_today"] == 0


async def test_get_face_bypass_stats_empty(db: ClipDatabase) -> None:
    stats = await db.get_face_bypass_stats()
    assert stats == {"total_bypassed": 0, "by_name": [], "recent": []}


async def test_get_face_bypass_stats_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_face_bypass_stats() == {
        "total_bypassed": 0,
        "by_name": [],
        "recent": [],
    }


async def test_get_face_bypass_stats_counts_and_recent(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    # Not a bypass — is_suspicious stayed true, no face match.
    await db.add_analysis_result(_make_analysis("c1", is_suspicious=True))
    await db.add_analysis_result(
        _make_analysis(
            "c2",
            camera="Front Door",
            is_suspicious=False,
            face_bypass_applied=True,
            face_bypass_names="Brian",
            analyzed_at="2024-06-01T10:00:00+00:00",
        )
    )
    await db.add_analysis_result(
        _make_analysis(
            "c3",
            camera="Driveway",
            is_suspicious=False,
            face_bypass_applied=True,
            face_bypass_names="Brian, Amy",
            analyzed_at="2024-06-01T11:00:00+00:00",
        )
    )

    stats = await db.get_face_bypass_stats()
    assert stats["total_bypassed"] == 2
    # Most recent first.
    assert [r["clip_id"] for r in stats["recent"]] == ["c3", "c2"]
    assert stats["recent"][0]["camera"] == "Driveway"
    assert stats["recent"][0]["face_bypass_names"] == "Brian, Amy"
    by_name = {row["name"]: row["count"] for row in stats["by_name"]}
    assert by_name == {"Brian": 2, "Amy": 1}


async def test_get_face_bypass_stats_tolerates_blank_names(db: ClipDatabase) -> None:
    """The by_name breakdown must not choke on a row with an empty
    face_bypass_names — the analyzer never produces face_bypass_applied=True
    with blank names (that combination requires a non-empty approved match,
    see _face_bypass_applies), but this is a DB-layer aggregation that
    shouldn't assume every caller upholds that invariant forever."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis(
            "c1",
            is_suspicious=False,
            face_bypass_applied=True,
            face_bypass_names="",
        )
    )
    stats = await db.get_face_bypass_stats()
    assert stats["total_bypassed"] == 1
    assert stats["by_name"] == []


async def test_get_face_bypass_stats_respects_recent_limit(db: ClipDatabase) -> None:
    for i in range(3):
        clip_id = f"c{i}"
        await db.add_clip(_make_clip(clip_id))
        await db.add_analysis_result(
            _make_analysis(
                clip_id,
                is_suspicious=False,
                face_bypass_applied=True,
                face_bypass_names="Brian",
                analyzed_at=f"2024-06-01T1{i}:00:00+00:00",
            )
        )
    stats = await db.get_face_bypass_stats(recent_limit=2)
    assert stats["total_bypassed"] == 3  # total is unaffected by the recent cap
    assert len(stats["recent"]) == 2


async def test_add_and_get_face_recognition_feedback(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_face_recognition_feedback(
        clip_id="c1",
        camera="Front Door",
        report_type="false_positive",
        note="That's not me, wrong person matched.",
        person_name="Brian",
    )

    feedback = await db.get_face_recognition_feedback()
    assert len(feedback) == 1
    assert feedback[0]["clip_id"] == "c1"
    assert feedback[0]["camera"] == "Front Door"
    assert feedback[0]["report_type"] == "false_positive"
    assert feedback[0]["note"] == "That's not me, wrong person matched."
    assert feedback[0]["person_name"] == "Brian"


async def test_add_face_recognition_feedback_person_name_defaults_empty(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_face_recognition_feedback(
        clip_id="c1", camera="Front Door", report_type="false_negative"
    )

    feedback = await db.get_face_recognition_feedback()
    assert feedback[0]["person_name"] == ""


async def test_get_face_recognition_feedback_orders_newest_first_and_respects_limit(
    db: ClipDatabase,
) -> None:
    for i in range(3):
        clip_id = f"c{i}"
        await db.add_clip(_make_clip(clip_id))
        await db.add_face_recognition_feedback(
            clip_id=clip_id, camera="Front Door", report_type="false_negative"
        )
    feedback = await db.get_face_recognition_feedback(limit=2)
    assert len(feedback) == 2
    assert feedback[0]["clip_id"] == "c2"


async def test_get_face_recognition_feedback_empty(db: ClipDatabase) -> None:
    assert await db.get_face_recognition_feedback() == []


async def test_get_face_recognition_feedback_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_face_recognition_feedback() == []


async def test_add_face_recognition_feedback_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.add_face_recognition_feedback(
        clip_id="c1", camera="Front Door", report_type="false_positive"
    )  # should not raise


async def test_save_and_get_detected_objects_summary(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="person",
                confidence=0.9,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=1,
                frame_index=0,
            ),
            DetectedObject(
                label="person",
                confidence=0.7,
                box=(1.0, 1.0, 6.0, 6.0),
                track_id=1,
                frame_index=1,
            ),
            DetectedObject(
                label="car",
                confidence=0.95,
                box=(10.0, 10.0, 20.0, 20.0),
                track_id=None,
                frame_index=0,
            ),
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    by_label = {row["label"]: row for row in summary}
    # One person (one track id), seen in two frames — one person, not two.
    # The raw box total is kept separately as supporting detail.
    assert by_label["person"]["count"] == 1
    assert by_label["person"]["detections"] == 2
    assert by_label["person"]["max_confidence"] == pytest.approx(0.9)
    assert by_label["car"]["count"] == 1
    assert by_label["car"]["detections"] == 1
    assert by_label["car"]["max_confidence"] == pytest.approx(0.95)
    # Ties on the count break on how much evidence there is for it.
    assert summary[0]["label"] == "person"


async def test_detected_objects_summary_counts_objects_not_boxes(
    db: ClipDatabase,
) -> None:
    """Three cars parked through a whole clip are three cars.

    The detector runs over every sampled frame, so counting stored rows
    reported a driveway with three cars in it as "33 cars" — once per car
    per frame. The count follows the tracker's identities; the raw box
    total stays available as `detections`.
    """
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="car",
                confidence=0.8,
                box=(float(car), 0.0, float(car) + 5.0, 5.0),
                # A parked car holds its track id across sampled frames —
                # its box barely moves, so it is matched trivially.
                track_id=car,
                frame_index=frame,
            )
            for frame in range(11)
            for car in range(3)
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    assert summary == [
        {
            "label": "car",
            "count": 3,
            "detections": 33,
            "max_confidence": pytest.approx(0.8),
        }
    ]


async def test_detected_objects_summary_counts_two_that_never_share_a_frame(
    db: ClipDatabase,
) -> None:
    """One car leaves, another arrives: two cars were in the clip, even
    though only ever one was in frame. Counting the per-frame peak alone
    would report one."""
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="car",
                confidence=0.8,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=1,
                frame_index=frame,
            )
            for frame in range(3)
        ]
        + [
            DetectedObject(
                label="car",
                confidence=0.8,
                box=(9.0, 0.0, 14.0, 5.0),
                track_id=2,
                frame_index=frame,
            )
            for frame in range(4, 7)
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    assert summary[0]["count"] == 2
    assert summary[0]["detections"] == 6


async def test_detected_objects_summary_counts_one_moving_subject_once(
    db: ClipDatabase,
) -> None:
    """The shape a real moving subject actually arrives in.

    ByteTrack auto-activates a track only on its first frame and cannot
    re-match a subject that has moved between frames sampled seconds
    apart, so one dog crossing the yard is stored as an id on frame 0 and
    then id-less rows for the rest. That must still read as one dog — the
    per-frame peak is 1 and the only id present is 1.
    """
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="dog",
                confidence=0.87,
                box=(float(frame * 40), 0.0, float(frame * 40 + 20), 30.0),
                track_id=1 if frame == 0 else None,
                frame_index=frame,
            )
            for frame in range(8)
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    assert summary[0]["label"] == "dog"
    assert summary[0]["count"] == 1
    assert summary[0]["detections"] == 8


async def test_detected_objects_summary_falls_back_to_the_peak_without_tracking(
    db: ClipDatabase,
) -> None:
    """Rows stored with no track_id at all (tracking off, or written by an
    older build) would count zero by identity — the per-frame peak is the
    floor precisely so they do not vanish."""
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="person",
                confidence=0.6,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=None,
                frame_index=0,
            ),
            *[
                DetectedObject(
                    label="person",
                    confidence=0.6,
                    box=(float(i), 0.0, float(i) + 4.0, 5.0),
                    track_id=None,
                    frame_index=1,
                )
                for i in range(4)
            ],
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    assert summary[0]["count"] == 4
    assert summary[0]["detections"] == 5


async def test_detected_objects_summary_peak_wins_over_partial_tracking(
    db: ClipDatabase,
) -> None:
    """Three in frame together, only one of them tracked: the count can
    never be lower than what was demonstrably there at once."""
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="car",
                confidence=0.7,
                box=(float(i), 0.0, float(i) + 4.0, 5.0),
                track_id=1 if i == 0 else None,
                frame_index=0,
            )
            for i in range(3)
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    assert summary[0]["count"] == 3


async def test_save_detected_objects_replaces_not_accumulates(
    db: ClipDatabase,
) -> None:
    """A re-analyze must leave exactly the latest detection set behind, not
    pile detections from every past run on top of each other — unlike
    analysis_results, which is kept as history on purpose."""
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="person",
                confidence=0.9,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=1,
                frame_index=0,
            )
        ],
    )
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="dog",
                confidence=0.8,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=None,
                frame_index=0,
            )
        ],
    )

    summary = await db.get_detected_objects_summary("c1")
    assert len(summary) == 1
    assert summary[0]["label"] == "dog"


async def test_save_detected_objects_empty_list_clears_stale_rows(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="person",
                confidence=0.9,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=1,
                frame_index=0,
            )
        ],
    )
    await db.save_detected_objects("c1", [])
    assert await db.get_detected_objects_summary("c1") == []


async def test_get_detected_objects_summary_empty(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    assert await db.get_detected_objects_summary("c1") == []


async def test_detected_objects_deleted_when_clip_deleted(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="person",
                confidence=0.9,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=1,
                frame_index=0,
            )
        ],
    )
    await db.delete_clip("c1")
    assert await db.get_detected_objects_summary("c1") == []


async def test_save_detected_objects_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.save_detected_objects(
        "c1",
        [
            DetectedObject(
                label="person",
                confidence=0.9,
                box=(0.0, 0.0, 5.0, 5.0),
                track_id=1,
                frame_index=0,
            )
        ],
    )  # should not raise


async def test_get_detected_objects_summary_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_detected_objects_summary("c1") == []


# ------------------------------------------------------------------
# Battery History
# ------------------------------------------------------------------


async def test_add_battery_reading_first_reading_inserts_but_returns_false(
    db: ClipDatabase,
) -> None:
    transitioned = await db.add_battery_reading("Front Door", "ok", 3, 165)
    assert transitioned is False

    history = await db.get_battery_history("Front Door")
    assert len(history) == 1
    assert history[0]["battery_state"] == "ok"
    assert history[0]["battery_level"] == 3
    assert history[0]["battery_voltage"] == 165


async def test_add_battery_reading_same_state_does_not_insert(
    db: ClipDatabase,
) -> None:
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    transitioned = await db.add_battery_reading("Front Door", "ok", 3, 160)

    assert transitioned is False
    history = await db.get_battery_history("Front Door")
    assert len(history) == 1


async def test_add_battery_reading_dedupes_on_state_only_not_level_or_voltage(
    db: ClipDatabase,
) -> None:
    """battery_level/battery_voltage drift continuously even while state
    stays the same — only a state change should ever produce a new row."""
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.add_battery_reading("Front Door", "ok", 2, 140)
    await db.add_battery_reading("Front Door", "ok", 1, 120)

    history = await db.get_battery_history("Front Door")
    assert len(history) == 1
    assert history[0]["battery_level"] == 3


async def test_add_battery_reading_transition_returns_true(
    db: ClipDatabase,
) -> None:
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    transitioned = await db.add_battery_reading("Front Door", "low", 0, 105)

    assert transitioned is True
    history = await db.get_battery_history("Front Door")
    assert len(history) == 2
    assert history[0]["battery_state"] == "low"
    assert history[1]["battery_state"] == "ok"


async def test_add_battery_reading_recovery_returns_true(db: ClipDatabase) -> None:
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.add_battery_reading("Front Door", "low", 0, 105)
    transitioned = await db.add_battery_reading("Front Door", "ok", 3, 168)

    assert transitioned is True


async def test_add_battery_reading_preserves_null_level_and_voltage(
    db: ClipDatabase,
) -> None:
    """None is a real "not reported" value, distinct from 0 — must not be
    coerced to 0 or dropped."""
    await db.add_battery_reading("Front Door", "ok", None, None)

    history = await db.get_battery_history("Front Door")
    assert history[0]["battery_level"] is None
    assert history[0]["battery_voltage"] is None


async def test_reset_battery_history_for_replaced_camera(db: ClipDatabase) -> None:
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.reset_battery_history("Front Door")

    assert await db.get_battery_history("Front Door") == []
    assert await db.get_latest_battery_state() == []


async def test_reset_camera_baselines_for_replaced_camera(db: ClipDatabase) -> None:
    await db.record_clip_baseline("Front Door", 8, 10.0)
    await db.record_scene_baseline("Front Door", [0.1, 0.2])
    assert db._pool is not None

    await db.reset_camera_baselines("Front Door")

    assert (
        await db._pool.fetchval(
            "SELECT COUNT(*) FROM camera_baselines WHERE LOWER(camera) = LOWER($1)",
            "Front Door",
        )
        == 0
    )
    assert (
        await db._pool.fetchval(
            "SELECT COUNT(*) FROM camera_duration_stats WHERE LOWER(camera) = LOWER($1)",
            "Front Door",
        )
        == 0
    )
    assert (
        await db._pool.fetchval(
            "SELECT COUNT(*) FROM camera_scene_baselines WHERE LOWER(camera) = LOWER($1)",
            "Front Door",
        )
        == 0
    )


async def test_reset_camera_baselines_without_init_is_safe() -> None:
    database = ClipDatabase()
    await database.reset_camera_baselines("Front Door")


async def test_reset_battery_history_without_init_is_safe() -> None:
    database = ClipDatabase()
    await database.reset_battery_history("Front Door")


async def test_get_battery_history_orders_newest_first_and_respects_limit(
    db: ClipDatabase,
) -> None:
    for state in ("ok", "low", "ok", "low"):
        await db.add_battery_reading("Front Door", state, None, None)

    history = await db.get_battery_history("Front Door", limit=2)
    assert len(history) == 2
    assert history[0]["battery_state"] == "low"
    assert history[1]["battery_state"] == "ok"


async def test_get_battery_history_filters_by_camera_case_insensitive(
    db: ClipDatabase,
) -> None:
    await db.add_battery_reading("Front Door", "ok", None, None)
    await db.add_battery_reading("Backyard", "low", None, None)

    history = await db.get_battery_history("front door")
    assert len(history) == 1
    assert history[0]["camera"] == "Front Door"


async def test_get_battery_history_empty(db: ClipDatabase) -> None:
    assert await db.get_battery_history("Front Door") == []


async def test_get_latest_battery_state_one_row_per_camera(db: ClipDatabase) -> None:
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.add_battery_reading("Front Door", "low", 0, 105)
    await db.add_battery_reading("Backyard", "ok", 3, 170)

    latest = await db.get_latest_battery_state()
    assert len(latest) == 2
    by_camera = {row["camera"]: row for row in latest}
    assert by_camera["Front Door"]["battery_state"] == "low"
    assert by_camera["Backyard"]["battery_state"] == "ok"


async def test_get_latest_battery_state_empty(db: ClipDatabase) -> None:
    assert await db.get_latest_battery_state() == []


async def test_battery_history_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_battery_history("Front Door") == []
    assert await d.get_latest_battery_state() == []


async def test_add_battery_reading_without_init_returns_false() -> None:
    d = ClipDatabase()
    assert await d.add_battery_reading("Front Door", "ok", 3, 165) is False


# ------------------------------------------------------------------
# Analysis Queue
# ------------------------------------------------------------------


async def test_enqueue_and_get_pending(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    pending = await db.get_pending_analysis()
    assert len(pending) == 1
    assert pending[0]["clip_id"] == "c1"
    assert pending[0]["status"] == "pending"
    assert pending[0]["retry_count"] == 0


async def test_requeue_for_retry(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    await db.update_queue_status("c1", "processing")

    await db.requeue_for_retry("c1", retry_count=1, error="connection reset")

    counts = await db.get_queue_counts()
    assert counts["pending"] == 1
    assert counts["processing"] == 0
    pending = await db.get_pending_analysis()
    assert pending[0]["retry_count"] == 1
    assert pending[0]["error_message"] == "connection reset"
    # Requeuing must not mark the clip completed - it hasn't finished.
    assert pending[0]["completed_at"] == ""


async def test_requeue_for_retry_without_init() -> None:
    d = ClipDatabase()
    await d.requeue_for_retry("c1", retry_count=1)  # must not raise


async def test_enqueue_duplicate_ignored(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    pending = await db.get_pending_analysis()
    assert len(pending) == 1


async def test_update_queue_status(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    await db.update_queue_status("c1", "completed")
    pending = await db.get_pending_analysis()
    assert len(pending) == 0


async def test_update_queue_status_failed(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    await db.update_queue_status("c1", "failed", error="Ollama timeout")
    counts = await db.get_queue_counts()
    assert counts["failed"] == 1
    assert counts["pending"] == 0


async def test_get_queue_counts(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    await db.enqueue_for_analysis("c1", "A", "/c1.mp4")
    await db.enqueue_for_analysis("c2", "B", "/c2.mp4")
    await db.enqueue_for_analysis("c3", "C", "/c3.mp4")
    await db.update_queue_status("c2", "completed")
    await db.update_queue_status("c3", "failed", error="err")
    counts = await db.get_queue_counts()
    assert counts["pending"] == 1
    assert counts["completed"] == 1
    assert counts["failed"] == 1


async def test_get_failed_analysis_queue_returns_error_details(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    await db.requeue_for_retry("c1", retry_count=2, error="transient timeout")
    await db.update_queue_status("c1", "failed", error="Ollama timeout")

    failed = await db.get_failed_analysis_queue()
    assert len(failed) == 1
    assert failed[0]["clip_id"] == "c1"
    assert failed[0]["camera"] == "Front Door"
    assert failed[0]["error_message"] == "Ollama timeout"
    assert failed[0]["retry_count"] == 2


async def test_get_failed_analysis_queue_excludes_other_statuses(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.enqueue_for_analysis("c1", "A", "/c1.mp4")
    await db.enqueue_for_analysis("c2", "B", "/c2.mp4")
    await db.update_queue_status("c2", "completed")

    assert await db.get_failed_analysis_queue() == []


async def test_get_failed_analysis_queue_empty(db: ClipDatabase) -> None:
    assert await db.get_failed_analysis_queue() == []


async def test_get_failed_analysis_queue_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_failed_analysis_queue() == []


async def test_analysis_operations_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_analysis_for_clip("x") is None
    assert await d.get_suspicious_clips() == []
    assert await d.count_suspicious_clips() == 0
    assert await d.get_analysis_stats() == {}
    assert await d.get_pending_analysis() == []
    counts = await d.get_queue_counts()
    assert counts == {"pending": 0, "processing": 0, "completed": 0, "failed": 0}


# ------------------------------------------------------------------
# Google Drive Upload Queue
# ------------------------------------------------------------------


async def test_gdrive_enqueue_and_get_pending(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/clips/c1.mp4")
    pending = await db.get_pending_gdrive_uploads()
    assert len(pending) == 1
    assert pending[0]["clip_id"] == "c1"
    assert pending[0]["status"] == "pending"
    assert pending[0]["folder_id"] == ""


async def test_gdrive_enqueue_with_folder_id(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload(
        "c1", "Front Door", "/clips/c1.mp4", folder_id="f1"
    )
    pending = await db.get_pending_gdrive_uploads()
    assert pending[0]["folder_id"] == "f1"


async def test_gdrive_enqueue_duplicate_ignored(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/clips/c1.mp4")
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/clips/c1.mp4")
    pending = await db.get_pending_gdrive_uploads()
    assert len(pending) == 1


async def test_gdrive_update_queue_status(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/clips/c1.mp4")
    await db.update_gdrive_queue_status("c1", "completed")
    assert await db.get_pending_gdrive_uploads() == []


async def test_gdrive_update_queue_status_failed(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/clips/c1.mp4")
    await db.update_gdrive_queue_status("c1", "failed", error="upload timed out")
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1
    assert counts["pending"] == 0


async def test_gdrive_get_queue_counts(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    await db.enqueue_for_gdrive_upload("c1", "A", "/c1.mp4")
    await db.enqueue_for_gdrive_upload("c2", "B", "/c2.mp4")
    await db.enqueue_for_gdrive_upload("c3", "C", "/c3.mp4")
    await db.update_gdrive_queue_status("c2", "completed")
    await db.update_gdrive_queue_status("c3", "failed", error="err")
    counts = await db.get_gdrive_queue_counts()
    assert counts["pending"] == 1
    assert counts["completed"] == 1
    assert counts["failed"] == 1


async def test_gdrive_enqueue_returns_true_for_new_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    assert await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4") is True


async def test_gdrive_enqueue_returns_false_for_already_pending_clip(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4")
    assert await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4") is False


async def test_gdrive_enqueue_retries_a_failed_clip(db: ClipDatabase) -> None:
    """The core bug fix: a clip that failed once must be retryable, not
    stuck forever — get_pending_gdrive_uploads only ever selects
    status='pending', so a failed row needs to actually flip back."""
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4")
    await db.update_gdrive_queue_status("c1", "failed", error="upload timed out")

    requeued = await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4")

    assert requeued is True
    pending = await db.get_pending_gdrive_uploads()
    assert len(pending) == 1
    assert pending[0]["clip_id"] == "c1"
    assert pending[0]["error_message"] == ""


async def test_gdrive_enqueue_does_not_retry_completed_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4")
    await db.update_gdrive_queue_status("c1", "completed")

    requeued = await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4")

    assert requeued is False
    assert await db.get_pending_gdrive_uploads() == []


async def test_gdrive_enqueue_retry_picks_up_new_folder_id(db: ClipDatabase) -> None:
    """A retry via Library's "Upload to Drive" (explicit folder_id) must
    not silently keep the old default folder_id from the failed attempt."""
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4", folder_id="")
    await db.update_gdrive_queue_status("c1", "failed", error="err")

    await db.enqueue_for_gdrive_upload(
        "c1", "Front Door", "/c1.mp4", folder_id="new-folder"
    )

    pending = await db.get_pending_gdrive_uploads()
    assert pending[0]["folder_id"] == "new-folder"


async def test_gdrive_enqueue_without_init_returns_false() -> None:
    d = ClipDatabase()
    assert await d.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4") is False


async def test_get_failed_gdrive_uploads_returns_error_details(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "Front Door", "/c1.mp4")
    await db.update_gdrive_queue_status("c1", "failed", error="quota exceeded")

    failed = await db.get_failed_gdrive_uploads()
    assert len(failed) == 1
    assert failed[0]["clip_id"] == "c1"
    assert failed[0]["camera"] == "Front Door"
    assert failed[0]["error_message"] == "quota exceeded"


async def test_get_failed_gdrive_uploads_excludes_other_statuses(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.enqueue_for_gdrive_upload("c1", "A", "/c1.mp4")
    await db.enqueue_for_gdrive_upload("c2", "B", "/c2.mp4")
    await db.update_gdrive_queue_status("c2", "completed")

    assert await db.get_failed_gdrive_uploads() == []


async def test_get_failed_gdrive_uploads_empty(db: ClipDatabase) -> None:
    assert await db.get_failed_gdrive_uploads() == []


async def test_get_failed_gdrive_uploads_pages_through_a_long_list(
    db: ClipDatabase,
) -> None:
    """A spell of Drive being unreachable fails every queued clip at once,
    so this list is routinely hundreds long — the tab used to render all of
    them in one unbounded column."""
    for i in range(12):
        clip_id = f"c{i:02d}"
        await db.add_clip(_make_clip(clip_id))
        await db.enqueue_for_gdrive_upload(clip_id, "Front Door", f"/{clip_id}.mp4")
        await db.update_gdrive_queue_status(clip_id, "failed", error="quota exceeded")

    first = await db.get_failed_gdrive_uploads(limit=5)
    second = await db.get_failed_gdrive_uploads(limit=5, offset=5)
    last = await db.get_failed_gdrive_uploads(limit=5, offset=10)

    assert [len(page) for page in (first, second, last)] == [5, 5, 2]
    # Every row appears on exactly one page — the ordering is stable enough
    # to page through without repeating or skipping one.
    seen = [row["clip_id"] for page in (first, second, last) for row in page]
    assert len(set(seen)) == 12


async def test_clear_failed_gdrive_uploads_all(db: ClipDatabase) -> None:
    """A failure a user has looked at and decided not to act on should not
    be stuck on their Storage tab forever with Retry as the only way out."""
    for clip_id in ("c1", "c2"):
        await db.add_clip(_make_clip(clip_id))
        await db.enqueue_for_gdrive_upload(clip_id, "Front Door", f"/{clip_id}.mp4")
        await db.update_gdrive_queue_status(clip_id, "failed", error="nope")

    cleared = await db.clear_failed_gdrive_uploads()

    assert cleared == 2
    assert await db.get_failed_gdrive_uploads() == []
    # The clips themselves are untouched — only the queue rows went.
    assert await db.get_clip("c1") is not None


async def test_clear_failed_gdrive_uploads_one(db: ClipDatabase) -> None:
    for clip_id in ("c1", "c2"):
        await db.add_clip(_make_clip(clip_id))
        await db.enqueue_for_gdrive_upload(clip_id, "Front Door", f"/{clip_id}.mp4")
        await db.update_gdrive_queue_status(clip_id, "failed", error="nope")

    assert await db.clear_failed_gdrive_uploads("c1") == 1

    remaining = await db.get_failed_gdrive_uploads()
    assert [row["clip_id"] for row in remaining] == ["c2"]


async def test_clear_failed_gdrive_uploads_leaves_pending_work_alone(
    db: ClipDatabase,
) -> None:
    """A mistimed "Clear All" must not drop a clip that is queued or
    mid-upload out of the queue."""
    for clip_id, status in (("c1", "failed"), ("c2", "pending"), ("c3", "processing")):
        await db.add_clip(_make_clip(clip_id))
        await db.enqueue_for_gdrive_upload(clip_id, "Front Door", f"/{clip_id}.mp4")
        await db.update_gdrive_queue_status(clip_id, status)

    assert await db.clear_failed_gdrive_uploads() == 1

    counts = await db.get_gdrive_queue_counts()
    assert counts["pending"] == 1
    assert counts["processing"] == 1
    assert counts["failed"] == 0


async def test_clear_failed_gdrive_uploads_without_init_returns_zero() -> None:
    d = ClipDatabase()
    assert await d.clear_failed_gdrive_uploads() == 0


async def test_get_failed_gdrive_uploads_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_failed_gdrive_uploads() == []


async def test_retry_failed_gdrive_uploads_all(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))
    for cid in ("c1", "c2", "c3"):
        await db.enqueue_for_gdrive_upload(cid, "A", f"/{cid}.mp4")
    await db.update_gdrive_queue_status("c1", "failed", error="e1")
    await db.update_gdrive_queue_status("c2", "failed", error="e2")
    # c3 stays pending.

    retried = await db.retry_failed_gdrive_uploads()

    assert retried == 2
    counts = await db.get_gdrive_queue_counts()
    assert counts["pending"] == 3
    assert counts["failed"] == 0


async def test_retry_failed_gdrive_uploads_one_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.enqueue_for_gdrive_upload("c1", "A", "/c1.mp4")
    await db.enqueue_for_gdrive_upload("c2", "B", "/c2.mp4")
    await db.update_gdrive_queue_status("c1", "failed", error="e1")
    await db.update_gdrive_queue_status("c2", "failed", error="e2")

    retried = await db.retry_failed_gdrive_uploads(clip_id="c1")

    assert retried == 1
    counts = await db.get_gdrive_queue_counts()
    assert counts["pending"] == 1
    assert counts["failed"] == 1


async def test_retry_failed_gdrive_uploads_preserves_folder_id(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload(
        "c1", "A", "/c1.mp4", folder_id="original-folder"
    )
    await db.update_gdrive_queue_status("c1", "failed", error="e1")

    await db.retry_failed_gdrive_uploads()

    pending = await db.get_pending_gdrive_uploads()
    assert pending[0]["folder_id"] == "original-folder"


async def test_retry_failed_gdrive_uploads_no_failed_rows(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.enqueue_for_gdrive_upload("c1", "A", "/c1.mp4")
    assert await db.retry_failed_gdrive_uploads() == 0


async def test_retry_failed_gdrive_uploads_without_init_returns_zero() -> None:
    d = ClipDatabase()
    assert await d.retry_failed_gdrive_uploads() == 0


async def test_mark_gdrive_uploaded(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.mark_gdrive_uploaded("c1", "drive-file-id-1")
    clip = await db.get_clip("c1")
    assert clip is not None
    assert clip["gdrive_backed_up"] is True
    assert clip["gdrive_file_id"] == "drive-file-id-1"
    assert clip["gdrive_uploaded_at"]


async def test_get_clips_pending_gdrive_backup_archived_only_excludes_unarchived(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("archived-1"))
    await db.mark_archived("archived-1", "/archives/2024-06.zip")
    await db.add_clip(_make_clip("regular-1"))

    pending = await db.get_clips_pending_gdrive_backup(include_unarchived=False)
    assert [c["id"] for c in pending] == ["archived-1"]


async def test_get_clips_pending_gdrive_backup_all_clips_includes_unarchived(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("archived-1"))
    await db.mark_archived("archived-1", "/archives/2024-06.zip")
    await db.add_clip(_make_clip("regular-1"))

    pending = await db.get_clips_pending_gdrive_backup(include_unarchived=True)
    assert {c["id"] for c in pending} == {"archived-1", "regular-1"}


async def test_get_clips_pending_gdrive_backup_excludes_already_backed_up(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("archived-1"))
    await db.mark_archived("archived-1", "/archives/2024-06.zip")
    await db.mark_gdrive_uploaded("archived-1", "already-uploaded-id")

    pending = await db.get_clips_pending_gdrive_backup(include_unarchived=True)
    assert pending == []


async def test_reset_stale_processing_resets_both_queue_tables(
    db: ClipDatabase,
) -> None:
    """_reset_stale_processing (called from init()) must reset a row stuck
    at 'processing' — from a crash mid-analysis/mid-upload — back to
    'pending' in *both* background queue tables, not just analysis_queue."""
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.enqueue_for_analysis("c1", "Front Door", "/c1.mp4")
    await db.enqueue_for_gdrive_upload("c2", "Front Door", "/c2.mp4")
    await db.update_queue_status("c1", "processing")
    await db.update_gdrive_queue_status("c2", "processing")

    await db._reset_stale_processing()

    analysis_counts = await db.get_queue_counts()
    gdrive_counts = await db.get_gdrive_queue_counts()
    assert analysis_counts["pending"] == 1
    assert analysis_counts["processing"] == 0
    assert gdrive_counts["pending"] == 1
    assert gdrive_counts["processing"] == 0


async def test_gdrive_queue_operations_without_init() -> None:
    d = ClipDatabase()
    await d.enqueue_for_gdrive_upload("c1", "Cam", "/c1.mp4")  # must not raise
    await d.update_gdrive_queue_status("c1", "completed")  # must not raise
    await d.mark_gdrive_uploaded("c1", "file-id")  # must not raise
    assert await d.get_pending_gdrive_uploads() == []
    counts = await d.get_gdrive_queue_counts()
    assert counts == {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
    assert await d.get_clips_pending_gdrive_backup(include_unarchived=True) == []


# ------------------------------------------------------------------
# Coverage gap tests
# ------------------------------------------------------------------


def test_row_to_dict_invalid_json_tags() -> None:
    """_row_to_dict falls back to [] when tags column contains invalid JSON (lines 76-77)."""
    row: dict = {
        "id": "c1",
        "camera": "Cam",
        "file_path": "/c1.mp4",
        "timestamp": "2024-01-01T00:00:00+00:00",
        "size_bytes": 100,
        "duration": 5,
        "source": "pir",
        "network_id": 1,
        "starred": 0,
        "tags": "not-valid-json!!!",
        "downloaded_at": "2024-01-01",
        "archived": 0,
        "archive_path": "",
    }
    result = _row_to_dict(row)  # type: ignore[arg-type]
    assert result["tags"] == []


def test_affected_valid_command_tag() -> None:
    assert _affected("UPDATE 3") == 3
    assert _affected("DELETE 0") == 0


def test_affected_malformed_command_tag_returns_zero() -> None:
    """A command tag with no trailing integer (or an empty string) must not
    raise — falls back to 0 rather than crash the caller."""
    assert _affected("not a command tag") == 0
    assert _affected("") == 0


async def test_mark_archived_without_init() -> None:
    """mark_archived() silently returns when db is not initialised (line 161)."""
    d = ClipDatabase()
    await d.mark_archived("c1", "/archive/2024-06.zip")  # must not raise


async def test_get_clips_since_filter(db: ClipDatabase) -> None:
    """get_clips(since=...) restricts results to clips after the timestamp (lines 218-219)."""
    await db.add_clip(_make_clip("old", timestamp="2024-01-01T00:00:00+00:00"))
    await db.add_clip(_make_clip("new", timestamp="2024-06-01T00:00:00+00:00"))
    clips = await db.get_clips(ClipFilters(since="2024-03-01T00:00:00+00:00"))
    assert len(clips) == 1
    assert clips[0]["id"] == "new"


async def test_get_clips_until_filter(db: ClipDatabase) -> None:
    """get_clips(until=...) restricts results to clips before the timestamp (lines 221-222)."""
    await db.add_clip(_make_clip("old", timestamp="2024-01-01T00:00:00+00:00"))
    await db.add_clip(_make_clip("new", timestamp="2024-06-01T00:00:00+00:00"))
    clips = await db.get_clips(ClipFilters(until="2024-03-01T00:00:00+00:00"))
    assert len(clips) == 1
    assert clips[0]["id"] == "old"


async def test_get_clips_source_filter(db: ClipDatabase) -> None:
    """get_clips(source=...) filters by clip source (lines 227-228)."""
    await db.add_clip(_make_clip("c1", source="pir"))
    await db.add_clip(_make_clip("c2", source="cloud"))
    clips = await db.get_clips(ClipFilters(source="cloud"))
    assert len(clips) == 1
    assert clips[0]["id"] == "c2"


async def test_get_clips_tag_filter(db: ClipDatabase) -> None:
    """get_clips(tag=...) filters by tag substring in JSON array (lines 230-231)."""
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.set_tags("c1", ["important", "night"])
    clips = await db.get_clips(ClipFilters(tag="important"))
    assert len(clips) == 1
    assert clips[0]["id"] == "c1"


async def test_get_distinct_tags_without_init() -> None:
    """get_distinct_tags() returns [] when db is not initialised (line 368)."""
    d = ClipDatabase()
    result = await d.get_distinct_tags()
    assert result == []


async def test_get_distinct_tags_bad_json_skipped(db: ClipDatabase) -> None:
    """Clips whose tags column holds invalid JSON are silently skipped (lines 377-378)."""
    await db.add_clip(_make_clip("c1"))
    await db.set_tags("c1", ["good"])
    # Inject bad JSON directly via raw SQL
    assert db._pool is not None
    await db._pool.execute("UPDATE clips SET tags='bad-json!!!' WHERE id='c1'")
    tags = await db.get_distinct_tags()
    assert isinstance(tags, list)
    assert "good" not in tags  # bad JSON skipped entirely


async def test_add_analysis_result_without_init() -> None:
    """add_analysis_result() silently returns when db is not initialised (line 412)."""
    d = ClipDatabase()
    await d.add_analysis_result({"clip_id": "c1", "camera": "A"})  # must not raise


async def test_enqueue_for_analysis_without_init() -> None:
    """enqueue_for_analysis() silently returns when db is not initialised (line 504)."""
    d = ClipDatabase()
    await d.enqueue_for_analysis("c1", "Cam", "/c1.mp4")  # must not raise


async def test_update_queue_status_without_init() -> None:
    """update_queue_status() silently returns when db is not initialised (line 529)."""
    d = ClipDatabase()
    await d.update_queue_status("c1", "completed")  # must not raise


# ------------------------------------------------------------------
# Token usage stats
# ------------------------------------------------------------------


def _make_analysis_tokens(
    clip_id: str = "c1",
    model: str = "llava:7b",
    tokens_prompt: int = 0,
    tokens_completion: int = 0,
) -> dict:
    from datetime import datetime

    return {
        "clip_id": clip_id,
        "camera": "Front Door",
        "model": model,
        "response_text": "",
        "is_suspicious": False,
        "confidence": 0.1,
        "summary": "ok",
        "frame_count": 1,
        "analysis_duration": 0.5,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "tokens_prompt": tokens_prompt,
        "tokens_completion": tokens_completion,
    }


async def test_get_token_usage_stats_empty(db: ClipDatabase) -> None:
    stats = await db.get_token_usage_stats()
    assert stats["total_analyses"] == 0
    assert stats["total_tokens_prompt"] == 0
    assert stats["total_tokens_completion"] == 0
    assert stats["total_tokens"] == 0
    assert stats["by_model"] == []


async def test_get_token_usage_stats_with_data(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_clip(_make_clip("c3"))

    await db.add_analysis_result(_make_analysis_tokens("c1", "llava:7b", 100, 50))
    await db.add_analysis_result(_make_analysis_tokens("c2", "llava:7b", 200, 80))
    await db.add_analysis_result(
        _make_analysis_tokens("c3", "moondream:latest", 60, 30)
    )

    stats = await db.get_token_usage_stats()

    assert stats["total_analyses"] == 3
    assert stats["total_tokens_prompt"] == 360
    assert stats["total_tokens_completion"] == 160
    assert stats["total_tokens"] == 520
    assert len(stats["by_model"]) == 2

    llava = next(m for m in stats["by_model"] if m["model"] == "llava:7b")
    assert llava["analyses"] == 2
    assert llava["tokens_prompt"] == 300
    assert llava["tokens_completion"] == 130


async def test_get_token_usage_stats_without_init() -> None:
    d = ClipDatabase()
    stats = await d.get_token_usage_stats()
    assert stats["total_analyses"] == 0
    assert stats["total_escalations"] == 0
    assert stats["total_escalation_tokens"] == 0
    assert stats["by_model"] == []


def _make_analysis_escalation(
    clip_id: str = "c1",
    model: str = "gpt-4o-mini",
    escalation_model: str = "gpt-4o",
    tokens_prompt: int = 100,
    tokens_completion: int = 20,
    escalation_tokens_prompt: int = 300,
    escalation_tokens_completion: int = 60,
) -> dict:
    result = _make_analysis_tokens(clip_id, model, tokens_prompt, tokens_completion)
    result["escalation_model"] = escalation_model
    result["escalation_tokens_prompt"] = escalation_tokens_prompt
    result["escalation_tokens_completion"] = escalation_tokens_completion
    return result


async def test_get_token_usage_stats_breaks_out_escalation_model(
    db: ClipDatabase,
) -> None:
    """Escalation tokens get their own by_model row, not folded into tier 1's."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(_make_analysis_escalation("c1"))

    stats = await db.get_token_usage_stats()

    assert stats["total_analyses"] == 1
    assert stats["total_escalations"] == 1
    assert stats["total_tokens_prompt"] == 400  # 100 tier-1 + 300 escalation
    assert stats["total_tokens_completion"] == 80  # 20 tier-1 + 60 escalation
    assert stats["total_escalation_tokens"] == 360  # 300 + 60, escalation-only
    assert len(stats["by_model"]) == 2

    tier1 = next(m for m in stats["by_model"] if m["model"] == "gpt-4o-mini")
    assert tier1["escalated"] is False
    assert tier1["tokens_prompt"] == 100
    assert tier1["tokens_completion"] == 20

    escalated = next(m for m in stats["by_model"] if m["model"] == "gpt-4o")
    assert escalated["escalated"] is True
    assert escalated["analyses"] == 1
    assert escalated["tokens_prompt"] == 300
    assert escalated["tokens_completion"] == 60


async def test_add_analysis_result_stores_escalation_provider(
    db: ClipDatabase,
) -> None:
    """escalation_provider (v4.0.0, cross-provider escalation) is stored and
    surfaced on the escalated by_model row so the AI Usage tab can label it
    correctly even when tier 2 is a different provider than tier 1."""
    await db.add_clip(_make_clip("c1"))
    result = _make_analysis_escalation("c1")
    result["escalation_provider"] = "moondream_cloud"
    await db.add_analysis_result(result)

    stats = await db.get_token_usage_stats()
    escalated = next(m for m in stats["by_model"] if m["model"] == "gpt-4o")
    assert escalated["provider"] == "moondream_cloud"


async def test_get_token_usage_stats_escalation_dedupes_across_provider_values(
    db: ClipDatabase,
) -> None:
    """Regression test: rows written before the ``escalation_provider``
    column existed backfill to ``''`` (see :meth:`_migrate`), which used to
    split one escalation model into two duplicate-looking ``by_model`` rows
    whenever older ('') and newer (real provider) rows coexisted. Grouping
    by ``escalation_model`` alone merges them into a single row."""
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))

    old_row = _make_analysis_escalation("c1")
    old_row["escalation_provider"] = ""  # pre-v4.0.0 backfilled default
    await db.add_analysis_result(old_row)

    new_row = _make_analysis_escalation("c2")
    new_row["escalation_provider"] = "openai"
    await db.add_analysis_result(new_row)

    stats = await db.get_token_usage_stats()
    escalated_rows = [m for m in stats["by_model"] if m["model"] == "gpt-4o"]
    assert len(escalated_rows) == 1

    escalated = escalated_rows[0]
    assert escalated["analyses"] == 2
    assert escalated["tokens_prompt"] == 600  # 300 + 300
    assert escalated["tokens_completion"] == 120  # 60 + 60
    assert escalated["provider"] == "openai"  # non-empty value wins over ''


async def test_get_token_usage_stats_no_escalation_by_default(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", tokens_prompt=100, tokens_completion=20)
    )

    stats = await db.get_token_usage_stats()
    assert stats["total_escalations"] == 0
    assert stats["total_escalation_tokens"] == 0
    assert all(not m["escalated"] for m in stats["by_model"])


async def test_clear_ai_usage_stats_resets_counters(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", tokens_prompt=100, tokens_completion=20)
    )

    stats_before = await db.get_token_usage_stats()
    assert stats_before["total_analyses"] == 1

    await db.clear_ai_usage_stats()

    stats_after = await db.get_token_usage_stats()
    assert stats_after["total_analyses"] == 0
    assert stats_after["by_model"] == []

    # Per-clip analysis history (used by Suspicious Clips / clip detail) is
    # untouched by the reset — only the aggregate usage view is cleared.
    result = await db.get_analysis_for_clip("c1")
    assert result is not None
    assert result["tokens_prompt"] == 100


async def test_clear_ai_usage_stats_only_hides_rows_before_reset(
    db: ClipDatabase,
) -> None:
    """Clearing stats sets a cutoff — it doesn't retroactively wipe rows,
    and analyses recorded after the clear count normally again."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", tokens_prompt=100, tokens_completion=20)
    )
    await db.clear_ai_usage_stats()

    reset_at = await db._get_ai_usage_reset_at()
    later = datetime.fromisoformat(reset_at) + timedelta(seconds=1)

    await db.add_clip(_make_clip("c2"))
    row = _make_analysis_tokens("c2", tokens_prompt=50, tokens_completion=10)
    row["analyzed_at"] = later.isoformat()
    await db.add_analysis_result(row)

    stats = await db.get_token_usage_stats()
    assert stats["total_analyses"] == 1
    assert stats["total_tokens_prompt"] == 50
    assert stats["total_tokens_completion"] == 10


async def test_clear_ai_usage_stats_without_init() -> None:
    d = ClipDatabase()
    await d.clear_ai_usage_stats()  # must not raise


# ------------------------------------------------------------------
# Daily usage history (AI Usage tab's "last 14 days" table)
# ------------------------------------------------------------------


async def test_get_daily_usage_stats_empty(db: ClipDatabase) -> None:
    assert await db.get_daily_usage_stats() == []


async def test_get_daily_usage_stats_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_daily_usage_stats() == []


async def test_get_daily_usage_stats_buckets_same_day_same_model(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", "llava:7b", tokens_prompt=100, tokens_completion=20)
    )
    await db.add_analysis_result(
        _make_analysis_tokens("c2", "llava:7b", tokens_prompt=50, tokens_completion=10)
    )

    daily = await db.get_daily_usage_stats()
    assert len(daily) == 1
    row = daily[0]
    assert row["model"] == "llava:7b"
    assert row["escalated"] is False
    assert row["analyses"] == 2
    assert row["tokens_prompt"] == 150
    assert row["tokens_completion"] == 30


async def test_get_daily_usage_stats_separates_different_days(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    today = datetime.now(UTC)
    row_today = _make_analysis_tokens("c1", "llava:7b", 100, 20)
    row_today["analyzed_at"] = today.isoformat()
    row_yesterday = _make_analysis_tokens("c2", "llava:7b", 50, 10)
    row_yesterday["analyzed_at"] = (today - timedelta(days=1)).isoformat()
    await db.add_analysis_result(row_today)
    await db.add_analysis_result(row_yesterday)

    daily = await db.get_daily_usage_stats()
    assert len(daily) == 2
    days = {row["day"] for row in daily}
    # Bucketed by local calendar day (see database._local_utc_offset_sql),
    # not the UTC date these fixtures happen to be built from.
    today_local = today.astimezone().date().isoformat()
    yesterday_local = (today - timedelta(days=1)).astimezone().date().isoformat()
    assert today_local in days
    assert yesterday_local in days
    # Most recent day first.
    assert daily[0]["day"] == today_local


async def test_get_daily_usage_stats_excludes_data_outside_window(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    old_row = _make_analysis_tokens("c1", "llava:7b", 100, 20)
    old_row["analyzed_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.add_analysis_result(old_row)

    assert await db.get_daily_usage_stats(days=14) == []


async def test_get_daily_usage_stats_includes_escalation_as_separate_row(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(_make_analysis_escalation("c1"))

    daily = await db.get_daily_usage_stats()
    assert len(daily) == 2

    tier1 = next(r for r in daily if r["model"] == "gpt-4o-mini")
    escalated = next(r for r in daily if r["model"] == "gpt-4o")
    assert tier1["escalated"] is False
    assert escalated["escalated"] is True
    assert escalated["tokens_prompt"] == 300
    assert escalated["tokens_completion"] == 60


async def test_get_daily_usage_stats_respects_reset_cutoff(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", "llava:7b", tokens_prompt=100, tokens_completion=20)
    )
    await db.clear_ai_usage_stats()

    reset_at = await db._get_ai_usage_reset_at()
    later = datetime.fromisoformat(reset_at) + timedelta(seconds=1)

    await db.add_clip(_make_clip("c2"))
    row = _make_analysis_tokens(
        "c2", "llava:7b", tokens_prompt=50, tokens_completion=10
    )
    row["analyzed_at"] = later.isoformat()
    await db.add_analysis_result(row)

    daily = await db.get_daily_usage_stats()
    assert len(daily) == 1
    assert daily[0]["tokens_prompt"] == 50


async def test_analysis_result_stores_tokens(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", tokens_prompt=150, tokens_completion=75)
    )
    result = await db.get_analysis_for_clip("c1")
    assert result is not None
    assert result["tokens_prompt"] == 150
    assert result["tokens_completion"] == 75


async def test_reinit_against_existing_data_is_idempotent(db: ClipDatabase) -> None:
    """Running init() against an already-initialized database does not raise.

    CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS make this safe —
    unlike the old SQLite-per-install schema, PostgreSQL's schema is fully
    declared up front with no incremental ALTER TABLE migration step needed,
    since every install of this add-on starts from the same fresh database.
    """
    await db.add_clip(_make_clip("c1"))
    d2 = ClipDatabase(TEST_DB_DSN)
    await d2.init()  # should not raise, and must see the same data
    try:
        assert await d2.get_clip("c1") is not None
    finally:
        await d2.close()


# ------------------------------------------------------------------
# Behavior-memory baseline / anomaly scoring
# ------------------------------------------------------------------


async def test_record_clip_baseline_increments_count(db: ClipDatabase) -> None:
    await db.record_clip_baseline("Driveway", 8, 5.0)
    await db.record_clip_baseline("Driveway", 8, 6.0)
    await db.record_clip_baseline("Driveway", 8, 4.0)
    # Three events should be stored; score still 0 (total < 30)
    score = await db.get_anomaly_score("Driveway", 8, 5.0)
    assert score == 0.0


async def test_get_anomaly_score_returns_zero_below_threshold(db: ClipDatabase) -> None:
    for _ in range(29):
        await db.record_clip_baseline("Front Door", 10, 5.0)
    # 29 events – one below the 30-event activation threshold
    score = await db.get_anomaly_score("Front Door", 10, 5.0)
    assert score == 0.0


async def test_get_anomaly_score_activates_at_threshold(db: ClipDatabase) -> None:
    for _ in range(30):
        await db.record_clip_baseline("Backyard", 14, 5.0)
    # All events are at hour 14; querying hour 14 should score 0.0 (common hour)
    score = await db.get_anomaly_score("Backyard", 14, 5.0)
    assert score == 0.0


async def test_get_anomaly_score_unseen_hour_adds_max_rarity(db: ClipDatabase) -> None:
    # Record 30 events only at hour 8
    for _ in range(30):
        await db.record_clip_baseline("Garage", 8, 5.0)
    # Hour 3 has never been seen → +0.5 rarity contribution
    score = await db.get_anomaly_score("Garage", 3, 5.0)
    assert score >= 0.5


async def test_get_anomaly_score_capped_at_one(db: ClipDatabase) -> None:
    # 30 events at one hour; query an unseen hour with a very long clip
    for _ in range(30):
        await db.record_clip_baseline("Side Gate", 12, 5.0)
    # unseen hour (+0.5) + very long clip duration (+0.25) → would be 0.75; capped at 1.0
    score = await db.get_anomaly_score("Side Gate", 3, 100.0)
    assert 0.0 <= score <= 1.0


async def test_record_clip_baseline_zero_duration_skips_duration_stats(
    db: ClipDatabase,
) -> None:
    for _ in range(30):
        await db.record_clip_baseline("Porch", 9, 0.0)
    # No duration stats recorded; score should not include duration component
    score = await db.get_anomaly_score("Porch", 9, 0.0)
    assert score == 0.0


async def test_get_anomaly_score_uninitialised_db() -> None:
    d = ClipDatabase()
    score = await d.get_anomaly_score("Camera", 8, 5.0)
    assert score == 0.0


async def test_total_camera_events_uninitialised_db() -> None:
    """get_anomaly_score already short-circuits on an unconnected db before
    ever reaching this — call the private helper directly to reach its own
    redundant guard."""
    d = ClipDatabase()
    assert await d._total_camera_events("Camera") == 0


async def test_hour_event_count_uninitialised_db() -> None:
    d = ClipDatabase()
    assert await d._hour_event_count("Camera", 8) == 0


async def test_score_duration_anomaly_uninitialised_db() -> None:
    d = ClipDatabase()
    assert await d._score_duration_anomaly("Camera", 5.0) == 0.0


async def test_score_duration_anomaly_no_history_returns_zero(
    db: ClipDatabase,
) -> None:
    """A camera with no recorded duration stats at all (no row, as opposed
    to too few samples) must score 0.0, not raise on a None row."""
    assert await db._score_duration_anomaly("Never Seen Camera", 5.0) == 0.0


async def test_score_duration_anomaly_below_sample_floor_returns_zero(
    db: ClipDatabase,
) -> None:
    """Fewer than 10 recorded samples for this camera → not enough history
    to score, regardless of how anomalous the duration looks."""
    for _ in range(5):
        await db.record_clip_baseline("New Camera", 12, 5.0)
    assert await db._score_duration_anomaly("New Camera", 500.0) == 0.0


async def test_score_duration_anomaly_zero_average_returns_zero(
    db: ClipDatabase,
) -> None:
    """A stored avg_duration of 0 (not reachable via record_clip_baseline,
    which only ever writes a positive duration, but defensive against any
    other origin for this row) must not raise a divide-by-zero computing
    the ratio."""
    assert db._pool is not None
    await db._pool.execute(
        "INSERT INTO camera_duration_stats (camera, avg_duration, sample_count) "
        "VALUES ($1, $2, $3)",
        "Zero Avg Camera",
        0.0,
        10,
    )
    assert await db._score_duration_anomaly("Zero Avg Camera", 5.0) == 0.0


async def test_record_clip_baseline_uninitialised_db() -> None:
    d = ClipDatabase()
    # Should not raise even when db is not open
    await d.record_clip_baseline("Camera", 8, 5.0)


# ---------------------------------------------------------------------------
# Coverage: very-rare-hour (line 642), uncommon-hour (line 644), and
# slight-duration-anomaly (line 660) branches in get_anomaly_score
# ---------------------------------------------------------------------------


async def test_get_anomaly_score_very_rare_hour(db: ClipDatabase) -> None:
    """hour_count < expected * 0.15 → score += 0.35 (line 642)."""
    # 200 events at hour 8 → expected_per_hour = 200/24 ≈ 8.33
    # 0.15 threshold ≈ 1.25 → hour_count=1 qualifies as very rare
    for _ in range(200):
        await db.record_clip_baseline("Patio", 8, 5.0)
    # Add exactly 1 event at hour 6 so it is very rare but not zero
    await db.record_clip_baseline("Patio", 6, 5.0)
    score = await db.get_anomaly_score("Patio", 6, 0.0)
    assert score >= 0.35


async def test_get_anomaly_score_uncommon_hour(db: ClipDatabase) -> None:
    """hour_count in [0.15, 0.35) of expected → score += 0.15 (line 644)."""
    # 200 events at hour 8 → expected ≈ 8.33; 0.15 threshold ≈ 1.25, 0.35 ≈ 2.9
    # hour_count=2 is in [1.25, 2.9) → uncommon
    for _ in range(200):
        await db.record_clip_baseline("Pool", 8, 5.0)
    await db.record_clip_baseline("Pool", 10, 5.0)
    await db.record_clip_baseline("Pool", 10, 5.0)
    score = await db.get_anomaly_score("Pool", 10, 0.0)
    assert score >= 0.15
    assert score < 0.5  # not in the very-rare or zero-seen range


async def test_get_anomaly_score_slight_duration_anomaly(db: ClipDatabase) -> None:
    """duration ratio in (2.5, 4.0) → score += 0.1 (line 660)."""
    # Need at least 10 duration samples and a known average
    # Record 10 clips with duration=5.0 → avg_duration=5.0
    for _ in range(30):
        await db.record_clip_baseline("Gate", 12, 5.0)
    for _ in range(10):
        await db.record_clip_baseline("Gate", 12, 5.0)
    # A clip at a normal hour (hour 12 has many events → hour_count not zero)
    # Duration 15.0 → ratio = 15/5 = 3.0, which is in (2.5, 4.0) → slight anomaly
    score = await db.get_anomaly_score("Gate", 12, 15.0)
    assert score >= 0.1


# ------------------------------------------------------------------
# Scene baseline (visual "smart brain" learning)
# ------------------------------------------------------------------


async def test_get_scene_deviation_unknown_camera_returns_none(
    db: ClipDatabase,
) -> None:
    assert await db.get_scene_deviation("Nowhere", [0.5] * 4) is None


async def test_get_scene_deviation_returns_none_below_threshold(
    db: ClipDatabase,
) -> None:
    scene = [0.2] * 4
    for _ in range(19):  # one short of the 20-sample activation threshold
        await db.record_scene_baseline("Driveway", scene)
    assert await db.get_scene_deviation("Driveway", scene) is None


async def test_get_scene_deviation_activates_at_threshold_low_for_match(
    db: ClipDatabase,
) -> None:
    scene = [0.2] * 4
    for _ in range(20):
        await db.record_scene_baseline("Driveway", scene)
    deviation = await db.get_scene_deviation("Driveway", scene)
    assert deviation is not None
    assert deviation < 0.05  # identical thumbnail → near-zero deviation


async def test_get_scene_deviation_detects_large_change(db: ClipDatabase) -> None:
    usual = [0.0] * 4
    for _ in range(20):
        await db.record_scene_baseline("Backyard", usual)
    deviation = await db.get_scene_deviation("Backyard", [1.0] * 4)
    assert deviation is not None
    assert deviation > 0.5


async def test_get_scene_deviation_capped_at_one(db: ClipDatabase) -> None:
    for _ in range(20):
        await db.record_scene_baseline("Porch", [0.0] * 4)
    deviation = await db.get_scene_deviation("Porch", [1.0] * 4)
    assert deviation is not None
    assert deviation <= 1.0


async def test_get_scene_deviation_query_length_mismatch_returns_none(
    db: ClipDatabase,
) -> None:
    scene = [0.5] * 4
    for _ in range(20):
        await db.record_scene_baseline("Garage", scene)
    assert await db.get_scene_deviation("Garage", [0.5] * 5) is None


async def test_record_scene_baseline_restarts_on_size_change(
    db: ClipDatabase,
) -> None:
    """A thumbnail-size change (e.g. after a config change) restarts the
    baseline from scratch rather than blending mismatched data."""
    await db.record_scene_baseline("Side Gate", [0.0, 0.0])
    await db.record_scene_baseline("Side Gate", [1.0, 1.0, 1.0])
    # Restarted at sample_count=1 — well below the activation threshold.
    assert await db.get_scene_deviation("Side Gate", [1.0, 1.0, 1.0]) is None


async def test_record_scene_baseline_adapts_toward_new_normal(
    db: ClipDatabase,
) -> None:
    """The baseline should shift toward a consistently different scene over
    time rather than staying anchored to whatever the first sample showed."""
    old_scene = [0.0] * 4
    new_scene = [1.0] * 4
    await db.record_scene_baseline("Yard", old_scene)
    for _ in range(24):
        await db.record_scene_baseline("Yard", new_scene)
    deviation_from_new = await db.get_scene_deviation("Yard", new_scene)
    assert deviation_from_new is not None
    assert deviation_from_new < 0.2


async def test_get_scene_deviation_corrupt_thumbnail_returns_none(
    db: ClipDatabase,
) -> None:
    """A row whose stored ``thumbnail`` isn't valid JSON (e.g. from a prior
    schema/format change) should be treated as no usable baseline rather than
    raising."""
    assert db._pool is not None
    await db._pool.execute(
        "INSERT INTO camera_scene_baselines (camera, thumbnail, sample_count, "
        "updated_at) VALUES ($1, $2, $3, $4)",
        "Corrupt",
        "not valid json",
        25,
        "2024-01-01T00:00:00",
    )
    assert await db.get_scene_deviation("Corrupt", [0.5] * 4) is None


async def test_record_scene_baseline_recovers_from_corrupt_existing_data(
    db: ClipDatabase,
) -> None:
    """If the stored thumbnail is corrupt JSON, recording a new sample should
    restart the baseline from scratch instead of raising."""
    assert db._pool is not None
    await db._pool.execute(
        "INSERT INTO camera_scene_baselines (camera, thumbnail, sample_count, "
        "updated_at) VALUES ($1, $2, $3, $4)",
        "Corrupt2",
        "not valid json",
        5,
        "2024-01-01T00:00:00",
    )
    await db.record_scene_baseline("Corrupt2", [0.5, 0.5])
    # Restarted at sample_count=1 (0 + 1) since the prior data was unusable.
    for _ in range(19):
        await db.record_scene_baseline("Corrupt2", [0.5, 0.5])
    deviation = await db.get_scene_deviation("Corrupt2", [0.5, 0.5])
    assert deviation is not None
    assert deviation < 0.05


async def test_get_scene_deviation_uninitialised_db() -> None:
    d = ClipDatabase()
    assert await d.get_scene_deviation("Camera", [0.5] * 4) is None


async def test_record_scene_baseline_uninitialised_db() -> None:
    d = ClipDatabase()
    # Should not raise even when the db is not open
    await d.record_scene_baseline("Camera", [0.5] * 4)


async def _scene_streak(db: ClipDatabase, camera: str) -> int:
    assert db._pool is not None
    value = await db._pool.fetchval(
        "SELECT consecutive_deviation_count FROM camera_scene_baselines WHERE camera=$1",
        camera,
    )
    return int(value) if value is not None else 0


async def test_record_scene_baseline_fast_refresh_on_persistent_change(
    db: ClipDatabase,
) -> None:
    """Once established, 5 consecutive elevated-deviation clips in a row are
    treated as a persistent scene change and snap the baseline toward the
    new normal in one fast blend, instead of the ~0.23 an unassisted slow
    EMA would reach after the same 5 samples."""
    old_scene = [0.0] * 4
    new_scene = [1.0] * 4
    for _ in range(20):
        await db.record_scene_baseline("Patio", old_scene)
    deviation_before = await db.get_scene_deviation("Patio", new_scene)
    assert deviation_before is not None
    assert deviation_before > 0.5

    for _ in range(5):
        await db.record_scene_baseline("Patio", new_scene)
    assert await _scene_streak(db, "Patio") == 0  # streak resets once it fires

    deviation_after = await db.get_scene_deviation("Patio", new_scene)
    assert deviation_after is not None
    assert deviation_after < deviation_before
    assert deviation_after < 0.5


async def test_record_scene_baseline_streak_resets_on_matching_sample(
    db: ClipDatabase,
) -> None:
    """A deviation streak interrupted by a sample close to the current
    baseline resets to zero rather than accumulating toward the fast-refresh
    threshold, so a one-off flicker doesn't get treated as a real change."""
    old_scene = [0.0] * 4
    new_scene = [1.0] * 4
    for _ in range(20):
        await db.record_scene_baseline("Alley", old_scene)

    await db.record_scene_baseline("Alley", new_scene)
    await db.record_scene_baseline("Alley", new_scene)
    assert await _scene_streak(db, "Alley") == 2

    # Baseline has only drifted slightly toward new_scene so far — a sample
    # back at the original scene is still close enough to reset the streak.
    await db.record_scene_baseline("Alley", old_scene)
    assert await _scene_streak(db, "Alley") == 0

    for _ in range(4):
        await db.record_scene_baseline("Alley", new_scene)
    # Never reached 5 *consecutive* elevated hits, so no fast refresh fired.
    assert await _scene_streak(db, "Alley") == 4
    deviation = await db.get_scene_deviation("Alley", new_scene)
    assert deviation is not None
    assert deviation > 0.6


async def test_record_scene_baseline_no_streak_before_established(
    db: ClipDatabase,
) -> None:
    """While a camera's baseline is still ramping up (below the minimum
    sample count), elevated deviation between clips doesn't accumulate a
    fast-refresh streak — the fast early-sample alpha already adapts quickly."""
    for i in range(10):
        # Alternate wildly so every sample would count as "elevated" if the
        # streak counter were active this early.
        scene = [float(i % 2)] * 4
        await db.record_scene_baseline("Yard", scene)
    assert await _scene_streak(db, "Yard") == 0


async def test_record_scene_baseline_restart_resets_streak(
    db: ClipDatabase,
) -> None:
    """A thumbnail-size change restarts sample_count *and* the deviation
    streak, so a stale streak can't immediately trigger a fast refresh
    against the freshly-restarted baseline."""
    old_scene = [0.0] * 4
    new_scene = [1.0] * 4
    for _ in range(20):
        await db.record_scene_baseline("Roof", old_scene)
    for _ in range(3):
        await db.record_scene_baseline("Roof", new_scene)
    assert await _scene_streak(db, "Roof") == 3

    await db.record_scene_baseline("Roof", [0.5, 0.5, 0.5])  # size change
    assert await _scene_streak(db, "Roof") == 0


# ===========================================================================
# v4.0.0 — Adaptive learning from feedback (analysis_feedback)
# ===========================================================================


async def test_add_and_get_feedback_for_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        correction_note="It was just the mail carrier.",
        corrected_suspicious=False,
    )
    fb = await db.get_feedback_for_clip("c1")
    assert fb is not None
    assert fb["camera"] == "Front Door"
    assert fb["original_suspicious"] is True
    assert fb["correct"] is False
    assert fb["correction_note"] == "It was just the mail carrier."
    assert fb["corrected_suspicious"] is False


async def test_get_feedback_for_clip_missing_returns_none(db: ClipDatabase) -> None:
    assert await db.get_feedback_for_clip("ghost") is None


async def test_delete_feedback_removes_row_and_returns_true(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        corrected_suspicious=False,
    )
    assert await db.delete_feedback("c1") is True
    assert await db.get_feedback_for_clip("c1") is None


async def test_delete_feedback_missing_returns_false(db: ClipDatabase) -> None:
    assert await db.delete_feedback("ghost") is False


async def test_delete_feedback_without_init_returns_false() -> None:
    d = ClipDatabase()
    assert await d.delete_feedback("c1") is False


async def test_get_feedback_for_clip_without_init_returns_none() -> None:
    d = ClipDatabase()
    assert await d.get_feedback_for_clip("clip1") is None


async def test_add_feedback_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.add_feedback(
        clip_id="c1",
        camera="Cam",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.5,
        correct=True,
    )  # should not raise


async def test_add_feedback_resubmission_replaces_previous(db: ClipDatabase) -> None:
    """One feedback row per clip — resubmitting replaces, not accumulates."""
    await db.add_clip(_make_clip("c1"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
    )
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
        correction_note="actually correct after all",
    )
    fb = await db.get_feedback_for_clip("c1")
    assert fb is not None
    assert fb["correct"] is True
    assert fb["correction_note"] == "actually correct after all"

    recent = await db.get_recent_feedback("Front Door")
    assert len(recent) == 1


async def test_get_untrained_feedback_returns_new_rows_oldest_first(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        corrected_suspicious=False,
    )
    await db.add_feedback(
        clip_id="c2",
        camera="Driveway",
        analysis_result_id=None,
        original_suspicious=False,
        original_confidence=0.3,
        correct=True,
    )
    untrained = await db.get_untrained_feedback(limit=10)
    assert [row["clip_id"] for row in untrained] == ["c1", "c2"]


async def test_get_untrained_feedback_respects_limit(db: ClipDatabase) -> None:
    for i in range(3):
        clip_id = f"c{i}"
        await db.add_clip(_make_clip(clip_id))
        await db.add_feedback(
            clip_id=clip_id,
            camera="Front Door",
            analysis_result_id=None,
            original_suspicious=True,
            original_confidence=0.8,
            correct=True,
        )
    assert len(await db.get_untrained_feedback(limit=2)) == 2


async def test_mark_feedback_trained_excludes_from_future_queries(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
    )
    untrained = await db.get_untrained_feedback(limit=10)
    assert len(untrained) == 1

    await db.mark_feedback_trained([untrained[0]["id"]])
    assert await db.get_untrained_feedback(limit=10) == []


async def test_mark_feedback_trained_empty_list_is_noop(db: ClipDatabase) -> None:
    await db.mark_feedback_trained([])  # should not raise


async def test_get_untrained_feedback_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_untrained_feedback() == []


async def test_mark_feedback_trained_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.mark_feedback_trained([1, 2])  # should not raise


async def test_get_recent_feedback_filters_by_camera(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Driveway"))
    await db.add_feedback(
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
        corrected_suspicious=True,
    )
    await db.add_feedback(
        clip_id="c2",
        camera="Driveway",
        analysis_result_id=None,
        original_suspicious=False,
        original_confidence=0.2,
        correct=True,
    )
    front_door = await db.get_recent_feedback("Front Door")
    assert len(front_door) == 1
    assert front_door[0]["camera"] == "Front Door"

    all_feedback = await db.get_recent_feedback()
    assert len(all_feedback) == 2


async def test_get_recent_feedback_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_recent_feedback("Cam") == []


async def test_get_feedback_stats_counts_correct_and_incorrect(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Front Door"))
    await db.add_clip(_make_clip("c3", camera="Front Door"))
    # False positive: AI said suspicious, human says incorrect.
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
    )
    # False negative: AI said not suspicious, human says incorrect (missed it).
    await db.add_feedback(
        "c2",
        "Front Door",
        None,
        original_suspicious=False,
        original_confidence=0.1,
        correct=False,
    )
    # Correct verdict.
    await db.add_feedback(
        "c3",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.9,
        correct=True,
    )

    stats = await db.get_feedback_stats("Front Door")
    assert stats["total"] == 3
    assert stats["correct"] == 1
    assert stats["incorrect"] == 2
    assert stats["false_positive"] == 1
    assert stats["false_negative"] == 1


async def test_get_feedback_stats_without_init_returns_empty() -> None:
    d = ClipDatabase()
    stats = await d.get_feedback_stats()
    assert stats["total"] == 0


async def test_get_feedback_stats_returns_empty_when_fetchrow_returns_none(
    db: ClipDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive fallback: a bare aggregate query (no GROUP BY) always
    returns exactly one row even over an empty table, so this can't happen
    through real Postgres query semantics — covers the fallback directly in
    case a future change to this query ever changes that. asyncpg's real
    Pool doesn't allow monkeypatching individual methods (read-only
    attributes), so get_feedback_stats' one pool call (fetchrow) is stubbed
    via a minimal stand-in object instead."""

    class _FetchrowNonePool:
        fetchrow = AsyncMock(return_value=None)

    monkeypatch.setattr(db, "_pool", _FetchrowNonePool())
    stats = await db.get_feedback_stats()
    assert stats["total"] == 0


async def test_get_feedback_stats_global_when_no_camera_given(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Driveway"))
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
    )
    await db.add_feedback(
        "c2",
        "Driveway",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
    )
    stats = await db.get_feedback_stats()
    assert stats["total"] == 2


# ---------------------------------------------------------------------------
# get_effective_confidence_threshold
# ---------------------------------------------------------------------------


async def _add_n_feedback(
    db: ClipDatabase, camera: str, n: int, false_positives: int
) -> None:
    """Add n feedback rows for camera, with the first false_positives of them
    marked as false positives (suspicious=True, correct=False) and the rest
    marked correct."""
    for i in range(n):
        clip_id = f"{camera}-{i}"
        await db.add_clip(_make_clip(clip_id, camera=camera))
        is_fp = i < false_positives
        await db.add_feedback(
            clip_id,
            camera,
            None,
            original_suspicious=True,
            original_confidence=0.8,
            correct=not is_fp,
        )


async def test_effective_threshold_unchanged_below_min_samples(
    db: ClipDatabase,
) -> None:
    """Fewer than the minimum feedback rows for a camera means no adjustment
    at all, regardless of how many false positives are among them."""
    await _add_n_feedback(db, "Front Door", n=5, false_positives=5)
    threshold = await db.get_effective_confidence_threshold("Front Door", 0.5)
    assert threshold == 0.5


async def test_effective_threshold_unchanged_with_no_false_positives(
    db: ClipDatabase,
) -> None:
    await _add_n_feedback(db, "Front Door", n=15, false_positives=0)
    threshold = await db.get_effective_confidence_threshold("Front Door", 0.5)
    assert threshold == 0.5


async def test_effective_threshold_steps_up_with_false_positives(
    db: ClipDatabase,
) -> None:
    """Every 3 false positives in the trailing window nudges the threshold up
    by 0.05, so 6 false positives out of 15 samples means 2 steps (+0.10)."""
    await _add_n_feedback(db, "Front Door", n=15, false_positives=6)
    threshold = await db.get_effective_confidence_threshold("Front Door", 0.5)
    assert threshold == pytest.approx(0.6)


async def test_effective_threshold_capped_at_max_steps(db: ClipDatabase) -> None:
    """Even with every sample a false positive, the adjustment never exceeds
    3 steps (+0.15) so a burst of bad luck can't push the threshold to
    near-certainty."""
    await _add_n_feedback(db, "Front Door", n=20, false_positives=20)
    threshold = await db.get_effective_confidence_threshold("Front Door", 0.5)
    assert threshold == pytest.approx(0.65)


async def test_effective_threshold_never_exceeds_ceiling(db: ClipDatabase) -> None:
    await _add_n_feedback(db, "Front Door", n=20, false_positives=20)
    threshold = await db.get_effective_confidence_threshold("Front Door", 0.9)
    assert threshold <= 0.95


async def test_effective_threshold_without_init_returns_base() -> None:
    d = ClipDatabase()
    threshold = await d.get_effective_confidence_threshold("Cam", 0.5)
    assert threshold == 0.5


# ---------------------------------------------------------------------------
# get_prompt_corrections
# ---------------------------------------------------------------------------


async def test_prompt_corrections_only_includes_notes(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Front Door"))
    # No note — not eligible for prompt injection.
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
    )
    # Has a note — eligible.
    await db.add_feedback(
        "c2",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        correction_note="Just a cat.",
        corrected_suspicious=False,
    )
    corrections = await db.get_prompt_corrections("Front Door")
    assert len(corrections) == 1
    assert corrections[0]["correction_note"] == "Just a cat."
    assert corrections[0]["corrected_suspicious"] is False


async def test_prompt_corrections_excludes_correct_verdicts(
    db: ClipDatabase,
) -> None:
    """Only feedback marking the AI WRONG is a correction — a confirmed
    correct verdict has nothing to teach the prompt."""
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=True,
        correction_note="yep, correct",
    )
    corrections = await db.get_prompt_corrections("Front Door")
    assert corrections == []


async def test_prompt_corrections_limited_to_three(db: ClipDatabase) -> None:
    for i in range(5):
        clip_id = f"c{i}"
        await db.add_clip(_make_clip(clip_id, camera="Front Door"))
        await db.add_feedback(
            clip_id,
            "Front Door",
            None,
            original_suspicious=True,
            original_confidence=0.8,
            correct=False,
            correction_note=f"note {i}",
        )
    corrections = await db.get_prompt_corrections("Front Door")
    assert len(corrections) == 3


async def test_prompt_corrections_excludes_other_cameras(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Driveway"))
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        correction_note="front door note",
    )
    await db.add_feedback(
        "c2",
        "Driveway",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        correction_note="driveway note",
    )
    corrections = await db.get_prompt_corrections("Front Door")
    assert len(corrections) == 1
    assert corrections[0]["correction_note"] == "front door note"


async def test_prompt_corrections_without_init_returns_empty() -> None:
    d = ClipDatabase()
    assert await d.get_prompt_corrections("Cam") == []


async def test_feedback_cascades_on_clip_delete(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_feedback(
        "c1",
        "Front Door",
        None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
    )
    await db.delete_clip("c1")
    assert await db.get_feedback_for_clip("c1") is None


# ------------------------------------------------------------------
# Local-only face enrollment (see vision/faces.py, ai_face_recognition_enabled)
# ------------------------------------------------------------------


async def test_add_and_list_face_enrollment(db: ClipDatabase) -> None:
    enrollment_id = await db.add_face_enrollment("Brian", [0.1, 0.2, 0.3])
    assert enrollment_id > 0

    enrollments = await db.list_face_enrollments()
    assert len(enrollments) == 1
    assert enrollments[0]["name"] == "Brian"
    assert enrollments[0]["embedding"] == [0.1, 0.2, 0.3]
    assert enrollments[0]["id"] == enrollment_id
    # approved defaults to True so the common "add a family member" flow
    # grants bypass trust immediately (see database.py's migration comment).
    assert enrollments[0]["approved"] is True


async def test_add_face_enrollment_unapproved(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Nanny", [0.1, 0.2], approved=False)
    enrollments = await db.list_face_enrollments()
    assert enrollments[0]["approved"] is False


async def test_add_face_enrollment_stores_its_thumbnail(db: ClipDatabase) -> None:
    with_photo = await db.add_face_enrollment("Brian", [0.1], thumbnail=b"jpeg")
    without = await db.add_face_enrollment("Brian", [0.2])

    by_id = {e["id"]: e for e in await db.list_face_enrollments()}
    assert by_id[with_photo]["has_thumbnail"] is True
    assert by_id[without]["has_thumbnail"] is False
    # Clip analysis reads this list for every clip; the bytes stay out of it.
    assert "thumbnail" not in by_id[with_photo]

    assert await db.get_face_enrollment_thumbnail(with_photo) == b"jpeg"
    assert await db.get_face_enrollment_thumbnail(without) is None
    assert await db.get_face_enrollment_thumbnail(999) is None


async def test_get_person_approval(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Brian", [0.1], approved=True)
    await db.add_face_enrollment("Brian", [0.2], approved=True)
    await db.add_face_enrollment("Nanny", [0.3], approved=False)
    await db.add_face_enrollment("Mixed", [0.4], approved=True)
    await db.add_face_enrollment("Mixed", [0.5], approved=False)

    assert await db.get_person_approval("Brian") is True
    assert await db.get_person_approval("Nanny") is False
    # A mix left by an older version reads as not approved — the same as the
    # Biometrics tab's switch shows it.
    assert await db.get_person_approval("Mixed") is False
    assert await db.get_person_approval("Nobody") is None


async def test_set_face_enrollments_approved_by_name(db: ClipDatabase) -> None:
    """Multi-frame enrollment stores one row per selected photo under the
    same name — bulk approve must affect every one of that person's rows."""
    await db.add_face_enrollment("Brian", [0.1], approved=True)
    await db.add_face_enrollment("Brian", [0.2], approved=True)
    await db.add_face_enrollment("Amy", [0.3], approved=True)

    await db.set_face_enrollments_approved_by_name("Brian", False)

    enrollments = {e["id"]: e for e in await db.list_face_enrollments()}
    approved_by_name = {e["name"]: e["approved"] for e in enrollments.values()}
    brian_rows = [e for e in enrollments.values() if e["name"] == "Brian"]
    assert all(r["approved"] is False for r in brian_rows)
    assert approved_by_name["Amy"] is True


async def test_rename_face_enrollments_by_name(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Brain", [0.1])
    await db.add_face_enrollment("Brain", [0.2])
    await db.add_face_enrollment("Amy", [0.3])

    await db.rename_face_enrollments_by_name("Brain", "Brian")

    names = [e["name"] for e in await db.list_face_enrollments()]
    assert names.count("Brian") == 2
    assert "Brain" not in names
    assert "Amy" in names


async def test_delete_face_enrollments_by_name(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Brian", [0.1])
    await db.add_face_enrollment("Brian", [0.2])
    await db.add_face_enrollment("Amy", [0.3])

    await db.delete_face_enrollments_by_name("Brian")

    names = [e["name"] for e in await db.list_face_enrollments()]
    assert names == ["Amy"]


async def test_face_enrollments_by_name_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.set_face_enrollments_approved_by_name("Brian", False)  # must not raise
    await d.rename_face_enrollments_by_name("Brian", "Brain")  # must not raise
    await d.delete_face_enrollments_by_name("Brian")  # must not raise


async def test_list_face_enrollments_ordered_by_name_then_age(db: ClipDatabase) -> None:
    zoe = await db.add_face_enrollment("Zoe", [0.1])
    amy_first = await db.add_face_enrollment("Amy", [0.2])
    amy_second = await db.add_face_enrollment("Amy", [0.3])
    ids = [e["id"] for e in await db.list_face_enrollments()]
    assert ids == [amy_first, amy_second, zoe]


async def test_delete_face_enrollment(db: ClipDatabase) -> None:
    enrollment_id = await db.add_face_enrollment("Brian", [0.1, 0.2])
    await db.delete_face_enrollment(enrollment_id)
    assert await db.list_face_enrollments() == []


async def test_face_enrollment_without_init_is_noop() -> None:
    d = ClipDatabase()
    assert await d.add_face_enrollment("Brian", [0.1]) == 0
    assert await d.list_face_enrollments() == []
    assert await d.get_person_approval("Brian") is None
    assert await d.get_face_enrollment_thumbnail(1) is None
    await d.delete_face_enrollment(1)  # must not raise


# ======================================================================
# Security events (see blink_downloader/security)
# ======================================================================


def _event(
    event_type: str = "contact_candidate",
    severity: str = "suspicious",
    confidence: float = 0.8,
    start: float = 0.0,
) -> SecurityEvent:
    return SecurityEvent(
        event_type=SecurityEventType(event_type),
        severity=Severity(severity),
        confidence=confidence,
        detail=f"{event_type} happened",
        subject_label="person",
        track_id=3,
        asset_name="blue sedan",
        asset_type="vehicle",
        start_offset=start,
        end_offset=start + 2.0,
        evidence={"min_gap_feet": 0.4},
    )


async def test_save_and_get_security_events(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_security_events(
        "c1",
        "Front Door",
        [_event(), _event("zone_entered", "noteworthy", start=4.0)],
        risk_score=82.5,
        evidence_quality=0.61,
    )
    events = await db.get_security_events("c1")
    assert [e["event_type"] for e in events] == ["contact_candidate", "zone_entered"]
    assert events[0]["risk_score"] == pytest.approx(82.5)
    assert events[0]["evidence_quality"] == pytest.approx(0.61)
    assert events[0]["evidence"] == {"min_gap_feet": 0.4}
    assert events[0]["track_id"] == 3


async def test_security_events_replace_rather_than_accumulate(db: ClipDatabase) -> None:
    """A re-analyze must leave exactly the latest conclusions behind — a
    timeline showing the same clip twice is worse than no timeline."""
    await db.add_clip(_make_clip("c1"))
    await db.save_security_events("c1", "Front Door", [_event()])
    await db.save_security_events(
        "c1", "Front Door", [_event("loitering", "noteworthy")]
    )
    events = await db.get_security_events("c1")
    assert [e["event_type"] for e in events] == ["loitering"]


async def test_saving_no_security_events_clears_stale_rows(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_security_events("c1", "Front Door", [_event()])
    await db.save_security_events("c1", "Front Door", [])
    assert await db.get_security_events("c1") == []


async def test_get_security_events_for_an_unknown_clip(db: ClipDatabase) -> None:
    assert await db.get_security_events("nope") == []


async def test_malformed_stored_evidence_decodes_to_an_empty_dict(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_security_events("c1", "Front Door", [_event()])
    assert db._pool is not None
    await db._pool.execute("UPDATE security_events SET evidence='{not json'")
    assert (await db.get_security_events("c1"))[0]["evidence"] == {}


async def test_security_events_are_deleted_with_their_clip(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_security_events("c1", "Front Door", [_event()])
    await db.delete_clip("c1")
    assert await db.get_security_events("c1") == []


# ---- timeline --------------------------------------------------------


async def _seed_timeline(db: ClipDatabase) -> None:
    # Recent timestamps: get_security_stats' window is relative to now.
    now = datetime.now(UTC)
    await db.add_clip(
        _make_clip("c1", "Front Door", timestamp=(now - timedelta(hours=3)).isoformat())
    )
    await db.add_clip(
        _make_clip("c2", "Driveway", timestamp=(now - timedelta(hours=2)).isoformat())
    )
    await db.add_clip(
        _make_clip("c3", "Driveway", timestamp=(now - timedelta(hours=1)).isoformat())
    )
    await db.save_security_events(
        "c1", "Front Door", [_event("subject_present", "routine", 0.9)]
    )
    await db.save_security_events(
        "c2",
        "Driveway",
        [_event("subject_present", "routine", 0.9), _event("loitering", "suspicious")],
        risk_score=55.0,
    )
    await db.save_security_events(
        "c3",
        "Driveway",
        [_event("impact_candidate", "critical", 0.7)],
        risk_score=90.0,
    )


async def test_timeline_returns_one_row_per_clip_newest_first(
    db: ClipDatabase,
) -> None:
    """A single visit legitimately produces half a dozen events; a timeline
    that repeats one clip six times is a worse view than none."""
    await _seed_timeline(db)
    result = await db.get_security_timeline()
    assert [e["clip_id"] for e in result["events"]] == ["c3", "c2", "c1"]
    assert result["total"] == 3


async def test_timeline_keeps_each_clips_most_severe_event(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    result = await db.get_security_timeline()
    by_clip = {e["clip_id"]: e for e in result["events"]}
    assert by_clip["c2"]["event_type"] == "loitering"
    assert by_clip["c2"]["severity"] == "suspicious"


async def test_timeline_includes_clip_metadata(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    first = (await db.get_security_timeline())["events"][0]
    assert first["clip_id"] == "c3"
    assert first["file_path"]
    assert first["starred"] is False


async def test_timeline_carries_the_models_own_verdict(db: ClipDatabase) -> None:
    """The tab's whole point is comparing what code measured with what the
    model concluded, so the row has to carry both."""
    await _seed_timeline(db)
    await db.add_analysis_result(
        {
            "clip_id": "c3",
            "camera": "Driveway",
            "model": "llava",
            "response_text": "{}",
            "is_suspicious": False,
            "confidence": 0.2,
            "summary": "Nothing unusual.",
            "frame_count": 3,
            "analysis_duration": 1.0,
            "analyzed_at": "2026-01-05T00:00:00+00:00",
        }
    )
    by_clip = {e["clip_id"]: e for e in (await db.get_security_timeline())["events"]}
    assert by_clip["c3"]["ai_suspicious"] is False
    assert by_clip["c3"]["ai_summary"] == "Nothing unusual."
    # A clip with no analysis row at all still appears, just without one.
    assert by_clip["c1"]["ai_suspicious"] is None


async def test_timeline_shows_only_the_latest_verdict(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    for analyzed_at, suspicious, summary in (
        ("2026-01-05T00:00:00+00:00", True, "Someone at the car."),
        ("2026-01-06T00:00:00+00:00", False, "A cat, on reflection."),
    ):
        await db.add_analysis_result(
            {
                "clip_id": "c3",
                "camera": "Driveway",
                "model": "llava",
                "response_text": "{}",
                "is_suspicious": suspicious,
                "confidence": 0.5,
                "summary": summary,
                "frame_count": 3,
                "analysis_duration": 1.0,
                "analyzed_at": analyzed_at,
            }
        )
    row = next(
        e for e in (await db.get_security_timeline())["events"] if e["clip_id"] == "c3"
    )
    assert row["ai_summary"] == "A cat, on reflection."


async def test_timeline_filters_by_camera(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    result = await db.get_security_timeline(camera="Driveway")
    assert {e["clip_id"] for e in result["events"]} == {"c2", "c3"}
    assert result["total"] == 2


async def test_timeline_filters_by_minimum_severity(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    result = await db.get_security_timeline(min_severity="suspicious")
    assert {e["clip_id"] for e in result["events"]} == {"c2", "c3"}


async def test_timeline_unknown_severity_filter_hides_nothing(
    db: ClipDatabase,
) -> None:
    """A filter this build doesn't understand must not silently hide a
    critical event."""
    await _seed_timeline(db)
    result = await db.get_security_timeline(min_severity="apocalyptic")
    assert result["total"] == 3


async def test_timeline_paginates(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    page = await db.get_security_timeline(limit=1, offset=1)
    assert [e["clip_id"] for e in page["events"]] == ["c2"]
    assert page["total"] == 3


async def test_timeline_filters_by_when_the_clip_was_recorded(
    db: ClipDatabase,
) -> None:
    """Not by when it was analyzed: the list is ordered by clip time, and a
    backlog processed overnight would otherwise file three-day-old footage
    under "Today" while sorting it among today's clips."""
    await db.add_clip(_make_clip("old", timestamp="2020-01-01T08:00:00+00:00"))
    await db.save_security_events("old", "Front Door", [_event()])
    assert (await db.get_security_timeline(period="today"))["total"] == 0
    assert (await db.get_security_timeline())["total"] == 1

    await db.add_clip(_make_clip("new", timestamp=datetime.now(UTC).isoformat()))
    await db.save_security_events("new", "Front Door", [_event()])
    assert (await db.get_security_timeline(period="today"))["total"] == 1


async def test_timeline_rolling_period_has_no_upper_bound(
    db: ClipDatabase,
) -> None:
    """ "week"/"month" are rolling windows open through now, so only a lower
    bound is applied -- unlike "today", which is a closed calendar day."""
    await db.add_clip(_make_clip("old", timestamp="2020-01-01T08:00:00+00:00"))
    await db.save_security_events("old", "Front Door", [_event()])
    await db.add_clip(_make_clip("new", timestamp=datetime.now(UTC).isoformat()))
    await db.save_security_events("new", "Front Door", [_event()])

    for rolling in ("week", "month"):
        page = await db.get_security_timeline(period=rolling)
        assert [e["clip_id"] for e in page["events"]] == ["new"], rolling
        assert page["total"] == 1


async def test_timeline_unknown_period_filters_nothing(db: ClipDatabase) -> None:
    """An unrecognised keyword resolves to no bounds at all rather than to
    an empty window, matching get_suspicious_clips' own behaviour."""
    await db.add_clip(_make_clip("old", timestamp="2020-01-01T08:00:00+00:00"))
    await db.save_security_events("old", "Front Door", [_event()])
    page = await db.get_security_timeline(period="decade")
    assert page["total"] == 1


async def test_timeline_without_a_pool_is_empty() -> None:
    assert await ClipDatabase().get_security_timeline() == {"events": [], "total": 0}


# ---- stats -----------------------------------------------------------


async def test_security_stats_counts_clips_by_severity(db: ClipDatabase) -> None:
    await _seed_timeline(db)
    stats = await db.get_security_stats()
    assert stats["by_severity"]["critical"] == 1
    assert stats["by_severity"]["suspicious"] == 1
    assert stats["total"] == 4
    assert stats["days"] == 7


async def test_security_stats_ignores_clips_outside_the_window(
    db: ClipDatabase,
) -> None:
    await _seed_timeline(db)
    assert db._pool is not None
    await db._pool.execute("UPDATE clips SET timestamp='2020-01-01T00:00:00+00:00'")
    assert (await db.get_security_stats())["total"] == 0


async def test_security_stats_without_a_pool() -> None:
    assert await ClipDatabase().get_security_stats() == {
        "by_severity": {},
        "total": 0,
        "days": 7,
    }


async def test_save_security_events_without_a_pool_is_a_noop() -> None:
    await ClipDatabase().save_security_events("c1", "cam", [_event()])


async def test_get_security_events_without_a_pool() -> None:
    assert await ClipDatabase().get_security_events("c1") == []


def test_severities_at_or_above() -> None:
    assert _severities_at_or_above("suspicious") == ["suspicious", "critical"]
    assert _severities_at_or_above("routine") == [
        "routine",
        "noteworthy",
        "suspicious",
        "critical",
    ]
    assert len(_severities_at_or_above("nonsense")) == 4


# ======================================================================
# Learned protected-vehicle signatures
# ======================================================================


def _signature(sample_count: int = 5) -> VehicleSignature:
    return VehicleSignature(
        box=(0.1, 0.2, 0.5, 0.6), histogram=(0.25, 0.75), sample_count=sample_count
    )


async def test_save_and_get_vehicle_signature(db: ClipDatabase) -> None:
    await db.save_vehicle_signature("Driveway", _signature())
    stored = await db.get_vehicle_signature("Driveway")
    assert stored is not None
    assert stored.box == pytest.approx((0.1, 0.2, 0.5, 0.6))
    assert stored.histogram == pytest.approx((0.25, 0.75))
    assert stored.sample_count == 5
    assert stored.established is True


async def test_saving_a_signature_replaces_the_previous_one(db: ClipDatabase) -> None:
    await db.save_vehicle_signature("Driveway", _signature(1))
    await db.save_vehicle_signature("Driveway", _signature(9))
    stored = await db.get_vehicle_signature("Driveway")
    assert stored is not None
    assert stored.sample_count == 9


async def test_signatures_are_per_camera(db: ClipDatabase) -> None:
    await db.save_vehicle_signature("Driveway", _signature())
    assert await db.get_vehicle_signature("Back Yard") is None


async def test_reset_vehicle_signature(db: ClipDatabase) -> None:
    """A signature that has latched onto the wrong car would otherwise keep
    reinforcing its own mistake."""
    await db.save_vehicle_signature("Driveway", _signature())
    assert await db.reset_vehicle_signature("Driveway") is True
    assert await db.get_vehicle_signature("Driveway") is None
    assert await db.reset_vehicle_signature("Driveway") is False


async def test_malformed_signature_json_reads_as_absent(db: ClipDatabase) -> None:
    await db.save_vehicle_signature("Driveway", _signature())
    assert db._pool is not None
    await db._pool.execute("UPDATE camera_vehicle_signatures SET box='{not json'")
    assert await db.get_vehicle_signature("Driveway") is None


async def test_signature_with_a_wrong_shaped_box_reads_as_absent(
    db: ClipDatabase,
) -> None:
    await db.save_vehicle_signature("Driveway", _signature())
    assert db._pool is not None
    await db._pool.execute("UPDATE camera_vehicle_signatures SET box='[1, 2]'")
    assert await db.get_vehicle_signature("Driveway") is None


async def test_vehicle_signature_methods_without_a_pool() -> None:
    empty = ClipDatabase()
    assert await empty.get_vehicle_signature("Driveway") is None
    await empty.save_vehicle_signature("Driveway", _signature())
    assert await empty.reset_vehicle_signature("Driveway") is False


# ======================================================================
# save_analysis writes all three tables together
# ======================================================================


async def test_save_analysis_persists_verdict_detections_and_events(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="{}",
        is_suspicious=True,
        confidence=0.8,
        summary="Someone at the car",
        frame_count=3,
        analysis_duration=1.5,
        analyzed_at=datetime.now(UTC).isoformat(),
        risk_score=82.0,
        severity="critical",
        event_type="impact_candidate",
        evidence_quality=0.61,
        risk_override_applied=True,
        detected_objects=[DetectedObject("person", 0.9, (1.0, 2.0, 3.0, 4.0), 1, 0)],
        security_events=[_event()],
    )
    await db.save_analysis(result)

    stored = await db.get_analysis_for_clip("c1")
    assert stored is not None
    assert stored["risk_score"] == pytest.approx(82.0)
    assert stored["severity"] == "critical"
    assert stored["event_type"] == "impact_candidate"
    assert stored["evidence_quality"] == pytest.approx(0.61)
    assert stored["risk_override_applied"] is True
    assert len(await db.get_detected_objects_summary("c1")) == 1
    events = await db.get_security_events("c1")
    assert len(events) == 1
    assert events[0]["risk_score"] == pytest.approx(82.0)


async def test_a_failed_reanalysis_does_not_erase_the_previous_run_evidence(
    db: ClipDatabase,
) -> None:
    """Frame extraction failing (file moved, archived, still being written)
    still produces a result row — but it examined nothing, so it must not
    replace the detections and security events of a run that did. Erasing
    them would drop the clip out of the Security tab's timeline entirely."""
    await db.add_clip(_make_clip("c1"))
    good = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="{}",
        is_suspicious=True,
        confidence=0.8,
        summary="Someone at the car",
        frame_count=3,
        analysis_duration=1.5,
        analyzed_at=datetime.now(UTC).isoformat(),
        detected_objects=[DetectedObject("person", 0.9, (1.0, 2.0, 3.0, 4.0), 1, 0)],
        security_events=[_event()],
    )
    await db.save_analysis(good)

    await db.save_analysis(
        AnalysisResult(
            clip_id="c1",
            camera="Front Door",
            model="llava",
            response_text="",
            is_suspicious=False,
            confidence=0.0,
            summary="No frames could be extracted",
            frame_count=0,
            analysis_duration=0.1,
            analyzed_at=datetime.now(UTC).isoformat(),
        )
    )

    # The failure is recorded as the latest verdict, as it always was...
    stored = await db.get_analysis_for_clip("c1")
    assert stored is not None
    assert stored["summary"] == "No frames could be extracted"
    # ...but the evidence from the run that actually looked at frames stays.
    assert len(await db.get_detected_objects_summary("c1")) == 1
    assert len(await db.get_security_events("c1")) == 1


async def test_add_analysis_result_defaults_severity_when_absent(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "analyzed_at": datetime.now(UTC).isoformat(),
        }
    )
    stored = await db.get_analysis_for_clip("c1")
    assert stored is not None
    assert stored["severity"] == "routine"
    assert stored["risk_score"] == pytest.approx(0.0)


# ======================================================================
# Per-box detections (the clip modal's overlay)
# ======================================================================


async def test_detected_object_boxes_are_normalized_for_drawing(
    db: ClipDatabase,
) -> None:
    """Boxes are stored in the detector's own scaled pixel space, which the
    player knows nothing about."""
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1",
        [
            DetectedObject("person", 0.9, (64.0, 36.0, 128.0, 180.0), 1, 0),
            DetectedObject("car", 0.95, (320.0, 180.0, 640.0, 360.0), 2, 2),
        ],
        interval=2.0,
        frame_size=(640.0, 360.0),
    )
    result = await db.get_detected_object_boxes("c1")
    first, second = result["objects"]
    assert first["box"] == pytest.approx([0.1, 0.1, 0.2, 0.5])
    assert first["offset_seconds"] == pytest.approx(0.0)
    assert first["track_id"] == 1
    assert second["offset_seconds"] == pytest.approx(4.0)


async def test_detected_object_boxes_skip_rows_with_no_frame_size(
    db: ClipDatabase,
) -> None:
    """Rows written before the frame dimensions were recorded cannot be
    placed, and drawing them in the wrong spot is worse than omitting them."""
    await db.add_clip(_make_clip("c1"))
    await db.save_detected_objects(
        "c1", [DetectedObject("person", 0.9, (1.0, 2.0, 3.0, 4.0), 1, 0)]
    )
    assert await db.get_detected_object_boxes("c1") == {"objects": []}


async def test_detected_object_boxes_without_a_pool() -> None:
    assert await ClipDatabase().get_detected_object_boxes("c1") == {"objects": []}


async def test_save_analysis_records_the_detection_timing_and_frame_size(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.save_analysis(
        AnalysisResult(
            clip_id="c1",
            camera="Front Door",
            model="llava",
            response_text="",
            is_suspicious=False,
            confidence=0.1,
            summary="ok",
            frame_count=2,
            analysis_duration=1.0,
            analyzed_at=datetime.now(UTC).isoformat(),
            detected_objects=[
                DetectedObject("person", 0.9, (64.0, 36.0, 128.0, 180.0), 1, 3)
            ],
            detection_interval=2.5,
            detection_frame_size=(640.0, 360.0),
        )
    )
    (stored,) = (await db.get_detected_object_boxes("c1"))["objects"]
    assert stored["offset_seconds"] == pytest.approx(7.5)
    assert stored["box"] == pytest.approx([0.1, 0.1, 0.2, 0.5])


# ======================================================================
# Camera rename / replacement must carry the new tables too
# ======================================================================


async def test_rename_camera_moves_security_events(db: ClipDatabase) -> None:
    """A renamed camera's events must follow it, or the Security tab's
    camera filter silently loses everything recorded before the rename."""
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.save_security_events("c1", "Front Door", [_event()])
    assert await db.rename_camera("Front Door", "Porch") is True
    (stored,) = await db.get_security_events("c1")
    assert stored["camera"] == "Porch"
    assert (await db.get_security_timeline(camera="Porch"))["total"] == 1


async def test_rename_camera_carries_the_learned_vehicle_signature(
    db: ClipDatabase,
) -> None:
    """Same camera, same car, same spot — only the name changed."""
    await db.save_vehicle_signature("Front Door", _signature(6))
    assert await db.rename_camera("Front Door", "Porch") is True
    assert await db.get_vehicle_signature("Front Door") is None
    moved = await db.get_vehicle_signature("Porch")
    assert moved is not None
    assert moved.sample_count == 6


async def test_rename_camera_keeps_the_better_established_signature(
    db: ClipDatabase,
) -> None:
    """A stub row already under the new name must not displace the real
    history being carried across."""
    await db.save_vehicle_signature("Front Door", _signature(9))
    await db.save_vehicle_signature("Porch", _signature(1))
    await db.rename_camera("Front Door", "Porch")
    moved = await db.get_vehicle_signature("Porch")
    assert moved is not None
    assert moved.sample_count == 9


async def test_rename_camera_without_a_signature_is_still_fine(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    assert await db.rename_camera("Front Door", "Porch") is True
    assert await db.get_vehicle_signature("Porch") is None


async def test_replacing_a_camera_drops_its_learned_vehicle_signature(
    db: ClipDatabase,
) -> None:
    """A replacement unit's field of view differs, so where the vehicle
    "normally sits" is stale — and a signature pointing at the wrong part of
    the new frame would keep confirming its own mistake."""
    await db.save_vehicle_signature("Front Door", _signature(9))
    await db.reset_camera_baselines("Front Door")
    assert await db.get_vehicle_signature("Front Door") is None


async def test_notified_badge_matches_the_risk_override_dispatch_exemption(
    db: ClipDatabase,
) -> None:
    """AnalysisQueue skips the confidence threshold for an override-flagged
    clip; a badge that still applied it would say "not notified" about a
    notification that was actually sent."""
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "analyzed_at": datetime.now(UTC).isoformat(),
            "is_suspicious": True,
            "confidence": 0.2,
            "risk_override_applied": True,
        }
    )
    (clip,) = await db.get_clips(ClipFilters(min_confidence=0.9))
    assert clip["notified"] is True
    assert (
        len(await db.get_clips(ClipFilters(min_confidence=0.9, notified_only=True)))
        == 1
    )


async def test_notified_badge_still_applies_the_threshold_without_an_override(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        {
            "clip_id": "c1",
            "camera": "Front Door",
            "model": "llava",
            "analyzed_at": datetime.now(UTC).isoformat(),
            "is_suspicious": True,
            "confidence": 0.2,
        }
    )
    (clip,) = await db.get_clips(ClipFilters(min_confidence=0.9))
    assert clip["notified"] is False


# ----------------------------------------------------------------------
# Upgrading an existing install (see database.py's _MIGRATIONS)
# ----------------------------------------------------------------------


#: Columns v6.0.0 adds to tables that already existed in 5.x. A
#: ``CREATE TABLE IF NOT EXISTS`` is a no-op against a table that is already
#: there, so any of these missing from _MIGRATIONS would work perfectly on a
#: fresh database and break every upgrading install on the first write.
_V6_ADDED_COLUMNS = {
    "analysis_results": [
        "risk_score",
        "severity",
        "event_type",
        "evidence_quality",
        "risk_override_applied",
    ],
    "detected_objects": ["offset_seconds", "frame_width", "frame_height"],
}


async def test_upgrading_an_existing_database_gains_the_v6_columns(
    db: ClipDatabase,
) -> None:
    """Simulates a 5.x install upgrading: drop the columns v6 added, then
    re-run init() exactly as the add-on does on every start."""
    assert db._pool is not None
    for table, columns in _V6_ADDED_COLUMNS.items():
        for column in columns:
            await db._pool.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    upgraded = ClipDatabase(TEST_DB_DSN)
    await upgraded.init()
    try:
        assert upgraded._pool is not None
        for table, columns in _V6_ADDED_COLUMNS.items():
            present = {
                r["column_name"]
                for r in await upgraded._pool.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = $1",
                    table,
                )
            }
            assert set(columns) <= present, (
                f"{table} is missing {set(columns) - present}"
            )

        # ...and the upgraded database actually round-trips a v6 analysis.
        await upgraded.add_clip(_make_clip("upgraded"))
        await upgraded.save_analysis(
            AnalysisResult(
                clip_id="upgraded",
                camera="Front Door",
                model="llava",
                response_text="{}",
                is_suspicious=True,
                confidence=0.8,
                summary="Someone at the car",
                frame_count=3,
                analysis_duration=1.5,
                analyzed_at=datetime.now(UTC).isoformat(),
                risk_score=81.0,
                severity="suspicious",
                evidence_quality=0.6,
                detected_objects=[
                    DetectedObject("person", 0.9, (1.0, 2.0, 3.0, 4.0), 1, 0)
                ],
                detection_interval=2.0,
                detection_frame_size=(640.0, 360.0),
                security_events=[_event()],
            )
        )
        stored = await upgraded.get_analysis_for_clip("upgraded")
        assert stored is not None
        assert stored["risk_score"] == 81.0
        assert len(await upgraded.get_security_events("upgraded")) == 1
    finally:
        await upgraded.close()


async def test_upgrading_clears_a_vehicle_fingerprint_measured_the_old_way(
    db: ClipDatabase,
) -> None:
    """6.0.5 moved the model stages onto raw frames, so a colour fingerprint
    learned from CLAHE-enhanced ones is no longer measured the same way and
    has to go. Simulates a pre-6.0.5 install: drop the marker column and
    seed a row the way that version would have, then re-run init()."""
    assert db._pool is not None
    await db._pool.execute(
        "ALTER TABLE camera_vehicle_signatures DROP COLUMN histogram_pipeline"
    )
    await db._pool.execute(
        "INSERT INTO camera_vehicle_signatures "
        "(camera, box, histogram, sample_count, updated_at) VALUES "
        "($1, $2, $3, $4, $5)",
        "Front Door",
        json.dumps([0.1, 0.2, 0.3, 0.4]),
        json.dumps([0.5] * 64),
        22,
        datetime.now(UTC).isoformat(),
    )

    upgraded = ClipDatabase(TEST_DB_DSN)
    await upgraded.init()
    try:
        migrated = await upgraded.get_vehicle_signature("Front Door")
        assert migrated is not None
        # The stale vector is gone, so appearance simply abstains until the
        # next confident sighting relearns it.
        assert migrated.histogram == ()
        # Everything measured the same way as before survives untouched —
        # the learned parking position is the expensive half of this.
        assert migrated.box == (0.1, 0.2, 0.3, 0.4)
        assert migrated.sample_count == 22

        # A fingerprint relearned after the upgrade must survive every later
        # start: a migration that re-ran would wipe it on the next restart.
        relearned = VehicleSignature(
            box=migrated.box, histogram=tuple([0.9] * 64), sample_count=23
        )
        await upgraded.save_vehicle_signature("Front Door", relearned)
    finally:
        await upgraded.close()

    restarted = ClipDatabase(TEST_DB_DSN)
    await restarted.init()
    try:
        kept = await restarted.get_vehicle_signature("Front Door")
        assert kept is not None
        assert kept.histogram == tuple([0.9] * 64)
    finally:
        await restarted.close()
