"""The Backup & Restore tab's API (media_server/backup.py), over real HTTP."""

from __future__ import annotations

import asyncio
import re
import zipfile
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader import __version__
from blink_downloader.backup import BackupArchive, catalogue, create_backup
from blink_downloader.backup import staging as staging_module
from blink_downloader.database import ClipDatabase
from blink_downloader.gdrive_client import GDriveClient
from blink_downloader.media_server import MediaServer
from blink_downloader.media_server import backup as routes
from tests.backup_helpers import seed_library
from tests.conftest import _ALL_TABLES


@dataclass
class _Api:
    client: TestClient
    server: MediaServer
    db: ClipDatabase
    media: Path


@pytest.fixture
async def make_api(
    db: ClipDatabase, tmp_path: Path
) -> AsyncGenerator[Callable[..., Awaitable[_Api]]]:
    clients: list[TestClient] = []
    media = tmp_path / "media"
    media.mkdir()

    async def build(**kwargs: Any) -> _Api:
        server = MediaServer(db=db, port=0, clip_storage_dir=media, **kwargs)
        client = TestClient(TestServer(server._build_app()))
        await client.start_server()
        clients.append(client)
        return _Api(client, server, db, media)

    yield build
    for client in clients:
        await client.close()


@pytest.fixture
async def api(make_api: Callable[..., Awaitable[_Api]]) -> _Api:
    return await make_api()


def _drive(**overrides: Any) -> MagicMock:
    client = MagicMock(spec=GDriveClient)
    client.connected = True
    client.folder_id = "root-folder"
    client.folder_name = "Blink Clips"
    client.quota_exceeded = False
    client.rate_limited = False
    client.get_or_create_folder_path = AsyncMock(return_value="backups-folder")
    client.upload_file = AsyncMock(return_value="drive-file-1")
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


async def _backup_bytes(api: _Api, tmp_path: Path, **options: bool) -> bytes:
    target = tmp_path / "made.zip"
    await create_backup(
        api.db, target, addon_version="6.1.3", clip_folder="/share/x", **options
    )
    return target.read_bytes()


async def _eventually(condition: Callable[[], bool]) -> None:
    """Wait for something the server finishes just after the client has its
    answer, such as removing the file it sent."""
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never became true")


async def _wipe(db: ClipDatabase) -> None:
    assert db._pool is not None
    await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")


async def _send(api: _Api, data: bytes, piece: int = 4000) -> str:
    """Upload *data* the way the page does, in order, a piece at a time."""
    started = await api.client.post("/api/backup/uploads", json={"size": len(data)})
    upload_id = (await started.json())["upload_id"]
    for offset in range(0, len(data), piece):
        sent = await api.client.put(
            f"/api/backup/uploads/{upload_id}",
            params={"offset": offset},
            data=data[offset : offset + piece],
        )
        assert sent.status == 200
    return upload_id


# ---------------------------------------------------------------------------
# What a backup would hold
# ---------------------------------------------------------------------------


async def test_the_preview_says_what_a_backup_would_hold(
    api: _Api,
) -> None:
    await seed_library(api.db, api.media)
    (catalogue.DATA_DIR / "camera_configs.json").write_text("[]")
    body = await (await api.client.get("/api/backup/preview")).json()
    tables = {t["name"]: t for t in body["tables"]}
    assert tables["clips"]["rows"] == 3
    assert tables["face_enrollments"]["biometric"] is True
    assert body["clip_folder"] == str(api.media)
    assert {f["label"]: f["items"] for f in body["files"]}["Protected assets"] == 0


