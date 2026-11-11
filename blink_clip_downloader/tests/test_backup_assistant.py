"""Security Assistant chats and spend in a backup: carried, added, never doubled."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blink_downloader.backup import (
    BackupArchive,
    RestoreHooks,
    create_backup,
    restore_backup,
)
from blink_downloader.backup.catalogue import DATA_FILES
from blink_downloader.database import ClipDatabase
from tests.backup_helpers import insert, seed_library
from tests.conftest import _ALL_TABLES

_MESSAGES = [{"role": "user", "content": "Hi", "at": "2026-10-02T08:00:00+00:00"}]


async def _backup(db: ClipDatabase, folder: Path) -> BackupArchive:
    folder.mkdir(exist_ok=True)
    target = folder / "backup.zip"
    await create_backup(
        db, target, addon_version="6.2.0", clip_folder="/share/blink-clips"
    )
    return BackupArchive.open(target)


async def _wipe(db: ClipDatabase) -> None:
    assert db._pool is not None
    await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")


async def _restore(db: ClipDatabase, archive: BackupArchive):
    return await restore_backup(
        db, archive, replace_settings=False, hooks=RestoreHooks()
    )


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


def test_the_assistants_settings_file_is_carried() -> None:
    assert "assistant_settings.json" in {f.name for f in DATA_FILES}


async def test_chats_and_spend_are_in_the_archive(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    assert {"assistant_chats", "assistant_usage"} <= set(archive.manifest.tables)


async def test_a_restore_brings_chats_and_spend_back(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    before = await db.get_assistant_chat("chat1")
    spend = await db.get_token_usage_stats()
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    assert await db.list_assistant_chats() == []

    await _restore(db, archive)

    assert await db.get_assistant_chat("chat1") == before
    again = await db.get_token_usage_stats()
    assert again["assistant"] == spend["assistant"]
    assert again["assistant"]["questions"] == 1


async def test_restoring_twice_does_not_double_chats_or_spend(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)

    await _restore(db, archive)
    await _restore(db, archive)

    assert len(await db.list_assistant_chats()) == 1
    assert (await db.get_token_usage_stats())["assistant"]["questions"] == 1


async def test_a_chat_that_is_already_here_is_left_exactly_as_it_is(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    """The conversation here has moved on since the backup; the backup's copy
    of the same chat must not replace it, rename it or add its messages."""
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await db.rename_assistant_chat("chat1", "Renamed since")
    await db.append_assistant_messages("chat1", [{"role": "user", "content": "newer"}])
    here = await db.get_assistant_chat("chat1")

    await _restore(db, archive)

    assert await db.get_assistant_chat("chat1") == here


async def test_a_new_chat_from_the_backup_sits_beside_the_existing_ones(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await insert(
        db,
        "assistant_chats",
        id="local",
        title="Only here",
        created_at="2026-10-03T00:00:00+00:00",
        updated_at="2026-10-03T00:00:00+00:00",
        messages=json.dumps(_MESSAGES),
    )
    await db.delete_assistant_chat("chat1")

    await _restore(db, archive)

    assert {c["id"] for c in await db.list_assistant_chats()} == {"chat1", "local"}
