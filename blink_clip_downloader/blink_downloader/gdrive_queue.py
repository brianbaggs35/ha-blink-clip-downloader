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
import shutil
import time
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

# What the queue records on itself when a full Drive stops it, so the
# Storage tab can say why uploads are paused rather than leaving a stalled
# queue looking broken.
_QUOTA_PAUSE_REASON = "Google Drive storage quota exceeded"
# A rate limit clears on its own — unlike a full Drive, it needs no human
# action, so it gets a timed hold-off rather than a pause someone has to
# come back and undo. Only has to outlast the window Drive counts over.
_RATE_LIMIT_HOLD_OFF_SECONDS = 900


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
        # Monotonic deadline before which no upload is attempted at all —
        # see _hold_off(). Zero means "nothing is holding us back".
        self._hold_off_until = 0.0
        self._hold_off_reason = ""

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
                if (
                    counts.get("pending")
                    and self._client.connected
                    and not self._client.uploads_paused
                    and not self.holding_off
                ):
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
            if await self._hold_off_for_drive_state():
                # Every remaining clip in this batch would hit the exact same
                # rate limit or full-quota error immediately, so stop here —
                # and the hold-off is what keeps the *next* cycle from
                # picking straight up where this one left off.
                _LOGGER.info(
                    "Holding off after a %s — %d clip(s) remain pending",
                    self._hold_off_reason or "Drive error",
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
                # In a thread: this decompresses a whole clip out of its
                # monthly ZIP and writes it to scratch, which is the same
                # multi-megabyte blocking work archiver.py hands off for
                # the same reason.
                temp_path = await asyncio.to_thread(self._extract_archived_clip, clip)
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
                # A full Drive or a rate limit says nothing about this clip
                # — it will upload perfectly once there is room, or once the
                # limit resets. Recording it as *failed* was what buried the
                # Storage tab under hundreds of identical "Google Drive
                # storage quota exceeded" rows: one more clip was consumed
                # and written off on every single cycle, for as long as the
                # Drive stayed full. It goes back to pending instead,
                # keeping its place in the queue, while
                # _hold_off_for_drive_state stops the next cycle from
                # immediately doing the same thing again.
                if self._client.quota_exceeded or self._client.rate_limited:
                    await self._db.update_gdrive_queue_status(clip_id, "pending")
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
        # A list, not a set: file_path.parents already yields nearest-parent
        # first, and preserving that order (via dict.fromkeys for dedup)
        # keeps the fallback match below deterministic. A set here would let
        # Python's per-process hash-randomized iteration order pick a
        # different candidate on different runs whenever more than one
        # fallback arcname happens to exist in the ZIP.
        fallback_arcnames = list(
            dict.fromkeys(
                f"{parent.name}/{original_name}"
                for parent in file_path.parents
                if parent.name and parent.name != clip.get("camera", "unknown")
            )
        )

        try:
            with zipfile.ZipFile(archive_path) as zf:
                member_name = arcname
                # Fast path: the overwhelming majority of extractions hit the
                # camera's current name on the first try, so check that one
                # member directly (an O(1) dict lookup) rather than building
                # a set from the archive's full namelist() on every call.
                # Only a rename that predates this arcname makes the direct
                # lookup miss, and that's when the slower full-enumeration
                # fallback below is actually needed.
                try:
                    zf.getinfo(member_name)
                except KeyError:
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
                    # Streamed, not member.read() into one bytes object:
                    # that held a whole decompressed clip in memory on top
                    # of the copy being written, which on a Pi-class box is
                    # a needless spike for no gain.
                    shutil.copyfileobj(member, tmp)
                    return Path(tmp.name)
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            _LOGGER.warning(
                "Could not extract %s from %s: %s", arcname, archive_path, exc
            )
            return None

    # ------------------------------------------------------------------
    # Hold-off
    # ------------------------------------------------------------------

    @property
    def holding_off(self) -> bool:
        """Whether a Drive-side condition is currently stopping uploads."""
        return time.monotonic() < self._hold_off_until

    @property
    def hold_off_seconds(self) -> int:
        """Seconds left before uploads resume, or 0 when nothing is holding
        them back — so the Storage tab can say when it will try again
        rather than leaving a stalled queue looking broken."""
        remaining = self._hold_off_until - time.monotonic()
        return max(0, int(remaining))

    async def _hold_off_for_drive_state(self) -> bool:
        """Stop uploading if Drive has just told us to. Returns whether it did.

        Deliberately checked after *every* clip, including one that
        uploaded successfully: the folder lookups on the way to an upload
        share the same session and the same limits, so a clip can go up
        having already been told to slow down.

        The two conditions are treated differently on purpose. A rate limit
        clears on its own, so it gets a timed hold-off. A full Drive does
        not clear on its own — it clears when a person deletes something or
        buys more space — so there is no point in any amount of retrying,
        and the queue pauses itself outright and says so.
        """
        if self._client.quota_exceeded:
            await self._pause_for_quota()
            return True
        if self._client.rate_limited:
            self._hold_off("Google Drive rate limit", _RATE_LIMIT_HOLD_OFF_SECONDS)
        return self.holding_off

    async def _pause_for_quota(self) -> None:
        """Pause uploads because Drive is full, and say so once.

        Nothing here is retryable: every attempt costs a round trip to be
        told the same thing, which is how the Storage tab ended up buried
        under hundreds of identical "quota exceeded" rows. Resuming is the
        user's to do, from the Storage tab, once they have made room —
        which is also the signal that it is worth trying again at all.
        """
        if self._client.uploads_paused:
            return
        try:
            self._client.set_uploads_paused(True, _QUOTA_PAUSE_REASON)
        except OSError as exc:
            # set_uploads_paused updates in memory before persisting, so
            # uploads are already stopped for this session; only the
            # survives-a-restart part is lost.
            _LOGGER.warning("Could not persist the Drive upload pause: %s", exc)
        _LOGGER.warning("%s — pausing Drive uploads until resumed", _QUOTA_PAUSE_REASON)
        await self._notify_quota_exceeded()

    def _hold_off(self, reason: str, seconds: int) -> None:
        """Stop attempting uploads for *seconds*, and log why once.

        Plain ``def``: nothing here awaits (a hold-off is bookkeeping and a
        log line), and an ``async`` that never awaits is just a coroutine
        its callers have to remember to await.

        Extending an already-running hold-off deliberately does not log
        again — a rate limit that is still in force a cycle later is not
        news, and a line per cycle for as long as it lasts is its own kind
        of noise. Nothing here notifies: a hold-off resolves itself, so
        there is nothing for a person to do about it.
        """
        already_holding = self.holding_off and self._hold_off_reason == reason
        self._hold_off_until = time.monotonic() + seconds
        self._hold_off_reason = reason
        if not already_holding:
            _LOGGER.warning("%s — holding off Drive uploads for %ds", reason, seconds)

    def resume(self) -> None:
        """Clear any hold-off so the next cycle tries again immediately.

        What Retry (and Resume Uploads) on the Storage tab is for: someone
        who has just fixed the problem should not sit out the rest of a
        window that is no longer true. Note this only clears the queue's own
        timed hold-off — an outright pause is the client's state, cleared by
        the same endpoint that calls this.
        """
        self._hold_off_until = 0.0
        self._hold_off_reason = ""

    async def _notify_quota_exceeded(self) -> None:
        if not self._notifier:
            return
        await self._notifier.notify(
            "Google Drive storage quota exceeded — backups are paused. Free up "
            "space in Drive (or upgrade your plan), then press Resume Uploads "
            "on the add-on's Storage tab.",
            title="Blink Downloader: Google Drive Full",
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def prune_empty_backup_folders(self, clips: list[dict[str, Any]]) -> int:
        """Trash the ``<date>/<camera>`` folders *clips* were backed up into,
        for any that nothing is left in. Returns how many were trashed.

        This queue decides where a backup lands (see ``_process_one``), so
        it is also the thing that knows where to tidy up afterwards. Without
        it, deleting a month's archive trashed every clip inside it and left
        the whole empty date-folder scaffolding standing in Drive — which
        looks a great deal like nothing was deleted at all.

        Folders are resolved by *looking up* the same path the upload used,
        never creating, and only trashed once Drive itself confirms they are
        empty — so a folder holding anything the user put there survives,
        and so does one still holding a clip whose own delete failed.
        """
        if not self._client.connected:
            return 0
        # One entry per folder, not per clip: an archive holds hundreds of
        # clips but only ever a handful of date/camera folders, and each
        # check is its own Drive round trip.
        camera_folders = {
            (
                _local_date_str(str(clip.get("timestamp", ""))),
                str(clip.get("camera") or "unknown"),
            )
            for clip in clips
        }
        trashed = 0
        for date_str, camera in sorted(camera_folders):
            trashed += await self._trash_if_empty([date_str, camera])
        # Only after every camera folder under it has had its turn, or the
        # date folder would still look occupied by a sibling about to go.
        for date_str in sorted({date for date, _camera in camera_folders}):
            trashed += await self._trash_if_empty([date_str])
        if trashed:
            _LOGGER.info("Trashed %d now-empty Google Drive backup folder(s)", trashed)
        return trashed

    async def _trash_if_empty(self, path_parts: list[str]) -> int:
        """Trash one backup folder if it resolves and is empty. 1 if it went."""
        folder_id = await self._client.find_folder_path(path_parts)
        if not folder_id:
            return 0
        if not await self._client.folder_is_empty(folder_id):
            return 0
        if not await self._client.delete_file(folder_id):
            return 0
        # The path memo would otherwise send the next upload for this date
        # and camera straight into a folder sitting in the trash.
        self._client.forget_folder(folder_id)
        return 1

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_queue_status(self) -> dict[str, Any]:
        counts = await self._db.get_gdrive_queue_counts()
        return {
            "connected": self._client.connected,
            "uploads_paused": self._client.uploads_paused,
            "pause_reason": self._client.pause_reason,
            "hold_off_reason": self._hold_off_reason if self.holding_off else "",
            "hold_off_seconds": self.hold_off_seconds,
            **counts,
        }
