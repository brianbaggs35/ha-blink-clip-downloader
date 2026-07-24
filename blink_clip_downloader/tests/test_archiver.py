"""Tests for ClipArchiver."""

from __future__ import annotations

import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from blink_downloader.archiver import ClipArchiver


def _make_archiver(
    tmp_path: Path,
    clips: list[dict],
    enabled: bool = True,
    archive_after_days: int = 30,
) -> tuple[ClipArchiver, MagicMock]:
    db = MagicMock()
    db.get_clips_to_archive = AsyncMock(return_value=clips)
    db.mark_archived = AsyncMock()

    archiver = ClipArchiver(
        db=db,
        archive_dir=tmp_path / "archives",
        archive_after_days=archive_after_days,
        enabled=enabled,
    )
    return archiver, db


def _old_ts(days: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ------------------------------------------------------------------
# Disabled
# ------------------------------------------------------------------


async def test_run_disabled_returns_zero(tmp_path: Path) -> None:
    archiver, db = _make_archiver(tmp_path, clips=[{"id": "c1"}], enabled=False)
    result = await archiver.run()
    assert result == 0
    db.get_clips_to_archive.assert_not_awaited()


# ------------------------------------------------------------------
# No clips to archive
# ------------------------------------------------------------------


async def test_run_no_clips_returns_zero(tmp_path: Path) -> None:
    archiver, _ = _make_archiver(tmp_path, clips=[])
    result = await archiver.run()
    assert result == 0


# ------------------------------------------------------------------
# Normal archiving
# ------------------------------------------------------------------


async def test_run_archives_clip_into_zip(tmp_path: Path) -> None:
    src = tmp_path / "Front_Door_2024-06-01.mp4"
    src.write_bytes(b"fake video data")

    clip = {
        "id": "c1",
        "camera": "Front Door",
        "file_path": str(src),
        "timestamp": "2024-06-01T08:00:00+00:00",
    }
    archiver, _db = _make_archiver(tmp_path, clips=[clip])
    result = await archiver.run()

    assert result == 1
    zip_path = tmp_path / "archives" / "blink_archive_2024-06.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("Front_Door_2024-06-01.mp4" in n for n in names)

    # Original file should be deleted
    assert not src.exists()


async def test_run_marks_db_archived(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(src),
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])
    await archiver.run()

    db.mark_archived.assert_awaited_once()
    call_args = db.mark_archived.call_args
    assert call_args[0][0] == "c1"
    assert "blink_archive_2024-06.zip" in call_args[0][1]


async def test_run_archives_multiple_months(tmp_path: Path) -> None:
    clips = []
    for month, clip_id in [("2024-05", "c1"), ("2024-06", "c2")]:
        src = tmp_path / f"{clip_id}.mp4"
        src.write_bytes(b"data")
        clips.append(
            {
                "id": clip_id,
                "camera": "Cam",
                "file_path": str(src),
                "timestamp": f"{month}-01T00:00:00+00:00",
            }
        )

    archiver, _ = _make_archiver(tmp_path, clips=clips)
    result = await archiver.run()

    assert result == 2
    assert (tmp_path / "archives" / "blink_archive_2024-05.zip").exists()
    assert (tmp_path / "archives" / "blink_archive_2024-06.zip").exists()


async def test_run_missing_file_still_marks_archived(tmp_path: Path) -> None:
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(tmp_path / "missing.mp4"),  # does not exist
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])
    result = await archiver.run()

    assert result == 1
    db.mark_archived.assert_awaited_once()


async def test_run_appends_to_existing_zip(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    zip_path = archive_dir / "blink_archive_2024-06.zip"

    # Pre-create a ZIP with one existing file
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Cam/existing.mp4", "old data")

    src = tmp_path / "new_clip.mp4"
    src.write_bytes(b"new data")

    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(src),
        "timestamp": "2024-06-15T00:00:00+00:00",
    }
    archiver, _ = _make_archiver(tmp_path, clips=[clip])
    await archiver.run()

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("existing.mp4" in n for n in names)
    assert any("new_clip.mp4" in n for n in names)


