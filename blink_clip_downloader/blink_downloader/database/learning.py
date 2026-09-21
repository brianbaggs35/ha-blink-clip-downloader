"""What each camera learns over time, and what the user teaches it.

Two feedback loops that both make the next verdict better than the last.
The explicit one is the user's: thumbs-up/down on a clip tunes that
camera's effective confidence threshold and feeds recent corrections back
into the prompt. The implicit one is the camera's own history — how often
it fires, at which hours, for how long, and what its scene normally looks
like — which is what lets an ordinary evening read as ordinary and a
3 a.m. event read as unusual.

Every threshold here is deliberately conservative and bounded: automatic
tuning may only ever make a camera *less* trigger-happy, never more.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .core import _DatabaseBase
from .sql import (
    _affected,
    _qm,
)

_LOGGER = logging.getLogger(__name__)


# Minimum recorded clips before a camera's visual scene baseline is trusted
# enough to report a deviation score — see get_scene_deviation().
_SCENE_BASELINE_MIN_SAMPLES = 20

# Deviation (see get_scene_deviation()) at or above this level counts as
# "elevated" for the purposes of detecting a persistent scene change, once
# the baseline is already established. Mirrors analyzer._SCENE_DEVIATION_ALERT_THRESHOLD.
_SCENE_REFRESH_DEVIATION_THRESHOLD = 0.12

# Number of consecutive ordinary (non-suspicious) clips that must show
# elevated deviation before the baseline is treated as a persistent change
# (something added/removed from the scene) rather than transient noise.
_SCENE_REFRESH_STREAK = 5

# Blend weight applied the one time the streak threshold is hit, so the
# baseline snaps to the new normal quickly instead of waiting 45+ samples
# for the slow steady-state EMA (alpha floor 0.05) to catch up.
_SCENE_REFRESH_ALPHA = 0.5

# --- Adaptive learning from feedback (analysis_feedback) ---
# Trailing window of feedback rows considered per camera by
# get_effective_confidence_threshold() and get_prompt_corrections().
_FEEDBACK_WINDOW = 20

# Minimum feedback rows for a camera before its notification threshold is
# auto-tuned at all — below this, too little history to trust an adjustment.
_FEEDBACK_MIN_SAMPLES_FOR_THRESHOLD = 10

# Every this many false positives in the trailing window nudges the
# effective threshold up by _FEEDBACK_THRESHOLD_STEP.
_FEEDBACK_FALSE_POSITIVES_PER_STEP = 3

_FEEDBACK_THRESHOLD_STEP = 0.05

# At most this many 0.05 steps apply (i.e. up to +0.15 total) — a burst of
# false positives shouldn't be able to push the threshold to near-1.0.
_FEEDBACK_THRESHOLD_MAX_STEPS = 3

# The auto-tuned threshold is never allowed to drop the admin's own floor by
# more than this, nor rise above this absolute ceiling.
_FEEDBACK_THRESHOLD_FLOOR_DELTA = 0.15

_FEEDBACK_THRESHOLD_CEILING = 0.95

# Recent corrections folded into the prompt (see get_prompt_corrections) are
# capped at this many, matching analyzer._build_prompt's own cap.
_FEEDBACK_PROMPT_CORRECTIONS_LIMIT = 3


class AdaptiveLearningMixin(_DatabaseBase):
    """User feedback, per-camera activity baselines, and scene drift."""

    async def add_feedback(
        self,
        clip_id: str,
        camera: str,
        analysis_result_id: int | None,
        original_suspicious: bool,
        original_confidence: float,
        correct: bool,
        correction_note: str = "",
        corrected_suspicious: bool | None = None,
    ) -> None:
        """Record (or replace) human feedback on a clip's stored AI verdict.

        One feedback row per clip — resubmitting for the same clip (e.g. the
        user changes their mind) replaces the previous entry rather than
        accumulating duplicates. The delete-then-insert runs in a single
        transaction so a concurrent reader never observes a moment with no
        feedback row for this clip.
        """
        if self._pool is None:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                _qm("DELETE FROM analysis_feedback WHERE clip_id=?"), clip_id
            )
            await conn.execute(
                _qm(
                    """
                    INSERT INTO analysis_feedback
                      (clip_id, camera, analysis_result_id, original_suspicious,
                       original_confidence, correct, correction_note,
                       corrected_suspicious, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                clip_id,
                camera,
                analysis_result_id,
                bool(original_suspicious),
                float(original_confidence),
                bool(correct),
                correction_note or "",
                None if corrected_suspicious is None else bool(corrected_suspicious),
                datetime.now(UTC).isoformat(),
            )

    async def delete_feedback(self, clip_id: str) -> bool:
        """Remove stored feedback for a clip entirely. Returns True if a row existed.

        Distinct from resubmitting feedback (which replaces the row via
        :meth:`add_feedback`) — this fully retracts it, e.g. when a reviewer
        gave mistaken feedback and wants it out of the adaptive-learning
        signal (confidence-threshold tuning, prompt corrections, fine-tuning
        examples) rather than merely changed.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("DELETE FROM analysis_feedback WHERE clip_id=?"), clip_id
        )
        return _affected(status) > 0

    async def get_feedback_for_clip(self, clip_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(
            _qm("SELECT * FROM analysis_feedback WHERE clip_id=?"), clip_id
        )
        return dict(row) if row else None

    async def get_recent_feedback(
        self, camera: str | None = None, limit: int = _FEEDBACK_WINDOW
    ) -> list[dict[str, Any]]:
        """Return the most recent feedback rows, optionally filtered by camera."""
        if self._pool is None:
            return []
        if camera:
            query = (
                "SELECT * FROM analysis_feedback WHERE camera=? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (camera, limit)
        else:
            query = "SELECT * FROM analysis_feedback ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        rows = await self._pool.fetch(_qm(query), *params)
        return [dict(r) for r in rows]

    async def get_untrained_feedback(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return feedback rows not yet folded into a Moondream fine-tune.

        Oldest first, so a training run works through the backlog in order
        rather than repeatedly picking up the same most-recent rows. See
        :meth:`mark_feedback_trained` and
        ``MoondreamFineTuneManager.train_from_examples`` in
        ``moondream_finetune.py``.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM analysis_feedback WHERE trained_at='' OR trained_at IS NULL "
                "ORDER BY created_at ASC LIMIT ?"
            ),
            limit,
        )
        return [dict(r) for r in rows]

    async def mark_feedback_trained(self, feedback_ids: list[int]) -> None:
        """Mark feedback rows as consumed by a fine-tuning training run."""
        if self._pool is None or not feedback_ids:
            return
        placeholders = ",".join("?" for _ in feedback_ids)
        await self._pool.execute(
            _qm(
                f"UPDATE analysis_feedback SET trained_at=? WHERE id IN ({placeholders})"
            ),
            datetime.now(UTC).isoformat(),
            *feedback_ids,
        )

    async def get_feedback_stats(self, camera: str | None = None) -> dict[str, Any]:
        """Return aggregate feedback accuracy counts, optionally per camera.

        A "false positive" is a clip the AI flagged suspicious that a human
        marked incorrect; a "false negative" is a clip the AI cleared that a
        human marked incorrect (i.e. it should have been flagged).
        """
        empty = {
            "total": 0,
            "correct": 0,
            "incorrect": 0,
            "false_positive": 0,
            "false_negative": 0,
        }
        if self._pool is None:
            return empty

        where = "WHERE camera=?" if camera else ""
        params: tuple[Any, ...] = (camera,) if camera else ()
        row = await self._pool.fetchrow(
            _qm(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN correct THEN 1 ELSE 0 END), 0) AS correct,
                    COALESCE(SUM(CASE WHEN NOT correct THEN 1 ELSE 0 END), 0) AS incorrect,
                    COALESCE(SUM(CASE WHEN NOT correct AND original_suspicious
                                       THEN 1 ELSE 0 END), 0) AS false_positive,
                    COALESCE(SUM(CASE WHEN NOT correct AND NOT original_suspicious
                                       THEN 1 ELSE 0 END), 0) AS false_negative
                FROM analysis_feedback
                {where}
                """
            ),
            *params,
        )
        if not row:
            return empty
        return dict(row)

    async def get_effective_confidence_threshold(
        self, camera: str, base_threshold: float
    ) -> float:
        """Return *base_threshold* auto-tuned by recent feedback for *camera*.

        Every :data:`_FEEDBACK_FALSE_POSITIVES_PER_STEP` false positives in
        the trailing :data:`_FEEDBACK_WINDOW` feedback rows for this camera
        nudges the threshold up by :data:`_FEEDBACK_THRESHOLD_STEP`, capped at
        :data:`_FEEDBACK_THRESHOLD_MAX_STEPS` steps. Requires at least
        :data:`_FEEDBACK_MIN_SAMPLES_FOR_THRESHOLD` feedback rows for this
        camera before adjusting at all. Recomputed fresh from the trailing
        window each call, so the adjustment decays automatically as old false
        positives roll out of the window — no accumulator to reset. Only
        gates notification-worthiness, never analysis or storage.
        """
        recent = await self.get_recent_feedback(camera, limit=_FEEDBACK_WINDOW)
        if len(recent) < _FEEDBACK_MIN_SAMPLES_FOR_THRESHOLD:
            return base_threshold

        false_positives = sum(
            1 for r in recent if not r["correct"] and r["original_suspicious"]
        )
        steps = min(
            _FEEDBACK_THRESHOLD_MAX_STEPS,
            false_positives // _FEEDBACK_FALSE_POSITIVES_PER_STEP,
        )
        adjusted = base_threshold + steps * _FEEDBACK_THRESHOLD_STEP
        return max(
            base_threshold - _FEEDBACK_THRESHOLD_FLOOR_DELTA,
            min(_FEEDBACK_THRESHOLD_CEILING, adjusted),
        )

    async def get_prompt_corrections(
        self, camera: str, limit: int = _FEEDBACK_PROMPT_CORRECTIONS_LIMIT
    ) -> list[dict[str, Any]]:
        """Return the most recent same-camera corrections with a usable note.

        Only rows with a non-empty ``correction_note`` are eligible — a bare
        correct/incorrect click with no note carries no reusable textual
        signal for the prompt (see ``analyzer._build_prompt``'s RECENT HUMAN
        CORRECTIONS block, which applies its own limit/length bounds too).
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT * FROM analysis_feedback
                WHERE camera=? AND NOT correct AND correction_note != ''
                ORDER BY created_at DESC
                LIMIT ?
                """
            ),
            camera,
            limit,
        )
        return [dict(r) for r in rows]

    async def record_clip_baseline(
        self, camera: str, hour: int, duration: float
    ) -> None:
        """Record a clip event to build the per-camera behavioral baseline.

        Call this every time a clip is downloaded regardless of whether AI
        analysis is enabled.  The baseline is used later to compute anomaly
        scores for new events.
        """
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO camera_baselines (camera, hour, count)
                VALUES (?, ?, 1)
                ON CONFLICT(camera, hour) DO UPDATE SET count = camera_baselines.count + 1
                """
            ),
            camera,
            hour,
        )
        if duration > 0:
            await self._pool.execute(
                _qm(
                    """
                    INSERT INTO camera_duration_stats (camera, avg_duration, sample_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(camera) DO UPDATE SET
                        avg_duration = (camera_duration_stats.avg_duration
                                        * camera_duration_stats.sample_count + ?)
                                       / (camera_duration_stats.sample_count + 1),
                        sample_count = camera_duration_stats.sample_count + 1
                    """
                ),
                camera,
                duration,
                duration,
            )

    async def get_anomaly_score(self, camera: str, hour: int, duration: float) -> float:
        """Return an anomaly score 0.0–1.0 for a clip at *hour* with *duration*.

        Requires at least 30 historical events for the camera before scoring
        activates; returns 0.0 until enough history exists so that early
        installs don't produce false positives.
        """
        if self._pool is None:
            return 0.0

        total = await self._total_camera_events(camera)
        if total < 30:
            return 0.0

        score = 0.0
        hour_count = await self._hour_event_count(camera, hour)
        score += self._score_hour_rarity(hour_count, total / 24.0)
        if duration > 0:
            score += await self._score_duration_anomaly(camera, duration)

        return min(1.0, score)

    async def _total_camera_events(self, camera: str) -> int:
        """Total historical event count for *camera* across all hours."""
        if self._pool is None:
            return 0
        total = await self._pool.fetchval(
            _qm("SELECT COALESCE(SUM(count), 0) FROM camera_baselines WHERE camera=?"),
            camera,
        )
        return total or 0

    async def _hour_event_count(self, camera: str, hour: int) -> int:
        """Historical event count for *camera* at a specific *hour*."""
        if self._pool is None:
            return 0
        count = await self._pool.fetchval(
            _qm(
                "SELECT COALESCE(count, 0) FROM camera_baselines WHERE camera=? AND hour=?"
            ),
            camera,
            hour,
        )
        return count or 0

    @staticmethod
    def _score_hour_rarity(hour_count: int, expected_per_hour: float) -> float:
        """Score how unusual it is for *camera* to see activity at this hour."""
        if hour_count == 0:
            return 0.5  # Never seen activity at this hour
        if hour_count < expected_per_hour * 0.15:
            return 0.35  # Very rare hour
        if hour_count < expected_per_hour * 0.35:
            return 0.15  # Uncommon hour
        return 0.0

    async def _score_duration_anomaly(self, camera: str, duration: float) -> float:
        """Score how unusual *duration* is relative to this camera's history."""
        if self._pool is None:
            return 0.0
        row = await self._pool.fetchrow(
            _qm(
                "SELECT avg_duration, sample_count FROM camera_duration_stats WHERE camera=?"
            ),
            camera,
        )
        if not row or int(row["sample_count"]) < 10:
            return 0.0
        avg = float(row["avg_duration"])
        if avg <= 0:
            return 0.0
        ratio = duration / avg
        if ratio > 4.0 or ratio < 0.2:
            return 0.25  # Very long or very short clip
        if ratio > 2.5 or ratio < 0.4:
            return 0.1
        return 0.0

    @staticmethod
    def _blend_scene_baseline(
        row: asyncpg.Record, thumbnail: list[float]
    ) -> tuple[list[float], int, int]:
        """Blend a new thumbnail into an existing scene baseline row.

        Returns ``(blended_thumbnail, sample_count_before_this_sample,
        deviation_streak)``.
        """
        try:
            existing = json.loads(row["thumbnail"])
        except (json.JSONDecodeError, TypeError):
            existing = []
        count = int(row["sample_count"])
        streak = (
            int(row["consecutive_deviation_count"])
            if row["consecutive_deviation_count"] is not None
            else 0
        )

        if not existing or len(existing) != len(thumbnail):
            # Thumbnail size changed (or prior data was corrupt) — restart
            # the baseline from this sample rather than blending mismatched data.
            return thumbnail, 0, 0

        alpha = max(0.05, 1.0 / (count + 1))
        if count < _SCENE_BASELINE_MIN_SAMPLES:
            # Still ramping up — the fast early-sample alpha above already
            # converges quickly, so don't also track a deviation streak
            # against a baseline that isn't considered trustworthy yet.
            streak = 0
        else:
            diff = sum(abs(e - t) for e, t in zip(existing, thumbnail)) / len(existing)
            streak = streak + 1 if diff >= _SCENE_REFRESH_DEVIATION_THRESHOLD else 0
            if streak >= _SCENE_REFRESH_STREAK:
                alpha = _SCENE_REFRESH_ALPHA
                streak = 0
        blended = [e * (1 - alpha) + t * alpha for e, t in zip(existing, thumbnail)]
        return blended, count, streak

    async def record_scene_baseline(self, camera: str, thumbnail: list[float]) -> None:
        """Fold a clip's opening-frame thumbnail into this camera's learned scene.

        Blink cameras are fixed in place, so a given camera's background
        should look almost identical clip after clip — this running average
        *is* that "usual background". Call this only for clips that were NOT
        flagged suspicious (see ``analyzer.BaseAnalyzer.analyze_clip``) so a
        genuine intruder is never absorbed into what counts as normal.

        The blend rate is faster while a camera has little history (so the
        baseline converges quickly instead of being anchored to whatever the
        first clip or two happened to show) and settles into a slow-moving
        average once established, so gradual lighting/seasonal drift is
        absorbed without letting any single clip swing the baseline.

        Once established, if several consecutive ordinary clips in a row
        show elevated deviation from the current baseline, that's treated as
        a persistent scene change (something was actually added to or
        removed from the background) rather than transient noise, and the
        baseline is snapped toward the new normal in one fast blend instead
        of waiting 45+ samples for the slow steady-state average to catch up.
        """
        if self._pool is None:
            return
        row = await self._pool.fetchrow(
            _qm(
                "SELECT thumbnail, sample_count, consecutive_deviation_count "
                "FROM camera_scene_baselines WHERE camera=?"
            ),
            camera,
        )

        now = datetime.now(UTC).isoformat()
        if row is None:
            await self._pool.execute(
                _qm(
                    """
                    INSERT INTO camera_scene_baselines
                        (camera, thumbnail, sample_count, updated_at, consecutive_deviation_count)
                    VALUES (?, ?, 1, ?, 0)
                    """
                ),
                camera,
                json.dumps(thumbnail),
                now,
            )
            return

        blended, count, streak = self._blend_scene_baseline(row, thumbnail)

        await self._pool.execute(
            _qm(
                """
                UPDATE camera_scene_baselines
                SET thumbnail = ?, sample_count = ?, updated_at = ?, consecutive_deviation_count = ?
                WHERE camera = ?
                """
            ),
            json.dumps(blended),
            count + 1,
            now,
            streak,
            camera,
        )

    async def get_scene_deviation(
        self, camera: str, thumbnail: list[float]
    ) -> float | None:
        """Return how much *thumbnail* deviates (0.0-1.0) from the camera's learned scene.

        Returns ``None`` until at least :data:`_SCENE_BASELINE_MIN_SAMPLES` clips
        have been recorded for this camera — with too little history the
        "baseline" is just whatever the last clip or two happened to show,
        which isn't a reliable signal yet.
        """
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(
            _qm(
                "SELECT thumbnail, sample_count FROM camera_scene_baselines WHERE camera=?"
            ),
            camera,
        )
        if row is None or int(row["sample_count"]) < _SCENE_BASELINE_MIN_SAMPLES:
            return None
        try:
            existing = json.loads(row["thumbnail"])
        except (json.JSONDecodeError, TypeError):
            return None
        if not existing or len(existing) != len(thumbnail):
            return None
        diff = sum(abs(e - t) for e, t in zip(existing, thumbnail)) / len(existing)
        return min(1.0, diff)
