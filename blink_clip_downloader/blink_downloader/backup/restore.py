"""Restoring a backup: add what is missing, change nothing that is here.

The rule behind every choice below is that a restore may be run on an
install that has not been empty since the backup — the add-on starts
downloading the moment it is set up, usually before anyone reaches this tab —
and may be run twice. So it only ever *adds*:

* A clip whose id is already here is left exactly as it is, with its
  analysis, and so is everything that belongs to it in the backup.
* A clip whose video file is already tracked under another record is skipped
  rather than doubled, unless that record is only the stand-in the library
  scan made from the file name and nothing has been added to it, in which
  case the real record replaces it.
* Everything else is matched on what identifies it (a camera, a person and
  their photo, a battery reading) and added only if no such row exists.
* The downloader's list of what it has fetched is unioned, and the clip
  manifest gains only the lines it lacks.
* A settings file is written only where there is none, unless replacing was
  asked for.

The preview is not a separate calculation. :func:`preview_restore` runs the
same merge as :func:`restore_backup`, in a read-only transaction, so what the
summary promises and what the restore does cannot drift apart. The database
half is a single transaction: it all happens or none of it does. Files are
written afterwards, one at a time, each read back and compared with the
archive, and one failing never stops the others. Nothing after the database
commit is allowed to raise, because a caller that sees an exception is
entitled to believe nothing changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..database import BACKUP_TABLES, ClipDatabase
from ..database.backup import (
    ANALYSIS_TABLE,
    FEEDBACK_TABLE,
    ROLE_CHILD,
    ROLE_ROOT,
    BackupTable,
    RestoreSession,
)
from ..library_scanner import IMPORTED_CLIP_ID_PREFIX, clip_media_present
from .archive import BackupArchive, BackupError, MemberInfo
from .catalogue import DATA_DIR, DATA_DIRS, DATA_FILES, DataDir, DataFile

_LOGGER = logging.getLogger(__name__)

# What happened to a file, for the summary beforehand and the report after.
_NEW = "new"
_EXISTS = "exists"
_MERGE = "merge"
_RESTORED = "restored"
_REPLACED = "replaced"
_KEPT = "kept"
_MERGED = "merged"
_SKIPPED = "skipped"
_FAILED = "failed"

_PLAIN_DETAIL = {
    _RESTORED: "Restored.",
    _REPLACED: "Replaced with the backup's copy.",
    _KEPT: "Already here; left as it is.",
}
_HISTORY_UNREADABLE = "The download history in the backup cannot be read."


@dataclass(frozen=True)
class RestoreHooks:
    """What a restore needs from the running add-on, passed in rather than
    imported so this package knows nothing of the downloader."""

    #: Adds clip ids to the downloader's record of what it has fetched and
    #: returns how many were new. ``None`` where there is no downloader.
    merge_downloaded_ids: Callable[[Iterable[str]], int] | None = None


@dataclass
class TableOutcome:
    """What the merge did with one table's records."""

    name: str
    label: str
    #: The record count the backup's manifest declares.
    rows: int
    #: Records actually read from the archive.
    seen: int = 0
    added: int = 0
    skipped: int = 0

    def to_json(self) -> dict[str, Any]:
        # What the page shows: ``seen`` is the merge's own bookkeeping.
        return {k: v for k, v in asdict(self).items() if k != "seen"}


@dataclass
class ClipBreakdown:
    """Why clips were or were not added, and whether their video is here."""

    already_here: int = 0
    same_file: int = 0
    standins_replaced: int = 0
    with_video: int = 0
    without_video: int = 0

    def to_json(self) -> dict[str, int]:
        return asdict(self)


@dataclass
class DatabaseOutcome:
    """The merge's whole result, for the summary and for the report."""

    tables: list[TableOutcome] = field(default_factory=list)
    clips: ClipBreakdown = field(default_factory=ClipBreakdown)
    #: Ids of the clips added (or, in a preview, that would be).
    added_clip_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class FileOutcome:
    """What happened (or would) to one file or folder of pictures."""

    label: str
    status: str
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Check:
    """One thing verified after a restore."""

    label: str
    ok: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RestoreResult:
    """Everything a restore reports back."""

    database: DatabaseOutcome
    files: list[FileOutcome]
    checks: list[Check]
    restart_needed: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "tables": [t.to_json() for t in self.database.tables],
            "clips": self.database.clips.to_json(),
            "files": [f.to_json() for f in self.files],
            "checks": [c.to_json() for c in self.checks],
            "verified": all(c.ok for c in self.checks),
            "restart_needed": self.restart_needed,
        }


