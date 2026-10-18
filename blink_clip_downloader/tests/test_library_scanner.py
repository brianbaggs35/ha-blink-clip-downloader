"""Tests for library_scanner.import_existing_clips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from blink_downloader.database import ClipDatabase
from blink_downloader.library_scanner import (
    IMPORTED_CLIP_ID_PREFIX,
    _timestamp_from_filename,
    clip_media_present,
    import_existing_clips,
    reconcile_restored_media,
)


def _touch(path: Path, content: bytes = b"fake-mp4") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture(autouse=True)
def _mock_ffprobe():
    """Every import_existing_clips() call now shells out to ffprobe per
    file (see downloader.py's probe_clip_duration) — none of the .mp4
    fixtures in this file are real video, so a real ffprobe would just fail
    slowly and consistently return 0 anyway. Auto-mocked here so every
    existing test in this file keeps working without individually caring
    about duration probing; tests that specifically exercise the probing
    itself override this with their own more specific patch.
    """
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"5.0\n", b""))
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        yield


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
    assert clip["timestamp"] == datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC).isoformat()
    assert clip["size_bytes"] == len(b"fake-mp4")


async def test_imports_multiple_files_in_one_scan(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Coverage: the scan loop's non-yield-point index (i % 25 != 0) --
    every other test here only ever scans a single file."""
    download_path = tmp_path / "clips"
    _touch(
        download_path / "Front_Door" / "2024-06-01" / "Front_Door_20240601_080000.mp4"
    )
    _touch(download_path / "Backyard" / "2024-06-01" / "Backyard_20240601_090000.mp4")

    added = await import_existing_clips(db, download_path)
    assert added == 2

    clips = await db.get_clips()
    assert {c["camera"] for c in clips} == {"Front Door", "Backyard"}


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
        clips[0]["timestamp"] == datetime(2024, 6, 1, 8, 0, 0, tzinfo=UTC).isoformat()
    )


async def test_falls_back_to_mtime_when_no_timestamp_in_filename(
    db: ClipDatabase, tmp_path: Path
) -> None:
    download_path = tmp_path / "clips"
    clip_path = download_path / "Garage" / "clip.mp4"

    before = datetime.now(UTC)
    _touch(clip_path)
    added = await import_existing_clips(db, download_path)
    after = datetime.now(UTC)
    assert added == 1

    clips = await db.get_clips()
    assert clips[0]["camera"] == "Garage"
    ts = datetime.fromisoformat(clips[0]["timestamp"])
    # Allow a small tolerance: on some filesystems stat().st_mtime can be a
    # few milliseconds ahead of datetime.now() due to clock-source skew
    # between the filesystem and wall clock.
    tolerance = timedelta(seconds=1)
    assert before - tolerance <= ts <= after + tolerance


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


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


async def test_import_skips_directory_entries(db: ClipDatabase, tmp_path: Path) -> None:
    """rglob("*.mp4") can match directories named *.mp4; those are skipped (line 40)."""
    download_path = tmp_path / "clips"
    # Create a directory whose name ends in .mp4 (unusual but possible)
    fake_dir = download_path / "weird.mp4"
    fake_dir.mkdir(parents=True)

    added = await import_existing_clips(db, download_path)
    assert added == 0  # directories are skipped


def test_build_clip_record_handles_stat_error(tmp_path: Path) -> None:
    """If stat() raises OSError while measuring size, size defaults to 0 (lines 76-77)."""
    import unittest.mock

    from blink_downloader.library_scanner import _build_clip_record

    download_path = tmp_path / "clips"
    download_path.mkdir(parents=True)
    # Filename contains a parseable timestamp so the mtime stat() is not called;
    # the only stat() call inside _build_clip_record is for st_size.
    clip_path = download_path / "Front_Door_20240601_080000.mp4"
    clip_path.write_bytes(b"fake")

    with unittest.mock.patch.object(
        Path, "stat", side_effect=OSError("permission denied")
    ):
        record = _build_clip_record(download_path, clip_path)

    assert record["size_bytes"] == 0


