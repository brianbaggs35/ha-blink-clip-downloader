"""Tests for ClipDatabase."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blink_downloader.database import ClipDatabase, _row_to_dict


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
    d = ClipDatabase(Path("/tmp/neveropened_arch.db"))
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


async def test_count_clips_camera_filter(db: ClipDatabase) -> None:
    """count_clips(camera=...) counts only clips from that camera (lines 263-264)."""
    await db.add_clip(_make_clip("c1", camera="Front Door"))
    await db.add_clip(_make_clip("c2", camera="Back Yard"))
    await db.add_clip(_make_clip("c3", camera="Front Door"))
    count = await db.count_clips(camera="Front Door")
    assert count == 2


async def test_count_clips_starred_filter(db: ClipDatabase) -> None:
    """count_clips(starred=True) counts only starred clips (lines 266-267)."""
    await db.add_clip(_make_clip("c1"))
    await db.add_clip(_make_clip("c2"))
    await db.star_clip("c1", True)
    count = await db.count_clips(starred=True)
    assert count == 1


async def test_get_distinct_cameras_without_init() -> None:
    """get_distinct_cameras() returns [] when db is not initialised (line 358)."""
    d = ClipDatabase(Path("/tmp/neveropened_cams.db"))
    result = await d.get_distinct_cameras()
    assert result == []


async def test_get_distinct_tags_without_init() -> None:
    """get_distinct_tags() returns [] when db is not initialised (line 368)."""
    d = ClipDatabase(Path("/tmp/neveropened_tags.db"))
    result = await d.get_distinct_tags()
    assert result == []


async def test_get_distinct_tags_bad_json_skipped(db: ClipDatabase) -> None:
    """Clips whose tags column holds invalid JSON are silently skipped (lines 377-378)."""
    await db.add_clip(_make_clip("c1"))
    await db.set_tags("c1", ["good"])
    # Inject bad JSON directly via raw SQL
    assert db._db is not None
    await db._db.execute("UPDATE clips SET tags='bad-json!!!' WHERE id='c1'")
    await db._db.commit()
    tags = await db.get_distinct_tags()
    assert isinstance(tags, list)
    assert "good" not in tags  # bad JSON skipped entirely


async def test_add_analysis_result_without_init() -> None:
    """add_analysis_result() silently returns when db is not initialised (line 412)."""
    d = ClipDatabase(Path("/tmp/neveropened_ar.db"))
    await d.add_analysis_result({"clip_id": "c1", "camera": "A"})  # must not raise


async def test_enqueue_for_analysis_without_init() -> None:
    """enqueue_for_analysis() silently returns when db is not initialised (line 504)."""
    d = ClipDatabase(Path("/tmp/neveropened_eq.db"))
    await d.enqueue_for_analysis("c1", "Cam", "/c1.mp4")  # must not raise


async def test_update_queue_status_without_init() -> None:
    """update_queue_status() silently returns when db is not initialised (line 529)."""
    d = ClipDatabase(Path("/tmp/neveropened_uq.db"))
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
    d = ClipDatabase(Path("/tmp/neveropened_tu.db"))
    stats = await d.get_token_usage_stats()
    assert stats["total_analyses"] == 0
    assert stats["by_model"] == []


async def test_analysis_result_stores_tokens(db: ClipDatabase) -> None:
    await db.add_clip(_make_clip("c1"))
    await db.add_analysis_result(
        _make_analysis_tokens("c1", tokens_prompt=150, tokens_completion=75)
    )
    result = await db.get_analysis_for_clip("c1")
    assert result is not None
    assert result["tokens_prompt"] == 150
    assert result["tokens_completion"] == 75


async def test_migrate_adds_columns_idempotent(tmp_path: Path) -> None:
    """Running init() twice does not raise errors (migration is idempotent)."""
    d = ClipDatabase(tmp_path / "migrate_test.db")
    await d.init()
    await d.close()
    d2 = ClipDatabase(tmp_path / "migrate_test.db")
    await d2.init()  # should not raise
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
    d = ClipDatabase(Path("/tmp/neveropened_baseline.db"))
    score = await d.get_anomaly_score("Camera", 8, 5.0)
    assert score == 0.0


async def test_record_clip_baseline_uninitialised_db() -> None:
    d = ClipDatabase(Path("/tmp/neveropened_baseline2.db"))
    # Should not raise even when db is not open
    await d.record_clip_baseline("Camera", 8, 5.0)
