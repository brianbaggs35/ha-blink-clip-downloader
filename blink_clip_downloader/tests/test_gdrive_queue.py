"""Tests for GDriveUploadQueue."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.database import ClipDatabase
from blink_downloader.gdrive_client import GDriveClient
from blink_downloader.gdrive_queue import GDriveUploadQueue, _local_date_str


@contextlib.contextmanager
def _local_timezone(tz_name: str):
    """Force the process's local timezone for the duration of the block —
    see test_database.py's identical helper for why this beats depending on
    whatever zone the test host happens to be configured with."""
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


def _make_client_mock(**kwargs: Any) -> MagicMock:
    m = MagicMock(spec=GDriveClient)
    m.connected = kwargs.get("connected", True)
    # MagicMock(spec=...) would otherwise make these truthy child Mocks
    # (properties/attrs resolve to a fresh Mock on a spec'd class), which
    # GDriveUploadQueue._process_pending's rate-limit early-break and
    # _process_one's quota branch would misread as always-true — mirrors
    # test_analysis_queue.py's identical concern for ClipAnalyzer.rate_limited.
    m.rate_limited = kwargs.get("rate_limited", False)
    m.quota_exceeded = kwargs.get("quota_exceeded", False)
    # Empty by default (matches a real, unconnected GDriveClient's own
    # _folder_id default) so tests that don't care about the date/camera
    # folder hierarchy skip that branch entirely instead of following
    # get_or_create_folder_path's auto-mocked (truthy, non-None) return.
    m.folder_id = kwargs.get("folder_id", "")
    m.upload_file = AsyncMock(return_value=kwargs.get("file_id", "drive-file-123"))
    m.get_or_create_folder_path = AsyncMock(
        return_value=kwargs.get("dest_folder_id", "dest-folder-id")
    )
    return m


def _make_queue(
    client: MagicMock,
    db: ClipDatabase,
    notifier: MagicMock | None = None,
    **kwargs: Any,
) -> GDriveUploadQueue:
    return GDriveUploadQueue(
        client=client,
        db=db,
        notifier=notifier,
        batch_size=int(kwargs.get("batch_size", 10)),
        check_interval=int(kwargs.get("check_interval", 1)),
    )


def _add_clip(clip_id: str = "c1") -> dict:
    return {
        "id": clip_id,
        "camera": "Front Door",
        "path": f"/share/blink-clips/{clip_id}.mp4",
        "timestamp": "2024-06-01T08:00:00+00:00",
        "size_bytes": 1_048_576,
        "duration": 5,
        "source": "pir",
        "network_id": 10,
    }


# ------------------------------------------------------------------
# Enqueue
# ------------------------------------------------------------------


async def test_enqueue_inserts_pending_record(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    await queue.enqueue({"id": "c1", "camera": "Front", "path": "/c1.mp4"})

    pending = await db.get_pending_gdrive_uploads()
    assert len(pending) == 1
    assert pending[0]["clip_id"] == "c1"
    assert pending[0]["folder_id"] == ""


async def test_enqueue_skips_empty_id(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await queue.enqueue({"id": "", "camera": "A", "path": "/x.mp4"})
    assert await db.get_pending_gdrive_uploads() == []


async def test_enqueue_with_folder_id_stores_it(db: ClipDatabase) -> None:
    """A manual per-batch upload (Library's Upload to Drive) can target a
    folder other than the client's default — stored on the queue row."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    await queue.enqueue(
        {"id": "c1", "camera": "Front", "path": "/c1.mp4"}, folder_id="folder-xyz"
    )

    pending = await db.get_pending_gdrive_uploads()
    assert pending[0]["folder_id"] == "folder-xyz"