def test_timestamp_from_filename_invalid_date() -> None:
    """A match that cannot be parsed as a date returns None (lines 101-102)."""
    # strptime would raise ValueError for month=99
    result = _timestamp_from_filename("camera_99990099_999999.mp4")
    assert result is None


# ---------------------------------------------------------------------------
# Duration probing (probe_clip_duration / import_existing_clips wiring —
# probe_clip_duration itself now lives in downloader.py, since the normal
# download path needs the same fallback; see test_downloader.py for its
# own unit tests)
# ---------------------------------------------------------------------------


async def test_import_stores_probed_duration(db: ClipDatabase, tmp_path: Path) -> None:
    """Regression test: a reconciled clip's Library detail view showed
    Duration as "—" (0) even though the file itself has a real duration
    ffprobe can read — reconciliation just never probed it, unlike the
    normal download path (which gets duration for free from the Blink API
    response). Confirmed directly against a real clip found in this
    session's own testing."""
    download_path = tmp_path / "clips"
    clip_path = (
        download_path / "Front_Door" / "2024-06-01" / "Front_Door_20240601_080000.mp4"
    )
    _touch(clip_path)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"12.7\n", b""))
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ) as mock_exec:
        added = await import_existing_clips(db, download_path)
    assert added == 1

    clips = await db.get_clips()
    assert clips[0]["duration"] == 12  # truncated to whole seconds

    args = mock_exec.call_args.args
    assert args[0] == "ffprobe"
    assert str(clip_path) in args


# ---------------------------------------------------------------------------
# The ids the scan mints, and clips a backup restore kept without their video
# ---------------------------------------------------------------------------


async def test_imported_clips_carry_the_placeholder_prefix(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """A backup restore tells a rebuilt stand-in from a real record by this."""
    download_path = tmp_path / "clips"
    _touch(download_path / "Front_Door_20240601_080000.mp4")
    await import_existing_clips(db, download_path)
    (clip,) = await db.get_clips()
    assert clip["id"].startswith(IMPORTED_CLIP_ID_PREFIX)


def test_clip_media_present_follows_the_file_for_an_ordinary_clip(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    clip = {"file_path": str(video), "archived": False, "archive_path": ""}
    assert clip_media_present(clip) is False
    video.write_bytes(b"x")
    assert clip_media_present(clip) is True


def test_clip_media_present_follows_the_zip_for_an_archived_clip(
    tmp_path: Path,
) -> None:
    """An archived clip's own file is deleted by design; its ZIP is its video."""
    archive = tmp_path / "blink_archive_2024-06.zip"
    clip = {
        "file_path": str(tmp_path / "gone.mp4"),
        "archived": True,
        "archive_path": str(archive),
    }
    assert clip_media_present(clip) is False
    archive.write_bytes(b"x")
    assert clip_media_present(clip) is True


def test_clip_media_present_is_false_for_an_empty_location() -> None:
    """Path("") is the current directory, which does exist."""
    assert clip_media_present({"file_path": "", "archived": False}) is False
    assert (
        clip_media_present({"file_path": "x", "archived": True, "archive_path": ""})
        is False
    )


async def test_reconcile_clears_only_clips_whose_video_is_back(
    db: ClipDatabase, tmp_path: Path
) -> None:
    assert db._pool is not None
    back, still_gone = tmp_path / "back.mp4", tmp_path / "gone.mp4"
    for clip_id, path in (("back", back), ("gone", still_gone)):
        await db.add_clip(
            {
                "id": clip_id,
                "camera": "Front Door",
                "path": str(path),
                "timestamp": "2024-06-01T08:00:00+00:00",
            }
        )
    await db._pool.execute("UPDATE clips SET media_missing=TRUE")
    back.write_bytes(b"x")

    assert await reconcile_restored_media(db) == 1
    assert [c["id"] for c in await db.get_media_missing_clips()] == ["gone"]


async def test_reconcile_with_nothing_flagged_changes_nothing(
    db: ClipDatabase,
) -> None:
    assert await reconcile_restored_media(db) == 0
