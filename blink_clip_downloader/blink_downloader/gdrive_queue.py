"""Background queue that uploads archived/backed-up clips to Google Drive.

Structural mirror of analysis_queue.py's AnalysisQueue — same DB-table-as-
queue design, same batch/rate-limit/crash-recovery shape — against
gdrive_upload_queue instead of analysis_queue. No schedule window (unlike AI
analysis, Drive backup isn't paid-per-token, so there's no reason to
restrict it to a time-of-day window).
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import ClipDatabase
    from .gdrive_client import GDriveClient
    from .notifier import HANotifier

_LOGGER = logging.getLogger(__name__)


def _local_date_str(timestamp: str) -> str:
    """Best-effort local calendar date (``YYYY-MM-DD``) for a clip's stored
    (UTC) timestamp — the Drive backup folder structure
    (``<date>/<camera>/<file>``, see ``_process_one`` below) uses the date a
    person would actually call "today", not the UTC date, which can differ
    by a day right around midnight (same reasoning as database.py's
    ``_local_day_bounds``). Falls back to today's local date for a
    missing/unparseable timestamp rather than failing the upload outright —
    an approximately-right folder beats no backup at all.
    """
    if timestamp:
        try:
            dt = datetime.fromisoformat(timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone().strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now().astimezone().strftime("%Y-%m-%d")


class GDriveUploadQueue:
    """Manages a queue of clips awaiting Google Drive backup.

    Runs as a background ``asyncio.Task``. Periodically checks for pending
    uploads and, if the client has a valid connection, processes them in
    batches.
    """

    def __init__(
        self,
        client: GDriveClient,
        db: ClipDatabase,
        notifier: HANotifier | None = None,
        batch_size: int = 5,
        check_interval: int = 300,
    ) -> None:
        self._client = client
        self._db = db
        self._notifier = notifier
        self._batch_size = batch_size
        self._check_interval = check_interval
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the upload queue loop (blocks until stopped)."""
        self._running = True
        _LOGGER.info(
            "Google Drive upload queue started (check every %ds)", self._check_interval
        )

        while self._running:
            try:
                # A cheap local DB query, checked before touching the client
                # at all — same ordering AnalysisQueue.start() uses for
                # get_queue_counts() vs. health_check(), so an idle cycle
                # with nothing queued never has to reach out to Drive just
                # to immediately find no work.
                counts = await self._db.get_gdrive_queue_counts()
                if counts.get("pending") and self._client.connected:
                    await self._process_pending()
            except asyncio.CancelledError:
                _LOGGER.info("Google Drive upload queue stopped")
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Google Drive upload queue error: %s", exc)

            for _ in range(self._check_interval):
                if not self._running:
                    return
                await asyncio.sleep(1)

        _LOGGER.info("Google Drive upload queue stopped")

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    async def enqueue(self, clip: dict[str, Any], folder_id: str = "") -> bool:
        """Add a clip to the Google Drive upload queue.

        *folder_id* is empty for the automatic archived/all_clips path (use
        the client's default connected folder) and set for a manual, one-off
        upload (e.g. Library's "Upload to Drive" bulk action) targeting a
        different folder.

        Returns whether this call actually (re)queued the clip — ``False``
        for a missing clip_id or one already pending/processing/completed,
        so callers (e.g. the Storage tab's "Back Up Existing Clips Now")
        can report an accurate count instead of assuming every call queued
        something new.
        """
        clip_id = str(clip.get("id") or "")
        camera = str(clip.get("camera") or "")
        clip_path = str(clip.get("path") or clip.get("file_path") or "")
        if not clip_id:
            return False
        queued = await self._db.enqueue_for_gdrive_upload(
            clip_id, camera, clip_path, folder_id
        )
        if queued:
            _LOGGER.debug("Enqueued clip %s for Google Drive upload", clip_id)
        return queued

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    async def _process_pending(self) -> None:
        pending = await self._db.get_pending_gdrive_uploads(limit=self._batch_size)
        if not pending:
            return

        _LOGGER.info(
            "Processing %d pending clip(s) for Google Drive upload", len(pending)
        )

        for i, item in enumerate(pending):
            if not self._running:
                break
            await self._process_one(item)
            if self._client.rate_limited or self._client.quota_exceeded:
                # Every remaining clip in this batch would hit the exact same
                # rate limit or full-quota error immediately — stop now and
                # let the next check_interval cycle retry (rate limit) or
                # wait for the user to free up space (quota) instead of
                # burning through the whole batch and spamming a duplicate
                # "Drive full" notification per remaining clip (same
                # reasoning as AnalysisQueue._process_pending).
                _LOGGER.info(
                    "Pausing this batch after a rate limit or quota error — %d "
                    "clip(s) remain pending and will be retried next cycle",
                    len(pending) - (i + 1),
                )
                break

    async def _process_one(self, item: dict[str, Any]) -> None:
        clip_id = item["clip_id"]
        await self._db.update_gdrive_queue_status(clip_id, "processing")

        # Re-fetch the clip rather than trusting the queue row's denormalized
        # clip_path: a clip enqueued while still a regular file can be
        # archived by archiver.py while its upload is backlogged, which
        # deletes the original file out from under a stale path.
        clip = await self._db.get_clip(clip_id)
        if not clip:
            # Deleted entirely since being queued — ON DELETE CASCADE means
            # this queue row is on its way out too; defensive, not expected.
            await self._db.update_gdrive_queue_status(
                clip_id, "failed", error="Clip no longer exists"
            )
            return

        temp_path: Path | None = None
        try:
            if clip.get("archived"):
                temp_path = self._extract_archived_clip(clip)
                if temp_path is None:
                    await self._db.update_gdrive_queue_status(
                        clip_id, "failed", error="Could not extract clip from archive"
                    )
                    return
                upload_path = temp_path
            else:
                upload_path = Path(str(clip.get("file_path", "")))
                if not upload_path.exists():
                    await self._db.update_gdrive_queue_status(
                        clip_id, "failed", error="Source file no longer exists"
                    )
                    return

            # Organize backups as <date>/<camera>/<file> instead of dumping
            # everything flat into one folder — otherwise unnavigable once
            # a library has more than a handful of clips in Drive. Root is
            # a manual one-off target if this was queued via Library's
            # "Upload to Drive" bulk action (item["folder_id"]), else the
            # connected default backup folder — same precedence upload_file
            # itself already uses, just resolved a level earlier so the
            # date/camera subfolders land under the *right* root either way.
            root_folder = item.get("folder_id") or self._client.folder_id
            dest_folder_id: str | None = None
            if root_folder:
                camera = str(clip.get("camera") or "unknown")
                date_str = _local_date_str(str(clip.get("timestamp", "")))
                dest_folder_id = await self._client.get_or_create_folder_path(
                    [date_str, camera], root_id=root_folder
                )
                if dest_folder_id is None:
                    await self._db.update_gdrive_queue_status(
                        clip_id,
                        "failed",
                        error="Could not create Google Drive folder structure",
                    )
                    return

            # Derived from the clip's own recorded file_path, not
            # upload_path.name — for an archived clip, upload_path is a
            # NamedTemporaryFile's random OS-assigned path (see
            # _extract_archived_clip), so upload_path.name would upload
            # every archived clip as something like "tmpXXXXXX.mp4"
            # instead of its real filename. file_path still holds the
            # original name for both archived and non-archived clips. The
            # camera prefix some clip filenames carry is dropped — the
            # camera is already its own folder level above.
            remote_name = Path(str(clip.get("file_path", ""))).name or upload_path.name
            file_id = await self._client.upload_file(
                upload_path, remote_name, folder_id=dest_folder_id
            )

            if not file_id:
                if self._client.quota_exceeded:
                    await self._db.update_gdrive_queue_status(
                        clip_id, "failed", error="Google Drive storage quota exceeded"
                    )
                    await self._notify_quota_exceeded()
                else:
                    await self._db.update_gdrive_queue_status(
                        clip_id, "failed", error="Upload failed"
                    )
                return

            await self._db.mark_gdrive_uploaded(clip_id, file_id)
            await self._db.update_gdrive_queue_status(clip_id, "completed")
            _LOGGER.info(
                "Uploaded clip %s to Google Drive (file_id=%s)", clip_id, file_id
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to upload clip %s to Google Drive: %s", clip_id, exc
            )
            await self._db.update_gdrive_queue_status(
                clip_id, "failed", error=str(exc)[:500]
            )
        finally:
            # Scratch file only — always remove it regardless of outcome
            # above, and a cleanup failure here must not mask (or re-raise
            # over) a status that's already been recorded.
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as exc:
                    _LOGGER.warning("Could not remove temp file %s: %s", temp_path, exc)

    def _extract_archived_clip(self, clip: dict[str, Any]) -> Path | None:
        """Extract this clip's member from its monthly ZIP to a scratch temp file."""
        archive_path = Path(str(clip.get("archive_path", "")))
        if not archive_path.exists():
            _LOGGER.warning(
                "Archive %s for clip %s is missing", archive_path, clip.get("id")
            )
            return None

        # Matches archiver.py's own arcname construction exactly.
        original_name = Path(str(clip.get("file_path", ""))).name
        arcname = f"{clip.get('camera', 'unknown')}/{original_name}"
        file_path = Path(str(clip.get("file_path", "")))
        fallback_arcnames = {
            f"{parent.name}/{original_name}"
            for parent in file_path.parents
            if parent.name and parent.name != clip.get("camera", "unknown")
        }

        try:
            with zipfile.ZipFile(archive_path) as zf:
                member_name = arcname
                member_names = set(zf.namelist())
                if member_name not in member_names:
                    member_name = next(
                        (
                            candidate
                            for candidate in fallback_arcnames
                            if candidate in member_names
                        ),
                        member_name,
                    )
                if member_name not in member_names:
                    basename_matches = [
                        candidate
                        for candidate in member_names
                        if Path(candidate).name == original_name
                    ]
                    if len(basename_matches) == 1:
                        member_name = basename_matches[0]
                with (
                    zf.open(member_name) as member,
                    NamedTemporaryFile(
                        suffix=Path(original_name).suffix or ".mp4", delete=False
                    ) as tmp,
                ):
                    tmp.write(member.read())
                    return Path(tmp.name)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            _LOGGER.warning(
                "Could not extract %s from %s: %s", arcname, archive_path, exc
            )
            return None

    async def _notify_quota_exceeded(self) -> None:
        if not self._notifier:
            return
        await self._notifier.notify(
            "Google Drive storage quota exceeded — new backups are paused. Free "
            "up space in Drive (or upgrade your plan) to resume.",
            title="Blink Downloader: Google Drive Full",
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_queue_status(self) -> dict[str, Any]:
        counts = await self._db.get_gdrive_queue_counts()
        return {"connected": self._client.connected, **counts}