async def test_enqueue_falls_back_to_file_path_key(db: ClipDatabase) -> None:
    """Archived clips come from a DB row (file_path key), not the downloader
    (path key) — enqueue must accept either shape."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    await queue.enqueue({"id": "c1", "camera": "Front", "file_path": "/c1.mp4"})

    pending = await db.get_pending_gdrive_uploads()
    assert len(pending) == 1


async def test_enqueue_returns_true_when_queued(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    assert (
        await queue.enqueue({"id": "c1", "camera": "Front", "path": "/c1.mp4"}) is True
    )


async def test_enqueue_returns_false_for_empty_id(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)

    assert await queue.enqueue({"id": "", "camera": "A", "path": "/x.mp4"}) is False


async def test_enqueue_returns_false_for_already_pending_clip(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    clip = {"id": "c1", "camera": "Front", "path": "/c1.mp4"}
    await queue.enqueue(clip)

    assert await queue.enqueue(clip) is False


async def test_enqueue_returns_true_when_retrying_a_failed_clip(
    db: ClipDatabase,
) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    clip = {"id": "c1", "camera": "Front", "path": "/c1.mp4"}
    await queue.enqueue(clip)
    await db.update_gdrive_queue_status("c1", "failed", error="boom")

    assert await queue.enqueue(clip) is True
    pending = await db.get_pending_gdrive_uploads()
    assert len(pending) == 1


# ------------------------------------------------------------------
# Processing — normal (unarchived) clips
# ------------------------------------------------------------------


async def test_process_pending_uploads_clip(db: ClipDatabase, tmp_path: Path) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"video data")
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    call_args = client.upload_file.call_args
    assert call_args[0][0] == src
    assert call_args.kwargs["folder_id"] is None
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1
    updated = await db.get_clip("c1")
    assert updated is not None
    assert updated["gdrive_backed_up"] is True
    assert updated["gdrive_file_id"] == "drive-file-123"


async def test_process_pending_skips_when_empty(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    await queue._process_pending()
    client.upload_file.assert_not_awaited()


async def test_process_one_missing_source_file_marks_failed(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = "/nonexistent/missing.mp4"
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_not_awaited()
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1


async def test_process_one_upload_failure_marks_failed(
    db: ClipDatabase, tmp_path: Path
) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(file_id=None)
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1
    updated = await db.get_clip("c1")
    assert updated is not None
    assert updated["gdrive_backed_up"] is False


async def test_process_one_quota_exceeded_notifies_and_marks_failed(
    db: ClipDatabase, tmp_path: Path
) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(file_id=None, quota_exceeded=True)
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value=True)
    queue = _make_queue(client, db, notifier=notifier)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    notifier.notify.assert_awaited_once()
    title = (
        notifier.notify.call_args.kwargs.get("title") or notifier.notify.call_args[0][1]
    )
    assert "Google Drive" in str(title)
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1


async def test_process_one_quota_exceeded_without_notifier_does_not_raise(
    db: ClipDatabase, tmp_path: Path
) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(file_id=None, quota_exceeded=True)
    queue = _make_queue(client, db, notifier=None)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()  # must not raise with no notifier configured

    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1


async def test_process_one_missing_clip_marks_failed(db: ClipDatabase) -> None:
    """Defensive branch: the clip vanished between enqueue and processing.
    ON DELETE CASCADE means this queue row would normally vanish too, so
    this is only reachable by calling _process_one directly."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await queue._process_one({"clip_id": "ghost", "camera": "X", "clip_path": "/x.mp4"})

    client.upload_file.assert_not_awaited()