# ---------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------


async def _batches(
    archive: BackupArchive, table: str
) -> AsyncIterator[list[dict[str, Any]]]:
    """*table*'s records from the archive, read off the event loop."""
    batches = archive.iter_rows(table)
    try:
        while (batch := await asyncio.to_thread(next, batches, None)) is not None:
            yield batch
    finally:
        batches.close()


class _Merge:
    """One pass over the archive's tables, adding what is missing.

    With ``apply`` False it asks every question and writes nothing.
    """

    def __init__(
        self, session: RestoreSession, archive: BackupArchive, *, apply: bool
    ) -> None:
        self._session = session
        self._archive = archive
        self._apply = apply
        self.outcome = DatabaseOutcome()
        # File paths claimed by clips added so far, so two clips in the backup
        # (or one in each of two batches) do not both take one video.
        self._claimed_paths: set[str] = set()
        # An analysis's id in the backup -> the id it was given here, which
        # the feedback written about it has to point at.
        self._analysis_ids: dict[Any, int] = {}

    async def run(self) -> DatabaseOutcome:
        for table in BACKUP_TABLES:
            member = self._archive.manifest.tables.get(table.name)
            if member is None:
                continue
            report = TableOutcome(table.name, table.label, member.rows or 0)
            self.outcome.tables.append(report)
            if table.role == ROLE_ROOT:
                await self._merge_clips(report)
            elif table.role == ROLE_CHILD:
                await self._merge_children(table, report)
            else:
                await self._merge_independent(table, report)
        return self.outcome

    # -- clips ---------------------------------------------------------------

    async def _merge_clips(self, report: TableOutcome) -> None:
        async for rows in _batches(self._archive, report.name):
            report.seen += len(rows)
            unseen = await self._unseen(rows)
            wanted, standins = await self._claim_paths(unseen)
            report.added += await self._store_clips(wanted, standins)
        report.skipped = self.outcome.clips.already_here + self.outcome.clips.same_file

    async def _unseen(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The rows whose id is not already a clip here or earlier in the backup."""
        here = await self._session.existing_clip_ids([row["id"] for row in rows])
        added = self.outcome.added_clip_ids
        earlier: set[str] = set()
        unseen = []
        for row in rows:
            clip_id = row["id"]
            if clip_id in here or clip_id in added or clip_id in earlier:
                self.outcome.clips.already_here += 1
            else:
                earlier.add(clip_id)
                unseen.append(row)
        return unseen

    async def _claim_paths(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Split *rows* into those to add and the stand-ins they replace.

        A row is skipped when another record already holds its file. The one
        exception is a record the library scan made from the file name that
        carries nothing since, which the real record replaces.
        """
        holders = await self._session.clips_at_paths(
            [row["file_path"] for row in rows], IMPORTED_CLIP_ID_PREFIX
        )
        wanted: list[dict[str, Any]] = []
        standins: list[str] = []
        for row in rows:
            path = row["file_path"]
            holder = holders.get(path)
            # An empty path is "no file recorded", which is not one file.
            if path and (
                path in self._claimed_paths or (holder and not holder.replaceable)
            ):
                self.outcome.clips.same_file += 1
                continue
            if holder:
                standins.extend(holder.ids)
            self._claimed_paths.add(path)
            wanted.append(row)
        return wanted, standins

    async def _store_clips(
        self, rows: list[dict[str, Any]], standins: list[str]
    ) -> int:
        """Add *rows*; returns how many were added."""
        flagged = await self._flag_missing_video(rows)
        if self._apply:
            if standins:
                await self._session.delete_clips(standins)
            added = await self._session.insert_new_clips(flagged)
        else:
            added = {row["id"] for row in flagged}
        clips = self.outcome.clips
        # A clip that turned up meanwhile (the downloader fetching it again)
        # is here, just not by our doing.
        clips.already_here += len(flagged) - len(added)
        clips.standins_replaced += len(standins)
        for row in flagged:
            if row["id"] in added:
                if row["media_missing"]:
                    clips.without_video += 1
                else:
                    clips.with_video += 1
        self.outcome.added_clip_ids |= added
        return len(added)

    @staticmethod
    async def _flag_missing_video(
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Mark each clip whose video (or archive) is not on this install.

        The archiver deletes a clip whose file has gone, so a restored clip
        without its video has to say so or the next archive run would erase
        the record that was just restored.
        """
        absent = await asyncio.to_thread(
            lambda: [not clip_media_present(row) for row in rows]
        )
        return [
            {**row, "media_missing": missing}
            for row, missing in zip(rows, absent, strict=True)
        ]

    # -- everything that belongs to a clip ----------------------------------

    async def _merge_children(self, table: BackupTable, report: TableOutcome) -> None:
        async for rows in _batches(self._archive, table.name):
            report.seen += len(rows)
            # Only with a clip added by this restore: the analysis of a clip
            # that was already here is its own, and is not doubled up.
            mine = [r for r in rows if r["clip_id"] in self.outcome.added_clip_ids]
            if self._apply:
                await self._store_children(table, mine)
            report.added += len(mine)
            report.skipped += len(rows) - len(mine)

    async def _store_children(
        self, table: BackupTable, rows: list[dict[str, Any]]
    ) -> None:
        if table.name == ANALYSIS_TABLE:
            new_ids = await self._session.insert_rows_returning_ids(table.name, rows)
            self._analysis_ids.update(
                zip((row["id"] for row in rows), new_ids, strict=True)
            )
        elif table.name == FEEDBACK_TABLE:
            await self._session.insert_rows(table.name, self._renumbered(rows))
        else:
            await self._session.insert_rows(table.name, rows)

    def _renumbered(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Feedback pointing at the id each analysis has now, or at none."""
        return [
            {
                **row,
                "analysis_result_id": self._analysis_ids.get(
                    row.get("analysis_result_id")
                ),
            }
            for row in rows
        ]

    # -- everything else -----------------------------------------------------

    async def _merge_independent(
        self, table: BackupTable, report: TableOutcome
    ) -> None:
        existing = await self._session.existing_keys(table.name)
        async for rows in _batches(self._archive, table.name):
            report.seen += len(rows)
            fresh = []
            for row in rows:
                key = tuple(row[column] for column in table.key)
                if key not in existing:
                    existing.add(key)
                    fresh.append(row)
            if self._apply:
                await self._session.insert_rows(table.name, fresh)
            report.added += len(fresh)
            report.skipped += len(rows) - len(fresh)


async def _merge(
    db: ClipDatabase, archive: BackupArchive, *, apply: bool
) -> DatabaseOutcome:
    try:
        async with db.restore_session(write=apply) as session:
            return await _Merge(session, archive, apply=apply).run()
    except (KeyError, TypeError, ValueError) as exc:
        # A record that is not what this version writes. The transaction has
        # been rolled back by the time this is caught.
        _LOGGER.warning("A record in the backup could not be used: %r", exc)
        raise BackupError("The backup holds a record this version cannot use.") from exc


async def preview_restore(db: ClipDatabase, archive: BackupArchive) -> DatabaseOutcome:
    """What restoring *archive* would add, without adding anything."""
    return await _merge(db, archive, apply=False)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _entry_members(archive: BackupArchive, entry: DataFile | DataDir) -> list[str]:
    """The archive-relative names of *entry*'s files that the backup holds."""
    names = archive.manifest.files
    if isinstance(entry, DataFile):
        return [entry.archive_name] if entry.archive_name in names else []
    return sorted(name for name in names if name.startswith(f"{entry.name}/"))


def preview_files(archive: BackupArchive) -> list[FileOutcome]:
    """What would happen to each file the backup holds, if restored now."""
    outcomes = []
    for entry in (*DATA_FILES, *DATA_DIRS):
        members = _entry_members(archive, entry)
        if members:
            outcomes.append(_preview_entry(entry, members))
    return outcomes


def _preview_entry(entry: DataFile | DataDir, members: list[str]) -> FileOutcome:
    if isinstance(entry, DataFile) and entry.role in ("event_log", "download_history"):
        return FileOutcome(entry.label, _MERGE, "Only what is missing is added.")
    present = sum((DATA_DIR / name).exists() for name in members)
    if isinstance(entry, DataDir):
        detail = f"{len(members)} in the backup, {present} already here"
        return FileOutcome(entry.label, _EXISTS if present else _NEW, detail)
    if present:
        return FileOutcome(
            entry.label, _EXISTS, "Already here; kept unless you choose to replace it."
        )
    return FileOutcome(entry.label, _NEW, "Not here yet; will be restored.")


def _write_checked(target: Path, data: bytes, expected: MemberInfo) -> None:
    """Write *data* to *target* atomically, then read it back and compare."""
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + ".restoring")
    try:
        scratch.write_bytes(data)
        os.replace(scratch, target)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected.sha256:
        raise OSError(f"{target.name} did not read back the way it was written")


def _restore_plain(archive: BackupArchive, name: str, replace: bool) -> str:
    """Restore one file or picture; returns what was done to it."""
    target = DATA_DIR / name
    existed = target.exists()
    if existed and not replace:
        return _KEPT
    _write_checked(target, archive.read_file(name), archive.manifest.files[name])
    return _REPLACED if existed else _RESTORED


def _merge_lines(archive: BackupArchive, name: str, target: Path) -> int:
    """Append the backup's lines that *target* does not already have."""
    try:
        current = target.read_bytes()
    except FileNotFoundError:
        current = b""
    have = set(current.splitlines())
    fresh: list[bytes] = []
    for line in archive.iter_lines(archive.manifest.files[name]):
        line = line.rstrip(b"\r\n")
        if line and line not in have:
            have.add(line)
            fresh.append(line)
    if fresh:
        # The file may end mid-line if the add-on was stopped while writing.
        lead = b"\n" if current and not current.endswith(b"\n") else b""
        with target.open("ab") as handle:
            handle.write(lead + b"\n".join(fresh) + b"\n")
    return len(fresh)


def _read_downloaded_ids(archive: BackupArchive, name: str) -> list[str]:
    """The clip ids in the backup's download history."""
    try:
        document = json.loads(archive.read_file(name))
    except ValueError as exc:
        raise BackupError(_HISTORY_UNREADABLE) from exc
    ids = document.get("downloaded_ids") if isinstance(document, dict) else None
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise BackupError(_HISTORY_UNREADABLE)
    return ids


async def _restore_history(
    archive: BackupArchive, entry: DataFile, name: str, hooks: RestoreHooks
) -> FileOutcome:
    if hooks.merge_downloaded_ids is None:
        return FileOutcome(
            entry.label,
            _SKIPPED,
            "The downloader is not running here, so its history cannot be updated.",
        )
    ids = await asyncio.to_thread(_read_downloaded_ids, archive, name)
    # On the event loop, not a worker thread: the downloader's tracker is
    # changed from this thread and is not safe to change from another.
    added = hooks.merge_downloaded_ids(ids)
    return FileOutcome(entry.label, _MERGED, f"{added:,} added")


async def _restore_pictures(
    archive: BackupArchive, directory: DataDir, names: list[str], replace: bool
) -> FileOutcome:
    done = await asyncio.to_thread(
        lambda: [_restore_plain(archive, name, replace) for name in names]
    )
    kept = done.count(_KEPT)
    detail = f"{len(done) - kept} restored, {kept} already here"
    return FileOutcome(
        directory.label, _KEPT if kept == len(done) else _RESTORED, detail
    )


async def _restore_entry(
    archive: BackupArchive,
    entry: DataFile | DataDir,
    names: list[str],
    replace: bool,
    hooks: RestoreHooks,
) -> tuple[FileOutcome, bool]:
    """Restore one catalogue entry; also whether a setting was written."""
    if isinstance(entry, DataDir):
        return await _restore_pictures(archive, entry, names, replace), False
    if entry.role == "event_log":
        added = await asyncio.to_thread(_merge_lines, archive, names[0], entry.path)
        return FileOutcome(entry.label, _MERGED, f"{added:,} added"), False
    if entry.role == "download_history":
        return await _restore_history(archive, entry, names[0], hooks), False
    status = await asyncio.to_thread(_restore_plain, archive, names[0], replace)
    changed = status != _KEPT and entry.role == "settings"
    return FileOutcome(entry.label, status, _PLAIN_DETAIL[status]), changed


async def restore_files(
    archive: BackupArchive, *, replace_settings: bool, hooks: RestoreHooks
) -> tuple[list[FileOutcome], bool]:
    """Restore each file the backup holds; also whether a restart is needed.

    A failure is recorded against the file it happened to and the rest go on.
    """
    outcomes: list[FileOutcome] = []
    restart = False
    for entry in (*DATA_FILES, *DATA_DIRS):
        names = _entry_members(archive, entry)
        if not names:
            continue
        try:
            outcome, changed = await _restore_entry(
                archive, entry, names, replace_settings, hooks
            )
        except Exception as exc:  # one file must not stop the rest
            _LOGGER.exception("Could not restore %s from the backup", entry.label)
            outcome = FileOutcome(entry.label, _FAILED, _reason(exc))
            changed = False
        outcomes.append(outcome)
        restart = restart or changed
    return outcomes, restart


def _reason(exc: Exception) -> str:
    if isinstance(exc, BackupError):
        return str(exc)
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return "Unexpected error; see the add-on logs."


# ---------------------------------------------------------------------------
# The whole restore
# ---------------------------------------------------------------------------


def _ledger_check(database: DatabaseOutcome) -> Check:
    """Every record the backup promised was read, and was added or set aside."""
    unbalanced = [
        t.label
        for t in database.tables
        if t.seen != t.rows or t.added + t.skipped != t.seen
    ]
    return Check(
        "Every record in the backup was restored or deliberately left alone",
        not unbalanced,
        f"Does not add up for: {', '.join(unbalanced)}" if unbalanced else "",
    )


async def _readback_check(db: ClipDatabase, database: DatabaseOutcome) -> Check:
    """The clips this restore added are in the library now, after the commit."""
    added = sorted(database.added_clip_ids)
    if not added:
        return Check("No new clips needed adding", True)
    label = (
        "The added clip is in the library"
        if len(added) == 1
        else f"All {len(added):,} added clips are in the library"
    )
    try:
        async with db.restore_session(write=False) as session:
            present = await session.existing_clip_ids(added)
    except Exception:  # nothing after the commit may raise
        _LOGGER.exception("Could not read the restored clips back")
        return Check(label, False, "Could not check; see the add-on logs.")
    missing = len(added) - len(present)
    return Check(label, not missing, f"{missing:,} are missing" if missing else "")


def _files_check(files: list[FileOutcome]) -> Check:
    failed = [f.label for f in files if f.status == _FAILED]
    return Check(
        "Files were written and read back correctly",
        not failed,
        f"Failed: {', '.join(failed)}" if failed else "",
    )


async def restore_backup(
    db: ClipDatabase,
    archive: BackupArchive,
    *,
    replace_settings: bool,
    hooks: RestoreHooks,
) -> RestoreResult:
    """Restore *archive* into this install.

    An exception means the database was left exactly as it was. Once the
    merge has committed, everything that can go wrong is reported in the
    result instead.
    """
    database = await _merge(db, archive, apply=True)
    files, restart = await restore_files(
        archive, replace_settings=replace_settings, hooks=hooks
    )
    checks = [
        _ledger_check(database),
        await _readback_check(db, database),
        _files_check(files),
    ]
    return RestoreResult(database, files, checks, restart)


def summarize(
    archive: BackupArchive, database: DatabaseOutcome, files: list[FileOutcome]
) -> dict[str, Any]:
    """The pre-restore summary: what is in the backup and what restoring does."""
    manifest = archive.manifest
    return {
        "created_at": manifest.created_at,
        "addon_version": manifest.addon_version,
        "clip_folder": manifest.clip_folder,
        "verified_parts": len(manifest.members()),
        "tables": [t.to_json() for t in database.tables],
        "clips": database.clips.to_json(),
        "files": [f.to_json() for f in files],
    }