@pytest.mark.parametrize(
    ("client", "state", "folder"),
    [
        pytest.param(None, "unavailable", "", id="no drive support"),
        pytest.param(_drive(connected=False), "not_connected", "", id="not connected"),
        pytest.param(_drive(folder_id=""), "no_folder", "", id="no folder chosen"),
        pytest.param(_drive(), "ready", "Blink Clips/Backups", id="ready"),
    ],
)
async def test_the_preview_says_whether_drive_can_take_it(
    make_api: Callable[..., Awaitable[_Api]],
    client: MagicMock | None,
    state: str,
    folder: str,
) -> None:
    api = await make_api(gdrive_client=client)
    body = await (await api.client.get("/api/backup/preview")).json()
    assert body["gdrive"] == {"state": state, "folder": folder}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def test_a_backup_downloads_as_a_zip_that_opens(
    api: _Api, tmp_path: Path
) -> None:
    await seed_library(api.db, api.media)
    resp = await api.client.post("/api/backup/create")
    assert resp.status == 200
    assert resp.content_type == "application/zip"
    assert re.fullmatch(
        r'attachment; filename="blink-clips-backup-\d{8}-\d{6}\.zip"',
        resp.headers["Content-Disposition"],
    )
    data = await resp.read()
    assert int(resp.headers["Content-Length"]) == len(data)
    saved = tmp_path / "downloaded.zip"
    saved.write_bytes(data)
    archive = BackupArchive.open(saved)
    assert archive.manifest.addon_version == __version__
    assert archive.manifest.clip_folder == str(api.media)
    assert archive.manifest.tables["clips"].rows == 3
    assert "face_enrollments" in archive.manifest.tables


async def test_a_download_can_leave_the_faces_out(api: _Api, tmp_path: Path) -> None:
    await seed_library(api.db, api.media)
    resp = await api.client.post("/api/backup/create", json={"include_faces": False})
    saved = tmp_path / "downloaded.zip"
    saved.write_bytes(await resp.read())
    assert "face_enrollments" not in BackupArchive.open(saved).manifest.tables


@pytest.mark.parametrize("value", ["no", 0, None, [True]])
async def test_include_faces_has_to_be_a_real_true_or_false(
    api: _Api, value: Any
) -> None:
    resp = await api.client.post("/api/backup/create", json={"include_faces": value})
    assert resp.status == 400
    assert "include_faces" in await resp.text()


async def test_the_scratch_file_is_removed_after_a_download(
    api: _Api, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scratch = tmp_path / "scratch.zip"
    scratch.write_bytes(b"")
    monkeypatch.setattr(routes, "_scratch_zip", lambda: scratch)
    resp = await api.client.post("/api/backup/create")
    assert resp.status == 200
    await resp.read()
    await _eventually(lambda: not scratch.exists())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            OSError(28, "No space left on device"),
            "Creating the backup failed: No space left on device.",
            id="the disk is full",
        ),
        pytest.param(
            PermissionError(13, "Permission denied", "/data/camera_configs.json"),
            "Creating the backup failed: Permission denied (camera_configs.json).",
            id="a file cannot be read",
        ),
        pytest.param(
            RuntimeError("secret internals"),
            "Creating the backup failed. The add-on logs have the reason.",
            id="anything else",
        ),
    ],
)
async def test_a_backup_that_cannot_be_made_says_why_and_cleans_up(
    api: _Api,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: Exception,
    expected: str,
) -> None:
    scratch = tmp_path / "scratch.zip"
    scratch.write_bytes(b"")
    monkeypatch.setattr(routes, "_scratch_zip", lambda: scratch)
    monkeypatch.setattr(routes, "create_backup", AsyncMock(side_effect=error))

    resp = await api.client.post("/api/backup/create")

    assert resp.status == 500
    assert await resp.text() == expected
    assert not scratch.exists()


async def test_a_second_backup_while_one_runs_is_refused(api: _Api) -> None:
    async with api.server._backup_lock:
        for path in ("/api/backup/create", "/api/backup/gdrive"):
            resp = await api.client.post(path)
            assert resp.status in (409, 503)
    resp = await api.client.post("/api/backup/create")
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


