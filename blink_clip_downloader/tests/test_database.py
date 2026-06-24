"""Tests for ClipDatabase."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blink_downloader.database import ClipDatabase


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[ClipDatabase, None]:
    d = ClipDatabase(tmp_path / "test.db")
    await d.init()
    yield d
    await d.close()


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


async def test_init_creates_tables(tmp_path: Path) -> None:
    d = ClipDatabase(tmp_path / "new.db")
    await d.init()
    assert (tmp_path / "new.db").exists()
    await d.close()


async def test_double_close_is_safe(db: ClipDatabase) -> None:
    await db.close()
    await db.close()  # should not raise


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
    count = await db.count_clips()
    assert count == 1


async def test_get_clip_missing_returns_none(db: ClipDatabase) -> None:
    assert await db.get_clip("nonexistent") is None


async def test_add_clip_when_db_not_init() -> None:
    d = ClipDatabase(Path("/tmp/never_opened.db"))
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


async def test_get_distinct_cameras(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1", camera="A"))
    await db.add_clip(_make_clip("c2", camera="B"))
    await db.add_clip(_make_clip("c3", camera="A"))
    cameras = await db.get_distinct_cameras()
    assert cameras == ["A", "B"]


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
    d = ClipDatabase(Path("/tmp/neveropened2.db"))
    assert await d.get_clip("x") is None
    assert await d.get_clips() == []
    assert await d.count_clips() == 0
    assert await d.get_stats() == {}
    assert await d.get_camera_stats() == []
    assert await d.get_clips_to_archive(30) == []
    assert await d.get_all_file_paths() == set()
    assert await d.star_clip("x", True) is False
    assert await d.set_tags("x", []) is False
    assert await d.delete_clip("x") is False


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
    d = ClipDatabase(Path("/tmp/neveropened3.db"))
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
    await db.add_analysis_result(_make_analysis("c1", is_suspicious=True))
    await db.add_analysis_result(_make_analysis("c2", is_suspicious=False))
    stats = await db.get_analysis_stats()
    assert stats["total_analyzed"] == 2
    assert stats["suspicious_count"] == 1
    assert stats["last_analysis"] is not None


async def test_analysis_stats_empty(db: ClipDatabase) -> None:
    stats = await db.get_analysis_stats()
    assert stats["total_analyzed"] == 0
    assert stats["suspicious_count"] == 0
    assert stats["last_analysis"] is None


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
    d = ClipDatabase(Path("/tmp/neveropened4.db"))
    assert await d.get_analysis_for_clip("x") is None
    assert await d.get_suspicious_clips() == []
    assert await d.get_analysis_stats() == {}
    assert await d.get_pending_analysis() == []
    counts = await d.get_queue_counts()
    assert counts == {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
