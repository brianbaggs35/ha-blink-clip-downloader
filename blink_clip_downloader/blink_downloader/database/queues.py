"""The two work queues the add-on drains in the background.

``analysis_queue`` feeds clips to the AI analyzer; ``gdrive_upload_queue``
feeds archived clips to cold storage. Both are the same shape — enqueue,
claim a pending batch, report success or failure, retry — and both are
recovered on startup by :meth:`ConnectionMixin._reset_stale_processing`,
which is what stops a crash mid-item from stranding it in 'processing'
where neither queue would ever look at it again.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .core import _DatabaseBase
from .sql import (
    _affected,
    _qm,
)

_LOGGER = logging.getLogger(__name__)


class QueuesMixin(_DatabaseBase):
    """Analysis and Google Drive upload queues: enqueue, claim, retry."""

    async def enqueue_for_analysis(
        self, clip_id: str, camera: str, clip_path: str
    ) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO analysis_queue
                  (clip_id, camera, clip_path, status, queued_at)
                VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT (clip_id) DO NOTHING
                """
            ),
            clip_id,
            camera,
            clip_path,
            datetime.now(UTC).isoformat(),
        )

    async def get_pending_analysis(self, limit: int = 10) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM analysis_queue WHERE status='pending' "
                "ORDER BY queued_at LIMIT ?"
            ),
            limit,
        )
        return [dict(r) for r in rows]

    async def update_queue_status(
        self, clip_id: str, status: str, error: str = ""
    ) -> None:
        if self._pool is None:
            return
        completed = (
            datetime.now(UTC).isoformat() if status in ("completed", "failed") else ""
        )
        await self._pool.execute(
            _qm(
                """
                UPDATE analysis_queue
                SET status=?, completed_at=?, error_message=?
                WHERE clip_id=?
                """
            ),
            status,
            completed,
            error,
            clip_id,
        )

    async def requeue_for_retry(
        self, clip_id: str, retry_count: int, error: str = ""
    ) -> None:
        """Requeue a transiently-failed clip as 'pending' with an incremented
        retry_count, instead of marking it 'failed' outright — see
        AnalysisQueue._process_one, which decides between this and
        update_queue_status(..., "failed", ...) based on
        BaseAnalyzer.transient_error and a bounded retry cap.
        completed_at is left blank since the clip hasn't actually finished.
        """
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                UPDATE analysis_queue
                SET status='pending', retry_count=?, error_message=?
                WHERE clip_id=?
                """
            ),
            retry_count,
            error,
            clip_id,
        )

    async def get_queue_counts(self) -> dict[str, int]:
        if self._pool is None:
            return {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
        rows = await self._pool.fetch(
            "SELECT status, COUNT(*) AS cnt FROM analysis_queue GROUP BY status"
        )
        counts = {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
        for r in rows:
            counts[r["status"]] = r["cnt"]
        return counts

    async def get_failed_analysis_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        """Failed analysis rows with their error message, newest-failure
        first -- powers the AI tab's Queue Status "Failed" modal."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, clip_path, error_message, completed_at, retry_count
                FROM analysis_queue
                WHERE status='failed'
                ORDER BY completed_at DESC
                LIMIT ?
                """
            ),
            limit,
        )
        return [dict(r) for r in rows]

    async def enqueue_for_gdrive_upload(
        self, clip_id: str, camera: str, clip_path: str, folder_id: str = ""
    ) -> bool:
        """Queue *clip_id* for upload, or reset it to retry if it previously failed.

        A clip already ``pending``/``processing``/``completed`` is left
        completely untouched (the ``WHERE`` clause on the ``DO UPDATE``
        below only matches a ``failed`` row) — this used to be a plain
        ``DO NOTHING``, which meant a clip that failed once could never be
        retried again, automatically or manually: every future enqueue
        attempt for that same clip_id silently no-op'd forever, since
        get_pending_gdrive_uploads only ever selects status='pending'.
        ``folder_id=EXCLUDED.folder_id`` matters for the retry case
        specifically — a clip that failed via the automatic backup path
        (folder_id='') and is later re-queued via Library's "Upload to
        Drive" bulk action (an explicit folder_id) must pick up that new
        target, not silently keep uploading to the old default. Returns
        whether a row was actually inserted or reset, so callers can report
        an accurate count instead of assuming every call queued something
        new.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm(
                """
                INSERT INTO gdrive_upload_queue
                  (clip_id, camera, clip_path, status, queued_at, folder_id)
                VALUES (?, ?, ?, 'pending', ?, ?)
                ON CONFLICT (clip_id) DO UPDATE
                  SET status='pending', queued_at=EXCLUDED.queued_at,
                      error_message='', folder_id=EXCLUDED.folder_id
                  WHERE gdrive_upload_queue.status='failed'
                """
            ),
            clip_id,
            camera,
            clip_path,
            datetime.now(UTC).isoformat(),
            folder_id,
        )
        return _affected(status) > 0

    async def get_pending_gdrive_uploads(self, limit: int = 10) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM gdrive_upload_queue WHERE status='pending' "
                "ORDER BY queued_at LIMIT ?"
            ),
            limit,
        )
        return [dict(r) for r in rows]

    async def update_gdrive_queue_status(
        self, clip_id: str, status: str, error: str = ""
    ) -> None:
        if self._pool is None:
            return
        completed = (
            datetime.now(UTC).isoformat() if status in ("completed", "failed") else ""
        )
        await self._pool.execute(
            _qm(
                """
                UPDATE gdrive_upload_queue
                SET status=?, completed_at=?, error_message=?
                WHERE clip_id=?
                """
            ),
            status,
            completed,
            error,
            clip_id,
        )

    async def get_failed_gdrive_uploads(
        self, limit: int = 25, offset: int = 0
    ) -> list[dict[str, Any]]:
        """One page of failed upload rows with their error message,
        newest-failure first — powers the Storage tab's failed-uploads list.

        Paged rather than a flat "first 50": a spell of Drive being
        unreachable can fail every clip in the library, and the tab used to
        render every one of them in a single unbounded column with no way
        to move through it. ``clear_failed_gdrive_uploads`` is the other
        half of that — a list this long also needs a way out.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, clip_path, error_message, completed_at
                FROM gdrive_upload_queue
                WHERE status='failed'
                ORDER BY completed_at DESC, clip_id
                LIMIT ? OFFSET ?
                """
            ),
            limit,
            offset,
        )
        return [dict(r) for r in rows]

    async def clear_failed_gdrive_uploads(self, clip_id: str | None = None) -> int:
        """Delete failed upload row(s) outright. Returns how many went.

        Only ever touches ``status='failed'`` rows, so a clip that is
        pending or mid-upload cannot be dropped from the queue by a
        mistimed "Clear All". Deleting the row does not delete the clip,
        and does not mark it as backed up — it only stops the Storage tab
        reporting a failure the user has decided not to act on. The clip is
        still eligible to be queued again later by the normal backup path.
        """
        if self._pool is None:
            return 0
        sql = "DELETE FROM gdrive_upload_queue WHERE status='failed'"
        params: list[Any] = []
        if clip_id is not None:
            sql += " AND clip_id=?"
            params.append(clip_id)
        status = await self._pool.execute(_qm(sql), *params)
        return _affected(status)

    async def retry_failed_gdrive_uploads(self, clip_id: str | None = None) -> int:
        """Reset failed upload(s) back to pending so the queue retries them.

        A direct bulk UPDATE rather than routing through
        enqueue_for_gdrive_upload: every failed row already has its own
        camera/clip_path/folder_id sitting right here, so there's no need
        for a per-row SELECT-then-INSERT round trip, and this naturally
        preserves each row's own folder_id (a retry should go back to
        wherever it originally failed to reach). *clip_id* narrows to one
        clip; ``None`` retries every currently-failed upload. Returns how
        many rows were actually reset.
        """
        if self._pool is None:
            return 0
        sql = (
            "UPDATE gdrive_upload_queue SET status='pending', queued_at=?, "
            "error_message='' WHERE status='failed'"
        )
        params: list[Any] = [datetime.now(UTC).isoformat()]
        if clip_id is not None:
            sql += " AND clip_id=?"
            params.append(clip_id)
        status = await self._pool.execute(_qm(sql), *params)
        return _affected(status)

    async def get_gdrive_queue_counts(self) -> dict[str, int]:
        if self._pool is None:
            return {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
        rows = await self._pool.fetch(
            "SELECT status, COUNT(*) AS cnt FROM gdrive_upload_queue GROUP BY status"
        )
        counts = {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
        for r in rows:
            counts[r["status"]] = r["cnt"]
        return counts
