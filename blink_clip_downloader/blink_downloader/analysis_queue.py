"""Background queue that schedules and processes AI clip analysis."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .analyzer import BaseAnalyzer
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
        analyzer: BaseAnalyzer,
        db: ClipDatabase,
        dispatcher: NotificationDispatcher | None,
        schedule_start: str = "",
        schedule_end: str = "",
        batch_size: int = 10,
        check_interval: int = 60,
        min_confidence: float = 0.0,
    ) -> None:
        self._analyzer = analyzer
        self._db = db
        self._dispatcher = dispatcher
        self._schedule_start = self._parse_time(schedule_start)
        self._schedule_end = self._parse_time(schedule_end)
        self._batch_size = batch_size
        self._check_interval = check_interval
        self._min_confidence = min_confidence
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

    @property
    def min_confidence(self) -> float:
        """Confidence threshold a suspicious result must meet to notify.

        Also used to determine whether a clip counts as "notified" for the
        media server's notification filter, so that filter always reflects
        the currently configured threshold.
        """
        return self._min_confidence

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
                # Look up clip metadata to enrich analysis with temporal context
                clip = await self._db.get_clip(clip_id)
                clip_timestamp = str(clip.get("timestamp", "")) if clip else ""
                clip_duration = float((clip or {}).get("duration") or 0)

                # Compute anomaly score before analysis so the prompt can reference it
                anomaly_score = 0.0
                try:
                    from datetime import datetime as _dt, timezone as _tz  # noqa: PLC0415

                    hour = (
                        _dt.fromisoformat(clip_timestamp.replace("Z", "+00:00")).hour
                        if clip_timestamp
                        else _dt.now(_tz.utc).hour
                    )
                    anomaly_score = await self._db.get_anomaly_score(
                        camera=item["camera"],
                        hour=hour,
                        duration=clip_duration,
                    )
                except Exception:  # noqa: BLE001
                    pass

                result = await self._analyzer.analyze_clip(
                    clip_path=item["clip_path"],
                    clip_id=clip_id,
                    camera=item["camera"],
                    anomaly_score=anomaly_score,
                    clip_timestamp=clip_timestamp,
                    clip_duration=clip_duration,
                )
                await self._db.add_analysis_result(result.to_dict())
                await self._db.update_queue_status(clip_id, "completed")

                if result.is_suspicious:
                    _LOGGER.info(
                        "Analyzed clip %s: SUSPICIOUS confidence=%.2f — %s",
                        clip_id,
                        result.confidence,
                        result.summary[:100] if result.summary else "",
                    )
                else:
                    _LOGGER.debug(
                        "Analyzed clip %s: no suspicious activity (confidence=%.2f)",
                        clip_id,
                        result.confidence,
                    )

                should_alert = (
                    result.is_suspicious
                    and result.confidence >= self._min_confidence
                    and self._dispatcher is not None
                )
                if should_alert and self._dispatcher:
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
            "min_confidence": self._min_confidence,
            **counts,
        }
