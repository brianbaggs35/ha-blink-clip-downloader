"""Tests for library_scanner.import_existing_clips."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blink_downloader.database import ClipDatabase
from blink_downloader.library_scanner import import_existing_clips


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[ClipDatabase, None]:
    d = ClipDatabase(tmp_path / "test.db")
    await d.init()
    yield d
    await d.close()


def _touch(path: Path, content: bytes = b"fake-mp4") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# ---------------------------------------------------------------------------


async def test_no_download_path_returns_zero(db: ClipDatabase, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert await import_existing_clips(db, missing) == 0


async def test_imports_clip_from_camera_date_layout(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """download_path/<camera>/<YYYY-MM-DD>/<camera>_<ts>.mp4 (default layout)."""
    download_path = tmp_path / "clips"
    clip_path = (
        download_path / "Front_Door" / "2024-06-01" / "Front_Door_20240601_080000.mp4"
    )
    _touch(clip_path)

    added = await import_existing_clips(db, download_path)
    assert added == 1

    paths = await db.get_all_file_paths()
    assert str(clip_path) in paths

    clips = await db.get_clips()
    assert len(clips) == 1
    clip = clips[0]
    assert clip["camera"] == "Front Door"
    assert (
        clip["timestamp"]
        == datetime(2024, 6, 1, 8, 0, 0, tzinfo=timezone.utc).isoformat()
    )
    assert clip["size_bytes"] == len(b"fake-mp4")


async def test_skips_files_already_in_database(
    db: ClipDatabase, tmp_path: Path
) -> None:
    download_path = tmp_path / "clips"
    clip_path = (
        download_path / "Front_Door" / "2024-06-01" / "Front_Door_20240601_080000.mp4"
    )
    _touch(clip_path)

    await db.add_clip(
        {
            "id": "already-known",
            "camera": "Front Door",
            "path": str(clip_path),
            "timestamp": "2024-06-01T08:00:00+00:00",
            "size_bytes": 8,
            "duration": 5,
            "source": "pir",
            "network_id": 1,
        }
    )

    added = await import_existing_clips(db, download_path)
    assert added == 0

    clips = await db.get_clips()
    assert len(clips) == 1
    assert clips[0]["id"] == "already-known"


async def test_skips_archives_directory(db: ClipDatabase, tmp_path: Path) -> None:
    download_path = tmp_path / "clips"
    archive_clip = download_path / "archives" / "leftover.mp4"
    _touch(archive_clip)

    added = await import_existing_clips(db, download_path)
    assert added == 0
    assert await db.get_all_file_paths() == set()


async def test_derives_camera_from_filename_when_not_organized(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Files directly under download_path fall back to filename parsing."""
    download_path = tmp_path / "clips"
    clip_path = download_path / "Back_Yard_20240601_080000.mp4"
    _touch(clip_path)

    added = await import_existing_clips(db, download_path)
    assert added == 1

    clips = await db.get_clips()
    assert clips[0]["camera"] == "Back Yard"
    assert (
        clips[0]["timestamp"]
        == datetime(2024, 6, 1, 8, 0, 0, tzinfo=timezone.utc).isoformat()
    )


async def test_falls_back_to_mtime_when_no_timestamp_in_filename(
    db: ClipDatabase, tmp_path: Path
) -> None:
    download_path = tmp_path / "clips"
    clip_path = download_path / "Garage" / "clip.mp4"
    _touch(clip_path)

    before = datetime.now(timezone.utc)
    added = await import_existing_clips(db, download_path)
    after = datetime.now(timezone.utc)
    assert added == 1

    clips = await db.get_clips()
    assert clips[0]["camera"] == "Garage"
    ts = datetime.fromisoformat(clips[0]["timestamp"])
    assert before <= ts <= after


async def test_rescanning_does_not_duplicate(db: ClipDatabase, tmp_path: Path) -> None:
    download_path = tmp_path / "clips"
    clip_path = (
        download_path / "Front_Door" / "2024-06-01" / "Front_Door_20240601_080000.mp4"
    )
    _touch(clip_path)

    first = await import_existing_clips(db, download_path)
    second = await import_existing_clips(db, download_path)
    assert first == 1
    assert second == 0

    clips = await db.get_clips()
    assert len(clips) == 1
