"""Tests for GDriveUploadQueue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    # Same reason as the two above: a truthy child Mock here would read as
    # "the user paused uploads" and stop the queue in every single test.
    m.uploads_paused = kwargs.get("uploads_paused", False)
    m.pause_reason = kwargs.get("pause_reason", "")

    def _set_uploads_paused(paused: bool, reason: str = "") -> None:
        # The real client updates in memory and persists; the queue reads
        # uploads_paused straight back to decide whether it has already
        # stopped, so the mock has to actually hold the value.
        m.uploads_paused = paused
        m.pause_reason = reason if paused else ""

    m.set_uploads_paused = MagicMock(side_effect=_set_uploads_paused)
    # Empty by default (matches a real, unconnected GDriveClient's own
    # _folder_id default) so tests that don't care about the date/camera
    # folder hierarchy skip that branch entirely instead of following
    # get_or_create_folder_path's auto-mocked (truthy, non-None) return.
    m.folder_id = kwargs.get("folder_id", "")
    m.upload_file = AsyncMock(return_value=kwargs.get("file_id", "drive-file-123"))
    # Folder cleanup after an archive delete. Defaults resolve every folder
    # and find it empty, so a test only overrides the case it is about.
    m.find_folder_path = AsyncMock(return_value=kwargs.get("found_folder", "folder-id"))
    m.folder_is_empty = AsyncMock(return_value=kwargs.get("folder_empty", True))
    m.delete_file = AsyncMock(return_value=kwargs.get("deleted", True))
    m.forget_folder = MagicMock()
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


async def test_process_one_quota_exceeded_notifies_and_stays_pending(
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
    # Left pending, not failed: a full Drive says nothing about this clip,
    # which will upload perfectly once there is room. Writing it off as a
    # failure is what buried the Storage tab under hundreds of identical
    # rows — one more clip consumed on every cycle for as long as the Drive
    # stayed full.
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 0
    assert counts["pending"] == 1
    # ...and the queue stops outright rather than retrying: a full Drive
    # clears when a person makes room, not on a timer, so every attempt in
    # between just costs a round trip to be told the same thing.
    client.set_uploads_paused.assert_called_once_with(
        True, "Google Drive storage quota exceeded"
    )
    assert client.uploads_paused is True


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
    assert counts["failed"] == 0
    assert counts["pending"] == 1


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


async def test_process_one_archived_clip_skips_full_enumeration_on_the_fast_path(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """The common case (no rename involved) must resolve via a direct
    getinfo() lookup, not by building a set from the archive's full
    namelist() -- that full enumeration is only for the rare fallback path.
    """
    zip_path = _make_archive(tmp_path, "Front Door", "c1.mp4", b"archived video bytes")
    client = _make_client_mock()
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(tmp_path / "c1.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    with patch.object(
        zipfile.ZipFile, "namelist", wraps=zipfile.ZipFile.namelist, autospec=True
    ) as namelist_spy:
        await queue._process_pending()

    namelist_spy.assert_not_called()
    client.upload_file.assert_awaited_once()
    assert (await db.get_gdrive_queue_counts())["completed"] == 1


async def test_process_one_archived_clip_prefers_nearest_parent_fallback(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Multiple ancestor directories of the clip's stored path can each
    coincidentally match a ZIP member after more than one rename. The
    nearest parent must win deterministically, not an arbitrary one --
    fallback_arcnames used to be a set, so Python's per-process hash
    randomization could pick either candidate across different runs.
    """
    zip_path = tmp_path / "blink_archive_2024-06.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Distant Name/c1.mp4", b"stale distant-ancestor video")
        zf.writestr("Nearest Name/c1.mp4", b"correct nearest-parent video")

    uploaded_bytes: list[bytes] = []

    async def _fake_upload(path: Path, *_args: Any, **_kwargs: Any) -> str:
        uploaded_bytes.append(Path(path).read_bytes())
        return "drive-file-123"

    client = _make_client_mock()
    client.upload_file = AsyncMock(side_effect=_fake_upload)
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["camera"] = "Entryway"
    clip["path"] = str(tmp_path / "Distant Name/Nearest Name/c1.mp4")
    await db.add_clip(clip)
    await db.mark_archived("c1", str(zip_path))
    await queue.enqueue(clip)

    await queue._process_pending()

    client.upload_file.assert_awaited_once()
    assert uploaded_bytes == [b"correct nearest-parent video"]
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


