"""ZIP archiver — compresses old clips into dated archives to reclaim space."""

from __future__ import annotations

import logging
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)


class ClipArchiver:
    """Groups clips by calendar month and compresses them into ZIP archives.

    Archived clips are removed from the download folder; their metadata stays in
    the database with ``archived=1`` and the path to the ZIP.
    """

    def __init__(
        self,
        db: ClipDatabase,
        archive_dir: Path,
        archive_after_days: int,
        enabled: bool,
    ) -> None:
        self._db = db
        self._archive_dir = archive_dir
        self._archive_after = archive_after_days
        self._enabled = enabled

    async def run(self) -> list[dict[str, Any]]:
        """Archive clips older than *archive_after_days*. Returns the archived clips."""
        if not self._enabled:
            return []

        clips = await self._db.get_clips_to_archive(self._archive_after)
        if not clips:
            return []

        _LOGGER.info(
            "Archiving %d clip(s) older than %d days", len(clips), self._archive_after
        )
        self._archive_dir.mkdir(parents=True, exist_ok=True)

        # Group by "YYYY-MM" for one ZIP per month.
        by_month: dict[str, list[dict]] = defaultdict(list)
        for clip in clips:
            month = str(clip.get("timestamp", ""))[:7] or "unknown"
            by_month[month].append(clip)

        archived: list[dict[str, Any]] = []
        for month, month_clips in by_month.items():
            archived.extend(await self._archive_month(month, month_clips))

        _LOGGER.info("Archive run complete: %d file(s) archived", len(archived))
        return archived

    async def prune_orphaned_archives(self) -> int:
        """Remove clip rows left orphaned by a since-fixed archiving bug.

        Before this fix, a clip whose source file was already gone by the
        time ``_archive_month`` reached it got marked ``archived=True``
        with an ``archive_path`` pointing at a ZIP that was never actually
        created (see the comment in ``_archive_month`` above) — invisible
        until something later tried to read that ZIP and failed, most
        visibly as "Archive ... is missing" Google Drive upload failures.
        Run once at startup (see app.py) regardless of whether archiving is
        currently enabled — this cleans up existing bad rows rather than
        preventing new ones, which the ``_archive_month`` fix already does.
        The clip's data is confirmed unrecoverable at this point (no file,
        no ZIP), so the row is deleted rather than left around as a phantom
        entry the Storage tab and Drive backup would keep failing on.
        Returns the number of rows removed.
        """
        records = await self._db.get_archived_clip_records()
        removed = 0
        for record in records:
            archive_path = str(record.get("archive_path") or "")
            if archive_path and Path(archive_path).exists():
                continue
            try:
                if await self._db.delete_clip(str(record["id"])):
                    removed += 1
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Could not remove orphaned archived clip %s: %s",
                    record.get("id"),
                    exc,
                )
        if removed:
            _LOGGER.warning(
                "Removed %d archived clip record(s) whose ZIP archive no "
                "longer exists on disk — their data was unrecoverable",
                removed,
            )
        return removed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _archive_month(
        self, month: str, clips: list[dict]
    ) -> list[dict[str, Any]]:
        zip_path = self._archive_dir / f"blink_archive_{month}.zip"
        self._quarantine_if_corrupted(zip_path, month)
        archived: list[dict[str, Any]] = []

        for clip in clips:
            src = Path(str(clip.get("file_path", "")))
            if not src.exists():
                # File already gone before we ever got to compress it —
                # there's nothing left to write into the ZIP. Marking this
                # clip archived with zip_path here (the old behavior) was a
                # data-integrity bug: zip_path is only ever actually created
                # a few lines below, inside the branch that requires a real
                # source file, so a month whose clips *all* hit this branch
                # left every one of them pointing at a ZIP that was never
                # created — surfacing later as "Archive ... is missing"
                # Google Drive upload failures (see
                # GDriveUploadQueue._extract_archived_clip). The clip's data
                # is unrecoverable at this point, so remove the row entirely
                # rather than keep a phantom archive reference around —
                # matches how retention/gdrive_queue.py already treat a
                # confirmed-gone source file elsewhere in this codebase
                # (delete the record, don't invent a fake success state).
                # See ClipArchiver.prune_orphaned_archives() for the
                # one-time cleanup of rows this bug already produced.
                try:
                    await self._db.delete_clip(str(clip["id"]))
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Could not remove already-missing clip %s: %s",
                        clip.get("id"),
                        exc,
                    )
                continue

            arcname = f"{clip.get('camera', 'unknown')}/{src.name}"
            try:
                # Open/close the archive once per clip instead of keeping one
                # ZipFile open across the whole month's batch: the central
                # directory is only written on close(), so a crash mid-batch
                # with a single long-lived handle would leave the ZIP unreadable
                # and its already-unlinked source clips unrecoverable. Closing
                # after every file finalizes the central directory each time,
                # so a crash can lose at most the one clip in flight.
                #
                # Corruption itself is handled proactively, above, by
                # _quarantine_if_corrupted rather than by catching
                # BadZipFile here: appending to a corrupted file does *not*
                # reliably raise it (see that method's docstring) — it can
                # silently discard every previously-archived entry instead
                # and report success, which no exception handler here could
                # ever catch.
                with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(src, arcname)
            except (zipfile.BadZipFile, OSError) as exc:
                _LOGGER.warning("Could not archive %s: %s", src, exc)
                continue

            try:
                # A DB failure here (e.g. a dropped connection) must not
                # propagate: the clip is already written into the ZIP, and
                # letting this raise would abort every remaining clip/month
                # in this run. Leave the source file in place (not marked
                # archived) so the next run retries it — it'll be re-written
                # into the ZIP as a duplicate entry, but that's preferable to
                # losing the rest of this batch.
                await self._db.mark_archived(str(clip["id"]), str(zip_path))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Archived %s into %s but failed to record it in the "
                    "database; will retry next run: %s",
                    src,
                    zip_path.name,
                    exc,
                )
                continue

            try:
                src.unlink()
            except OSError as exc:
                _LOGGER.warning(
                    "Marked %s archived but could not delete the source file: %s",
                    src,
                    exc,
                )
                continue

            archived.append(clip)
            _LOGGER.debug("Archived %s → %s", src.name, zip_path.name)

        return archived

    @staticmethod
    def _quarantine_if_corrupted(zip_path: Path, month: str) -> None:
        """If *zip_path* already exists but isn't a readable ZIP, move it
        aside so the next write starts a fresh archive instead of silently
        losing every clip already inside it.

        This is deliberately proactive, not a response to catching
        ``zipfile.BadZipFile`` around the per-clip write below: Python's
        ``zipfile.ZipFile(path, "a")`` does **not** reliably raise that (or
        any) exception when *path* exists but is truncated/corrupted
        (e.g. left mid-write by an ungraceful container stop, despite the
        per-clip open/close elsewhere in this class minimizing that
        window) — verified directly against CPython's zipfile module.
        Instead it silently treats the unreadable file as having zero
        existing entries and happily "succeeds", so the very next
        successful-looking archive run overwrites the central directory
        and permanently discards every clip archived before the
        corruption — with no exception, no warning, nothing — since their
        source files were already deleted from disk once
        ``mark_archived`` recorded them. Only ``zipfile.is_zipfile()``
        actually opens and validates the file's structure, so it's the
        only reliable way to catch this before it causes silent data
        loss rather than after.
        """
        if not zip_path.exists() or zipfile.is_zipfile(zip_path):
            return
        quarantine_path = zip_path.with_name(
            f"{zip_path.name}.corrupted-{int(time.time())}"
        )
        try:
            zip_path.rename(quarantine_path)
        except OSError as exc:
            _LOGGER.warning(
                "%s is corrupted (unreadable as a ZIP) but could not be "
                "quarantined: %s — archiving for %s will keep failing "
                "until this is resolved manually",
                zip_path,
                exc,
                month,
            )
            return
        _LOGGER.warning(
            "%s was corrupted and unreadable as a ZIP — quarantined to %s "
            "(any clips already inside it are unrecoverable) and starting "
            "a fresh archive for %s",
            zip_path,
            quarantine_path.name,
            month,
        )