async def test_a_backup_goes_into_the_backups_folder_in_drive(
    make_api: Callable[..., Awaitable[_Api]], tmp_path: Path
) -> None:
    drive = _drive()
    seen: dict[str, Any] = {}

    def take(path: Path, name: str, **kwargs: Any) -> str:
        # The zip has to be complete and valid at the moment Drive is given it.
        seen["archive"] = BackupArchive.open(path)
        seen["name"], seen["kwargs"] = name, kwargs
        return "drive-file-1"

    drive.upload_file = AsyncMock(side_effect=take)
    api = await make_api(gdrive_client=drive)
    await seed_library(api.db, api.media)

    resp = await api.client.post("/api/backup/gdrive")

    body = await resp.json()
    assert resp.status == 200
    assert body["file_id"] == "drive-file-1"
    assert body["folder"] == "Blink Clips/Backups"
    assert body["size_bytes"] > 0
    assert re.fullmatch(r"blink-clips-backup-\d{8}-\d{6}\.zip", body["name"])
    assert seen["name"] == body["name"]
    assert seen["kwargs"] == {
        "content_type": "application/zip",
        "folder_id": "backups-folder",
    }
    assert seen["archive"].manifest.tables["clips"].rows == 3
    drive.get_or_create_folder_path.assert_awaited_once_with(["Backups"])


async def test_a_drive_backup_can_leave_the_faces_out(
    make_api: Callable[..., Awaitable[_Api]],
) -> None:
    drive = _drive()
    tables: list[set[str]] = []

    def take(path: Path, _name: str, **_kwargs: Any) -> str:
        tables.append(set(BackupArchive.open(path).manifest.tables))
        return "f"

    drive.upload_file = AsyncMock(side_effect=take)
    api = await make_api(gdrive_client=drive)
    await seed_library(api.db, api.media)
    await api.client.post("/api/backup/gdrive", json={"include_faces": False})
    assert "face_enrollments" not in tables[0]


@pytest.mark.parametrize(
    ("client", "status", "message"),
    [
        pytest.param(None, 503, "not available", id="no drive support"),
        pytest.param(_drive(connected=False), 409, "not connected", id="not connected"),
        pytest.param(
            _drive(folder_id=""), 409, "No Google Drive folder", id="no folder"
        ),
    ],
)
async def test_drive_is_refused_up_front_when_it_cannot_be_used(
    make_api: Callable[..., Awaitable[_Api]],
    client: MagicMock | None,
    status: int,
    message: str,
) -> None:
    api = await make_api(gdrive_client=client)
    resp = await api.client.post("/api/backup/gdrive")
    assert resp.status == status
    assert message in await resp.text()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"quota_exceeded": True}, "out of space", id="drive is full"),
        pytest.param({"rate_limited": True}, "limiting requests", id="rate limited"),
        pytest.param({}, "did not accept", id="refused without saying why"),
    ],
)
async def test_drive_not_taking_the_backup_says_why(
    make_api: Callable[..., Awaitable[_Api]],
    overrides: dict[str, Any],
    message: str,
) -> None:
    drive = _drive(upload_file=AsyncMock(return_value=None), **overrides)
    api = await make_api(gdrive_client=drive)
    resp = await api.client.post("/api/backup/gdrive")
    assert resp.status == 502
    assert message in await resp.text()


async def test_a_backups_folder_that_cannot_be_opened_is_a_clear_failure(
    make_api: Callable[..., Awaitable[_Api]],
) -> None:
    drive = _drive(get_or_create_folder_path=AsyncMock(return_value=None))
    api = await make_api(gdrive_client=drive)
    resp = await api.client.post("/api/backup/gdrive")
    assert resp.status == 502
    assert "Backups folder" in await resp.text()
    drive.upload_file.assert_not_awaited()