async def test_run_unknown_timestamp_uses_unknown_bucket(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    clip = {"id": "c1", "camera": "Cam", "file_path": str(src), "timestamp": ""}
    archiver, _ = _make_archiver(tmp_path, clips=[clip])
    result = await archiver.run()

    assert result == 1
    assert (tmp_path / "archives" / "blink_archive_unknown.zip").exists()


# ------------------------------------------------------------------
# Coverage gap tests
# ------------------------------------------------------------------


async def test_archive_month_bad_zip_file_is_logged(tmp_path: Path) -> None:
    """OSError opening the ZIP is caught and error is logged (lines 88-89)."""
    from unittest.mock import patch

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(src),
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, _db = _make_archiver(tmp_path, clips=[clip])

    # Make ZipFile constructor raise OSError to trigger the outer handler
    with patch(
        "blink_downloader.archiver.zipfile.ZipFile", side_effect=OSError("disk full")
    ):
        result = await archiver.run()

    assert result == 0  # nothing archived because zip open failed


async def test_archive_month_write_oserror_is_logged(tmp_path: Path) -> None:
    """OSError from ZipFile.write is caught and logged (lines 86-87)."""
    from unittest.mock import patch

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(src),
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])

    with patch("zipfile.ZipFile.write", side_effect=OSError("write failed")):
        result = await archiver.run()

    assert result == 0  # write failed, not marked as archived
    db.mark_archived.assert_not_awaited()


async def test_archive_month_unlink_failure_is_logged(tmp_path: Path) -> None:
    """An OSError deleting the source file after a successful archive write
    and DB mark must be caught and logged, not propagate and abort the
    batch — the clip is left un-counted as archived (its source file is
    still on disk) rather than crashing the rest of the run."""
    from unittest.mock import patch

    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(src),
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])

    with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
        result = await archiver.run()

    assert result == 0
    db.mark_archived.assert_awaited_once()


async def test_archive_month_mark_archived_failure_does_not_abort_batch(
    tmp_path: Path,
) -> None:
    """Regression test: a DB failure marking one clip archived (e.g. a
    dropped connection) must not abort the rest of the batch — other clips
    in the same run must still archive successfully, and the failing clip's
    source file must be left in place (untouched, not marked archived) so
    it's retried on the next archive run instead of crashing the whole
    month's batch."""
    src1 = tmp_path / "clip1.mp4"
    src1.write_bytes(b"data1")
    src2 = tmp_path / "clip2.mp4"
    src2.write_bytes(b"data2")

    clips = [
        {
            "id": "c1",
            "camera": "Cam",
            "file_path": str(src1),
            "timestamp": "2024-06-01T00:00:00+00:00",
        },
        {
            "id": "c2",
            "camera": "Cam",
            "file_path": str(src2),
            "timestamp": "2024-06-02T00:00:00+00:00",
        },
    ]
    archiver, db = _make_archiver(tmp_path, clips=clips)
    db.mark_archived = AsyncMock(side_effect=[RuntimeError("connection dropped"), None])

    result = await archiver.run()

    assert result == 1  # only c2 fully archived
    assert src1.exists()  # c1 left in place so it's retried next run
    assert not src2.exists()  # c2 successfully archived and removed

    zip_path = tmp_path / "archives" / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("clip1.mp4" in n for n in names)
    assert any("clip2.mp4" in n for n in names)


async def test_archive_month_missing_file_mark_archived_failure_is_logged(
    tmp_path: Path,
) -> None:
    """A DB failure marking an already-missing-file clip as archived must be
    caught and logged rather than propagating out of the archive run."""
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(tmp_path / "missing.mp4"),  # does not exist
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])
    db.mark_archived = AsyncMock(side_effect=RuntimeError("connection dropped"))

    result = await archiver.run()

    assert result == 0
