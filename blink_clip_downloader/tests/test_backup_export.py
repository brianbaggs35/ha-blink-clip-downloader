"""Tests for making a backup."""

from __future__ import annotations

import json
import logging
import tempfile
import zipfile
from pathlib import Path

import pytest

from blink_downloader.backup import catalogue, create_backup, export, preview_backup
from blink_downloader.backup.archive import MANIFEST_MEMBER, BackupArchive
from blink_downloader.database import BACKUP_TABLES, ClipDatabase
from tests.backup_helpers import seed_library


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


async def _made(db: ClipDatabase, tmp_path: Path, **options: bool) -> BackupArchive:
    target = tmp_path / "backup.zip"
    await create_backup(
        db, target, addon_version="6.1.4", clip_folder="/share/blink-clips", **options
    )
    return BackupArchive.open(target)


async def _stored(db: ClipDatabase, table: str) -> list[dict]:
    assert db._pool is not None
    spec = next(t for t in BACKUP_TABLES if t.name == table)
    rows = await db._pool.fetch(f"SELECT * FROM {table} ORDER BY {spec.order_by}")
    return [dict(row) for row in rows]


async def test_a_backup_reads_back_as_exactly_what_the_database_holds(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _made(db, tmp_path)
    assert set(archive.manifest.tables) == {t.name for t in BACKUP_TABLES}
    for table in BACKUP_TABLES:
        in_backup = [r for batch in archive.iter_rows(table.name) for r in batch]
        assert in_backup == await _stored(db, table.name), table.name


async def test_the_manifest_says_where_it_came_from(
    db: ClipDatabase, tmp_path: Path
) -> None:
    archive = await _made(db, tmp_path)
    assert archive.manifest.addon_version == "6.1.4"
    assert archive.manifest.clip_folder == "/share/blink-clips"
    assert archive.manifest.created_at.endswith("+00:00")


async def test_an_empty_library_still_makes_a_valid_backup(
    db: ClipDatabase, tmp_path: Path
) -> None:
    archive = await _made(db, tmp_path)
    assert all(m.rows == 0 for m in archive.manifest.tables.values())
    assert archive.manifest.files == {}


async def test_the_same_data_gives_the_same_bytes(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    (tmp_path / "again").mkdir()
    first = await _made(db, tmp_path)
    second = await _made(db, tmp_path / "again")
    assert {n: m.sha256 for n, m in first.manifest.tables.items()} == {
        n: m.sha256 for n, m in second.manifest.tables.items()
    }


async def test_enrolled_faces_can_be_left_out(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _made(db, tmp_path, include_faces=False)
    assert "face_enrollments" not in archive.manifest.tables
    # The rest of the face data is not biometric and stays.
    assert "face_recognition_feedback" in archive.manifest.tables
    assert "clips" in archive.manifest.tables


async def test_every_carried_file_goes_in_and_nothing_else(
    db: ClipDatabase, tmp_path: Path
) -> None:
    data = catalogue.DATA_DIR
    (data / "camera_configs.json").write_text('[{"camera": "Front Door"}]')
    (data / "clip_manifest.json").write_text('{"id": "c1"}\n{"id": "c2"}\n')
    (data / "downloaded_clips.json").write_text(
        json.dumps(
            {
                "downloaded_ids": ["c1", "c2"],
                "last_download_time": "2026-10-01T00:00:00+00:00",
                "stats": {"total_downloaded": 2},
            }
        )
    )
    (data / "auth_credentials.json").write_text('{"token": "secret"}')
    (data / "web_access.json").write_text('{"secret": "secret"}')
    pictures = data / "asset_snapshots"
    pictures.mkdir()
    (pictures / "front-door-0123456789.jpg").write_bytes(b"jpeg")
    (pictures / "notes.txt").write_text("not a picture")
    (pictures / "UPPER.jpg").write_bytes(b"not a camera slug")
    (pictures / "nested").mkdir()

    archive = await _made(db, tmp_path)

    assert set(archive.manifest.files) == {
        "camera_configs.json",
        "clip_manifest.json",
        "downloaded_clip_ids.json",
        "asset_snapshots/front-door-0123456789.jpg",
    }
    assert archive.read_file("camera_configs.json") == b'[{"camera": "Front Door"}]'
    # Only the ids of the download history: the bookmark and counters belong
    # to the install they were measured on.
    assert json.loads(archive.read_file("downloaded_clip_ids.json")) == {
        "downloaded_ids": ["c1", "c2"]
    }
    with zipfile.ZipFile(tmp_path / "backup.zip") as written:
        assert not [
            n
            for n in written.namelist()
            if "secret" in written.read(n).decode("latin-1")
        ]


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("{not json", id="unreadable"),
        pytest.param('["c1"]', id="not an object"),
        pytest.param('{"downloaded_ids": "c1"}', id="ids not a list"),
        pytest.param("{}", id="no ids"),
    ],
)
async def test_a_download_history_the_downloader_could_not_use_is_left_out(
    db: ClipDatabase, tmp_path: Path, contents: str, caplog: pytest.LogCaptureFixture
) -> None:
    (catalogue.DATA_DIR / "downloaded_clips.json").write_text(contents)
    with caplog.at_level(logging.WARNING):
        archive = await _made(db, tmp_path)
    assert "downloaded_clip_ids.json" not in archive.manifest.files


async def test_only_text_ids_are_kept_from_a_download_history(
    db: ClipDatabase, tmp_path: Path
) -> None:
    (catalogue.DATA_DIR / "downloaded_clips.json").write_text(
        '{"downloaded_ids": ["c1", 7, null, "c2"]}'
    )
    archive = await _made(db, tmp_path)
    assert json.loads(archive.read_file("downloaded_clip_ids.json")) == {
        "downloaded_ids": ["c1", "c2"]
    }


async def test_a_file_that_cannot_be_read_stops_the_backup(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup that succeeded without camera_configs.json would only be
    noticed when it was needed."""
    (catalogue.DATA_DIR / "camera_configs.json").write_text("[]")

    def refuse(*_args: object) -> None:
        raise PermissionError(13, "Permission denied", "camera_configs.json")

    monkeypatch.setattr(export.shutil, "copyfile", refuse)
    with pytest.raises(PermissionError):
        await create_backup(
            db, tmp_path / "b.zip", addon_version="6.1.4", clip_folder="/share"
        )


async def test_the_scratch_space_is_cleaned_up(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    real = export.tempfile.TemporaryDirectory

    def tracked(prefix: str) -> tempfile.TemporaryDirectory[str]:
        directory = real(prefix=prefix)
        seen.append(Path(directory.name))
        return directory

    monkeypatch.setattr(export.tempfile, "TemporaryDirectory", tracked)
    await _made(db, tmp_path)
    assert seen
    assert not any(path.exists() for path in seen)


async def test_the_preview_counts_what_a_backup_would_hold(
    db: ClipDatabase, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    (catalogue.DATA_DIR / "camera_configs.json").write_text("[]")
    pictures = catalogue.DATA_DIR / "vehicle_zone_snapshots"
    pictures.mkdir()
    (pictures / "driveway-0123456789.jpg").write_bytes(b"jpeg")
    (pictures / "junk.txt").write_text("x")

    preview = await preview_backup(db)

    tables = {t["name"]: t for t in preview["tables"]}
    assert tables["clips"]["rows"] == 3
    assert tables["analysis_results"]["rows"] == 3
    assert tables["face_enrollments"]["biometric"] is True
    assert tables["clips"]["biometric"] is False
    files = {f["label"]: f["items"] for f in preview["files"]}
    assert files["Camera settings (descriptions, prompts, car zones)"] == 1
    assert files["Vehicle zone reference pictures"] == 1
    assert files["Asset reference pictures"] == 0
    assert files["Protected assets"] == 0


async def test_a_backup_is_a_zip_with_the_manifest_first(
    db: ClipDatabase, tmp_path: Path
) -> None:
    await _made(db, tmp_path)
    with zipfile.ZipFile(tmp_path / "backup.zip") as written:
        assert written.namelist()[0] == MANIFEST_MEMBER