async def test_a_drive_client_that_blows_up_is_a_failure_not_a_crash(
    make_api: Callable[..., Awaitable[_Api]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "scratch.zip"
    scratch.write_bytes(b"")
    monkeypatch.setattr(routes, "_scratch_zip", lambda: scratch)
    drive = _drive(upload_file=AsyncMock(side_effect=OSError(5, "Input/output error")))
    api = await make_api(gdrive_client=drive)
    resp = await api.client.post("/api/backup/gdrive")
    assert resp.status == 500
    assert "Input/output error" in await resp.text()
    assert not scratch.exists()


async def test_a_backup_that_cannot_be_made_is_not_sent_to_drive(
    make_api: Callable[..., Awaitable[_Api]], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = _drive()
    api = await make_api(gdrive_client=drive)
    monkeypatch.setattr(
        routes, "create_backup", AsyncMock(side_effect=RuntimeError("x"))
    )
    resp = await api.client.post("/api/backup/gdrive")
    assert resp.status == 500
    drive.upload_file.assert_not_awaited()


# ---------------------------------------------------------------------------
# Restore: the upload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", ["10", 1.5, True, None])
async def test_an_upload_has_to_say_how_big_it_is_in_whole_bytes(
    api: _Api, size: Any
) -> None:
    resp = await api.client.post("/api/backup/uploads", json={"size": size})
    assert resp.status == 400


@pytest.mark.parametrize(
    ("size", "status"), [(0, 400), (staging_module.MAX_UPLOAD_BYTES + 1, 413)]
)
async def test_an_upload_no_backup_could_be_is_refused_with_a_reason(
    api: _Api, size: int, status: int
) -> None:
    resp = await api.client.post("/api/backup/uploads", json={"size": size})
    assert resp.status == status
    assert (await resp.json())["error"]


async def test_an_upload_opens_and_says_how_to_send_the_pieces(api: _Api) -> None:
    resp = await api.client.post("/api/backup/uploads", json={"size": 100})
    body = await resp.json()
    assert resp.status == 200
    assert body["upload_id"]
    assert body["chunk_bytes"] == staging_module.CHUNK_BYTES


@pytest.mark.parametrize("params", [{}, {"offset": "x"}, {"offset": "1.5"}])
async def test_a_piece_has_to_say_where_it_starts(
    api: _Api, params: dict[str, str]
) -> None:
    started = await api.client.post("/api/backup/uploads", json={"size": 10})
    upload_id = (await started.json())["upload_id"]
    resp = await api.client.put(
        f"/api/backup/uploads/{upload_id}", params=params, data=b"abc"
    )
    assert resp.status == 400
    assert "offset" in await resp.text()


async def test_a_piece_out_of_order_is_told_where_to_carry_on(api: _Api) -> None:
    started = await api.client.post("/api/backup/uploads", json={"size": 10})
    upload_id = (await started.json())["upload_id"]
    url = f"/api/backup/uploads/{upload_id}"
    assert (
        await api.client.put(url, params={"offset": 0}, data=b"abcde")
    ).status == 200

    again = await api.client.put(url, params={"offset": 0}, data=b"abcde")

    assert again.status == 409
    body = await again.json()
    assert body["received"] == 5
    assert body["error"]


async def test_a_piece_for_an_upload_that_is_gone_is_not_found(api: _Api) -> None:
    resp = await api.client.put(
        "/api/backup/uploads/nope", params={"offset": 0}, data=b"abc"
    )
    assert resp.status == 404
    assert "no longer available" in (await resp.json())["error"]


async def test_a_piece_larger_than_a_piece_may_be_is_refused(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staging_module, "MAX_CHUNK_BYTES", 4)
    started = await api.client.post("/api/backup/uploads", json={"size": 100})
    upload_id = (await started.json())["upload_id"]
    resp = await api.client.put(
        f"/api/backup/uploads/{upload_id}", params={"offset": 0}, data=b"x" * 5
    )
    assert resp.status == 413


async def test_starting_an_upload_while_a_restore_runs_is_refused(api: _Api) -> None:
    async with api.server._backup_lock:
        resp = await api.client.post("/api/backup/uploads", json={"size": 10})
    assert resp.status == 409


# ---------------------------------------------------------------------------
# Restore: look it over, then do it
# ---------------------------------------------------------------------------


async def test_a_backup_is_looked_over_and_then_restored(
    api: _Api, tmp_path: Path
) -> None:
    await seed_library(api.db, api.media)
    (catalogue.DATA_DIR / "camera_configs.json").write_text(
        '[{"camera": "Front Door"}]'
    )
    data = await _backup_bytes(api, tmp_path)
    await _wipe(api.db)
    (catalogue.DATA_DIR / "camera_configs.json").unlink()
    upload_id = await _send(api, data)

    looked = await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    summary = await looked.json()

    assert looked.status == 200
    assert summary["upload_id"] == upload_id
    assert summary["addon_version"] == "6.1.3"
    assert summary["verified_parts"] > 0
    clips = next(t for t in summary["tables"] if t["name"] == "clips")
    assert (clips["rows"], clips["added"], clips["skipped"]) == (3, 3, 0)
    assert summary["clips"]["without_video"] == 3
    config = next(
        f for f in summary["files"] if f["label"].startswith("Camera settings")
    )
    assert config["status"] == "new"
    # Looking is not doing.
    assert (await api.db.backup_row_counts())["clips"] == 0
    assert not (catalogue.DATA_DIR / "camera_configs.json").exists()

    done = await api.client.post(f"/api/backup/uploads/{upload_id}/restore")
    report = await done.json()

    assert done.status == 200
    assert report["verified"] is True
    assert report["restart_needed"] is True
    restored = next(t for t in report["tables"] if t["name"] == "clips")
    assert restored["added"] == clips["added"]  # what was promised is what happened
    assert (await api.db.backup_row_counts())["clips"] == 3
    assert (catalogue.DATA_DIR / "camera_configs.json").exists()
    # And the upload is spent: a second press cannot restore it again.
    assert (
        await api.client.post(f"/api/backup/uploads/{upload_id}/restore")
    ).status == 404


async def test_restoring_something_nobody_has_looked_over_is_refused(
    api: _Api, tmp_path: Path
) -> None:
    await seed_library(api.db, api.media)
    upload_id = await _send(api, await _backup_bytes(api, tmp_path))
    await _wipe(api.db)

    resp = await api.client.post(f"/api/backup/uploads/{upload_id}/restore")

    assert resp.status == 409
    assert "Look over the summary" in await resp.text()
    assert (await api.db.backup_row_counts())["clips"] == 0


async def test_settings_are_replaced_only_when_asked(api: _Api, tmp_path: Path) -> None:
    target = catalogue.DATA_DIR / "camera_configs.json"
    target.write_text("from the backup")
    data = await _backup_bytes(api, tmp_path)
    target.write_text("mine")

    first = await _send(api, data)
    await api.client.post(f"/api/backup/uploads/{first}/inspect")
    await api.client.post(f"/api/backup/uploads/{first}/restore", json={})
    assert target.read_text() == "mine"

    second = await _send(api, data)
    await api.client.post(f"/api/backup/uploads/{second}/inspect")
    await api.client.post(
        f"/api/backup/uploads/{second}/restore", json={"replace_settings": True}
    )
    assert target.read_text() == "from the backup"


@pytest.mark.parametrize("value", ["yes", 1, None])
async def test_replace_settings_has_to_be_a_real_true_or_false(
    api: _Api, tmp_path: Path, value: Any
) -> None:
    upload_id = await _send(api, await _backup_bytes(api, tmp_path))
    await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    resp = await api.client.post(
        f"/api/backup/uploads/{upload_id}/restore", json={"replace_settings": value}
    )
    assert resp.status == 400


async def test_the_download_history_reaches_the_running_downloader(
    make_api: Callable[..., Awaitable[_Api]], tmp_path: Path
) -> None:
    received: list[list[str]] = []

    def merge(ids: Any) -> int:
        received.append(list(ids))
        return len(received[-1])

    api = await make_api(merge_downloaded_clip_ids=merge)
    (catalogue.DATA_DIR / "downloaded_clips.json").write_text(
        '{"downloaded_ids": ["c1", "c2"]}'
    )
    data = await _backup_bytes(api, tmp_path)
    upload_id = await _send(api, data)
    await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")

    report = await (
        await api.client.post(f"/api/backup/uploads/{upload_id}/restore")
    ).json()

    assert received == [["c1", "c2"]]
    history = next(
        f for f in report["files"] if f["label"].startswith("Download history")
    )
    assert (history["status"], history["detail"]) == ("merged", "2 added")


async def test_a_file_that_is_not_a_backup_is_refused_and_thrown_away(
    api: _Api,
) -> None:
    upload_id = await _send(api, b"this is certainly not a zip file")
    resp = await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    assert resp.status == 422
    assert "not a readable backup" in await resp.text()
    # It is no use to anyone, so it is not left taking space.
    assert (
        await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    ).status == 404


async def test_a_backup_that_was_tampered_with_is_refused(
    api: _Api, tmp_path: Path
) -> None:
    await seed_library(api.db, api.media)
    made = tmp_path / "made.zip"
    await create_backup(api.db, made, addon_version="6.1.3", clip_folder="/share")
    with zipfile.ZipFile(made) as source:
        members = {n: source.read(n) for n in source.namelist()}
    members["database/clips.jsonl"] = members["database/clips.jsonl"].replace(
        b"c1", b"cX"
    )
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(forged, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)

    upload_id = await _send(api, forged.read_bytes())
    resp = await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")

    assert resp.status == 422
    assert "damaged" in await resp.text()


async def test_looking_over_an_upload_that_has_not_all_arrived_is_refused(
    api: _Api,
) -> None:
    started = await api.client.post("/api/backup/uploads", json={"size": 100})
    upload_id = (await started.json())["upload_id"]
    await api.client.put(
        f"/api/backup/uploads/{upload_id}", params={"offset": 0}, data=b"x" * 10
    )
    resp = await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    assert resp.status == 409
    assert "not finished" in await resp.text()


async def test_an_unexpected_failure_while_looking_keeps_the_upload_for_a_retry(
    api: _Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = await _send(api, await _backup_bytes(api, tmp_path))
    monkeypatch.setattr(
        routes, "preview_restore", AsyncMock(side_effect=RuntimeError("x"))
    )

    failed = await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    assert failed.status == 500
    assert "add-on logs" in await failed.text()

    monkeypatch.undo()
    assert (
        await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    ).status == 200


async def test_a_restore_that_fails_says_nothing_was_changed(
    api: _Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_library(api.db, api.media)
    upload_id = await _send(api, await _backup_bytes(api, tmp_path))
    await _wipe(api.db)
    await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    monkeypatch.setattr(
        routes, "restore_backup", AsyncMock(side_effect=ConnectionError("gone"))
    )

    resp = await api.client.post(f"/api/backup/uploads/{upload_id}/restore")

    assert resp.status == 500
    assert (await resp.text()).endswith("Nothing was changed.")
    # Still there to try again once the cause is dealt with.
    monkeypatch.undo()
    assert (
        await api.client.post(f"/api/backup/uploads/{upload_id}/restore")
    ).status == 200


async def test_a_backup_the_merge_cannot_use_says_nothing_was_changed(
    api: _Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blink_downloader.backup import BackupError

    upload_id = await _send(api, await _backup_bytes(api, tmp_path))
    await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    monkeypatch.setattr(
        routes,
        "restore_backup",
        AsyncMock(
            side_effect=BackupError("The backup holds a record that cannot be read.")
        ),
    )
    resp = await api.client.post(f"/api/backup/uploads/{upload_id}/restore")
    assert resp.status == 422
    assert await resp.text() == (
        "The backup holds a record that cannot be read. Nothing was changed."
    )


async def test_a_second_restore_while_one_runs_is_refused(
    api: _Api, tmp_path: Path
) -> None:
    upload_id = await _send(api, await _backup_bytes(api, tmp_path))
    await api.client.post(f"/api/backup/uploads/{upload_id}/inspect")
    async with api.server._backup_lock:
        for path in ("inspect", "restore"):
            resp = await api.client.post(f"/api/backup/uploads/{upload_id}/{path}")
            assert resp.status == 409
        assert (
            await api.client.delete(f"/api/backup/uploads/{upload_id}")
        ).status == 409


async def test_an_upload_can_be_thrown_away(api: _Api) -> None:
    started = await api.client.post("/api/backup/uploads", json={"size": 10})
    upload_id = (await started.json())["upload_id"]
    path = api.server._restore_staging.get(upload_id).path

    resp = await api.client.delete(f"/api/backup/uploads/{upload_id}")

    assert (resp.status, await resp.json()) == (200, {"discarded": True})
    assert not path.exists()
    assert (await api.client.delete(f"/api/backup/uploads/{upload_id}")).status == 404


async def test_stopping_the_server_throws_away_an_upload_it_still_holds(
    api: _Api,
) -> None:
    started = await api.client.post("/api/backup/uploads", json={"size": 10})
    upload_id = (await started.json())["upload_id"]
    path = api.server._restore_staging.get(upload_id).path
    await api.server.stop()
    assert not path.exists()
