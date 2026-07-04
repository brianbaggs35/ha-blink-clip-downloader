"""SQLite-backed clip library with metadata, starring, tagging, and stats."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_LOGGER = logging.getLogger(__name__)

DEFAULT_DB_FILE = Path("/data/clip_library.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id            TEXT    PRIMARY KEY,
    camera        TEXT    NOT NULL,
    file_path     TEXT    NOT NULL,
    timestamp     TEXT    NOT NULL,
    size_bytes    INTEGER DEFAULT 0,
    duration      INTEGER DEFAULT 0,
    source        TEXT    DEFAULT '',
    network_id    INTEGER DEFAULT 0,
    starred       INTEGER DEFAULT 0,
    tags          TEXT    DEFAULT '[]',
    downloaded_at TEXT    NOT NULL,
    archived      INTEGER DEFAULT 0,
    archive_path  TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_clips_camera    ON clips (camera);
CREATE INDEX IF NOT EXISTS idx_clips_timestamp ON clips (timestamp);
CREATE INDEX IF NOT EXISTS idx_clips_starred   ON clips (starred);
CREATE INDEX IF NOT EXISTS idx_clips_archived  ON clips (archived);

CREATE TABLE IF NOT EXISTS analysis_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id           TEXT    NOT NULL,
    camera            TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    response_text     TEXT    DEFAULT '',
    is_suspicious     INTEGER DEFAULT 0,
    confidence        REAL    DEFAULT 0.0,
    summary           TEXT    DEFAULT '',
    frame_count       INTEGER DEFAULT 0,
    analysis_duration REAL    DEFAULT 0.0,
    analyzed_at       TEXT    NOT NULL,
    tokens_prompt     INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_analysis_clip   ON analysis_results (clip_id);
CREATE INDEX IF NOT EXISTS idx_analysis_suspicious ON analysis_results (is_suspicious);
CREATE INDEX IF NOT EXISTS idx_analysis_notified ON analysis_results (clip_id, is_suspicious, confidence);

CREATE TABLE IF NOT EXISTS analysis_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id       TEXT    NOT NULL UNIQUE,
    camera        TEXT    NOT NULL,
    clip_path     TEXT    NOT NULL,
    status        TEXT    DEFAULT 'pending',
    queued_at     TEXT    NOT NULL,
    completed_at  TEXT    DEFAULT '',
    error_message TEXT    DEFAULT '',
    FOREIGN KEY (clip_id) REFERENCES clips(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON analysis_queue (status);

CREATE TABLE IF NOT EXISTS camera_baselines (
    camera TEXT    NOT NULL,
    hour   INTEGER NOT NULL,
    count  INTEGER DEFAULT 0,
    PRIMARY KEY (camera, hour)
);
CREATE TABLE IF NOT EXISTS camera_duration_stats (
    camera       TEXT PRIMARY KEY,
    avg_duration REAL    DEFAULT 0.0,
    sample_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS camera_scene_baselines (
    camera       TEXT PRIMARY KEY,
    thumbnail    TEXT    NOT NULL,
    sample_count INTEGER DEFAULT 0,
    updated_at   TEXT
);
"""

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


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["starred"] = bool(d["starred"])
    d["archived"] = bool(d["archived"])
    if "notified" in d:
        d["notified"] = bool(d["notified"])
    try:
        d["tags"] = json.loads(d.get("tags", "[]") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d


class ClipDatabase:
    """Async wrapper around the SQLite clip library."""

    def __init__(self, db_path: Path = DEFAULT_DB_FILE) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open the database and create tables if needed."""
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        # SQLite ignores declared FOREIGN KEY ... ON DELETE CASCADE constraints
        # unless this pragma is enabled per-connection — without it,
        # delete_clip() leaves orphaned analysis_results/analysis_queue rows
        # behind instead of cascading the delete.
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._migrate()
        await self._reset_stale_processing()
        _LOGGER.debug("Clip database opened at %s", self._path)

    async def _reset_stale_processing(self) -> None:
        """Reset any items stuck in 'processing' back to 'pending'.

        Items land in 'processing' when the app crashes or is restarted
        mid-analysis. They are never retried otherwise because the queue
        only fetches status='pending'.
        """
        assert self._db is not None
        async with self._db.execute(
            "SELECT COUNT(*) FROM analysis_queue WHERE status='processing'"
        ) as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0
        if count:
            await self._db.execute(
                "UPDATE analysis_queue SET status='pending', completed_at='', "
                "error_message='' WHERE status='processing'"
            )
            await self._db.commit()
            _LOGGER.info("Reset %d stale processing item(s) to pending", count)

    async def _migrate(self) -> None:
        """Apply incremental schema migrations for existing databases."""
        assert self._db is not None
        new_columns = [
            ("analysis_results", "tokens_prompt", "INTEGER DEFAULT 0"),
            ("analysis_results", "tokens_completion", "INTEGER DEFAULT 0"),
            ("analysis_results", "anomaly_score", "REAL DEFAULT 0.0"),
            (
                "camera_scene_baselines",
                "consecutive_deviation_count",
                "INTEGER DEFAULT 0",
            ),
        ]
        for table, col, definition in new_columns:
            try:
                await self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col} {definition}"
                )
                await self._db.commit()
                _LOGGER.debug("Migrated: added %s.%s", table, col)
            except Exception:  # noqa: BLE001
                pass  # Column already exists — safe to ignore

        await self._normalize_legacy_model_names()

    async def _normalize_legacy_model_names(self) -> None:
        """Fold pre-3.0 Moondream Cloud rows into the current model identifier.

        Early versions stored the provider name (``moondream-cloud`` /
        ``moondream_cloud``) in ``analysis_results.model`` instead of the
        actual model ID, and predate per-request token tracking. This left
        those rows permanently split out from ``moondream3-preview`` in the
        Per-Model Breakdown with 0 tokens, looking like a second, broken
        model. They're the same model, just analyzed before token tracking
        existed, so merge them into the current identifier.
        """
        assert self._db is not None
        await self._db.execute(
            """
            UPDATE analysis_results SET model = 'moondream3-preview'
            WHERE model IN ('moondream-cloud', 'moondream_cloud')
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def add_clip(self, clip: dict[str, Any]) -> None:
        """Insert or ignore a clip record."""
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT OR IGNORE INTO clips
              (id, camera, file_path, timestamp, size_bytes, duration,
               source, network_id, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(clip.get("id") or ""),
                str(clip.get("camera") or "unknown"),
                str(clip.get("path") or ""),
                str(clip.get("timestamp") or ""),
                int(clip.get("size_bytes") or 0),
                # duration / network_id can be None (null) in the Blink API
                # response for live-view and some camera types — use `or 0`
                # so int() never receives NoneType.
                int(clip.get("duration") or 0),
                str(clip.get("source") or ""),
                int(clip.get("network_id") or 0),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._db.commit()

    async def star_clip(self, clip_id: str, starred: bool) -> bool:
        """Star or unstar a clip. Returns True if the record was found."""
        if self._db is None:
            return False
        cursor = await self._db.execute(
            "UPDATE clips SET starred=? WHERE id=?",
            (1 if starred else 0, clip_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def set_tags(self, clip_id: str, tags: list[str]) -> bool:
        """Replace the tag list for a clip."""
        if self._db is None:
            return False
        cursor = await self._db.execute(
            "UPDATE clips SET tags=? WHERE id=?",
            (json.dumps(tags), clip_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def mark_archived(self, clip_id: str, archive_path: str) -> None:
        if self._db is None:
            return
        await self._db.execute(
            "UPDATE clips SET archived=1, archive_path=? WHERE id=?",
            (archive_path, clip_id),
        )
        await self._db.commit()

    async def delete_clip(self, clip_id: str) -> bool:
        """Remove a clip record from the database."""
        if self._db is None:
            return False
        cursor = await self._db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_clip_by_path(self, file_path: str) -> bool:
        """Remove a clip record by its file_path.

        Used by retention cleanup, which deletes files directly from the
        filesystem (glob + unlink) rather than by clip ID, so it doesn't
        leave an orphaned DB row (and orphaned analysis_results/
        analysis_queue rows) behind for a file that no longer exists.
        """
        if self._db is None:
            return False
        cursor = await self._db.execute(
            "DELETE FROM clips WHERE file_path=?", (file_path,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def get_clip(self, clip_id: str) -> dict[str, Any] | None:
        """Return a single clip record or None."""
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM clips WHERE id=?", (clip_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def get_clips(
        self,
        camera: str | None = None,
        since: str | None = None,
        until: str | None = None,
        starred: bool | None = None,
        source: str | None = None,
        tag: str | None = None,
        search: str | None = None,
        archived: bool = False,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
        notified_only: bool = False,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Query clips with optional filters and sort order.

        sort values: "newest" | "oldest" | "camera" | "size" | "duration"

        Every returned clip includes a ``notified`` boolean (True if the clip
        has an AI analysis result that was/would be suspicious at
        *min_confidence* or higher — the same gate ``AnalysisQueue`` uses to
        decide whether to dispatch a notification). Set *notified_only* to
        restrict results to just those clips.
        """
        if self._db is None:
            return []

        notified_exists = (
            "EXISTS (SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
            "AND ar.is_suspicious = 1 AND ar.confidence >= ?)"
        )

        where: list[str] = [f"archived = {1 if archived else 0}"]
        params: list[Any] = []

        if camera and camera != "all":
            where.append("LOWER(camera) = LOWER(?)")
            params.append(camera)
        if since:
            where.append("timestamp >= ?")
            params.append(since)
        if until:
            where.append("timestamp <= ?")
            params.append(until)
        if starred is not None:
            where.append("starred = ?")
            params.append(1 if starred else 0)
        if source:
            where.append("source = ?")
            params.append(source)
        if tag:
            where.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        if search:
            where.append("(LOWER(camera) LIKE LOWER(?) OR id LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if notified_only:
            where.append(notified_exists)
            params.append(min_confidence)

        _sort_map = {
            "newest": "timestamp DESC",
            "oldest": "timestamp ASC",
            "camera": "LOWER(camera) ASC, timestamp DESC",
            "size": "size_bytes DESC",
            "duration": "duration DESC",
        }
        order = _sort_map.get(sort, "timestamp DESC")

        sql = (
            f"SELECT *, {notified_exists} AS notified FROM clips "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        params = [min_confidence, *params, limit, offset]

        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def count_clips(
        self, camera: str | None = None, starred: bool | None = None
    ) -> int:
        if self._db is None:
            return 0
        where = ["archived=0"]
        params: list[Any] = []
        if camera and camera != "all":
            where.append("LOWER(camera)=LOWER(?)")
            params.append(camera)
        if starred is not None:
            where.append("starred=?")
            params.append(1 if starred else 0)
        async with self._db.execute(
            f"SELECT COUNT(*) FROM clips WHERE {' AND '.join(where)}", params
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def get_all_file_paths(self) -> set[str]:
        """Return the set of all ``file_path`` values currently indexed."""
        if self._db is None:
            return set()
        async with self._db.execute("SELECT file_path FROM clips") as cursor:
            rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def get_clips_to_archive(self, older_than_days: int) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()
        async with self._db.execute(
            "SELECT * FROM clips WHERE archived=0 AND timestamp < ? ORDER BY timestamp",
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics for the library."""
        if self._db is None:
            return {}

        _now_utc = datetime.now(timezone.utc)
        today = _now_utc.date().isoformat()
        yesterday = (_now_utc - timedelta(days=1)).date().isoformat()
        week_ago = (_now_utc - timedelta(days=7)).date().isoformat()

        queries = {
            "total_count": "SELECT COUNT(*) FROM clips WHERE archived=0",
            "starred_count": "SELECT COUNT(*) FROM clips WHERE starred=1",
            "archived_count": "SELECT COUNT(*) FROM clips WHERE archived=1",
            "total_size_bytes": "SELECT COALESCE(SUM(size_bytes),0) FROM clips",
            "today_count": f"SELECT COUNT(*) FROM clips WHERE timestamp LIKE '{today}%'",
            "yesterday_count": f"SELECT COUNT(*) FROM clips WHERE timestamp LIKE '{yesterday}%'",
            "week_count": f"SELECT COUNT(*) FROM clips WHERE timestamp >= '{week_ago}'",
        }

        results: dict[str, Any] = {}
        for key, sql in queries.items():
            async with self._db.execute(sql) as cur:
                row = await cur.fetchone()
            results[key] = row[0] if row else 0

        return results

    async def get_camera_stats(self) -> list[dict[str, Any]]:
        """Return per-camera clip counts, sizes, and activity."""
        if self._db is None:
            return []

        _now_utc = datetime.now(timezone.utc)
        today = _now_utc.date().isoformat()
        week_ago = (_now_utc - timedelta(days=7)).date().isoformat()

        async with self._db.execute(
            """
            SELECT
                camera,
                COUNT(*) AS total,
                COALESCE(SUM(size_bytes), 0) AS size_bytes,
                SUM(CASE WHEN timestamp LIKE ? THEN 1 ELSE 0 END) AS today,
                SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS this_week,
                MAX(timestamp) AS last_seen
            FROM clips
            WHERE archived=0
            GROUP BY LOWER(camera)
            ORDER BY total DESC
            """,
            (f"{today}%", week_ago),
        ) as cursor:
            rows = await cursor.fetchall()

        return [dict(r) for r in rows]

    async def get_distinct_cameras(self) -> list[str]:
        if self._db is None:
            return []
        async with self._db.execute(
            "SELECT DISTINCT camera FROM clips WHERE archived=0 ORDER BY camera"
        ) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def get_distinct_tags(self) -> list[str]:
        """Return all unique tags used across clips (best-effort)."""
        if self._db is None:
            return []
        async with self._db.execute(
            "SELECT DISTINCT tags FROM clips WHERE tags != '[]' AND tags != ''"
        ) as cur:
            rows = await cur.fetchall()
        all_tags: set[str] = set()
        for (raw,) in rows:
            try:
                all_tags.update(json.loads(raw or "[]"))
            except json.JSONDecodeError:
                pass
        return sorted(all_tags)

    async def get_activity_data(self, days: int = 7) -> list[dict[str, Any]]:
        """Return per-hour clip counts for the last *days* days.

        Each row: ``{"date": "YYYY-MM-DD", "hour": 0-23, "count": n}``.
        Useful for rendering an activity heat-map in the UI.
        """
        if self._db is None:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        async with self._db.execute(
            """
            SELECT
                date(timestamp)                        AS date,
                CAST(strftime('%H', timestamp) AS INTEGER) AS hour,
                COUNT(*)                               AS count
            FROM clips
            WHERE timestamp >= ?
            GROUP BY date, hour
            ORDER BY date, hour
            """,
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # AI Analysis
    # ------------------------------------------------------------------

    async def add_analysis_result(self, result: dict[str, Any]) -> None:
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT INTO analysis_results
              (clip_id, camera, model, response_text, is_suspicious,
               confidence, summary, frame_count, analysis_duration, analyzed_at,
               tokens_prompt, tokens_completion, anomaly_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(result.get("clip_id") or ""),
                str(result.get("camera") or ""),
                str(result.get("model") or ""),
                str(result.get("response_text") or ""),
                1 if result.get("is_suspicious") else 0,
                float(result.get("confidence") or 0.0),
                str(result.get("summary") or ""),
                int(result.get("frame_count") or 0),
                float(result.get("analysis_duration") or 0.0),
                str(result.get("analyzed_at") or ""),
                int(result.get("tokens_prompt") or 0),
                int(result.get("tokens_completion") or 0),
                float(result.get("anomaly_score") or 0.0),
            ),
        )
        await self._db.commit()

    async def get_analysis_for_clip(self, clip_id: str) -> dict[str, Any] | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM analysis_results WHERE clip_id=? ORDER BY analyzed_at DESC LIMIT 1",
            (clip_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["is_suspicious"] = bool(d["is_suspicious"])
        return d

    async def get_suspicious_clips(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        async with self._db.execute(
            """
            SELECT ar.*, c.file_path, c.timestamp AS clip_timestamp,
                   c.duration, c.size_bytes
            FROM analysis_results ar
            JOIN clips c ON c.id = ar.clip_id
            WHERE ar.is_suspicious = 1
            ORDER BY ar.analyzed_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["is_suspicious"] = bool(d["is_suspicious"])
            results.append(d)
        return results

    async def get_analysis_stats(self) -> dict[str, Any]:
        if self._db is None:
            return {}
        today = datetime.now(timezone.utc).date().isoformat()
        queries = {
            "total_analyzed": "SELECT COUNT(*) FROM analysis_results",
            "suspicious_count": (
                "SELECT COUNT(*) FROM analysis_results WHERE is_suspicious=1"
            ),
            "total_frames_analyzed": (
                "SELECT COALESCE(SUM(frame_count),0) FROM analysis_results"
            ),
            "frames_analyzed_today": (
                "SELECT COALESCE(SUM(frame_count),0) FROM analysis_results "
                f"WHERE analyzed_at LIKE '{today}%'"
            ),
        }
        results: dict[str, Any] = {}
        for key, sql in queries.items():
            async with self._db.execute(sql) as cur:
                row = await cur.fetchone()
            results[key] = row[0] if row else 0

        async with self._db.execute(
            "SELECT analyzed_at FROM analysis_results ORDER BY analyzed_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        results["last_analysis"] = row[0] if row else None
        return results

    async def get_token_usage_stats(self) -> dict[str, Any]:
        """Return per-model token usage totals for the AI Usage tab."""
        if self._db is None:
            return {
                "total_analyses": 0,
                "total_tokens_prompt": 0,
                "total_tokens_completion": 0,
                "total_tokens": 0,
                "by_model": [],
            }

        async with self._db.execute(
            """
            SELECT
                model,
                COUNT(*)                             AS analyses,
                COALESCE(SUM(tokens_prompt), 0)      AS tokens_prompt,
                COALESCE(SUM(tokens_completion), 0)  AS tokens_completion
            FROM analysis_results
            GROUP BY model
            ORDER BY analyses DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()

        by_model = [dict(r) for r in rows]
        total_prompt = sum(int(m["tokens_prompt"]) for m in by_model)
        total_completion = sum(int(m["tokens_completion"]) for m in by_model)
        total_analyses = sum(int(m["analyses"]) for m in by_model)

        return {
            "total_analyses": total_analyses,
            "total_tokens_prompt": total_prompt,
            "total_tokens_completion": total_completion,
            "total_tokens": total_prompt + total_completion,
            "by_model": by_model,
        }

    # ------------------------------------------------------------------
    # Behavior Memory (per-camera baseline learning)
    # ------------------------------------------------------------------

    async def record_clip_baseline(
        self, camera: str, hour: int, duration: float
    ) -> None:
        """Record a clip event to build the per-camera behavioral baseline.

        Call this every time a clip is downloaded regardless of whether AI
        analysis is enabled.  The baseline is used later to compute anomaly
        scores for new events.
        """
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT INTO camera_baselines (camera, hour, count)
            VALUES (?, ?, 1)
            ON CONFLICT(camera, hour) DO UPDATE SET count = count + 1
            """,
            (camera, hour),
        )
        if duration > 0:
            await self._db.execute(
                """
                INSERT INTO camera_duration_stats (camera, avg_duration, sample_count)
                VALUES (?, ?, 1)
                ON CONFLICT(camera) DO UPDATE SET
                    avg_duration = (avg_duration * sample_count + ?) / (sample_count + 1),
                    sample_count = sample_count + 1
                """,
                (camera, duration, duration),
            )
        await self._db.commit()

    async def get_anomaly_score(self, camera: str, hour: int, duration: float) -> float:
        """Return an anomaly score 0.0–1.0 for a clip at *hour* with *duration*.

        Requires at least 30 historical events for the camera before scoring
        activates; returns 0.0 until enough history exists so that early
        installs don't produce false positives.
        """
        if self._db is None:
            return 0.0

        # Total event count for this camera
        async with self._db.execute(
            "SELECT COALESCE(SUM(count), 0) FROM camera_baselines WHERE camera=?",
            (camera,),
        ) as cur:
            row = await cur.fetchone()
        total: int = int(row[0]) if row else 0

        if total < 30:
            return 0.0

        score = 0.0

        # Hour rarity: how often does this camera fire at this hour vs. average?
        async with self._db.execute(
            "SELECT COALESCE(count, 0) FROM camera_baselines WHERE camera=? AND hour=?",
            (camera, hour),
        ) as cur:
            row = await cur.fetchone()
        hour_count: int = int(row[0]) if row else 0

        expected_per_hour = total / 24.0
        if hour_count == 0:
            score += 0.5  # Never seen activity at this hour
        elif hour_count < expected_per_hour * 0.15:
            score += 0.35  # Very rare hour
        elif hour_count < expected_per_hour * 0.35:
            score += 0.15  # Uncommon hour

        # Duration anomaly
        if duration > 0:
            async with self._db.execute(
                "SELECT avg_duration, sample_count FROM camera_duration_stats WHERE camera=?",
                (camera,),
            ) as cur:
                row = await cur.fetchone()
            if row and int(row[1]) >= 10:
                avg = float(row[0])
                if avg > 0:
                    ratio = duration / avg
                    if ratio > 4.0 or ratio < 0.2:
                        score += 0.25  # Very long or very short clip
                    elif ratio > 2.5 or ratio < 0.4:
                        score += 0.1

        return min(1.0, score)

    # ------------------------------------------------------------------
    # Scene Baseline (per-camera visual "smart brain" learning)
    # ------------------------------------------------------------------

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
        if self._db is None:
            return
        async with self._db.execute(
            "SELECT thumbnail, sample_count, consecutive_deviation_count "
            "FROM camera_scene_baselines WHERE camera=?",
            (camera,),
        ) as cur:
            row = await cur.fetchone()

        now = datetime.now(timezone.utc).isoformat()
        if row is None:
            await self._db.execute(
                """
                INSERT INTO camera_scene_baselines
                    (camera, thumbnail, sample_count, updated_at, consecutive_deviation_count)
                VALUES (?, ?, 1, ?, 0)
                """,
                (camera, json.dumps(thumbnail), now),
            )
            await self._db.commit()
            return

        try:
            existing = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            existing = []
        count = int(row[1])
        streak = int(row[2]) if row[2] is not None else 0

        if not existing or len(existing) != len(thumbnail):
            # Thumbnail size changed (or prior data was corrupt) — restart
            # the baseline from this sample rather than blending mismatched data.
            blended = thumbnail
            count = 0
            streak = 0
        else:
            alpha = max(0.05, 1.0 / (count + 1))
            if count < _SCENE_BASELINE_MIN_SAMPLES:
                # Still ramping up — the fast early-sample alpha above already
                # converges quickly, so don't also track a deviation streak
                # against a baseline that isn't considered trustworthy yet.
                streak = 0
            else:
                diff = sum(abs(e - t) for e, t in zip(existing, thumbnail)) / len(
                    existing
                )
                streak = streak + 1 if diff >= _SCENE_REFRESH_DEVIATION_THRESHOLD else 0
                if streak >= _SCENE_REFRESH_STREAK:
                    alpha = _SCENE_REFRESH_ALPHA
                    streak = 0
            blended = [e * (1 - alpha) + t * alpha for e, t in zip(existing, thumbnail)]

        await self._db.execute(
            """
            UPDATE camera_scene_baselines
            SET thumbnail = ?, sample_count = ?, updated_at = ?, consecutive_deviation_count = ?
            WHERE camera = ?
            """,
            (json.dumps(blended), count + 1, now, streak, camera),
        )
        await self._db.commit()

    async def get_scene_deviation(
        self, camera: str, thumbnail: list[float]
    ) -> float | None:
        """Return how much *thumbnail* deviates (0.0-1.0) from the camera's learned scene.

        Returns ``None`` until at least :data:`_SCENE_BASELINE_MIN_SAMPLES` clips
        have been recorded for this camera — with too little history the
        "baseline" is just whatever the last clip or two happened to show,
        which isn't a reliable signal yet.
        """
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT thumbnail, sample_count FROM camera_scene_baselines WHERE camera=?",
            (camera,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or int(row[1]) < _SCENE_BASELINE_MIN_SAMPLES:
            return None
        try:
            existing = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        if not existing or len(existing) != len(thumbnail):
            return None
        diff = sum(abs(e - t) for e, t in zip(existing, thumbnail)) / len(existing)
        return min(1.0, diff)

    # ------------------------------------------------------------------
    # Analysis Queue
    # ------------------------------------------------------------------

    async def enqueue_for_analysis(
        self, clip_id: str, camera: str, clip_path: str
    ) -> None:
        if self._db is None:
            return
        await self._db.execute(
            """
            INSERT OR IGNORE INTO analysis_queue
              (clip_id, camera, clip_path, status, queued_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (clip_id, camera, clip_path, datetime.now(timezone.utc).isoformat()),
        )
        await self._db.commit()

    async def get_pending_analysis(self, limit: int = 10) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        async with self._db.execute(
            "SELECT * FROM analysis_queue WHERE status='pending' ORDER BY queued_at LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_queue_status(
        self, clip_id: str, status: str, error: str = ""
    ) -> None:
        if self._db is None:
            return
        completed = (
            datetime.now(timezone.utc).isoformat()
            if status in ("completed", "failed")
            else ""
        )
        await self._db.execute(
            """
            UPDATE analysis_queue
            SET status=?, completed_at=?, error_message=?
            WHERE clip_id=?
            """,
            (status, completed, error, clip_id),
        )
        await self._db.commit()

    async def get_queue_counts(self) -> dict[str, int]:
        if self._db is None:
            return {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
        async with self._db.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM analysis_queue
            GROUP BY status
            """
        ) as cursor:
            rows = await cursor.fetchall()
        counts = {"pending": 0, "processing": 0, "completed": 0, "failed": 0}
        for r in rows:
            counts[r["status"]] = r["cnt"]
        return counts
