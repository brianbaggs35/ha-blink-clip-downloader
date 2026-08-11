"""Tests for ClipArchiver."""

from __future__ import annotations

import asyncio
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

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
    db.delete_clip = AsyncMock(return_value=True)
    db.get_archived_clip_records = AsyncMock(return_value=[])

    archiver = ClipArchiver(
        db=db,
        archive_dir=tmp_path / "archives",
        archive_after_days=archive_after_days,
        enabled=enabled,
    )
    return archiver, db


def _old_ts(days: int = 60) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _write_real_zip(path: Path, members: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


# ------------------------------------------------------------------
# Disabled
# ------------------------------------------------------------------


async def test_run_disabled_returns_zero(tmp_path: Path) -> None:
    archiver, db = _make_archiver(tmp_path, clips=[{"id": "c1"}], enabled=False)
    result = await archiver.run()
    assert result == []
    db.get_clips_to_archive.assert_not_awaited()


# ------------------------------------------------------------------
# Concurrency (poll loop vs. the Storage tab's "Run Archiving Now" button)
# ------------------------------------------------------------------


async def test_run_serializes_concurrent_calls(tmp_path: Path) -> None:
    """run() can now be invoked from two places (the poll loop and a manual
    trigger) as separate concurrent tasks — the lock must prevent their
    critical sections from ever overlapping."""
    archiver, db = _make_archiver(tmp_path, clips=[])
    active = 0
    max_active = 0

    async def slow_get_clips_to_archive(_days: int) -> list[dict]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return []

    db.get_clips_to_archive = AsyncMock(side_effect=slow_get_clips_to_archive)

    await asyncio.gather(archiver.run(), archiver.run())

    assert max_active == 1
    assert db.get_clips_to_archive.await_count == 2


async def test_archive_month_yields_periodically_for_large_batch(
    tmp_path: Path,
) -> None:
    clips = []
    for i in range(45):
        src = tmp_path / f"c{i}.mp4"
        src.write_bytes(b"data")
        clips.append(
            {
                "id": f"c{i}",
                "camera": "Cam",
                "file_path": str(src),
                "timestamp": "2024-06-01T00:00:00+00:00",
            }
        )
    archiver, _ = _make_archiver(tmp_path, clips=clips)

    with patch(
        "blink_downloader.archiver.asyncio.sleep", new=AsyncMock()
    ) as mock_sleep:
        result = await archiver.run()

    assert len(result) == 45
    # 45 clips, yielding every 20th (i=20, i=40) → 2 yields.
    assert mock_sleep.await_count == 2


# ------------------------------------------------------------------
# No clips to archive
# ------------------------------------------------------------------


async def test_run_no_clips_returns_zero(tmp_path: Path) -> None:
    archiver, _ = _make_archiver(tmp_path, clips=[])
    result = await archiver.run()
    assert result == []


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

    assert len(result) == 1
    assert result[0]["id"] == "c1"
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

    assert len(result) == 2
    assert {c["id"] for c in result} == {"c1", "c2"}
    assert (tmp_path / "archives" / "blink_archive_2024-05.zip").exists()
    assert (tmp_path / "archives" / "blink_archive_2024-06.zip").exists()


async def test_run_missing_file_deletes_the_orphaned_row(tmp_path: Path) -> None:
    """A clip whose source file is already gone before archiving has
    nothing left to write into a ZIP — marking it archived anyway (the old
    behavior) produced a row pointing at a ZIP that might never actually be
    created, surfacing later as "Archive ... is missing" Google Drive
    upload failures. The fix: remove the row instead, and never claim it
    was archived (not included in run()'s returned list, so callers like
    app.py's gdrive enqueue loop don't try to back up something just
    deleted)."""
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(tmp_path / "missing.mp4"),  # does not exist
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])
    result = await archiver.run()

    assert result == []
    db.delete_clip.assert_awaited_once_with("c1")
    db.mark_archived.assert_not_awaited()
    # Nothing to write for a file that was never there -- no ZIP created.
    assert not (tmp_path / "archives" / "blink_archive_2024-06.zip").exists()


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

    assert len(result) == 1
    assert (tmp_path / "archives" / "blink_archive_unknown.zip").exists()


