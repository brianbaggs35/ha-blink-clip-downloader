"""Background queue that schedules and processes AI clip analysis."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .analyzer import ClipAnalyzer
    from .database import ClipDatabase
    from .notification_channels import NotificationDispatcher

_LOGGER = logging.getLogger(__name__)


class AnalysisQueue:
    """Manages a queue of clips awaiting AI analysis.

    Runs as a background ``asyncio.Task``.  Periodically checks if Ollama
    is reachable and the current time falls within the configured schedule
    window, then processes pending clips in batches.
    """

    def __init__(
        self,
        analyzer: ClipAnalyzer,
        db: ClipDatabase,
        dispatcher: NotificationDispatcher | None,
        schedule_start: str = "",
        schedule_end: str = "",
        batch_size: int = 10,
        check_interval: int = 60,
    ) -> None:
        self._analyzer = analyzer
        self._db = db
        self._dispatcher = dispatcher
        self._schedule_start = self._parse_time(schedule_start)
        self._schedule_end = self._parse_time(schedule_end)
        self._batch_size = batch_size
        self._check_interval = check_interval
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the analysis queue loop (blocks until stopped)."""
        self._running = True
        _LOGGER.info("Analysis queue started (check every %ds)", self._check_interval)

        while self._running:
            try:
                if self._is_in_schedule() and await self._analyzer.health_check():
                    await self._process_pending()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Analysis queue error: %s", exc)

            for _ in range(self._check_interval):
                if not self._running:
                    return
                await asyncio.sleep(1)

        _LOGGER.info("Analysis queue stopped")

    async def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    async def enqueue(self, clip: dict[str, Any]) -> None:
        """Add a clip to the analysis queue."""
        clip_id = str(clip.get("id") or "")
        camera = str(clip.get("camera") or "")
        clip_path = str(clip.get("path") or "")
        if not clip_id or not clip_path:
            return
        await self._db.enqueue_for_analysis(clip_id, camera, clip_path)
        _LOGGER.debug("Enqueued clip %s for analysis", clip_id)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------

    async def _process_pending(self) -> None:
        pending = await self._db.get_pending_analysis(limit=self._batch_size)
        if not pending:
            return

        _LOGGER.info("Processing %d pending clip(s) for AI analysis", len(pending))

        for item in pending:
            if not self._running:
                break

            clip_id = item["clip_id"]
            await self._db.update_queue_status(clip_id, "processing")

            try:
                result = await self._analyzer.analyze_clip(
                    clip_path=item["clip_path"],
                    clip_id=clip_id,
                    camera=item["camera"],
                )
                await self._db.add_analysis_result(result.to_dict())
                await self._db.update_queue_status(clip_id, "completed")

                _LOGGER.info(
                    "Analyzed clip %s: suspicious=%s confidence=%.2f",
                    clip_id,
                    result.is_suspicious,
                    result.confidence,
                )

                if result.is_suspicious and self._dispatcher:
                    clip_data = {
                        "id": clip_id,
                        "camera": item["camera"],
                        "path": item["clip_path"],
                    }
                    await self._dispatcher.dispatch(result, clip_data)

            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Failed to analyze clip %s: %s", clip_id, exc)
                await self._db.update_queue_status(
                    clip_id, "failed", error=str(exc)[:500]
                )

    # ------------------------------------------------------------------
    # Schedule
    # ------------------------------------------------------------------

    def _is_in_schedule(self) -> bool:
        """Return True if the current time is within the analysis window.

        If no schedule is configured (both start and end are None),
        analysis is always allowed.
        """
        if self._schedule_start is None or self._schedule_end is None:
            return True

        now = datetime.now(timezone.utc).time()
        start = self._schedule_start
        end = self._schedule_end

        if start <= end:
            return start <= now <= end
        # Overnight window (e.g. 22:00 → 06:00)
        return now >= start or now <= end

    @staticmethod
    def _parse_time(value: str) -> time | None:
        """Parse ``"HH:MM"`` into a :class:`time`, or None if empty."""
        value = value.strip()
        if not value:
            return None
        try:
            parts = value.split(":")
            return time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            _LOGGER.warning("Invalid schedule time %r, ignoring", value)
            return None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_queue_status(self) -> dict[str, Any]:
        counts = await self._db.get_queue_counts()
        return {
            "schedule_start": (
                self._schedule_start.strftime("%H:%M") if self._schedule_start else None
            ),
            "schedule_end": (
                self._schedule_end.strftime("%H:%M") if self._schedule_end else None
            ),
            "in_schedule": self._is_in_schedule(),
            **counts,
        }
