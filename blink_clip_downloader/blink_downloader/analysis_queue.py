"""Background queue that schedules and processes AI clip analysis."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time
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
                # health_check() is a real authenticated API call for every
                # cloud provider (OpenAI, Anthropic, Ollama Cloud, Moondream
                # Cloud) — its own cache (_HEALTH_CHECK_CACHE_SECONDS, 30s)
                # is shorter than this loop's default 60s check_interval, so
                # it never actually avoided a call here; every single idle
                # cycle (nothing queued) was still hitting the provider just
                # to immediately find no work. A pending-count check is one
                # cheap local DB query, versus a real network round-trip on
                # every cycle regardless of whether there's anything to do.
                counts = await self._db.get_queue_counts()
                if (
                    counts.get("pending")
                    and self._is_in_schedule()
                    and await self._analyzer.health_check()
                ):
                    await self._process_pending()
            except asyncio.CancelledError:
                _LOGGER.info("Analysis queue stopped")
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Analysis queue error: %s", exc)

            for _ in range(self._check_interval):
                if not self._running:
                    return
                await asyncio.sleep(1)

        _LOGGER.info("Analysis queue stopped")

    def stop(self) -> None:
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

        for i, item in enumerate(pending):
            if not self._running:
                break
            await self._process_one(item)
            if self._analyzer.rate_limited:
                # Every remaining clip in this batch would hit the exact
                # same limit immediately (each retrying internally via the
                # provider SDK before failing again) — that's pure wasted
                # time and log noise, not a real attempt. Stop now and let
                # the next check_interval cycle retry with a cooled-down
                # quota instead of burning through the whole batch.
                _LOGGER.info(
                    "Pausing this batch after a rate limit — %d clip(s) remain "
                    "pending and will be retried next cycle",
                    len(pending) - (i + 1),
                )
                break

    async def _process_one(self, item: dict[str, Any]) -> None:
        clip_id = item["clip_id"]
        await self._db.update_queue_status(clip_id, "processing")

        try:
            # Look up clip metadata to enrich analysis with temporal context
            clip = await self._db.get_clip(clip_id)
            clip_timestamp = str(clip.get("timestamp", "")) if clip else ""
            clip_duration = float((clip or {}).get("duration") or 0)
            anomaly_score = await self._compute_anomaly_score(
                item["camera"], clip_timestamp, clip_duration
            )
            recent_corrections = await self._db.get_prompt_corrections(item["camera"])

            result = await self._analyzer.analyze_clip(
                clip_path=item["clip_path"],
                clip_id=clip_id,
                camera=item["camera"],
                anomaly_score=anomaly_score,
                clip_timestamp=clip_timestamp,
                clip_duration=clip_duration,
                recent_corrections=recent_corrections,
            )
            await self._db.add_analysis_result(result.to_dict())
            await self._db.update_queue_status(clip_id, "completed")
            self._log_result(clip_id, result)

        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Failed to analyze clip %s: %s", clip_id, exc)
            await self._db.update_queue_status(clip_id, "failed", error=str(exc)[:500])
            return

        # Dispatch is intentionally outside the try/except above: the
        # analysis result is already correctly persisted and the queue
        # status already flipped to "completed" at this point, so a
        # dispatch-time failure (e.g. a transient DB error computing the
        # adaptive confidence threshold) must not overwrite that status
        # with "failed" — doing so would both trigger a wasted re-analysis
        # of an already-analyzed clip and, worse, silently drop the alert
        # for a genuinely suspicious clip without surfacing the error.
        try:
            await self._maybe_dispatch_alert(item, clip_id, result)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Clip %s was analyzed successfully but alert dispatch failed: %s",
                clip_id,
                exc,
            )

    async def _compute_anomaly_score(
        self, camera: str, clip_timestamp: str, clip_duration: float
    ) -> float:
        """Compute anomaly score before analysis so the prompt can reference it."""
        try:
            from datetime import datetime as _dt

            hour = (
                _dt.fromisoformat(clip_timestamp).hour
                if clip_timestamp
                else _dt.now(UTC).hour
            )
            return await self._db.get_anomaly_score(
                camera=camera, hour=hour, duration=clip_duration
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Could not compute anomaly score for camera %s: %s", camera, exc
            )
            return 0.0

    @staticmethod
    def _log_result(clip_id: str, result: Any) -> None:
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

    async def _maybe_dispatch_alert(
        self, item: dict[str, Any], clip_id: str, result: Any
    ) -> None:
        effective_threshold = await self._db.get_effective_confidence_threshold(
            item["camera"], self._min_confidence
        )
        should_alert = (
            result.is_suspicious
            and result.confidence >= effective_threshold
            and self._dispatcher is not None
        )
        if should_alert and self._dispatcher:
            clip_data = {
                "id": clip_id,
                "camera": item["camera"],
                "path": item["clip_path"],
            }
            await self._dispatcher.dispatch(result, clip_data)

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

        # ai_schedule_start/end are documented as local HH:MM (matching
        # digest_time elsewhere in the app) — use local wall-clock time, not
        # UTC, or the analysis window silently runs on the wrong hours.
        now = datetime.now().time()  # noqa: DTZ005
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
