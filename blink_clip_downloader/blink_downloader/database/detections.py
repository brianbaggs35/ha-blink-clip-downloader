"""The structured evidence behind a verdict, not the verdict itself.

Three tables that together back the Security Events tab and the Vehicles
tab: the per-frame boxes the CV detector produced, the deterministic
events :mod:`blink_downloader.security` derived from them, and each
camera's learned protected-vehicle signature.

Counting ``detected_objects`` rows is never the answer to "how many were
there" — they are stored per box per sampled frame, so one parked car
across twelve frames is twelve rows. ``get_detected_objects_summary``
counts distinct ``track_id``s instead.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..security import SecurityEvent, VehicleSignature
from .core import _DatabaseBase
from .sql import (
    _SEVERITY_RANK_SQL,
    _affected,
    _decode_security_event,
    _qm,
    _severities_at_or_above,
    _suspicious_period_bounds,
)

if TYPE_CHECKING:
    from ..vision import DetectedObject

_LOGGER = logging.getLogger(__name__)


class DetectionsMixin(_DatabaseBase):
    """Detected objects, security events, and learned vehicle signatures."""

    async def save_detected_objects(
        self,
        clip_id: str,
        detections: list[DetectedObject],
        interval: float = 0.0,
        frame_size: tuple[float, float] | None = None,
    ) -> None:
        """Replace the stored object-detection results for *clip_id*.

        Replace, not accumulate — see detected_objects' own schema comment.
        Deletes any existing rows for this clip first (a no-op the first
        time) so a re-analyze always leaves exactly the latest detection
        set behind, then bulk-inserts *detections* in the same
        transaction. A clip analyzed with detection disabled, or where
        nothing was detected, simply clears any stale rows from a previous
        run and inserts nothing.
        """
        if self._pool is None:
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                _qm("DELETE FROM detected_objects WHERE clip_id=?"), clip_id
            )
            if detections:
                await conn.executemany(
                    _qm(
                        """
                            INSERT INTO detected_objects
                              (clip_id, label, confidence, box_x1, box_y1,
                               box_x2, box_y2, track_id, frame_index,
                               offset_seconds, frame_width, frame_height)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """
                    ),
                    [
                        (
                            clip_id,
                            d.label,
                            d.confidence,
                            d.box[0],
                            d.box[1],
                            d.box[2],
                            d.box[3],
                            d.track_id,
                            d.frame_index,
                            d.frame_index * interval,
                            frame_size[0] if frame_size else 0.0,
                            frame_size[1] if frame_size else 0.0,
                        )
                        for d in detections
                    ],
                )

    async def get_detected_object_boxes(self, clip_id: str) -> dict[str, Any]:
        """Every stored box for *clip_id*, normalized for drawing over the video.

        Boxes are stored in the detector's own scaled pixel space, which the
        player knows nothing about; dividing by the frame size it recorded
        turns them into 0-1 fractions that overlay correctly at any player
        size. Rows written before those dimensions were recorded are skipped
        rather than drawn in the wrong place.
        """
        if self._pool is None:
            return {"objects": []}
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT label, confidence, box_x1, box_y1, box_x2, box_y2,
                       track_id, offset_seconds, frame_width, frame_height
                FROM detected_objects
                WHERE clip_id=? AND frame_width > 0 AND frame_height > 0
                ORDER BY offset_seconds ASC, id ASC
                """
            ),
            clip_id,
        )
        return {
            "objects": [
                {
                    "label": r["label"],
                    "confidence": float(r["confidence"]),
                    "track_id": r["track_id"],
                    "offset_seconds": float(r["offset_seconds"]),
                    "box": [
                        float(r["box_x1"]) / float(r["frame_width"]),
                        float(r["box_y1"]) / float(r["frame_height"]),
                        float(r["box_x2"]) / float(r["frame_width"]),
                        float(r["box_y2"]) / float(r["frame_height"]),
                    ],
                }
                for r in rows
            ]
        }

    async def get_detected_objects_summary(self, clip_id: str) -> list[dict[str, Any]]:
        """Detections for *clip_id*, aggregated by label — {label, count,
        detections, max_confidence} per distinct label, most-numerous
        first. Empty if detection was never enabled for this clip or
        nothing was found.

        ``count`` is **how many distinct ones appeared in the clip**, not
        how many boxes were stored: the detector runs over every sampled
        frame, so one car parked through a twelve-frame clip contributes
        twelve rows. Counting those rows is what made the clip modal's
        chips read "33 cars" for a driveway with three in it.

        Distinct objects are counted by ``track_id``, the same identity the
        security layer groups its own ``ObjectTrack``s by — so a clip whose
        events read "2 separate people were tracked" can no longer sit
        above a chip saying 1. Accuracy therefore rests on the tracker,
        which is handed frames seconds apart rather than consecutive video
        (see ObjectDetector's own note in vision.py): a static object like
        a parked car is matched trivially, while something crossing the
        frame fast across few sampled frames can be counted twice.

        The per-frame peak is the floor. It covers rows stored with no
        ``track_id`` at all (tracking off, or written by an older build),
        where counting ids would return zero, and it can never exceed the
        truth — those objects were genuinely in frame together.

        ``detections`` keeps the raw box total, which is still worth
        showing as supporting detail: it says how much evidence the count
        rests on.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT f.label,
                       GREATEST(MAX(f.per_frame), MAX(t.tracked))::int AS count,
                       SUM(f.per_frame)::int AS detections,
                       MAX(f.frame_best) AS max_confidence
                FROM (
                    SELECT label,
                           frame_index,
                           COUNT(*) AS per_frame,
                           MAX(confidence) AS frame_best
                    FROM detected_objects
                    WHERE clip_id=?
                    GROUP BY label, frame_index
                ) f
                JOIN (
                    -- COUNT(DISTINCT ...) skips NULLs, so a label detected
                    -- with tracking off scores zero here and falls through
                    -- to the per-frame peak beside it.
                    SELECT label, COUNT(DISTINCT track_id) AS tracked
                    FROM detected_objects
                    WHERE clip_id=?
                    GROUP BY label
                ) t ON t.label = f.label
                GROUP BY f.label
                ORDER BY count DESC, detections DESC, f.label ASC
                """
            ),
            clip_id,
            clip_id,
        )
        return [dict(r) for r in rows]

    async def save_security_events(
        self,
        clip_id: str,
        camera: str,
        events: list[SecurityEvent],
        risk_score: float = 0.0,
        evidence_quality: float = 0.0,
    ) -> None:
        """Replace the stored security events for *clip_id*.

        Replace, not accumulate — a re-analyze must leave exactly the latest
        conclusions behind rather than a pile of every past run's, which
        would show the same clip several times over in the timeline. A clip
        analyzed with the security layer off, or one where nothing was
        detected, simply clears any stale rows and inserts nothing.
        """
        if self._pool is None:
            return
        now = datetime.now(UTC).isoformat()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                _qm("DELETE FROM security_events WHERE clip_id=?"), clip_id
            )
            if events:
                await conn.executemany(
                    _qm(
                        """
                        INSERT INTO security_events
                          (clip_id, camera, event_type, severity, confidence,
                           risk_score, evidence_quality, detail, subject_label,
                           track_id, asset_name, asset_type, start_offset,
                           end_offset, evidence, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    [
                        (
                            clip_id,
                            camera,
                            str(e.event_type),
                            str(e.severity),
                            e.confidence,
                            risk_score,
                            evidence_quality,
                            e.detail,
                            e.subject_label,
                            e.track_id,
                            e.asset_name,
                            e.asset_type,
                            e.start_offset,
                            e.end_offset,
                            json.dumps(e.evidence),
                            now,
                        )
                        for e in events
                    ],
                )

    async def get_security_events(self, clip_id: str) -> list[dict[str, Any]]:
        """Every stored security event for one clip, earliest first."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM security_events WHERE clip_id=? "
                "ORDER BY start_offset ASC, id ASC"
            ),
            clip_id,
        )
        return [_decode_security_event(dict(r)) for r in rows]

    async def get_security_timeline(
        self,
        limit: int = 50,
        offset: int = 0,
        camera: str | None = None,
        min_severity: str | None = None,
        period: str | None = None,
    ) -> dict[str, Any]:
        """One row per clip for the Security tab's timeline.

        Collapsed to the clip's most severe event rather than listing every
        event: a single visit legitimately produces half a dozen of them
        (present, approached, near, lingered, retreated) and a timeline that
        repeats one clip six times is a worse view of the property than no
        timeline at all. The per-event detail is one request away via
        :meth:`get_security_events`.
        """
        if self._pool is None:
            return {"events": [], "total": 0}

        where = ["1=1"]
        params: list[Any] = []
        if camera:
            where.append("se.camera=?")
            params.append(camera)
        if min_severity:
            allowed = _severities_at_or_above(min_severity)
            where.append(f"se.severity IN ({', '.join(['?'] * len(allowed))})")
            params.extend(allowed)
        if period:
            # Filtered on when the clip was *recorded*, not when it was
            # analyzed: the list is ordered by clip time, and a backlog
            # processed overnight would otherwise put three-day-old footage
            # under "Today" while sorting it among today's clips.
            start, end = _suspicious_period_bounds(period)
            if start is not None:
                where.append("c.timestamp >= ?")
                params.append(start)
            if end is not None:
                where.append("c.timestamp < ?")
                params.append(end)
        clause = " AND ".join(where)

        total_row = await self._pool.fetchrow(
            _qm(
                "SELECT COUNT(DISTINCT se.clip_id) AS total FROM security_events se "
                f"JOIN clips c ON c.id = se.clip_id WHERE {clause}"
            ),
            *params,
        )
        # DISTINCT ON keeps one row per clip, and the ORDER BY inside it is
        # what decides *which* one: most severe first, then most confident.
        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (se.clip_id)
                           se.*, c.timestamp AS clip_timestamp, c.file_path,
                           c.starred, c.archived,
                           ar.is_suspicious AS ai_suspicious,
                           ar.confidence AS ai_confidence,
                           ar.summary AS ai_summary,
                           ar.risk_override_applied
                    FROM security_events se
                    JOIN clips c ON c.id = se.clip_id
                    -- The model's own verdict, so the timeline can show
                    -- where code and model agreed and where they did not.
                    -- Latest run only, matching get_analysis_for_clip's
                    -- "most recent wins" semantics: a re-analysis must not
                    -- leave the previous verdict sitting beside the events
                    -- that replaced it.
                    LEFT JOIN LATERAL (
                        SELECT is_suspicious, confidence, summary,
                               risk_override_applied
                        FROM analysis_results
                        WHERE clip_id = se.clip_id
                        ORDER BY analyzed_at DESC
                        LIMIT 1
                    ) ar ON TRUE
                    WHERE {clause}
                    ORDER BY se.clip_id, {_SEVERITY_RANK_SQL} DESC,
                             se.confidence DESC, se.id ASC
                ) ranked
                ORDER BY ranked.clip_timestamp DESC, ranked.id DESC
                LIMIT ? OFFSET ?
                """
            ),
            *params,
            limit,
            offset,
        )
        return {
            "events": [_decode_security_event(dict(r)) for r in rows],
            "total": int(total_row["total"]) if total_row else 0,
        }

    async def get_security_stats(self, days: int = 7) -> dict[str, Any]:
        """Counts by severity over the last *days*, for the tab's header."""
        if self._pool is None:
            return {"by_severity": {}, "total": 0, "days": days}
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT se.severity, COUNT(DISTINCT se.clip_id) AS count
                FROM security_events se
                JOIN clips c ON c.id = se.clip_id
                WHERE c.timestamp >= ?
                GROUP BY se.severity
                """
            ),
            cutoff,
        )
        by_severity = {str(r["severity"]): int(r["count"]) for r in rows}
        return {
            "by_severity": by_severity,
            "total": sum(by_severity.values()),
            "days": days,
        }

    async def get_vehicle_signature(self, camera: str) -> VehicleSignature | None:
        """This camera's learned protected-vehicle signature, if it has one."""
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(
            _qm("SELECT * FROM camera_vehicle_signatures WHERE camera=?"), camera
        )
        if row is None:
            return None
        try:
            box = json.loads(row["box"])
            histogram = json.loads(row["histogram"] or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(box, list) or len(box) != 4:
            return None
        return VehicleSignature(
            box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
            histogram=tuple(float(v) for v in histogram),
            sample_count=int(row["sample_count"] or 0),
        )

    async def save_vehicle_signature(
        self, camera: str, signature: VehicleSignature
    ) -> None:
        """Store (or replace) this camera's learned vehicle signature."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO camera_vehicle_signatures
                  (camera, box, histogram, sample_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (camera) DO UPDATE SET
                  box = EXCLUDED.box,
                  histogram = EXCLUDED.histogram,
                  sample_count = EXCLUDED.sample_count,
                  updated_at = EXCLUDED.updated_at
                """
            ),
            camera,
            json.dumps(list(signature.box)),
            json.dumps(list(signature.histogram)),
            signature.sample_count,
            datetime.now(UTC).isoformat(),
        )

    async def reset_vehicle_signature(self, camera: str) -> bool:
        """Forget what this camera learned about the protected vehicle.

        Needed whenever the premise changes — a new car, a rearranged
        driveway, or a signature that has plainly latched onto the wrong
        vehicle. Without a way to clear it, a bad signature would keep
        reinforcing itself every time it "confirmed" its own mistake.
        """
        if self._pool is None:
            return False
        result = await self._pool.execute(
            _qm("DELETE FROM camera_vehicle_signatures WHERE camera=?"), camera
        )
        return _affected(result) > 0
