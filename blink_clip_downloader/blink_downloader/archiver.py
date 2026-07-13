"""ZIP archiver — compresses old clips into dated archives to reclaim space."""

from __future__ import annotations

import logging
import zipfile
from collections import defaultdict
from pathlib import Path

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

    async def run(self) -> int:
        """Archive clips older than *archive_after_days*. Returns archived count."""
        if not self._enabled:
            return 0

        clips = await self._db.get_clips_to_archive(self._archive_after)
        if not clips:
            return 0

        _LOGGER.info(
            "Archiving %d clip(s) older than %d days", len(clips), self._archive_after
        )
        self._archive_dir.mkdir(parents=True, exist_ok=True)

        # Group by "YYYY-MM" for one ZIP per month.
        by_month: dict[str, list[dict]] = defaultdict(list)
        for clip in clips:
            month = str(clip.get("timestamp", ""))[:7] or "unknown"
            by_month[month].append(clip)

        archived_count = 0
        for month, month_clips in by_month.items():
            archived_count += await self._archive_month(month, month_clips)

        _LOGGER.info("Archive run complete: %d file(s) archived", archived_count)
        return archived_count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _archive_month(self, month: str, clips: list[dict]) -> int:
        zip_path = self._archive_dir / f"blink_archive_{month}.zip"
        archived = 0

        for clip in clips:
            src = Path(str(clip.get("file_path", "")))
            if not src.exists():
                # File already gone — just mark DB record as archived. A DB
                # failure here must not abort the rest of the batch (see the
                # matching comment below) — skip this clip and let the next
                # archive run retry it.
                try:
                    await self._db.mark_archived(str(clip["id"]), str(zip_path))
                    archived += 1
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Could not mark already-missing clip %s as archived: %s",
                        clip.get("id"),
                        exc,
                    )
                continue

            try:
                # Open/close the archive once per clip instead of keeping one
                # ZipFile open across the whole month's batch: the central
                # directory is only written on close(), so a crash mid-batch
                # with a single long-lived handle would leave the ZIP unreadable
                # and its already-unlinked source clips unrecoverable. Closing
                # after every file finalizes the central directory each time,
                # so a crash can lose at most the one clip in flight.
                with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
                    # Store relative name to avoid path collisions in the ZIP.
                    arcname = f"{clip.get('camera', 'unknown')}/{src.name}"
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

            archived += 1
            _LOGGER.debug("Archived %s → %s", src.name, zip_path.name)

        return archived