async def test_process_one_exception_during_upload_is_caught(
    db: ClipDatabase, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock()
    client.upload_file = AsyncMock(side_effect=RuntimeError("network exploded"))
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    with caplog.at_level("WARNING"):
        await queue._process_pending()

    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1
    assert "network exploded" in caplog.text


# ------------------------------------------------------------------
# Processing — date/camera folder hierarchy
# ------------------------------------------------------------------


async def test_process_one_builds_date_camera_folder_structure(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """When the client has a connected backup folder, uploads are organized
    as <date>/<camera>/<file> rather than dropped flat into that folder."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"video data")
    client = _make_client_mock(folder_id="root-folder", dest_folder_id="leaf-folder")
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    clip["timestamp"] = "2024-06-01T08:00:00+00:00"
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    client.get_or_create_folder_path.assert_awaited_once()
    call = client.get_or_create_folder_path.call_args
    path_parts, root_id = call[0][0], call.kwargs["root_id"]
    assert path_parts[1] == "Front Door"
    assert root_id == "root-folder"

    client.upload_file.assert_awaited_once()
    assert client.upload_file.call_args.kwargs["folder_id"] == "leaf-folder"
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1


async def test_process_one_manual_folder_id_overrides_client_default(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """A manual, one-off upload (Library's "Upload to Drive") targets its
    own root folder rather than the client's connected default."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"video data")
    client = _make_client_mock(folder_id="default-root", dest_folder_id="leaf-folder")
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip, folder_id="manual-root")

    await queue._process_pending()

    assert client.get_or_create_folder_path.call_args.kwargs["root_id"] == "manual-root"


async def test_process_one_folder_structure_creation_fails_marks_failed(
    db: ClipDatabase, tmp_path: Path
) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"video data")
    client = _make_client_mock(folder_id="root-folder")
    client.get_or_create_folder_path = AsyncMock(return_value=None)
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_not_awaited()
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1


async def test_process_one_no_root_folder_skips_folder_resolution(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """No connected/override folder at all (client.folder_id == "") — upload
    proceeds straight to the account root, same as before this feature."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"video data")
    client = _make_client_mock(folder_id="")
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    client.get_or_create_folder_path.assert_not_awaited()
    assert client.upload_file.call_args.kwargs["folder_id"] is None


# ------------------------------------------------------------------
# Processing — archived clips (ZIP extraction)
# ------------------------------------------------------------------


def _make_archive(
    tmp_path: Path, camera: str, clip_filename: str, content: bytes
) -> Path:
    zip_path = tmp_path / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{camera}/{clip_filename}", content)
    return zip_path


async def test_process_one_archived_clip_extracts_from_zip(
    db: ClipDatabase, tmp_path: Path
) -> None:
    zip_path = _make_archive(tmp_path, "Front Door", "c1.mp4", b"archived video bytes")
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(tmp_path / "c1.mp4")  # original file no longer exists
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    extracted_path = client.upload_file.call_args[0][0]
    assert extracted_path != Path(clip["path"])
    assert not extracted_path.exists()  # temp file cleaned up after processing
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1


async def test_process_one_archived_clip_falls_back_to_unique_filename_after_rename(
    db: ClipDatabase, tmp_path: Path
) -> None:
    zip_path = _make_archive(tmp_path, "Front Door", "c1.mp4", b"archived video bytes")
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["camera"] = "Entryway"
    clip["path"] = str(tmp_path / "Front_Door/2024-06-01/c1.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    assert (await db.get_gdrive_queue_counts())["completed"] == 1


async def test_process_one_archived_clip_uploads_under_its_real_filename(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Regression test: the remote filename must be the clip's own real
    name (from its DB file_path), not upload_path.name - for an archived
    clip, upload_path is a NamedTemporaryFile's random OS-assigned path
    (e.g. tmpXXXXXX.mp4), which would otherwise upload every archived clip
    under a meaningless random name instead of anything recognizable."""
    zip_path = _make_archive(
        tmp_path, "Front Door", "Front_Door_20240601_080000.mp4", b"archived video"
    )
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(tmp_path / "Front_Door_20240601_080000.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    remote_name = client.upload_file.call_args[0][1]
    assert remote_name == "Front_Door_20240601_080000.mp4"
    assert not remote_name.startswith("tmp")


async def test_process_one_archived_mid_backlog_race(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Regression test: a clip enqueued while still a regular file can be
    archived by archiver.py before the upload queue gets to it (a backlog,
    Drive being briefly unreachable, etc.) — the original file is deleted
    when that happens, so _process_one must re-check archived status at
    processing time and read from the ZIP, not the stale original path."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"original bytes")
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)  # enqueued while NOT yet archived

    # Simulate archiver.py running in the meantime: zip it, mark archived,
    # delete the original — exactly what ClipArchiver._archive_month does.
    zip_path = _make_archive(tmp_path, "Front Door", "c1.mp4", b"original bytes")
    await db.mark_archived("c1", str(zip_path))
    src.unlink()

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    extracted_path = client.upload_file.call_args[0][0]
    assert extracted_path != src  # did not try the now-deleted original path
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1


async def test_process_one_archive_file_missing_marks_failed(
    db: ClipDatabase, tmp_path: Path
) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(tmp_path / "c1.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(tmp_path / "does-not-exist.zip"))
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_not_awaited()
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1


async def test_process_one_archive_member_missing_marks_failed(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """The ZIP exists but doesn't contain this clip's member (corrupt/edited
    archive) — extraction fails cleanly rather than raising."""
    zip_path = tmp_path / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Front Door/other-clip.mp4", b"not this one")

    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(tmp_path / "c1.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_not_awaited()
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 1


async def test_process_one_temp_file_cleanup_failure_does_not_override_completed(
    db: ClipDatabase, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure removing the scratch-extracted temp file must not mask an
    already-recorded "completed" status."""
    zip_path = _make_archive(tmp_path, "Front Door", "c1.mp4", b"data")
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(tmp_path / "c1.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    with (
        patch("pathlib.Path.unlink", side_effect=OSError("permission denied")),
        caplog.at_level("WARNING"),
    ):
        await queue._process_pending()

    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1
    assert "Could not remove temp file" in caplog.text


# ------------------------------------------------------------------
# Batch rate-limit handling
# ------------------------------------------------------------------


async def test_process_pending_stops_batch_on_rate_limit(
    db: ClipDatabase, tmp_path: Path
) -> None:
    client = _make_client_mock(rate_limited=True)
    queue = _make_queue(client, db, batch_size=3)
    queue._running = True

    for clip_id in ("c1", "c2", "c3"):
        src = tmp_path / f"{clip_id}.mp4"
        src.write_bytes(b"data")
        clip = _add_clip(clip_id)
        clip["path"] = str(src)
        await db.add_clip(clip)
        await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1
    assert counts["pending"] == 2


async def test_process_pending_stops_batch_on_quota_exceeded(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """A full Drive quota is just as sticky within a batch as a rate limit —
    every remaining clip would fail identically, so the batch must stop
    early here too instead of re-attempting (and re-notifying) per clip."""
    client = _make_client_mock(quota_exceeded=True)
    queue = _make_queue(client, db, batch_size=3)
    queue._running = True

    for clip_id in ("c1", "c2", "c3"):
        src = tmp_path / f"{clip_id}.mp4"
        src.write_bytes(b"data")
        clip = _add_clip(clip_id)
        clip["path"] = str(src)
        await db.add_clip(clip)
        await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 1
    assert counts["pending"] == 2


async def test_process_pending_continues_batch_without_rate_limit(
    db: ClipDatabase, tmp_path: Path
) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db, batch_size=3)
    queue._running = True

    for clip_id in ("c1", "c2", "c3"):
        src = tmp_path / f"{clip_id}.mp4"
        src.write_bytes(b"data")
        clip = _add_clip(clip_id)
        clip["path"] = str(src)
        await db.add_clip(clip)
        await queue.enqueue(clip)

    await queue._process_pending()

    assert client.upload_file.await_count == 3
    counts = await db.get_gdrive_queue_counts()
    assert counts["completed"] == 3


async def test_process_pending_breaks_when_queue_stopped(
    db: ClipDatabase, tmp_path: Path
) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock()
    queue = _make_queue(client, db, batch_size=3)
    queue._running = False

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_not_awaited()


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


async def test_stop_sets_running_false(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True
    queue.stop()
    assert not queue._running


async def test_start_runs_loop_and_exits_on_stop(db: ClipDatabase) -> None:
    client = _make_client_mock(connected=False)
    queue = _make_queue(client, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    assert not queue._running


async def test_start_skips_processing_when_nothing_pending(db: ClipDatabase) -> None:
    client = _make_client_mock()
    queue = _make_queue(client, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    client.upload_file.assert_not_awaited()


async def test_start_skips_processing_when_not_connected(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Pending clips exist, but the client isn't connected yet — nothing
    should be attempted until the user connects (they just accumulate)."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(connected=False)
    queue = _make_queue(client, db, check_interval=1)

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    client.upload_file.assert_not_awaited()


async def test_start_exits_on_cancelled_error(db: ClipDatabase, tmp_path: Path) -> None:
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock()
    client.upload_file = AsyncMock(side_effect=asyncio.CancelledError)
    queue = _make_queue(client, db, check_interval=1)

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    with pytest.raises(asyncio.CancelledError):
        await queue.start()


async def test_start_logs_exception_and_continues(db: ClipDatabase) -> None:
    call_count = 0

    async def flaky_get_counts() -> dict:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient db error")
        return {"pending": 0, "processing": 0, "completed": 0, "failed": 0}

    client = _make_client_mock()
    queue = _make_queue(client, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        if call_count >= 2:
            queue._running = False

    with (
        patch.object(db, "get_gdrive_queue_counts", side_effect=flaky_get_counts),
        patch("asyncio.sleep", fake_sleep),
    ):
        await queue.start()

    assert call_count >= 2


async def test_start_early_return_from_sleep_loop(db: ClipDatabase) -> None:
    client = _make_client_mock(connected=False)
    queue = _make_queue(client, db, check_interval=2)

    sleep_count = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    assert sleep_count == 1


# ------------------------------------------------------------------
# Queue status
# ------------------------------------------------------------------


async def test_get_queue_status(db: ClipDatabase) -> None:
    client = _make_client_mock(connected=True)
    queue = _make_queue(client, db)

    await db.add_clip(_add_clip("c1"))
    await queue.enqueue({"id": "c1", "camera": "Front", "path": "/c1.mp4"})

    status = await queue.get_queue_status()
    assert status["pending"] == 1
    assert status["connected"] is True


# ------------------------------------------------------------------
# _local_date_str
# ------------------------------------------------------------------


def test_local_date_str_uses_local_calendar_day_not_utc() -> None:
    """A UTC timestamp that's already "tomorrow" in an east-of-UTC zone must
    resolve to that later local date, not the earlier UTC one — this is the
    whole reason the helper exists instead of just slicing the ISO string."""
    with _local_timezone("Asia/Tokyo"):  # UTC+9
        assert _local_date_str("2024-06-01T20:00:00+00:00") == "2024-06-02"


def test_local_date_str_uses_local_calendar_day_west_of_utc() -> None:
    with _local_timezone("America/New_York"):  # UTC-4/-5
        assert _local_date_str("2024-06-02T02:00:00+00:00") == "2024-06-01"


def test_local_date_str_naive_timestamp_treated_as_utc() -> None:
    """No tzinfo on the stored timestamp (legacy rows) must not raise or
    silently be treated as already-local — it's assumed UTC first, matching
    how every timestamp in this codebase is written."""
    with _local_timezone("Asia/Tokyo"):
        assert _local_date_str("2024-06-01T20:00:00") == "2024-06-02"


def test_local_date_str_unparseable_falls_back_to_today() -> None:
    expected = datetime.now().astimezone().strftime("%Y-%m-%d")
    assert _local_date_str("not-a-timestamp") == expected


def test_local_date_str_empty_falls_back_to_today() -> None:
    expected = datetime.now().astimezone().strftime("%Y-%m-%d")
    assert _local_date_str("") == expected


def test_local_date_str_recent_real_timestamp_roundtrips() -> None:
    """Sanity check against a real "now" value end-to-end, not just the
    synthetic boundary cases above."""
    now_utc = datetime.now(UTC)
    result = _local_date_str(now_utc.isoformat())
    assert result == now_utc.astimezone().strftime("%Y-%m-%d")