async def test_start_skips_processing_while_uploads_are_paused(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Pausing used to mean disconnecting, which throws away the OAuth
    tokens and the chosen backup folder to achieve it. A paused-but-
    connected client must simply not upload."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(uploads_paused=True)
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
    # ...and the clip is still queued, waiting rather than written off.
    counts = await db.get_gdrive_queue_counts()
    assert counts["pending"] == 1


async def test_start_skips_processing_while_holding_off(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """The fix for the bug that filled the Storage tab: a rate limit used
    to consume one more clip per cycle. The hold-off is what stops the
    cycle after it from trying again straight away."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock()
    queue = _make_queue(client, db, check_interval=1)
    queue._hold_off("Google Drive rate limit", 900)

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    client.upload_file.assert_not_awaited()


async def test_quota_pause_notifies_once_however_long_it_lasts(
    db: ClipDatabase,
) -> None:
    """A Drive that is still full is not news twice. One push per cycle for
    as long as it stays full is its own kind of spam."""
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value=True)
    client = _make_client_mock(quota_exceeded=True)
    queue = _make_queue(client, db, notifier=notifier)

    await queue._pause_for_quota()
    await queue._pause_for_quota()
    await queue._pause_for_quota()

    notifier.notify.assert_awaited_once()
    client.set_uploads_paused.assert_called_once()


async def test_quota_pause_still_stops_uploads_when_it_cannot_be_persisted(
    db: ClipDatabase,
) -> None:
    """Only the survives-a-restart part is lost — this session must still
    stop, because the alternative is carrying on against a full Drive."""
    client = _make_client_mock()
    client.set_uploads_paused = MagicMock(side_effect=OSError("read-only /data"))
    queue = _make_queue(client, db)

    await queue._pause_for_quota()  # must not raise

    client.set_uploads_paused.assert_called_once()


async def test_resume_clears_the_hold_off(db: ClipDatabase) -> None:
    """What Retry (and Resume) on the Storage tab is for: someone who has
    just fixed the problem should not wait out the rest of a window that is
    no longer true."""
    queue = _make_queue(_make_client_mock(), db)
    queue._hold_off("Google Drive rate limit", 900)
    assert queue.holding_off

    queue.resume()

    assert not queue.holding_off
    assert queue.hold_off_seconds == 0
    status = await queue.get_queue_status()
    assert status["hold_off_reason"] == ""


async def test_extending_a_hold_off_for_the_same_reason_logs_once(
    db: ClipDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """A rate limit still in force a cycle later is not news. The window is
    still pushed out, but a line per cycle for as long as it lasts is its
    own kind of noise."""
    queue = _make_queue(_make_client_mock(), db)
    with caplog.at_level(logging.WARNING, logger="blink_downloader.gdrive_queue"):
        queue._hold_off("Google Drive rate limit", 60)
        first_until = queue._hold_off_until
        queue._hold_off("Google Drive rate limit", 900)

    assert queue._hold_off_until > first_until
    assert sum("holding off Drive uploads" in r.message for r in caplog.records) == 1


async def test_a_different_hold_off_reason_logs_again(
    db: ClipDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a repeat of the *same* reason is suppressed — a new condition
    taking over an existing hold-off is something to say."""
    queue = _make_queue(_make_client_mock(), db)
    with caplog.at_level(logging.WARNING, logger="blink_downloader.gdrive_queue"):
        queue._hold_off("Google Drive rate limit", 900)
        queue._hold_off("Drive is full", 900)

    assert queue._hold_off_reason == "Drive is full"
    assert sum("holding off Drive uploads" in r.message for r in caplog.records) == 2


async def test_queue_status_reports_pause_and_hold_off(db: ClipDatabase) -> None:
    queue = _make_queue(
        _make_client_mock(uploads_paused=True, pause_reason="Drive is full"), db
    )
    queue._hold_off("Google Drive rate limit", 900)

    status = await queue.get_queue_status()

    assert status["uploads_paused"] is True
    assert status["pause_reason"] == "Drive is full"
    assert status["hold_off_reason"] == "Google Drive rate limit"
    assert status["hold_off_seconds"] > 0


async def test_rate_limited_upload_stays_pending_rather_than_failing(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """Same reasoning as the quota case: a rate limit is about the moment,
    not about the clip, and it clears on its own."""
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(file_id=None, rate_limited=True)
    queue = _make_queue(client, db)
    queue._running = True

    clip = _add_clip("c1")
    clip["path"] = str(src)
    await db.add_clip(clip)
    await queue.enqueue(clip)

    await queue._process_pending()

    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 0
    assert counts["pending"] == 1
    assert queue.holding_off


async def test_a_full_drive_stops_the_queue_instead_of_burning_a_clip_a_cycle(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """The bug this whole change exists for.

    A full Drive used to consume one more clip on every single cycle and
    write it off as failed, for as long as the Drive stayed full — which is
    how the Storage tab ended up under hundreds of identical "quota
    exceeded" rows. Every clip must survive as pending, and the second
    cycle must not attempt anything at all.
    """
    src = tmp_path / "c1.mp4"
    src.write_bytes(b"data")
    client = _make_client_mock(file_id=None, quota_exceeded=True)
    queue = _make_queue(client, db, check_interval=1)

    for clip_id in ("c1", "c2", "c3"):
        clip = _add_clip(clip_id)
        clip["path"] = str(src)
        await db.add_clip(clip)
        await queue.enqueue(clip)

    cycles = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal cycles
        cycles += 1
        if cycles >= 2:
            queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    # One attempt in total, across both cycles — not one per clip, and not
    # one more on the cycle after.
    client.upload_file.assert_awaited_once()
    counts = await db.get_gdrive_queue_counts()
    assert counts["failed"] == 0
    assert counts["pending"] == 3


# ----------------------------------------------------------------------
# Cleaning up the folders a deleted archive's backups lived in
# ----------------------------------------------------------------------


def _backed_up_clip(clip_id: str, camera: str, timestamp: str) -> dict[str, Any]:
    return {"id": clip_id, "camera": camera, "timestamp": timestamp}


async def test_prune_trashes_the_now_empty_date_and_camera_folders(
    db: ClipDatabase,
) -> None:
    """Deleting a month's archive trashed every clip inside it and left the
    whole empty date-folder scaffolding standing in Drive — which looks a
    great deal like nothing was deleted at all."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    trashed = await queue.prune_empty_backup_folders(
        [_backed_up_clip("c1", "Driveway", "2026-06-05T12:00:00+00:00")]
    )

    # The camera folder and the date folder above it.
    assert trashed == 2
    assert client.delete_file.await_count == 2
    client.forget_folder.assert_called_with("folder-id")


async def test_prune_checks_each_folder_once_not_each_clip(
    db: ClipDatabase,
) -> None:
    """An archive holds hundreds of clips but only a handful of folders, and
    every check is its own Drive round trip."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    clips = [
        _backed_up_clip(f"c{i}", "Driveway", "2026-06-05T12:00:00+00:00")
        for i in range(50)
    ]

    await queue.prune_empty_backup_folders(clips)

    # One camera folder, one date folder — not 50 of each.
    assert client.find_folder_path.await_count == 2


async def test_prune_leaves_a_folder_that_still_holds_something(
    db: ClipDatabase,
) -> None:
    """A folder holding anything the user put there — or a clip whose own
    delete failed — has to survive."""
    client = _make_client_mock(folder_empty=False)
    queue = _make_queue(client, db)

    trashed = await queue.prune_empty_backup_folders(
        [_backed_up_clip("c1", "Driveway", "2026-06-05T12:00:00+00:00")]
    )

    assert trashed == 0
    client.delete_file.assert_not_awaited()


async def test_prune_skips_a_folder_that_is_no_longer_there(
    db: ClipDatabase,
) -> None:
    client = _make_client_mock(found_folder=None)
    queue = _make_queue(client, db)

    assert (
        await queue.prune_empty_backup_folders(
            [_backed_up_clip("c1", "Driveway", "2026-06-05T12:00:00+00:00")]
        )
        == 0
    )
    client.folder_is_empty.assert_not_awaited()


async def test_prune_does_not_forget_a_folder_drive_refused_to_trash(
    db: ClipDatabase,
) -> None:
    """Dropping it from the path memo on a failed delete would make the next
    upload re-resolve a folder that is still perfectly fine."""
    client = _make_client_mock(deleted=False)
    queue = _make_queue(client, db)

    assert (
        await queue.prune_empty_backup_folders(
            [_backed_up_clip("c1", "Driveway", "2026-06-05T12:00:00+00:00")]
        )
        == 0
    )
    client.forget_folder.assert_not_called()


async def test_prune_does_nothing_while_disconnected(db: ClipDatabase) -> None:
    client = _make_client_mock(connected=False)
    queue = _make_queue(client, db)

    assert await queue.prune_empty_backup_folders([]) == 0
    client.find_folder_path.assert_not_awaited()


async def test_prune_handles_several_cameras_on_one_day(db: ClipDatabase) -> None:
    """Both camera folders are checked before the date folder above them, or
    the date folder still looks occupied by a sibling about to go."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    trashed = await queue.prune_empty_backup_folders(
        [
            _backed_up_clip("c1", "Driveway", "2026-06-05T12:00:00+00:00"),
            _backed_up_clip("c2", "Front Door", "2026-06-05T13:00:00+00:00"),
        ]
    )

    # Two camera folders plus the one date folder they share.
    assert trashed == 3
    paths = [c.args[0] for c in client.find_folder_path.await_args_list]
    assert paths == [
        ["2026-06-05", "Driveway"],
        ["2026-06-05", "Front Door"],
        ["2026-06-05"],
    ]


async def test_prune_names_a_camera_less_clip_the_same_way_the_upload_did(
    db: ClipDatabase,
) -> None:
    """_process_one falls back to "unknown" for a clip with no camera, so
    the cleanup has to look in the same place."""
    client = _make_client_mock()
    queue = _make_queue(client, db)

    await queue.prune_empty_backup_folders(
        [{"id": "c1", "camera": "", "timestamp": "2026-06-05T12:00:00+00:00"}]
    )

    assert client.find_folder_path.await_args_list[0].args[0] == [
        "2026-06-05",
        "unknown",
    ]


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
