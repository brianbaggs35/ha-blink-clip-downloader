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
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import ClipDatabase
    from .gdrive_client import GDriveClient
    from .notifier import HANotifier

_LOGGER = logging.getLogger(__name__)


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

    async def enqueue(self, clip: dict[str, Any], folder_id: str = "") -> None:
        """Add a clip to the Google Drive upload queue.

        *folder_id* is empty for the automatic archived/all_clips path (use
        the client's default connected folder) and set for a manual, one-off
        upload (e.g. Library's "Upload to Drive" bulk action) targeting a
        different folder.
        """
        clip_id = str(clip.get("id") or "")
        camera = str(clip.get("camera") or "")
        clip_path = str(clip.get("path") or clip.get("file_path") or "")
        if not clip_id:
            return
        await self._db.enqueue_for_gdrive_upload(clip_id, camera, clip_path, folder_id)
        _LOGGER.debug("Enqueued clip %s for Google Drive upload", clip_id)

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
            if self._client.rate_limited:
                # Every remaining clip in this batch would hit the exact same
                # limit immediately — stop now and let the next
                # check_interval cycle retry with a cooled-down quota
                # instead of burning through the whole batch (same reasoning
                # as AnalysisQueue._process_pending).
                _LOGGER.info(
                    "Pausing this batch after a rate limit — %d clip(s) remain "
                    "pending and will be retried next cycle",
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

            remote_name = f"{clip.get('camera', 'unknown')}_{upload_path.name}"
            file_id = await self._client.upload_file(
                upload_path, remote_name, folder_id=item.get("folder_id") or None
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

        try:
            with (
                zipfile.ZipFile(archive_path) as zf,
                zf.open(arcname) as member,
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
