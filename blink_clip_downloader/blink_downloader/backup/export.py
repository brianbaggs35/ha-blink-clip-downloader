"""Making a backup: the library as of one instant, and the add-on's files.

The database is read through one read-only snapshot so every table is from
the same moment while the add-on keeps writing, staged to a scratch
directory a table at a time (the records are never all in memory), and then
zipped. Staging first matters for a second reason: each member's checksum is
taken from the exact bytes that go into the ZIP, so a settings file changing
between being measured and being stored cannot make a backup that fails its
own verification.

Nothing is silently left out. A file that exists but cannot be read stops
the backup with the reason — a backup that "succeeded" without
``camera_configs.json`` would only be discovered at the worst moment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..database import BACKUP_TABLES, ClipDatabase
from .archive import (
    FILES_DIR,
    BackupManifest,
    MemberInfo,
    RecordWriter,
    describe_file,
    file_member,
    table_member,
    write_archive,
)
from .catalogue import (
    DATA_DIRS,
    DATA_FILES,
    PICTURE_NAME,
    DataDir,
    DataFile,
    picture_member,
)

_LOGGER = logging.getLogger(__name__)

#: A staged member: what the manifest will say about it, and where it is now.
Staged = tuple[MemberInfo, Path]


async def create_backup(
    db: ClipDatabase,
    zip_path: Path,
    *,
    addon_version: str,
    clip_folder: str,
    include_faces: bool = True,
) -> BackupManifest:
    """Write a backup of the library and the add-on's files to *zip_path*.

    *include_faces* False leaves the enrolled faces out (see
    :attr:`~blink_downloader.database.backup.BackupTable.biometric`).
    """
    with tempfile.TemporaryDirectory(prefix="blink-backup-") as scratch:
        stage = Path(scratch)
        tables = await _stage_tables(db, stage, include_faces)
        files = await asyncio.to_thread(_stage_files, stage)
        manifest = BackupManifest(
            addon_version=addon_version,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            clip_folder=clip_folder,
            tables={name: info for name, (info, _) in tables.items()},
            files={name: info for name, (info, _) in files.items()},
        )
        sources = {
            info.name: path for info, path in (*tables.values(), *files.values())
        }
        await asyncio.to_thread(write_archive, zip_path, manifest, sources)
    return manifest


async def _stage_tables(
    db: ClipDatabase, stage: Path, include_faces: bool
) -> dict[str, Staged]:
    staged: dict[str, Staged] = {}
    async with db.backup_snapshot() as snapshot:
        for table in BACKUP_TABLES:
            if table.biometric and not include_faces:
                continue
            path = stage / f"{table.name}.jsonl"
            with closing(RecordWriter(path, table_member(table.name))) as writer:
                async for batch in snapshot.batches(table.name):
                    await asyncio.to_thread(writer.write, batch)
                staged[table.name] = (writer.info(), path)
    return staged


def _stage_files(stage: Path) -> dict[str, Staged]:
    """Copy every catalogued file that exists into *stage*."""
    staged: dict[str, Staged] = {}
    for entry in DATA_FILES:
        found = _stage_file(stage, entry)
        if found is not None:
            staged[entry.archive_name] = found
    for directory in DATA_DIRS:
        for picture in _pictures(directory):
            name = picture_member(directory, picture.name)
            staged[name] = _copy_into(stage, name, picture)
    return staged


def _stage_file(stage: Path, entry: DataFile) -> Staged | None:
    if entry.role == "download_history":
        ids = _downloaded_ids(entry.path)
        if ids is None:
            return None
        return _write_into(
            stage, entry.archive_name, json.dumps({"downloaded_ids": ids})
        )
    if not entry.path.is_file():
        return None
    return _copy_into(stage, entry.archive_name, entry.path)


def _copy_into(stage: Path, name: str, source: Path) -> Staged:
    target = stage / FILES_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return describe_file(target, file_member(name)), target


def _write_into(stage: Path, name: str, text: str) -> Staged:
    target = stage / FILES_DIR / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return describe_file(target, file_member(name)), target


def _downloaded_ids(path: Path) -> list[str] | None:
    """The clip ids the downloader has fetched, or ``None`` if it has no history.

    Only the ids are kept: the rest of that file is a bookmark and counters
    that belong to the install they were measured on.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        # The downloader itself starts fresh from a file it cannot parse.
        _LOGGER.warning(
            "Skipping the download history in the backup: %s is unreadable", path
        )
        return None
    ids = document.get("downloaded_ids") if isinstance(document, dict) else None
    if not isinstance(ids, list):
        return None
    return [clip_id for clip_id in ids if isinstance(clip_id, str)]


def _pictures(directory: DataDir) -> list[Path]:
    try:
        entries = sorted(directory.path.iterdir())
    except FileNotFoundError:
        return []
    return [p for p in entries if p.is_file() and PICTURE_NAME.fullmatch(p.name)]


async def preview_backup(db: ClipDatabase) -> dict[str, Any]:
    """What a backup made now would hold, without making one."""
    counts = await db.backup_row_counts()
    return {
        "tables": [
            {
                "name": table.name,
                "label": table.label,
                "rows": counts[table.name],
                "biometric": table.biometric,
            }
            for table in BACKUP_TABLES
        ],
        "files": await asyncio.to_thread(_describe_files),
    }


def _describe_files() -> list[dict[str, Any]]:
    """Each catalogued file or picture folder, and how many items it holds now."""
    described = [_item(entry.label, int(entry.path.is_file())) for entry in DATA_FILES]
    described.extend(
        _item(directory.label, len(_pictures(directory))) for directory in DATA_DIRS
    )
    return described


def _item(label: str, items: int) -> dict[str, Any]:
    return {"label": label, "items": items}