# ------------------------------------------------------------------
# Coverage gap tests
# ------------------------------------------------------------------


async def test_quarantine_if_corrupted_noop_when_file_does_not_exist(
    tmp_path: Path,
) -> None:
    """The normal case for a brand-new month's archive — nothing to do,
    must not raise just because there's nothing there yet."""
    from blink_downloader.archiver import ClipArchiver

    zip_path = tmp_path / "blink_archive_2024-06.zip"
    ClipArchiver._quarantine_if_corrupted(zip_path, "2024-06")
    assert not zip_path.exists()


async def test_quarantine_if_corrupted_leaves_valid_zip_alone(tmp_path: Path) -> None:
    from blink_downloader.archiver import ClipArchiver

    zip_path = tmp_path / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Cam/clip1.mp4", b"data")

    ClipArchiver._quarantine_if_corrupted(zip_path, "2024-06")

    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["Cam/clip1.mp4"]
    assert list(tmp_path.glob("*.corrupted-*")) == []


async def test_quarantine_if_corrupted_moves_aside_unreadable_zip(
    tmp_path: Path,
) -> None:
    """The actual bug this guards against: zipfile.ZipFile(path, "a") does
    not reliably raise on a truncated/corrupted file — it can silently
    treat it as empty and discard every entry already inside on the very
    next successful-looking write. Catching this proactively, before any
    write is attempted, is the only reliable way to avoid that."""
    from blink_downloader.archiver import ClipArchiver

    zip_path = tmp_path / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Cam/clip1.mp4", b"x" * 10_000)
    # Truncate well past the point a real crash-mid-write would - this is
    # confirmed (via direct testing against CPython's zipfile module) to
    # NOT raise BadZipFile on a subsequent append, only to silently drop
    # the existing entry - exactly the failure mode being guarded against.
    size = zip_path.stat().st_size
    with zip_path.open("r+b") as f:
        f.truncate(size // 2)
    assert not zipfile.is_zipfile(zip_path)

    ClipArchiver._quarantine_if_corrupted(zip_path, "2024-06")

    assert not zip_path.exists()
    quarantined = list(tmp_path.glob("blink_archive_2024-06.zip.corrupted-*"))
    assert len(quarantined) == 1


async def test_quarantine_if_corrupted_rename_failure_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from unittest.mock import patch

    from blink_downloader.archiver import ClipArchiver

    zip_path = tmp_path / "blink_archive_2024-06.zip"
    zip_path.write_bytes(b"not a zip")

    with (
        patch("pathlib.Path.rename", side_effect=OSError("permission denied")),
        caplog.at_level("WARNING"),
    ):
        ClipArchiver._quarantine_if_corrupted(zip_path, "2024-06")

    assert zip_path.exists()  # rename failed, original left in place
    assert "could not be quarantined" in caplog.text


async def test_run_recovers_from_corrupted_archive_for_remaining_clips(
    tmp_path: Path,
) -> None:
    """Integration test for the actual reported bug: a corrupted month's
    archive must not silently block every clip for that month forever -
    it gets quarantined and a fresh archive picks up where a healthy one
    would have."""
    archives_dir = tmp_path / "archives"
    archives_dir.mkdir()
    zip_path = archives_dir / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Cam/already-archived.mp4", b"x" * 10_000)
    size = zip_path.stat().st_size
    with zip_path.open("r+b") as f:
        f.truncate(size // 2)

    clips = []
    for i in range(3):
        src = tmp_path / f"clip{i}.mp4"
        src.write_bytes(f"data{i}".encode())
        clips.append(
            {
                "id": f"c{i}",
                "camera": "Cam",
                "file_path": str(src),
                "timestamp": "2024-06-01T00:00:00+00:00",
            }
        )
    archiver, db = _make_archiver(tmp_path, clips=clips)

    result = await archiver.run()

    assert len(result) == 3
    assert db.mark_archived.await_count == 3
    assert zipfile.is_zipfile(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert len(names) == 3
    assert all(f"clip{i}.mp4" in " ".join(names) for i in range(3))
    quarantined = list(archives_dir.glob("blink_archive_2024-06.zip.corrupted-*"))
    assert len(quarantined) == 1


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

    assert result == []  # nothing archived because zip open failed


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

    assert result == []  # write failed, not marked as archived
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

    assert result == []
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

    assert len(result) == 1  # only c2 fully archived
    assert result[0]["id"] == "c2"
    assert src1.exists()  # c1 left in place so it's retried next run
    assert not src2.exists()  # c2 successfully archived and removed

    zip_path = tmp_path / "archives" / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("clip1.mp4" in n for n in names)
    assert any("clip2.mp4" in n for n in names)


async def test_archive_month_missing_file_delete_failure_is_logged(
    tmp_path: Path,
) -> None:
    """A DB failure deleting an already-missing-file clip's orphaned row
    must be caught and logged rather than propagating out of the archive
    run."""
    clip = {
        "id": "c1",
        "camera": "Cam",
        "file_path": str(tmp_path / "missing.mp4"),  # does not exist
        "timestamp": "2024-06-01T00:00:00+00:00",
    }
    archiver, db = _make_archiver(tmp_path, clips=[clip])
    db.delete_clip = AsyncMock(side_effect=RuntimeError("connection dropped"))

    result = await archiver.run()

    assert result == []


# ------------------------------------------------------------------
# prune_orphaned_archives — one-time cleanup of rows the now-fixed bug
# above already produced (or any other cause of a since-deleted ZIP).
# ------------------------------------------------------------------


async def test_prune_orphaned_archives_no_records(tmp_path: Path) -> None:
    archiver, db = _make_archiver(tmp_path, clips=[])
    db.get_archived_clip_records = AsyncMock(return_value=[])

    removed = await archiver.prune_orphaned_archives()

    assert removed == 0
    db.delete_clip.assert_not_awaited()


async def test_prune_orphaned_archives_leaves_valid_archives_alone(
    tmp_path: Path,
) -> None:
    zip_path = tmp_path / "archives" / "blink_archive_2026-05.zip"
    _write_real_zip(zip_path, {"Front Door/clip1.mp4": b"real clip data"})

    archiver, db = _make_archiver(tmp_path, clips=[])
    db.get_archived_clip_records = AsyncMock(
        return_value=[
            {
                "id": "c1",
                "archive_path": str(zip_path),
                "camera": "Front Door",
                "file_path": "/share/blink-clips/clip1.mp4",
            }
        ]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 0
    db.delete_clip.assert_not_awaited()


async def test_prune_orphaned_archives_removes_rows_whose_member_is_missing(
    tmp_path: Path,
) -> None:
    """The ZIP exists and opens fine, but no longer actually contains this
    clip — the corrupted-then-silently-overwritten scenario
    _quarantine_if_corrupted's docstring describes. A plain
    does-the-file-exist check would miss this entirely."""
    zip_path = tmp_path / "archives" / "blink_archive_2026-05.zip"
    _write_real_zip(zip_path, {"Front Door/someone-elses-clip.mp4": b"data"})

    archiver, db = _make_archiver(tmp_path, clips=[])
    db.get_archived_clip_records = AsyncMock(
        return_value=[
            {
                "id": "c1",
                "archive_path": str(zip_path),
                "camera": "Front Door",
                "file_path": "/share/blink-clips/clip1.mp4",
            }
        ]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 1
    db.delete_clip.assert_awaited_once_with("c1")


async def test_prune_orphaned_archives_removes_rows_in_unreadable_zip(
    tmp_path: Path,
) -> None:
    zip_path = tmp_path / "archives" / "blink_archive_2026-05.zip"
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(b"not actually a zip file")

    archiver, db = _make_archiver(tmp_path, clips=[])
    db.get_archived_clip_records = AsyncMock(
        return_value=[
            {
                "id": "c1",
                "archive_path": str(zip_path),
                "camera": "Front Door",
                "file_path": "/share/blink-clips/clip1.mp4",
            }
        ]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 1
    db.delete_clip.assert_awaited_once_with("c1")


async def test_prune_orphaned_archives_removes_missing_zip_rows(
    tmp_path: Path,
) -> None:
    archiver, db = _make_archiver(tmp_path, clips=[])
    missing_zip = tmp_path / "archives" / "blink_archive_2026-05.zip"
    db.get_archived_clip_records = AsyncMock(
        return_value=[{"id": "c1", "archive_path": str(missing_zip)}]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 1
    db.delete_clip.assert_awaited_once_with("c1")


async def test_prune_orphaned_archives_removes_empty_archive_path_rows(
    tmp_path: Path,
) -> None:
    """A clip somehow marked archived with no archive_path at all (the
    emptiest possible orphan) must be treated as unrecoverable too, not
    skipped for lack of a path to check."""
    archiver, db = _make_archiver(tmp_path, clips=[])
    db.get_archived_clip_records = AsyncMock(
        return_value=[{"id": "c1", "archive_path": ""}]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 1
    db.delete_clip.assert_awaited_once_with("c1")


async def test_prune_orphaned_archives_counts_only_actually_deleted_rows(
    tmp_path: Path,
) -> None:
    """delete_clip returning False (e.g. a row already gone by the time this
    runs — ON DELETE CASCADE beat it there, or a concurrent delete) must not
    be counted as removed by this run."""
    archiver, db = _make_archiver(tmp_path, clips=[])
    missing_zip = tmp_path / "archives" / "blink_archive_2026-05.zip"
    db.get_archived_clip_records = AsyncMock(
        return_value=[{"id": "c1", "archive_path": str(missing_zip)}]
    )
    db.delete_clip = AsyncMock(return_value=False)

    removed = await archiver.prune_orphaned_archives()

    assert removed == 0


async def test_prune_orphaned_archives_mixed_valid_and_orphaned(
    tmp_path: Path,
) -> None:
    valid_zip = tmp_path / "archives" / "blink_archive_2026-04.zip"
    _write_real_zip(
        valid_zip,
        {
            "Front Door/clip1.mp4": b"real clip data",
            "Front Door/clip2.mp4": b"more real clip data",
        },
    )
    missing_zip = tmp_path / "archives" / "blink_archive_2026-05.zip"

    archiver, db = _make_archiver(tmp_path, clips=[])
    db.get_archived_clip_records = AsyncMock(
        return_value=[
            {
                "id": "valid-1",
                "archive_path": str(valid_zip),
                "camera": "Front Door",
                "file_path": "/share/blink-clips/clip1.mp4",
            },
            # Same ZIP as valid-1 — also exercises the per-pass member cache
            # being reused instead of re-opening the archive for every row.
            {
                "id": "valid-2",
                "archive_path": str(valid_zip),
                "camera": "Front Door",
                "file_path": "/share/blink-clips/clip2.mp4",
            },
            {"id": "orphan-1", "archive_path": str(missing_zip)},
            {"id": "orphan-2", "archive_path": str(missing_zip)},
        ]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 2
    assert db.delete_clip.await_args_list == [call("orphan-1"), call("orphan-2")]


async def test_prune_orphaned_archives_delete_failure_is_logged(
    tmp_path: Path,
) -> None:
    """A DB failure removing one orphaned row must be caught and logged,
    not propagate and abort the rest of the cleanup pass."""
    archiver, db = _make_archiver(tmp_path, clips=[])
    missing_zip = tmp_path / "archives" / "blink_archive_2026-05.zip"
    db.get_archived_clip_records = AsyncMock(
        return_value=[
            {"id": "c1", "archive_path": str(missing_zip)},
            {"id": "c2", "archive_path": str(missing_zip)},
        ]
    )
    db.delete_clip = AsyncMock(side_effect=[RuntimeError("connection dropped"), True])

    removed = await archiver.prune_orphaned_archives()

    assert removed == 1


async def test_prune_orphaned_archives_runs_regardless_of_enabled(
    tmp_path: Path,
) -> None:
    """Repairs existing bad data even when archiving itself is currently
    disabled — this is cleanup, not a feature of ongoing archiving."""
    archiver, db = _make_archiver(tmp_path, clips=[], enabled=False)
    missing_zip = tmp_path / "archives" / "blink_archive_2026-05.zip"
    db.get_archived_clip_records = AsyncMock(
        return_value=[{"id": "c1", "archive_path": str(missing_zip)}]
    )

    removed = await archiver.prune_orphaned_archives()

    assert removed == 1
