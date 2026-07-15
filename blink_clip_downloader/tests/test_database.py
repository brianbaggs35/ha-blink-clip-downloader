"""Tests for ClipDatabase."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from blink_downloader.database import ClipDatabase, _row_to_dict
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


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


async def test_init_creates_tables(db: ClipDatabase) -> None:
    assert db._pool is not None  # noqa: SLF001
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

    assert db._pool is not None  # noqa: SLF001
    remaining_results = await db._pool.fetchval(  # noqa: SLF001
        "SELECT COUNT(*) FROM analysis_results WHERE clip_id='clip1'"
    )
    assert remaining_results == 0
    remaining_queue = await db._pool.fetchval(  # noqa: SLF001
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
    clips = await db.get_clips(camera="Back Yard")
    assert len(clips) == 1
    assert clips[0]["camera"] == "Back Yard"


async def test_get_clips_filter_starred(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.star_clip("c1", True)
    starred = await db.get_clips(starred=True)
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

    all_clips = {c["id"]: c for c in await db.get_clips(min_confidence=0.5)}
    assert all_clips["c1"]["notified"] is True
    assert all_clips["c2"]["notified"] is False
    assert all_clips["c3"]["notified"] is False

    notified_only = await db.get_clips(notified_only=True, min_confidence=0.5)
    assert [c["id"] for c in notified_only] == ["c1"]


async def test_get_clips_search(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("abc123", camera="Garage"))
    await db.add_clip(_make_clip("xyz999", camera="Office"))
    results = await db.get_clips(search="Garage")
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
    old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    await db.add_clip(_make_clip("old", timestamp=old_ts))
    await db.add_clip(_make_clip("new", timestamp=new_ts))
    to_archive = await db.get_clips_to_archive(older_than_days=30)
    ids = [c["id"] for c in to_archive]
    assert "old" in ids
    assert "new" not in ids


# ------------------------------------------------------------------
# Statistics
# ------------------------------------------------------------------


async def test_get_stats_empty(db: ClipDatabase) -> None:
    stats = await db.get_stats()
    assert stats["total_count"] == 0
    assert stats["starred_count"] == 0


async def test_get_stats_counts(db: ClipDatabase) -> None:
    today = datetime.now(timezone.utc).isoformat()
    await db.add_clip(_make_clip("c1", timestamp=today, size_bytes=2_000_000))
    await db.add_clip(_make_clip("c2", timestamp=today))
    await db.star_clip("c1", True)
    stats = await db.get_stats()
    assert stats["total_count"] == 2
    assert stats["today_count"] == 2
    assert stats["starred_count"] == 1
    assert stats["total_size_bytes"] >= 2_000_000


async def test_get_camera_stats(db: ClipDatabase) -> None:
    today = datetime.now(timezone.utc).isoformat()
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
    today = datetime.now(timezone.utc).isoformat()
    await db.add_clip(_make_clip("c1", camera="Front Door", timestamp=today))
    await db.add_clip(_make_clip("c2", camera="front door", timestamp=today))
    await db.add_clip(_make_clip("c3", camera="FRONT DOOR", timestamp=today))
    cam_stats = await db.get_camera_stats()
    assert len(cam_stats) == 1
    assert cam_stats[0]["total"] == 3


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
    now = datetime.now(timezone.utc)
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
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await db.add_clip(_make_clip("old_clip", timestamp=old_ts))
    data = await db.get_activity_data(days=7)
    assert data == []


async def test_get_activity_data_counts_correctly(db: ClipDatabase) -> None:
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    today_str = base.date().isoformat()
    hour = base.hour
    for i in range(4):
        ts = (base + timedelta(minutes=i)).isoformat()
        await db.add_clip(_make_clip(f"batch{i}", timestamp=ts))
    data = await db.get_activity_data(days=1)
    matching = [r for r in data if r["date"] == today_str and r["hour"] == hour]
    assert len(matching) == 1
    assert matching[0]["count"] == 4


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
    today = datetime.now(timezone.utc).isoformat()
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.add_analysis_result(_make_analysis("c1", frame_count=4, analyzed_at=today))
    await db.add_analysis_result(
        _make_analysis("c2", frame_count=2, analyzed_at="2024-06-01T09:00:00+00:00")
    )
    stats = await db.get_analysis_stats()
    assert stats["total_frames_analyzed"] == 6
    assert stats["frames_analyzed_today"] == 4


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


async def test_analysis_operations_without_init() -> None:
    d = ClipDatabase()
    assert await d.get_analysis_for_clip("x") is None
    assert await d.get_suspicious_clips() == []
    assert await d.get_analysis_stats() == {}
    assert await d.get_pending_analysis() == []
    counts = await d.get_queue_counts()
    assert counts == {"pending": 0, "processing": 0, "completed": 0, "failed": 0}


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


async def test_mark_archived_without_init() -> None:
    """mark_archived() silently returns when db is not initialised (line 161)."""
    d = ClipDatabase()
    await d.mark_archived("c1", "/archive/2024-06.zip")  # must not raise


async def test_get_clips_since_filter(db: ClipDatabase) -> None:
    """get_clips(since=...) restricts results to clips after the timestamp (lines 218-219)."""
    await db.add_clip(_make_clip("old", timestamp="2024-01-01T00:00:00+00:00"))
    await db.add_clip(_make_clip("new", timestamp="2024-06-01T00:00:00+00:00"))
    clips = await db.get_clips(since="2024-03-01T00:00:00+00:00")
    assert len(clips) == 1
    assert clips[0]["id"] == "new"


async def test_get_clips_until_filter(db: ClipDatabase) -> None:
    """get_clips(until=...) restricts results to clips before the timestamp (lines 221-222)."""
    await db.add_clip(_make_clip("old", timestamp="2024-01-01T00:00:00+00:00"))
    await db.add_clip(_make_clip("new", timestamp="2024-06-01T00:00:00+00:00"))
    clips = await db.get_clips(until="2024-03-01T00:00:00+00:00")
    assert len(clips) == 1
    assert clips[0]["id"] == "old"


async def test_get_clips_source_filter(db: ClipDatabase) -> None:
    """get_clips(source=...) filters by clip source (lines 227-228)."""
    await db.add_clip(_make_clip("c1", source="pir"))
    await db.add_clip(_make_clip("c2", source="cloud"))
    clips = await db.get_clips(source="cloud")
    assert len(clips) == 1
    assert clips[0]["id"] == "c2"


async def test_get_clips_tag_filter(db: ClipDatabase) -> None:
    """get_clips(tag=...) filters by tag substring in JSON array (lines 230-231)."""
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.set_tags("c1", ["important", "night"])
    clips = await db.get_clips(tag="important")
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
    assert db._pool is not None  # noqa: SLF001
    await db._pool.execute("UPDATE clips SET tags='bad-json!!!' WHERE id='c1'")  # noqa: SLF001
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
    from datetime import datetime, timezone

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
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
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
    today = datetime.now(timezone.utc)
    row_today = _make_analysis_tokens("c1", "llava:7b", 100, 20)
    row_today["analyzed_at"] = today.isoformat()
    row_yesterday = _make_analysis_tokens("c2", "llava:7b", 50, 10)
    row_yesterday["analyzed_at"] = (today - timedelta(days=1)).isoformat()
    await db.add_analysis_result(row_today)
    await db.add_analysis_result(row_yesterday)

    daily = await db.get_daily_usage_stats()
    assert len(daily) == 2
    days = {row["day"] for row in daily}
    assert today.date().isoformat() in days
    assert (today - timedelta(days=1)).date().isoformat() in days
    # Most recent day first.
    assert daily[0]["day"] == today.date().isoformat()


async def test_get_daily_usage_stats_excludes_data_outside_window(
    db: ClipDatabase,
) -> None:
    await db.add_clip(_make_clip("c1"))
    old_row = _make_analysis_tokens("c1", "llava:7b", 100, 20)
    old_row["analyzed_at"] = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
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
    assert db._pool is not None  # noqa: SLF001
    await db._pool.execute(  # noqa: SLF001
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
    assert db._pool is not None  # noqa: SLF001
    await db._pool.execute(  # noqa: SLF001
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
    assert db._pool is not None  # noqa: SLF001
    value = await db._pool.fetchval(  # noqa: SLF001
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
# Local-only face enrollment (see vision.py, ai_face_recognition_enabled)
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


async def test_set_face_enrollment_approved(db: ClipDatabase) -> None:
    enrollment_id = await db.add_face_enrollment("Brian", [0.1, 0.2])
    await db.set_face_enrollment_approved(enrollment_id, False)
    enrollments = await db.list_face_enrollments()
    assert enrollments[0]["approved"] is False

    await db.set_face_enrollment_approved(enrollment_id, True)
    enrollments = await db.list_face_enrollments()
    assert enrollments[0]["approved"] is True


async def test_rename_face_enrollment(db: ClipDatabase) -> None:
    enrollment_id = await db.add_face_enrollment("Brain", [0.1, 0.2])
    await db.rename_face_enrollment(enrollment_id, "Brian")
    enrollments = await db.list_face_enrollments()
    assert enrollments[0]["name"] == "Brian"


async def test_set_face_enrollment_approved_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.set_face_enrollment_approved(1, False)  # must not raise


async def test_rename_face_enrollment_without_init_is_noop() -> None:
    d = ClipDatabase()
    await d.rename_face_enrollment(1, "New Name")  # must not raise


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


async def test_list_face_enrollments_ordered_by_name(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Zoe", [0.1])
    await db.add_face_enrollment("Amy", [0.2])
    names = [e["name"] for e in await db.list_face_enrollments()]
    assert names == ["Amy", "Zoe"]


async def test_delete_face_enrollment(db: ClipDatabase) -> None:
    enrollment_id = await db.add_face_enrollment("Brian", [0.1, 0.2])
    await db.delete_face_enrollment(enrollment_id)
    assert await db.list_face_enrollments() == []


async def test_face_enrollment_without_init_is_noop() -> None:
    d = ClipDatabase()
    assert await d.add_face_enrollment("Brian", [0.1]) == 0
    assert await d.list_face_enrollments() == []
    await d.delete_face_enrollment(1)  # must not raise
