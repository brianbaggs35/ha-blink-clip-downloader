"""PostgreSQL-backed clip library with metadata, starring, tagging, and stats.

Runs against a PostgreSQL 17 server bundled in this add-on's own container
(see the Dockerfile and rootfs/etc/services.d/postgresql) — not a
user-configured external database. Connects over a local Unix domain socket
with no password (trust auth is scoped to that socket only, never exposed
outside the container), matching the zero-configuration experience SQLite
used to provide while gaining real concurrent access, native types, and
richer query support.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from .security import SecurityEvent, VehicleSignature

if TYPE_CHECKING:
    from .analyzer import AnalysisResult
    from .vision import DetectedObject

_LOGGER = logging.getLogger(__name__)

# Overridable via BLINK_DB_DSN for local dev/testing against a different
# PostgreSQL instance; the container always uses the bundled server.
#
# SonarCloud flags this connection as unprotected by a password — but (see
# the module docstring above, and rootfs/etc/cont-init.d/01-postgres-init.sh)
# this server has no TCP listener at all (listen_addresses=''), only a Unix
# socket reachable from inside this exact container, with
# --auth-host=reject on top. A password here would have to live in the
# same trust boundary this process already reads from, so it would add
# real complexity (generation, migration, breaking the BLINK_DB_DSN
# override below) for no actual defense-in-depth.
DEFAULT_DSN = os.environ.get(
    "BLINK_DB_DSN",
    "postgresql://blink@/blink_clips?host=/var/run/postgresql",  # NOSONAR
)

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
    starred       BOOLEAN DEFAULT FALSE,
    tags          TEXT    DEFAULT '[]',
    downloaded_at TEXT    NOT NULL,
    archived      BOOLEAN DEFAULT FALSE,
    archive_path  TEXT    DEFAULT '',
    gdrive_backed_up   BOOLEAN DEFAULT FALSE,
    gdrive_file_id     TEXT    DEFAULT '',
    gdrive_uploaded_at TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_clips_camera    ON clips (camera);
CREATE INDEX IF NOT EXISTS idx_clips_timestamp ON clips (timestamp);
CREATE INDEX IF NOT EXISTS idx_clips_starred   ON clips (starred);
CREATE INDEX IF NOT EXISTS idx_clips_archived  ON clips (archived);

CREATE TABLE IF NOT EXISTS analysis_results (
    id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id           TEXT    NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    camera            TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    response_text     TEXT    DEFAULT '',
    is_suspicious     BOOLEAN DEFAULT FALSE,
    confidence        DOUBLE PRECISION DEFAULT 0.0,
    summary           TEXT    DEFAULT '',
    frame_count       INTEGER DEFAULT 0,
    analysis_duration DOUBLE PRECISION DEFAULT 0.0,
    analyzed_at       TEXT    NOT NULL,
    tokens_prompt     INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    anomaly_score     DOUBLE PRECISION DEFAULT 0.0,
    escalation_model             TEXT    DEFAULT '',
    escalation_tokens_prompt     INTEGER DEFAULT 0,
    escalation_tokens_completion INTEGER DEFAULT 0,
    escalation_provider          TEXT    DEFAULT '',
    prompt_text                  TEXT    DEFAULT '',
    face_bypass_applied          BOOLEAN DEFAULT FALSE,
    face_bypass_names            TEXT    DEFAULT '',
    approved_faces_seen          BOOLEAN DEFAULT FALSE,
    -- Deterministic security assessment (see blink_downloader/security).
    -- Stored alongside the model's own verdict rather than replacing it:
    -- the two are independent judgements and the UI shows both.
    risk_score                   DOUBLE PRECISION DEFAULT 0.0,
    severity                     TEXT    DEFAULT 'routine',
    event_type                   TEXT    DEFAULT '',
    evidence_quality             DOUBLE PRECISION DEFAULT 0.0,
    risk_override_applied        BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_analysis_clip   ON analysis_results (clip_id);
CREATE INDEX IF NOT EXISTS idx_analysis_suspicious ON analysis_results (is_suspicious);
CREATE INDEX IF NOT EXISTS idx_analysis_notified ON analysis_results (clip_id, is_suspicious, confidence);

-- Per-object detections from the optional computer-vision pipeline's
-- ObjectDetector (see vision.py, ai_enhanced_detection_enabled) — powers
-- the clip modal's object-detection chip summary. Replace, not accumulate,
-- semantics: unlike analysis_results (kept as history), a re-analyze
-- deletes and re-inserts this clip's rows (see save_detected_objects)
-- rather than piling up every past run's detections.
CREATE TABLE IF NOT EXISTS detected_objects (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id       TEXT    NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    label         TEXT    NOT NULL,
    confidence    DOUBLE PRECISION DEFAULT 0.0,
    box_x1        DOUBLE PRECISION DEFAULT 0.0,
    box_y1        DOUBLE PRECISION DEFAULT 0.0,
    box_x2        DOUBLE PRECISION DEFAULT 0.0,
    box_y2        DOUBLE PRECISION DEFAULT 0.0,
    track_id      INTEGER,
    frame_index   INTEGER DEFAULT 0,
    -- Where in the clip this box was seen, and how big the frame it came
    -- from was. Both are needed to draw the box back over the video: the
    -- detector works on scaled frames sampled at its own interval, neither
    -- of which the player knows anything about.
    offset_seconds DOUBLE PRECISION DEFAULT 0.0,
    frame_width    DOUBLE PRECISION DEFAULT 0.0,
    frame_height   DOUBLE PRECISION DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_detected_objects_clip ON detected_objects (clip_id);

-- Structured security events produced by the deterministic layer (see
-- blink_downloader/security). Same replace-not-accumulate semantics as
-- detected_objects above: a re-analyze deletes and re-inserts this clip's
-- rows rather than piling up every past run's conclusions.
--
-- risk_score and evidence_quality are deliberately denormalized onto every
-- row rather than joined from analysis_results. The Security tab's timeline
-- filters and sorts on them across every camera, and the alternative is a
-- join against "the most recent analysis row per clip", which is both
-- slower and — because analysis_results is append-only history — ambiguous
-- in exactly the case that matters (a clip analyzed more than once).
CREATE TABLE IF NOT EXISTS security_events (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id          TEXT    NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    camera           TEXT    NOT NULL,
    event_type       TEXT    NOT NULL,
    severity         TEXT    NOT NULL,
    confidence       DOUBLE PRECISION DEFAULT 0.0,
    risk_score       DOUBLE PRECISION DEFAULT 0.0,
    evidence_quality DOUBLE PRECISION DEFAULT 0.0,
    detail           TEXT    DEFAULT '',
    subject_label    TEXT    DEFAULT '',
    track_id         INTEGER,
    asset_name       TEXT    DEFAULT '',
    asset_type       TEXT    DEFAULT '',
    start_offset     DOUBLE PRECISION DEFAULT 0.0,
    end_offset       DOUBLE PRECISION DEFAULT 0.0,
    evidence         TEXT    DEFAULT '{}',
    created_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_events_clip ON security_events (clip_id);
CREATE INDEX IF NOT EXISTS idx_security_events_camera ON security_events (camera);
CREATE INDEX IF NOT EXISTS idx_security_events_severity
    ON security_events (severity, created_at DESC);

-- What each camera has learned about where its protected vehicle sits and
-- what colour it is (see blink_downloader/security/vehicles.py). This is
-- what lets the add-on tell your car from the one parked beside it when the
-- drawn zone alone cannot — on a shared driveway or in an apartment car
-- park, both cars overlap the zone and only the accumulated parking habit
-- separates them. box is a JSON-encoded normalized [x1, y1, x2, y2] so it
-- survives a change of frame resolution; histogram is a JSON list of floats.
-- Per camera, not per clip: a camera that never sees a protected vehicle
-- simply never gets a row.
CREATE TABLE IF NOT EXISTS camera_vehicle_signatures (
    camera       TEXT PRIMARY KEY,
    box          TEXT NOT NULL,
    histogram    TEXT NOT NULL DEFAULT '[]',
    sample_count INTEGER DEFAULT 0,
    updated_at   TEXT NOT NULL
);

-- Single-row marker for the AI Usage tab's "Clear Stats" button: usage
-- queries only aggregate analysis_results rows analyzed after reset_at,
-- so clearing stats doesn't delete per-clip analysis history (still used
-- by the Suspicious Clips list and clip detail view).
CREATE TABLE IF NOT EXISTS ai_usage_reset (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    reset_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS analysis_queue (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id       TEXT    NOT NULL UNIQUE REFERENCES clips(id) ON DELETE CASCADE,
    camera        TEXT    NOT NULL,
    clip_path     TEXT    NOT NULL,
    status        TEXT    DEFAULT 'pending',
    queued_at     TEXT    NOT NULL,
    completed_at  TEXT    DEFAULT '',
    error_message TEXT    DEFAULT '',
    -- Bumped each time a transient provider failure (timeout, connection
    -- drop, rate limit) requeues this clip as 'pending' instead of marking
    -- it 'failed' outright — see AnalysisQueue._process_one and
    -- BaseAnalyzer.transient_error in analyzer.py. Left at 0 for a clip
    -- that has never been retried.
    retry_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON analysis_queue (status);

-- Background upload queue for the optional Google Drive backup feature
-- (see gdrive_client.py/gdrive_queue.py, gdrive_enabled). Structurally
-- identical to analysis_queue above — same ON DELETE CASCADE reasoning
-- (a clip deleted locally shouldn't leave an orphaned upload row behind).
CREATE TABLE IF NOT EXISTS gdrive_upload_queue (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id       TEXT    NOT NULL UNIQUE REFERENCES clips(id) ON DELETE CASCADE,
    camera        TEXT    NOT NULL,
    clip_path     TEXT    NOT NULL,
    status        TEXT    DEFAULT 'pending',
    queued_at     TEXT    NOT NULL,
    completed_at  TEXT    DEFAULT '',
    error_message TEXT    DEFAULT '',
    -- Empty means "use the client's default connected folder" (automatic
    -- archived/all_clips backups); set for a manual one-off upload (e.g.
    -- Library's "Upload to Drive" bulk action) that targets a different
    -- folder without changing that default.
    folder_id     TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_gdrive_queue_status ON gdrive_upload_queue (status);

CREATE TABLE IF NOT EXISTS camera_baselines (
    camera TEXT    NOT NULL,
    hour   INTEGER NOT NULL,
    count  INTEGER DEFAULT 0,
    PRIMARY KEY (camera, hour)
);
CREATE TABLE IF NOT EXISTS camera_duration_stats (
    camera       TEXT PRIMARY KEY,
    avg_duration DOUBLE PRECISION DEFAULT 0.0,
    sample_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS camera_scene_baselines (
    camera       TEXT PRIMARY KEY,
    thumbnail    TEXT    NOT NULL,
    sample_count INTEGER DEFAULT 0,
    updated_at   TEXT,
    consecutive_deviation_count INTEGER DEFAULT 0
);

-- Human feedback on stored AI verdicts (adaptive learning — "smart brain").
-- One row per clip (resubmitting feedback for the same clip replaces it, see
-- add_feedback()). Powers per-camera notification-threshold auto-tuning
-- (get_effective_confidence_threshold), bounded few-shot prompt guidance
-- (get_prompt_corrections), and Moondream fine-tuning training examples
-- (get_untrained_feedback).
CREATE TABLE IF NOT EXISTS analysis_feedback (
    id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id              TEXT    NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    camera               TEXT    NOT NULL,
    analysis_result_id   INTEGER,
    original_suspicious  BOOLEAN NOT NULL,
    original_confidence  DOUBLE PRECISION NOT NULL,
    correct              BOOLEAN NOT NULL,
    correction_note      TEXT    DEFAULT '',
    corrected_suspicious BOOLEAN,
    created_at           TEXT    NOT NULL,
    trained_at           TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_feedback_camera ON analysis_feedback (camera);
CREATE INDEX IF NOT EXISTS idx_feedback_clip   ON analysis_feedback (clip_id);

-- Local-only face enrollment for the optional face-recognition pipeline
-- (see vision.py, ai_face_recognition_enabled). embedding is a JSON-encoded
-- list of floats (a 512-dim facenet-pytorch InceptionResnetV1 embedding) —
-- stored as TEXT rather than a native array/vector type for the same reason
-- `tags` above is TEXT: simplicity over compactness for a table that will
-- only ever hold a handful of rows. Never leaves this database.
CREATE TABLE IF NOT EXISTS face_enrollments (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT    NOT NULL,
    embedding  TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    approved   BOOLEAN NOT NULL DEFAULT TRUE
);

-- Human feedback specifically on face-recognition accuracy, distinct from
-- analysis_feedback above (which is about the suspicious-flag verdict, not
-- about whether a face was matched correctly). Deliberately a pure audit
-- trail — see the Biometrics tab's "Report a face match issue" flow and
-- FaceBypassActivityCard — not wired into any automatic threshold
-- adjustment. Unlike the suspicious-flag confidence threshold (which can
-- only ever get *more* conservative from feedback, see
-- get_effective_confidence_threshold), a wrong automatic adjustment here
-- risks the opposite mistake: loosening face-match tolerance from a
-- handful of reports could itself cause the false bypass the safety
-- design in analyzer.py's docs explicitly warns against. So this data is
-- surfaced for a human to review and act on (e.g. re-enrolling someone
-- with clearer reference photos), not consumed automatically.
CREATE TABLE IF NOT EXISTS face_recognition_feedback (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    clip_id     TEXT    NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    camera      TEXT    NOT NULL,
    report_type TEXT    NOT NULL,
    note        TEXT    DEFAULT '',
    person_name TEXT    DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_face_feedback_clip ON face_recognition_feedback (clip_id);

-- Per-camera battery state history (see battery_monitor.py). A new row is
-- only written when battery_state actually changes from that camera's most
-- recently recorded row (see add_battery_reading) — battery_level/
-- battery_voltage drift continuously even while the camera stays "ok", so
-- deduping on the full reading instead of state alone would defeat the
-- point of a change log. This makes "when did it go low / come back"
-- directly queryable from consecutive rows rather than needing to scan a
-- reading taken every poll cycle forever. Not clip-keyed (a battery
-- reading isn't tied to any specific clip), so unlike face_recognition_feedback
-- above this has no FK to clips and needs its own explicit TRUNCATE entry
-- in tests/conftest.py and scripts/standalone_server.py.
CREATE TABLE IF NOT EXISTS battery_history (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    camera          TEXT    NOT NULL,
    battery_state   TEXT    NOT NULL,
    battery_level   INTEGER,
    battery_voltage INTEGER,
    recorded_at     TEXT    NOT NULL
);
-- DESC on id matches get_latest_battery_state()'s own ORDER BY camera, id
-- DESC exactly, so that query (run on every Status tab load) can satisfy its
-- DISTINCT ON (camera) with a plain index scan instead of a sort — see the
-- migration below for upgrading a database created before this mattered.
CREATE INDEX IF NOT EXISTS idx_battery_history_camera ON battery_history (camera, id DESC);
"""

# Schema changes applied to *already-existing* tables, run every startup
# after _SCHEMA above. CREATE TABLE IF NOT EXISTS (in _SCHEMA) only creates
# missing tables — it is a silent no-op for a table that already exists, so
# a column added to an existing table's definition there would never
# actually reach a database created by an earlier version of this add-on.
# ADD COLUMN IF NOT EXISTS is idempotent on both a brand-new table (created
# with the column already, per _SCHEMA above) and an older one missing it.
# This is the first such migration since the SQLite-to-PostgreSQL switch —
# extend this string for any future column addition to an existing table.
# Any index on such a column must also live here, not in _SCHEMA: _SCHEMA
# runs first, in one multi-statement execute(), so a CREATE INDEX there on
# a column that only _MIGRATIONS (below) adds would fail on every
# already-existing database the moment it reached that statement.
_MIGRATIONS = """
ALTER TABLE face_enrollments ADD COLUMN IF NOT EXISTS approved BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS face_bypass_applied BOOLEAN DEFAULT FALSE;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS face_bypass_names TEXT DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS approved_faces_seen BOOLEAN DEFAULT FALSE;
ALTER TABLE face_recognition_feedback ADD COLUMN IF NOT EXISTS person_name TEXT DEFAULT '';
ALTER TABLE clips ADD COLUMN IF NOT EXISTS gdrive_backed_up BOOLEAN DEFAULT FALSE;
ALTER TABLE clips ADD COLUMN IF NOT EXISTS gdrive_file_id TEXT DEFAULT '';
ALTER TABLE clips ADD COLUMN IF NOT EXISTS gdrive_uploaded_at TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_clips_gdrive_backed_up ON clips (gdrive_backed_up);
ALTER TABLE gdrive_upload_queue ADD COLUMN IF NOT EXISTS folder_id TEXT DEFAULT '';
DROP INDEX IF EXISTS idx_battery_history_camera;
CREATE INDEX IF NOT EXISTS idx_battery_history_camera ON battery_history (camera, id DESC);
ALTER TABLE analysis_queue ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS risk_score DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS severity TEXT DEFAULT 'routine';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS event_type TEXT DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS evidence_quality DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS risk_override_applied BOOLEAN DEFAULT FALSE;
ALTER TABLE detected_objects ADD COLUMN IF NOT EXISTS offset_seconds DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE detected_objects ADD COLUMN IF NOT EXISTS frame_width DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE detected_objects ADD COLUMN IF NOT EXISTS frame_height DOUBLE PRECISION DEFAULT 0.0;
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

# Shared WHERE-clause fragments for the camera/since/until clip filters that
# get_clips/get_archive_groups/get_archive_clips each build independently.
_WHERE_CAMERA = "LOWER(camera) = LOWER(?)"
_WHERE_SINCE = "timestamp >= ?"
_WHERE_UNTIL = "timestamp <= ?"


def _qm(sql: str) -> str:
    """Convert ``?``-style positional placeholders to asyncpg's ``$1, $2, ...``.

    Every query in this module is written with SQLite-style ``?``
    placeholders (readable, and lets WHERE-clause-building code append
    ``"col=?"`` fragments without tracking a running placeholder index) and
    passed through this helper immediately before execution — it only
    renumbers ``?`` occurrences in final positional order, so dynamically
    assembled queries need no other change to run against PostgreSQL.
    """
    parts = sql.split("?")
    return parts[0] + "".join(
        f"${i}{part}" for i, part in enumerate(parts[1:], start=1)
    )


_LEGACY_CLIPS_INSERT = """
    INSERT INTO clips
      (id, camera, file_path, timestamp, size_bytes, duration, source,
       network_id, starred, tags, downloaded_at, archived, archive_path)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (id) DO NOTHING
"""

_LEGACY_ANALYSIS_RESULTS_INSERT = """
    INSERT INTO analysis_results
      (clip_id, camera, model, response_text, is_suspicious, confidence,
       summary, frame_count, analysis_duration, analyzed_at, tokens_prompt,
       tokens_completion, anomaly_score, escalation_model,
       escalation_tokens_prompt, escalation_tokens_completion,
       escalation_provider, prompt_text, face_bypass_applied,
       face_bypass_names, approved_faces_seen)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _affected(status: str) -> int:
    """Extract the row count from an asyncpg command-tag string (e.g. ``"UPDATE 1"``)."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags", "[]") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d


def _local_day_bounds(days_ago: int = 0) -> tuple[str, str]:
    """UTC ISO-8601 ``[start, end)`` instants bounding the local calendar
    day that was *days_ago* days before today, in the system's configured
    timezone — the same TZ HA Supervisor gives the container that
    analyzer.py's ``_time_of_day_segment`` already relies on via
    ``astimezone()``. Every timestamp in this schema is stored as UTC text
    (see ``init()``'s ``server_settings`` comment), so "today" must be
    resolved via local midnight and converted back to UTC here rather than
    compared as a bare UTC calendar-date string — otherwise evenings in any
    timezone behind UTC roll over into "tomorrow" hours early.
    """
    local_midnight = (
        datetime.now(UTC)
        .astimezone()
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    start = local_midnight - timedelta(days=days_ago)
    end = start + timedelta(days=1)
    return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()


# Suspicious-feed period filter keywords accepted by get_suspicious_clips()/
# count_suspicious_clips() — see _suspicious_period_bounds().
SUSPICIOUS_PERIODS: frozenset[str] = frozenset({"today", "yesterday", "week", "month"})


def _suspicious_period_bounds(period: str | None) -> tuple[str | None, str | None]:
    """Resolve a suspicious-feed filter keyword to UTC ``[start, end)`` bounds.

    ``today``/``yesterday`` are single local calendar days (see
    _local_day_bounds). ``week``/``month`` are rolling windows anchored at
    local midnight and open-ended through now — the same "rolling, not
    calendar-boundary" convention get_stats()'s ``week_count`` already uses
    (``timestamp >= week_start`` with no upper bound). Returns (None, None)
    for an unrecognized or missing period, meaning "no filter, all time".
    """
    if period == "today":
        return _local_day_bounds(0)
    if period == "yesterday":
        return _local_day_bounds(1)
    if period == "week":
        return _local_day_bounds(7)[0], None
    if period == "month":
        return _local_day_bounds(30)[0], None
    return None, None


#: Severity names in ascending order, mirroring
#: ``security.events.Severity``. Kept as plain strings because severity is
#: stored as text and this module must not depend on the security package's
#: enum ordering staying in lockstep with a column's contents written by an
#: older build.
_SEVERITY_ORDER: tuple[str, ...] = ("routine", "noteworthy", "suspicious", "critical")

#: SQL expression ranking a ``security_events.severity`` value, so "most
#: severe event for this clip" and "at least this severe" can both be
#: answered in the database rather than by fetching everything and sorting
#: in Python.
_SEVERITY_RANK_SQL = (
    "CASE se.severity "
    + " ".join(
        f"WHEN '{name}' THEN {rank}" for rank, name in enumerate(_SEVERITY_ORDER)
    )
    + " ELSE 0 END"
)


def _severities_at_or_above(severity: str) -> list[str]:
    """Severity names at least as severe as *severity*.

    An unrecognized name matches everything rather than nothing — a filter
    this build doesn't understand must not silently hide a critical event.
    """
    try:
        index = _SEVERITY_ORDER.index(severity)
    except ValueError:
        return list(_SEVERITY_ORDER)
    return list(_SEVERITY_ORDER[index:])


def _decode_security_event(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a stored row into the API's event shape, decoding its evidence."""
    try:
        row["evidence"] = json.loads(row.get("evidence") or "{}")
    except (json.JSONDecodeError, TypeError):
        row["evidence"] = {}
    return row


def _suspicious_clips_where(
    period: str | None, table_alias: str = "ar"
) -> tuple[str, list[str]]:
    """Shared ``WHERE`` clause + bind params for get_suspicious_clips() and
    count_suspicious_clips(), so the two queries can never drift out of
    sync on which rows count as "suspicious for this period"."""
    prefix = f"{table_alias}." if table_alias else ""
    start, end = _suspicious_period_bounds(period)
    where = f"WHERE {prefix}is_suspicious"
    params: list[str] = []
    if start is not None:
        where += f" AND {prefix}analyzed_at >= ?"
        params.append(start)
    if end is not None:
        where += f" AND {prefix}analyzed_at < ?"
        params.append(end)
    return where, params


def _local_utc_offset_sql() -> str:
    """SQL ``INTERVAL`` literal (e.g. ``"-03:00"``) for the system's current
    UTC offset — see :func:`_local_day_bounds`. For GROUP BY queries that
    need ``to_char()``/``EXTRACT()`` to read local calendar fields instead
    of the UTC ones the pinned-UTC session (see ``init()``) would otherwise
    produce.

    Always interpolated directly into query text, never bound as a ``?``
    parameter: asyncpg encodes a bound interval from a Python ``timedelta``
    with a nonzero ``days`` field whenever the offset is negative (that's
    just how a negative ``timedelta`` under 24h normalizes), and Postgres's
    ``AT TIME ZONE interval`` rejects that outright ("time zone interval
    must not contain months or days") — a literal ``HH:MM`` string
    sidesteps the whole problem. Always machine-computed from the system
    clock, never user input, so interpolating it directly is safe.
    """
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "-" if total_minutes < 0 else "+"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


class ClipDatabase:
    """Async wrapper around the PostgreSQL clip library."""

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Connect to PostgreSQL and create tables if needed."""
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            # Every timestamp column in this schema is a UTC ISO-8601 TEXT
            # string (see datetime.now(timezone.utc).isoformat() throughout
            # this module) compared/bucketed with plain string ops
            # (LIKE 'YYYY-MM-DD%') or cast to timestamptz for date/hour
            # extraction — EXTRACT()/::date on a timestamptz apply the
            # *session* time zone, so without pinning it here those casts
            # would silently bucket by the server's local zone instead of
            # UTC, shifting every "today"/hourly-activity figure.
            server_settings={"timezone": "UTC"},
        )
        await self._pool.execute(_SCHEMA)
        await self._pool.execute(_MIGRATIONS)
        await self._reset_stale_processing()
        _LOGGER.debug("Clip database connected (dsn=%s)", self._dsn)

    async def _reset_stale_processing(self) -> None:
        """Reset any items stuck in 'processing' back to 'pending'.

        Items land in 'processing' when the app crashes or is restarted
        mid-analysis/mid-upload. They are never retried otherwise because
        each queue only fetches status='pending'. Covers both background
        queue tables (analysis_queue, gdrive_upload_queue) — table names
        are hardcoded literals from this tuple, not user input, so an
        f-string is safe here (asyncpg placeholders can't parameterize
        identifiers).
        """
        assert self._pool is not None
        for table in ("analysis_queue", "gdrive_upload_queue"):
            count = await self._pool.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE status='processing'"
            )
            if count:
                await self._pool.execute(
                    f"UPDATE {table} SET status='pending', completed_at='', "
                    "error_message='' WHERE status='processing'"
                )
                _LOGGER.info(
                    "Reset %d stale processing item(s) to pending in %s", count, table
                )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def add_clip(self, clip: dict[str, Any]) -> None:
        """Insert or ignore a clip record."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO clips
                  (id, camera, file_path, timestamp, size_bytes, duration,
                   source, network_id, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """
            ),
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
            datetime.now(UTC).isoformat(),
        )

    async def import_legacy_sqlite_data(
        self,
        clips: list[tuple[Any, ...]],
        analysis_results: list[tuple[Any, ...]],
        ai_usage_reset_at: str | None,
    ) -> int:
        """Merge rows already read from a pre-5.0.0 SQLite clip library
        (see sqlite_migration.py, this method's only caller) into this
        database, in a single transaction.

        Deliberately a merge, not a plain "only if clips is empty" bulk
        insert: by the time this ships, anyone who already upgraded to
        5.0.0 already has a non-empty ``clips`` table, entirely populated
        by library_scanner.import_existing_clips()'s lossy filesystem
        reconstruction (synthetic "import-*" ids, ``source=''``, no
        starred/tags/archived, ``downloaded_at`` stamped at rescan time
        rather than the real download time — see its docstring). Gating on
        emptiness would make this a permanent no-op for exactly the
        installs it exists to fix. Instead, each old row is matched
        against ``clips`` by ``file_path`` (the one thing both a
        reconstructed row and the original row agree on, even though their
        ids differ): a match backfills that existing row in place —
        preserving its current id, and with it any real analysis already
        run against it since upgrading — with the old row's camera,
        source, timestamp, starred, tags, downloaded_at, archived, and
        archive_path (duration/size_bytes/network_id are left alone: the
        reconstruction's own fresh ffprobe/stat already gave those
        possibly-better values than a possibly-stale old one). No match
        means this clip was never reconstructed (e.g. its file no longer
        exists on disk) and is inserted fresh under its original id.

        The whole operation is one transaction so a failure partway
        through leaves the database exactly as it was — safe to just
        retry on the next restart — rather than needing per-row
        idempotency checks against a partially-completed previous attempt.

        *clips* rows must already be in ``(id, camera, file_path,
        timestamp, size_bytes, duration, source, network_id, starred,
        tags, downloaded_at, archived, archive_path)`` order. *analysis_results*
        rows must already be in ``(clip_id, camera, model, response_text,
        is_suspicious, confidence, summary, frame_count, analysis_duration,
        analyzed_at, tokens_prompt, tokens_completion, anomaly_score,
        escalation_model, escalation_tokens_prompt,
        escalation_tokens_completion, escalation_provider, prompt_text,
        face_bypass_applied, face_bypass_names, approved_faces_seen)``
        order, with ``clip_id`` remapped to whichever id the matching
        *clips* row above ended up with — any row whose ``clip_id`` isn't
        among *clips* at all is silently skipped (an orphaned row from the
        old file; SQLite's own FOREIGN KEY there was never enforced by
        default, so this can happen even though it shouldn't).

        Returns the number of clip rows processed, inserted or backfilled
        alike (0 if there was nothing to import).
        """
        if self._pool is None or not clips:
            return 0
        async with self._pool.acquire() as conn, conn.transaction():
            id_map: dict[Any, Any] = {}
            for row in clips:
                old_id, camera, file_path = row[0], row[1], row[2]
                existing_id = await conn.fetchval(
                    _qm("SELECT id FROM clips WHERE file_path = ?"), file_path
                )
                if existing_id is not None:
                    await conn.execute(
                        _qm(
                            """
                            UPDATE clips
                            SET camera=?, timestamp=?, source=?, starred=?,
                                tags=?, downloaded_at=?, archived=?,
                                archive_path=?
                            WHERE id=?
                            """
                        ),
                        camera,
                        row[3],  # timestamp
                        row[6],  # source
                        row[8],  # starred
                        row[9],  # tags
                        row[10],  # downloaded_at
                        row[11],  # archived
                        row[12],  # archive_path
                        existing_id,
                    )
                    id_map[old_id] = existing_id
                else:
                    await conn.execute(_qm(_LEGACY_CLIPS_INSERT), *row)
                    id_map[old_id] = old_id

            for row in analysis_results:
                resolved_id = id_map.get(row[0])
                if resolved_id is None:
                    continue
                await conn.execute(
                    _qm(_LEGACY_ANALYSIS_RESULTS_INSERT), resolved_id, *row[1:]
                )

            if ai_usage_reset_at:
                await conn.execute(
                    _qm(
                        "INSERT INTO ai_usage_reset (id, reset_at) "
                        "VALUES (1, ?) ON CONFLICT (id) DO NOTHING"
                    ),
                    ai_usage_reset_at,
                )
        return len(clips)

    async def star_clip(self, clip_id: str, starred: bool) -> bool:
        """Star or unstar a clip. Returns True if the record was found."""
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE clips SET starred=? WHERE id=?"), starred, clip_id
        )
        return _affected(status) > 0

    async def update_clip_duration(self, clip_id: str, duration: int) -> bool:
        """Backfill a clip's duration (see app.py's startup duration backfill).

        Only ever called with a freshly-probed, positive value — a clip
        already showing a real duration is never re-probed in the first
        place (see get_clips_missing_duration), so this never needs to
        guard against overwriting a good value with a worse one.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE clips SET duration=? WHERE id=?"), duration, clip_id
        )
        return _affected(status) > 0

    async def set_tags(self, clip_id: str, tags: list[str]) -> bool:
        """Replace the tag list for a clip."""
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE clips SET tags=? WHERE id=?"), json.dumps(tags), clip_id
        )
        return _affected(status) > 0

    async def mark_archived(self, clip_id: str, archive_path: str) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE clips SET archived=TRUE, archive_path=? WHERE id=?"),
            archive_path,
            clip_id,
        )

    async def mark_gdrive_uploaded(self, clip_id: str, file_id: str) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                "UPDATE clips SET gdrive_backed_up=TRUE, gdrive_file_id=?, "
                "gdrive_uploaded_at=? WHERE id=?"
            ),
            file_id,
            datetime.now(UTC).isoformat(),
            clip_id,
        )

    async def delete_clip(self, clip_id: str) -> bool:
        """Remove a clip record from the database."""
        if self._pool is None:
            return False
        status = await self._pool.execute(_qm("DELETE FROM clips WHERE id=?"), clip_id)
        return _affected(status) > 0

    async def delete_clip_by_path(self, file_path: str) -> bool:
        """Remove a clip record by its file_path.

        Used by retention cleanup, which deletes files directly from the
        filesystem (glob + unlink) rather than by clip ID, so it doesn't
        leave an orphaned DB row (and orphaned analysis_results/
        analysis_queue rows) behind for a file that no longer exists.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("DELETE FROM clips WHERE file_path=?"), file_path
        )
        return _affected(status) > 0

    async def delete_clips_by_archive_path(self, archive_path: str) -> int:
        """Remove every clip record stored in one archive ZIP.

        Used by the Storage tab's "delete entire archive" action, once the
        ZIP file itself and any Google Drive backups have already been
        handled by the caller (media_server.py's _handle_delete_archive) —
        this is just the bulk DB-side cleanup. Returns the number of rows
        removed.
        """
        if self._pool is None:
            return 0
        status = await self._pool.execute(
            _qm("DELETE FROM clips WHERE archived=TRUE AND archive_path=?"),
            archive_path,
        )
        return _affected(status)

    @staticmethod
    async def _migrate_camera_baselines(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        rows = await conn.fetch(
            _qm(
                "SELECT hour, count FROM camera_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        await conn.execute(
            _qm(
                "DELETE FROM camera_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        counts: dict[int, int] = {}
        for row in rows:
            hour = int(row["hour"])
            counts[hour] = counts.get(hour, 0) + int(row["count"] or 0)
        for hour, count in counts.items():
            await conn.execute(
                _qm(
                    "INSERT INTO camera_baselines (camera, hour, count) "
                    "VALUES (?, ?, ?)"
                ),
                new_name,
                hour,
                count,
            )
        return True

    @staticmethod
    async def _migrate_camera_duration_stats(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        rows = await conn.fetch(
            _qm(
                "SELECT avg_duration, sample_count FROM camera_duration_stats "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        await conn.execute(
            _qm(
                "DELETE FROM camera_duration_stats "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        sample_count = sum(int(row["sample_count"] or 0) for row in rows)
        if sample_count:
            weighted_duration = sum(
                float(row["avg_duration"] or 0) * int(row["sample_count"] or 0)
                for row in rows
            )
            await conn.execute(
                _qm(
                    "INSERT INTO camera_duration_stats "
                    "(camera, avg_duration, sample_count) VALUES (?, ?, ?)"
                ),
                new_name,
                weighted_duration / sample_count,
                sample_count,
            )
        return True

    @staticmethod
    async def _migrate_camera_scene_baseline(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        rows = await conn.fetch(
            _qm(
                "SELECT thumbnail, sample_count, updated_at, "
                "consecutive_deviation_count FROM camera_scene_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?) "
                "ORDER BY sample_count DESC LIMIT 1"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        scene = rows[0]
        await conn.execute(
            _qm(
                "DELETE FROM camera_scene_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        await conn.execute(
            _qm(
                "INSERT INTO camera_scene_baselines "
                "(camera, thumbnail, sample_count, updated_at, "
                "consecutive_deviation_count) VALUES (?, ?, ?, ?, ?)"
            ),
            new_name,
            scene["thumbnail"],
            scene["sample_count"],
            scene["updated_at"],
            scene["consecutive_deviation_count"],
        )
        return True

    @staticmethod
    async def _migrate_camera_vehicle_signature(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        """Carry a learned protected-vehicle signature across a rename.

        Same camera, same car, same parking spot — only the name changed, so
        throwing the signature away would restart four clips' worth of
        learning for nothing. Keyed on camera like camera_scene_baselines,
        so the same delete-then-insert dance is needed to avoid a primary
        key collision when a row already exists under the new name.
        """
        rows = await conn.fetch(
            _qm(
                "SELECT box, histogram, sample_count, updated_at "
                "FROM camera_vehicle_signatures "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?) "
                "ORDER BY sample_count DESC LIMIT 1"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        signature = rows[0]
        await conn.execute(
            _qm(
                "DELETE FROM camera_vehicle_signatures "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        await conn.execute(
            _qm(
                "INSERT INTO camera_vehicle_signatures "
                "(camera, box, histogram, sample_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            new_name,
            signature["box"],
            signature["histogram"],
            signature["sample_count"],
            signature["updated_at"],
        )
        return True

    @staticmethod
    async def _rename_camera_rows(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        changed = False
        for table in (
            "clips",
            "analysis_results",
            "analysis_queue",
            "gdrive_upload_queue",
            "analysis_feedback",
            "face_recognition_feedback",
            "battery_history",
            "security_events",
        ):
            status = await conn.execute(
                _qm(f"UPDATE {table} SET camera=? WHERE LOWER(camera)=LOWER(?)"),
                new_name,
                old_name,
            )
            changed = changed or _affected(status) > 0
        return changed

    async def rename_camera(self, old_name: str, new_name: str) -> bool:
        """Migrate all persisted camera-keyed state after a Blink rename."""
        if self._pool is None or not old_name or not new_name:
            return False
        if old_name == new_name:
            return False

        async with self._pool.acquire() as conn, conn.transaction():
            changed = await self._migrate_camera_baselines(conn, old_name, new_name)
            changed = (
                await self._migrate_camera_duration_stats(conn, old_name, new_name)
            ) or changed
            changed = (
                await self._migrate_camera_scene_baseline(conn, old_name, new_name)
            ) or changed
            changed = (
                await self._migrate_camera_vehicle_signature(conn, old_name, new_name)
            ) or changed
            changed = (
                await self._rename_camera_rows(conn, old_name, new_name)
            ) or changed

        if changed:
            _LOGGER.info(
                "Migrated persisted state for camera %r -> %r", old_name, new_name
            )
        return changed

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    async def get_clip(self, clip_id: str) -> dict[str, Any] | None:
        """Return a single clip record or None."""
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(_qm("SELECT * FROM clips WHERE id=?"), clip_id)
        return _row_to_dict(row) if row else None

    async def get_clips_missing_duration(self) -> list[dict[str, Any]]:
        """Clips with no known duration, for app.py's startup backfill.

        Covers both a clip whose Blink API response never included a real
        duration (some accounts/clip types consistently don't) and a
        pre-5.0.1 clip downloaded before that fallback existed — either
        way, the file itself is the source of truth once probed. Returns
        just id/file_path, the only two fields the backfill needs.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT id, file_path FROM clips WHERE duration <= 0"
        )
        return [dict(row) for row in rows]

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
        archive_path: str | None = None,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
        notified_only: bool = False,
        recognized_only: bool = False,
        min_confidence: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Query clips with optional filters and sort order.

        sort values: "newest" | "oldest" | "camera" | "size" | "duration"

        Every returned clip includes a ``notified`` boolean (True if the
        clip's *most recent* AI analysis result was/would be suspicious at
        *min_confidence* or higher — the same gate ``AnalysisQueue`` uses to
        decide whether to dispatch a notification, including its
        ``risk_override_applied`` exemption: a clip flagged by the
        deterministic risk score rather than by the model skips the
        confidence threshold there, so the badge has to skip it here too or
        the two disagree about a notification that was actually sent). Set
        *notified_only* to restrict results to just those clips. Set *recognized_only* to
        restrict to clips with ``face_recognized`` true (see below) —
        parameter-free, unlike *notified_only*/*min_confidence*, since
        ``approved_faces_seen`` has no equivalent threshold.

        Deliberately scoped to only the latest ``analysis_results`` row per
        clip (matching :meth:`get_analysis_for_clip`'s own "most recent
        wins" semantics) rather than "any row ever" — a clip re-analyzed
        after its first (suspicious) pass, e.g. via the Library's
        "Re-analyze" button or a later fix that changes the verdict, would
        otherwise show the 🔔 notified badge forever even once the current
        verdict is not-suspicious, since `add_analysis_result` always
        inserts a new row rather than replacing the old one.

        Also includes a ``face_recognized`` boolean, True when the clip's
        most recent analysis unambiguously matched only approved,
        locally-enrolled household member(s) with no stranger or
        unrecognized face anywhere in the sampled frames (``analyzer.py``'s
        ``approved_faces_seen``, the same all-or-nothing condition
        ``_face_bypass_applies`` itself checks — see its docstring — just
        recorded regardless of whether the clip also happened to need its
        suspicious flag cleared). Deliberately *not* the narrower
        ``face_bypass_applied`` (whether the bypass actually fired): most
        real matches are a household member's own routine, already
        non-suspicious visit, which never reaches the bypass check at all
        (see analyzer.py's `if is_suspicious and ...` gating) and would
        otherwise never show any recognition signal at all despite a clear
        match. `face_bypass_applied` itself is intentionally left alone
        for `get_face_bypass_stats`'s narrower audit purpose (confirming
        the *safety* bypass fires for the right people) — same latest-row
        scoping here, so a clip that no longer matches after a later
        re-analysis doesn't keep showing the badge forever either.
        """
        if self._pool is None:
            return []

        notified_exists = (
            "EXISTS (SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
            "AND ar.is_suspicious "
            "AND (ar.confidence >= ? OR ar.risk_override_applied) "
            "AND ar.analyzed_at = (SELECT MAX(ar2.analyzed_at) FROM analysis_results ar2 "
            "WHERE ar2.clip_id = clips.id))"
        )
        face_recognized_exists = (
            "EXISTS (SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
            "AND ar.approved_faces_seen "
            "AND ar.analyzed_at = (SELECT MAX(ar2.analyzed_at) FROM analysis_results ar2 "
            "WHERE ar2.clip_id = clips.id))"
        )

        where: list[str] = ["archived = ?"]
        params: list[Any] = [archived]

        if camera and camera != "all":
            where.append(_WHERE_CAMERA)
            params.append(camera)
        if archive_path:
            where.append("archive_path = ?")
            params.append(archive_path)
        if since:
            where.append(_WHERE_SINCE)
            params.append(since)
        if until:
            where.append(_WHERE_UNTIL)
            params.append(until)
        if starred is not None:
            where.append("starred = ?")
            params.append(starred)
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
        if recognized_only:
            where.append(face_recognized_exists)

        _sort_map = {
            "newest": "timestamp DESC",
            "oldest": "timestamp ASC",
            "camera": "LOWER(camera) ASC, timestamp DESC",
            "size": "size_bytes DESC",
            "duration": "duration DESC",
        }
        order = _sort_map.get(sort, "timestamp DESC")

        sql = (
            f"SELECT *, {notified_exists} AS notified, "
            f"{face_recognized_exists} AS face_recognized FROM clips "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        params = [min_confidence, *params, limit, offset]

        rows = await self._pool.fetch(_qm(sql), *params)
        return [_row_to_dict(r) for r in rows]

    async def get_archive_groups(
        self,
        camera: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one row per distinct ZIP archive: path, clip count, total
        size, and the most recent clip timestamp in it (for sort order).

        The Storage tab's Archived Clips list is grouped by archive rather
        than shown as a flat per-clip list — a large library can have
        thousands of archived clips but only a few dozen distinct monthly
        ZIPs, so this is cheap to fetch in full (no pagination needed here;
        the frontend paginates over this already-small result) and gives an
        accurate page count, unlike paginating the flat clip list itself.
        *since*/*until* filter individual clips before grouping. This keeps
        archive counts, sizes, and expanded archive pages consistent when a
        monthly ZIP contains clips both inside and outside the selected range.
        """
        if self._pool is None:
            return []

        where = ["archived = TRUE", "archive_path != ''"]
        params: list[Any] = []
        if camera and camera != "all":
            where.append(_WHERE_CAMERA)
            params.append(camera)

        if since:
            where.append(_WHERE_SINCE)
            params.append(since)
        if until:
            where.append(_WHERE_UNTIL)
            params.append(until)

        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    archive_path,
                    COUNT(*) AS clip_count,
                    COALESCE(SUM(size_bytes), 0) AS total_size,
                    MAX(timestamp) AS latest_timestamp
                FROM clips
                WHERE {" AND ".join(where)}
                GROUP BY archive_path
                ORDER BY latest_timestamp DESC
                """
            ),
            *params,
        )
        return [dict(r) for r in rows]

    async def get_archive_clips(
        self,
        archive_path: str,
        camera: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one page of clips from an archive and its total count."""
        if self._pool is None:
            return {"items": [], "total": 0}

        clips = await self.get_clips(
            camera=camera,
            archived=True,
            archive_path=archive_path,
            since=since,
            until=until,
            sort="newest",
            limit=limit,
            offset=offset,
        )

        where = ["archived = TRUE", "archive_path = ?"]
        params: list[Any] = [archive_path]
        if camera and camera != "all":
            where.append(_WHERE_CAMERA)
            params.append(camera)
        if since:
            where.append(_WHERE_SINCE)
            params.append(since)
        if until:
            where.append(_WHERE_UNTIL)
            params.append(until)
        total = await self._pool.fetchval(
            _qm(f"SELECT COUNT(*) FROM clips WHERE {' AND '.join(where)}"),
            *params,
        )
        return {"items": clips, "total": int(total or 0)}

    async def get_clips_by_archive_path(
        self, archive_path: str
    ) -> list[dict[str, Any]]:
        """Return every clip record stored in one archive ZIP, unpaginated.

        Used when deleting an entire archive — the caller needs each
        clip's ``gdrive_file_id`` to also remove its Google Drive backup,
        which the paginated/filtered :meth:`get_archive_clips` (built for
        the Storage tab's display) isn't a good fit for.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm("SELECT * FROM clips WHERE archived=TRUE AND archive_path=?"),
            archive_path,
        )
        return [_row_to_dict(r) for r in rows]

    async def get_all_file_paths(self) -> set[str]:
        """Return the set of all ``file_path`` values currently indexed."""
        if self._pool is None:
            return set()
        rows = await self._pool.fetch("SELECT file_path FROM clips")
        return {r["file_path"] for r in rows}

    async def get_clips_to_archive(self, older_than_days: int) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM clips WHERE archived=FALSE AND timestamp < ? "
                "ORDER BY timestamp"
            ),
            cutoff,
        )
        return [_row_to_dict(r) for r in rows]

    async def get_archived_clip_records(self) -> list[dict[str, Any]]:
        """Return ``{id, archive_path, camera, file_path}`` for every clip
        marked archived.

        Used by :meth:`ClipArchiver.prune_orphaned_archives` to find rows
        whose ZIP no longer exists on disk, is unreadable, or (for clips
        archived before a corrupted-ZIP bug was fixed) no longer actually
        contains that clip's member even though the ZIP file itself is
        still present — ``camera``/``file_path`` are what's needed to
        reconstruct the exact archive member name (``_archive_month``'s
        ``f"{camera}/{Path(file_path).name}"``) to check for. ``file_path``
        still holds the pre-archive path even after the source file is
        deleted — ``mark_archived`` never touches that column.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT id, archive_path, camera, file_path FROM clips "
                "WHERE archived=TRUE"
            )
        )
        return [_row_to_dict(r) for r in rows]

    async def get_clips_pending_gdrive_backup(
        self, include_unarchived: bool
    ) -> list[dict[str, Any]]:
        """Clips not yet backed up to Google Drive, for the "Back Up Existing
        Clips Now" action. Always includes archived clips (both backup
        policies cover them); *include_unarchived* additionally includes
        regular clips too, matching the "all_clips" policy.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM clips WHERE gdrive_backed_up=FALSE "
                "AND (archived=TRUE OR ?=TRUE) ORDER BY timestamp"
            ),
            include_unarchived,
        )
        return [_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics for the library."""
        if self._pool is None:
            return {}

        today_start, today_end = _local_day_bounds(0)
        yesterday_start, yesterday_end = _local_day_bounds(1)
        week_start, _week_end = _local_day_bounds(7)

        queries: dict[str, tuple[str, tuple[Any, ...]]] = {
            "total_count": ("SELECT COUNT(*) FROM clips WHERE archived=FALSE", ()),
            "starred_count": ("SELECT COUNT(*) FROM clips WHERE starred=TRUE", ()),
            "archived_count": ("SELECT COUNT(*) FROM clips WHERE archived=TRUE", ()),
            "total_size_bytes": (
                "SELECT COALESCE(SUM(size_bytes),0) FROM clips",
                (),
            ),
            "today_count": (
                "SELECT COUNT(*) FROM clips WHERE timestamp >= ? AND timestamp < ?",
                (today_start, today_end),
            ),
            "yesterday_count": (
                "SELECT COUNT(*) FROM clips WHERE timestamp >= ? AND timestamp < ?",
                (yesterday_start, yesterday_end),
            ),
            "week_count": (
                "SELECT COUNT(*) FROM clips WHERE timestamp >= ?",
                (week_start,),
            ),
            # Same "latest analysis row wins" + approved_faces_seen semantics
            # as get_clips()'s face_recognized column (see its docstring) —
            # a clip that no longer matches after a later re-analysis isn't
            # counted here either.
            "recognized_count": (
                (
                    "SELECT COUNT(*) FROM clips WHERE archived=FALSE AND EXISTS ("
                    "SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
                    "AND ar.approved_faces_seen "
                    "AND ar.analyzed_at = (SELECT MAX(ar2.analyzed_at) "
                    "FROM analysis_results ar2 WHERE ar2.clip_id = clips.id))"
                ),
                (),
            ),
        }

        results: dict[str, Any] = {}
        for key, (sql, params) in queries.items():
            results[key] = await self._pool.fetchval(_qm(sql), *params) or 0

        return results

    async def get_camera_stats(self) -> list[dict[str, Any]]:
        """Return per-camera clip counts, sizes, and activity."""
        if self._pool is None:
            return []

        today_start, today_end = _local_day_bounds(0)
        week_start, _week_end = _local_day_bounds(7)

        rows = await self._pool.fetch(
            _qm(
                """
                SELECT
                    MIN(camera) AS camera,
                    COUNT(*) AS total,
                    COALESCE(SUM(size_bytes), 0) AS size_bytes,
                    SUM(CASE WHEN timestamp >= ? AND timestamp < ? THEN 1 ELSE 0 END) AS today,
                    SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS this_week,
                    MAX(timestamp) AS last_seen
                FROM clips
                WHERE archived=FALSE
                GROUP BY LOWER(camera)
                ORDER BY total DESC
                """
            ),
            today_start,
            today_end,
            week_start,
        )
        return [dict(r) for r in rows]

    async def get_distinct_tags(self) -> list[str]:
        """Return all unique tags used across clips (best-effort)."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT DISTINCT tags FROM clips WHERE tags != '[]' AND tags != ''"
        )
        all_tags: set[str] = set()
        for r in rows:
            try:
                all_tags.update(json.loads(r["tags"] or "[]"))
            except json.JSONDecodeError:
                pass
        return sorted(all_tags)

    async def get_activity_data(self, days: int = 7) -> list[dict[str, Any]]:
        """Return per-hour clip counts for the last *days* days.

        Each row: ``{"date": "YYYY-MM-DD", "hour": 0-23, "count": n}``, both
        in the household's local calendar day/hour (see
        :func:`_local_utc_offset_sql`) rather than the UTC ones the
        pinned-UTC session (see ``init()``) would otherwise read back —
        without this, evenings in any timezone behind UTC show up as the
        next day. Useful for rendering an activity heat-map in the UI.
        """
        if self._pool is None:
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        tz = _local_utc_offset_sql()
        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    to_char(timestamp::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') AS date,
                    EXTRACT(HOUR FROM timestamp::timestamptz AT TIME ZONE INTERVAL '{tz}')::int AS hour,
                    COUNT(*) AS count
                FROM clips
                WHERE timestamp >= ?
                GROUP BY date, hour
                ORDER BY date, hour
                """
            ),
            cutoff,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # AI Analysis
    # ------------------------------------------------------------------

    @staticmethod
    def _res_str(result: dict[str, Any], key: str) -> str:
        return str(result.get(key) or "")

    @staticmethod
    def _res_int(result: dict[str, Any], key: str) -> int:
        return int(result.get(key) or 0)

    @staticmethod
    def _res_float(result: dict[str, Any], key: str) -> float:
        return float(result.get(key) or 0.0)

    async def add_analysis_result(self, result: dict[str, Any]) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO analysis_results
                  (clip_id, camera, model, response_text, is_suspicious,
                   confidence, summary, frame_count, analysis_duration, analyzed_at,
                   tokens_prompt, tokens_completion, anomaly_score,
                   escalation_model, escalation_tokens_prompt, escalation_tokens_completion,
                   escalation_provider, prompt_text, face_bypass_applied, face_bypass_names,
                   approved_faces_seen, risk_score, severity, event_type,
                   evidence_quality, risk_override_applied)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?)
                """
            ),
            self._res_str(result, "clip_id"),
            self._res_str(result, "camera"),
            self._res_str(result, "model"),
            self._res_str(result, "response_text"),
            bool(result.get("is_suspicious")),
            self._res_float(result, "confidence"),
            self._res_str(result, "summary"),
            self._res_int(result, "frame_count"),
            self._res_float(result, "analysis_duration"),
            self._res_str(result, "analyzed_at"),
            self._res_int(result, "tokens_prompt"),
            self._res_int(result, "tokens_completion"),
            self._res_float(result, "anomaly_score"),
            self._res_str(result, "escalation_model"),
            self._res_int(result, "escalation_tokens_prompt"),
            self._res_int(result, "escalation_tokens_completion"),
            self._res_str(result, "escalation_provider"),
            self._res_str(result, "prompt_text"),
            bool(result.get("face_bypass_applied")),
            self._res_str(result, "face_bypass_names"),
            bool(result.get("approved_faces_seen")),
            self._res_float(result, "risk_score"),
            self._res_str(result, "severity") or "routine",
            self._res_str(result, "event_type"),
            self._res_float(result, "evidence_quality"),
            bool(result.get("risk_override_applied")),
        )

    async def save_analysis(self, result: AnalysisResult) -> None:
        """Persist one analysis run in full.

        The verdict row, its object detections, and its security events are
        three tables that belong together — a clip whose security events are
        left over from a previous run reads as a different, older event in
        the timeline than the verdict beside it. Every caller goes through
        here rather than remembering all three.

        Each of the three writes is individually atomic, but they are not one
        transaction: a process killed between them leaves a new verdict
        beside the previous run's events until the clip is analyzed again.
        Widening the transaction would mean threading a connection through
        three otherwise-independent public methods, which is a poor trade for
        a window this small and self-healing — noted here so the next reader
        does not have to work out whether it was considered.

        A run that examined *no* frames is the one exception. Frame
        extraction failing (the file moved, archived, or still being
        written) produces a real result row saying so — but it looked at
        nothing, so it has no evidence of its own and must not replace the
        evidence of a run that did. Without this, re-analyzing a clip whose
        file has since gone erases its detections and its whole entry from
        the Security tab's timeline.
        """
        await self.add_analysis_result(result.to_dict())
        if result.frame_count <= 0:
            return
        await self.save_detected_objects(
            result.clip_id,
            result.detected_objects,
            interval=result.detection_interval,
            frame_size=result.detection_frame_size,
        )
        await self.save_security_events(
            result.clip_id,
            result.camera,
            result.security_events,
            risk_score=result.risk_score,
            evidence_quality=result.evidence_quality,
        )

    async def get_analysis_for_clip(self, clip_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(
            _qm(
                "SELECT * FROM analysis_results WHERE clip_id=? "
                "ORDER BY analyzed_at DESC LIMIT 1"
            ),
            clip_id,
        )
        return dict(row) if row else None

    async def get_suspicious_clips(
        self, limit: int = 50, offset: int = 0, period: str | None = None
    ) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        where, params = _suspicious_clips_where(period)
        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT ar.*, c.file_path, c.timestamp AS clip_timestamp,
                       c.duration, c.size_bytes
                FROM analysis_results ar
                JOIN clips c ON c.id = ar.clip_id
                {where}
                ORDER BY ar.analyzed_at DESC
                LIMIT ? OFFSET ?
                """
            ),
            *params,
            limit,
            offset,
        )
        return [dict(r) for r in rows]

    async def count_suspicious_clips(self, period: str | None = None) -> int:
        """Total suspicious-clip count for *period* — paired with
        get_suspicious_clips() so the AI tab's activity feed can show a
        real page count instead of guessing from a single page's length."""
        if self._pool is None:
            return 0
        where, params = _suspicious_clips_where(period, table_alias="")
        return (
            await self._pool.fetchval(
                _qm(f"SELECT COUNT(*) FROM analysis_results {where}"), *params
            )
            or 0
        )

    async def get_analysis_stats(self) -> dict[str, Any]:
        if self._pool is None:
            return {}
        today_start, today_end = _local_day_bounds(0)
        queries: dict[str, tuple[str, tuple[Any, ...]]] = {
            "total_analyzed": ("SELECT COUNT(*) FROM analysis_results", ()),
            "suspicious_count": (
                "SELECT COUNT(*) FROM analysis_results WHERE is_suspicious",
                (),
            ),
            "total_frames_analyzed": (
                "SELECT COALESCE(SUM(frame_count),0) FROM analysis_results",
                (),
            ),
            "frames_analyzed_today": (
                (
                    "SELECT COALESCE(SUM(frame_count),0) FROM analysis_results "
                    "WHERE analyzed_at >= ? AND analyzed_at < ?"
                ),
                (today_start, today_end),
            ),
        }
        results: dict[str, Any] = {}
        for key, (sql, params) in queries.items():
            results[key] = await self._pool.fetchval(_qm(sql), *params) or 0

        results["last_analysis"] = await self._pool.fetchval(
            "SELECT analyzed_at FROM analysis_results ORDER BY analyzed_at DESC LIMIT 1"
        )
        return results

    async def get_face_bypass_stats(self, recent_limit: int = 20) -> dict[str, Any]:
        """Auditable summary of face-recognition suspicious-flag bypasses.

        Backs the Biometrics tab's bypass activity card, which exists so a
        household member can confirm the bypass is firing for the right
        people (and catch it if it's ever wrong) instead of trusting it
        blindly — see analyzer.py's face-bypass-gating docstrings.
        """
        if self._pool is None:
            return {"total_bypassed": 0, "by_name": [], "recent": []}
        total = (
            await self._pool.fetchval(
                "SELECT COUNT(*) FROM analysis_results WHERE face_bypass_applied"
            )
            or 0
        )
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, face_bypass_names, analyzed_at
                FROM analysis_results
                WHERE face_bypass_applied
                ORDER BY analyzed_at DESC
                LIMIT ?
                """
            ),
            recent_limit,
        )
        recent = [dict(r) for r in rows]

        by_name: dict[str, int] = {}
        for row in recent:
            for name in str(row.get("face_bypass_names", "")).split(", "):
                if name:
                    by_name[name] = by_name.get(name, 0) + 1
        # The per-name breakdown above only covers `recent` (bounded, cheap)
        # rather than a full-table GROUP BY — good enough for "which names
        # are showing up lately", not meant to be an exact lifetime tally.

        return {
            "total_bypassed": total,
            "by_name": [
                {"name": name, "count": count}
                for name, count in sorted(by_name.items(), key=lambda kv: -kv[1])
            ],
            "recent": recent,
        }

    async def add_face_recognition_feedback(
        self,
        clip_id: str,
        camera: str,
        report_type: str,
        note: str = "",
        person_name: str = "",
    ) -> None:
        """Record a human report that face recognition got a clip wrong.

        *report_type* is ``"false_positive"`` (a face was matched/bypassed
        that shouldn't have been) or ``"false_negative"`` (an enrolled
        person was present but not recognized/bypassed). *person_name*
        identifies who the report is about — for a false positive it's
        whoever the (wrong) bypass matched, for a false negative it's
        whichever enrolled person the reporter says was missed, or '' if
        unknown/not selected. Pure audit trail — see
        face_recognition_feedback's own schema comment for why this
        deliberately doesn't feed any automatic adjustment; person_name
        only makes the trail more actionable for a human reviewing it
        (e.g. "re-enroll this person with clearer photos").
        """
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO face_recognition_feedback
                  (clip_id, camera, report_type, note, person_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """
            ),
            clip_id,
            camera,
            report_type,
            note,
            person_name,
            datetime.now(UTC).isoformat(),
        )

    async def get_face_recognition_feedback(
        self, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Most recent face-recognition accuracy reports, for the
        Biometrics tab's activity card — see add_face_recognition_feedback."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, report_type, note, person_name, created_at
                FROM face_recognition_feedback
                ORDER BY created_at DESC
                LIMIT ?
                """
            ),
            limit,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Detected objects (see vision.py's ObjectDetector)
    # ------------------------------------------------------------------

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

        ``count`` is **how many of that label were in frame at once at the
        peak**, not how many boxes were stored: the detector runs over
        every sampled frame, so one car parked through a twelve-frame clip
        contributes twelve rows. Counting those rows is what made the clip
        modal's chips read "33 cars" for a driveway with three in it — the
        per-frame peak is the honest answer to "how many were there", and
        can never be inflated by the same object simply being seen again.

        Distinct ``track_id`` is deliberately *not* used for this even
        though the column exists: this pipeline hands the tracker frames
        seconds apart rather than consecutive video (see ObjectDetector's
        own note in vision.py), so ids are best-effort and an object that
        picks up a fresh id each frame would put the inflated count
        straight back.

        ``detections`` keeps the raw box total, which is still worth
        showing as supporting detail — it says how much evidence the count
        rests on.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT label,
                       MAX(per_frame)::int AS count,
                       SUM(per_frame)::int AS detections,
                       MAX(frame_best) AS max_confidence
                FROM (
                    SELECT label,
                           frame_index,
                           COUNT(*) AS per_frame,
                           MAX(confidence) AS frame_best
                    FROM detected_objects
                    WHERE clip_id=?
                    GROUP BY label, frame_index
                ) per_frame_counts
                GROUP BY label
                ORDER BY count DESC, detections DESC, label ASC
                """
            ),
            clip_id,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Security events (see blink_downloader/security)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Learned protected-vehicle signatures (see security/vehicles.py)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Battery history (see battery_monitor.py)
    # ------------------------------------------------------------------

    async def add_battery_reading(
        self,
        camera: str,
        battery_state: str,
        battery_level: int | None,
        battery_voltage: int | None,
    ) -> bool:
        """Record a battery reading if it differs from this camera's last one.

        Dedupes on ``battery_state`` alone (not level/voltage — see the
        column comment on ``battery_history`` in ``_SCHEMA``). Returns
        ``True`` only when this is a genuine transition from a
        *previously recorded, different* state — the very first reading
        ever seen for a camera is still inserted (so the Status tab isn't
        empty), but returns ``False``, since there is nothing to have
        transitioned from. Callers use this return value to decide whether
        to fire a low-battery alert, so a fresh install or a newly
        discovered camera that already happens to be low must not alert on
        its first-ever reading.
        """
        if self._pool is None:
            return False
        last_state = await self._pool.fetchval(
            _qm(
                "SELECT battery_state FROM battery_history "
                "WHERE camera=? ORDER BY id DESC LIMIT 1"
            ),
            camera,
        )
        if last_state == battery_state:
            return False
        await self._pool.execute(
            _qm(
                """
                INSERT INTO battery_history
                  (camera, battery_state, battery_level, battery_voltage, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """
            ),
            camera,
            battery_state,
            battery_level,
            battery_voltage,
            datetime.now(UTC).isoformat(),
        )
        return last_state is not None

    async def reset_battery_history(self, camera: str) -> None:
        """Drop readings when a physical camera is replaced under the same name."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("DELETE FROM battery_history WHERE LOWER(camera)=LOWER(?)"),
            camera,
        )

    async def reset_camera_baselines(self, camera: str) -> None:
        """Drop AI baseline state when a physical camera is replaced under the
        same name.

        A replacement's field of view, mount angle, and typical clip
        duration/motion frequency can all differ from the unit it replaced,
        so keeping the old baselines would compare the new hardware against
        a reference that no longer describes what it actually sees. Unlike
        ``rename_camera``'s baseline migration (same camera, new name --
        the data is still valid and worth carrying forward), a replacement
        means the *data itself* is stale, so it's dropped rather than kept.
        """
        if self._pool is None:
            return
        for table in (
            "camera_baselines",
            "camera_duration_stats",
            "camera_scene_baselines",
            # A replacement unit's field of view and mount angle differ, so
            # where the protected vehicle "normally sits" in frame is stale
            # by exactly the reasoning that drops the scene baseline above —
            # and a signature pointing at the wrong part of the new frame
            # would keep confirming its own mistake.
            "camera_vehicle_signatures",
        ):
            await self._pool.execute(
                _qm(f"DELETE FROM {table} WHERE LOWER(camera)=LOWER(?)"),
                camera,
            )

    async def get_battery_history(
        self, camera: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Most recent battery state-change rows for one camera, newest
        first — powers the Status tab's battery history modal."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT camera, battery_state, battery_level, battery_voltage, recorded_at
                FROM battery_history
                WHERE LOWER(camera) = LOWER(?)
                ORDER BY id DESC
                LIMIT ?
                """
            ),
            camera,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_latest_battery_state(self) -> list[dict[str, Any]]:
        """Current battery state for every camera with a recorded reading —
        one row per camera — powers the Status tab's battery strip."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (camera)
                camera, battery_state, battery_level, battery_voltage, recorded_at
            FROM battery_history
            ORDER BY camera, id DESC
            """
        )
        return [dict(r) for r in rows]

    async def _get_ai_usage_reset_at(self) -> str:
        """Return the AI Usage "Clear Stats" cutoff timestamp, or '' if never reset."""
        assert self._pool is not None
        value = await self._pool.fetchval(
            "SELECT reset_at FROM ai_usage_reset WHERE id = 1"
        )
        return str(value) if value else ""

    async def clear_ai_usage_stats(self) -> None:
        """Reset the AI Usage tab's token/cost/escalation counters.

        Only bumps the cutoff timestamp used by :meth:`get_token_usage_stats`
        — per-clip ``analysis_results`` rows (is_suspicious, summary, etc.)
        are left intact since the Suspicious Clips list and clip detail view
        still depend on that history.
        """
        if self._pool is None:
            return
        reset_at = datetime.now(UTC).isoformat()
        await self._pool.execute(
            _qm(
                "INSERT INTO ai_usage_reset (id, reset_at) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET reset_at = excluded.reset_at"
            ),
            reset_at,
        )

    async def get_token_usage_stats(self) -> dict[str, Any]:
        """Return per-model token usage totals for the AI Usage tab.

        Escalation-model usage (see ``analysis_results.escalation_model``/
        ``escalation_provider``) is broken out into its own ``by_model``
        entries — tagged ``"escalated": True`` — rather than folded into the
        tier-1 model's row, so a configured ``ai_escalation_provider``/
        ``ai_escalation_model`` (any provider, not just OpenAI) gets its own
        accurately-priced line instead of silently inflating tier-1's totals.

        Escalation rows are grouped by ``escalation_model`` alone (not also
        ``escalation_provider``), matching how the tier-1 query groups by
        ``model`` alone. Grouping by both columns used to split one model
        into two duplicate-looking rows in the UI whenever
        ``escalation_provider`` differed across rows for the same model.
        ``MAX(escalation_provider)`` picks a representative non-empty
        provider for the row's label when one is available.
        """
        empty: dict[str, Any] = {
            "total_analyses": 0,
            "total_tokens_prompt": 0,
            "total_tokens_completion": 0,
            "total_tokens": 0,
            "total_escalations": 0,
            "total_escalation_tokens": 0,
            "by_model": [],
        }
        if self._pool is None:
            return empty

        reset_at = await self._get_ai_usage_reset_at()
        since_clause = "WHERE analyzed_at > ?" if reset_at else ""
        since_params: tuple[str, ...] = (reset_at,) if reset_at else ()

        primary_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    model,
                    COUNT(*)                             AS analyses,
                    COALESCE(SUM(tokens_prompt), 0)      AS tokens_prompt,
                    COALESCE(SUM(tokens_completion), 0)  AS tokens_completion
                FROM analysis_results
                {since_clause}
                GROUP BY model
                ORDER BY analyses DESC
                """
            ),
            *since_params,
        )

        escalation_since_clause = (
            "WHERE escalation_model != '' AND analyzed_at > ?"
            if reset_at
            else "WHERE escalation_model != ''"
        )
        escalation_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    escalation_model                              AS model,
                    MAX(escalation_provider)                       AS provider,
                    COUNT(*)                                      AS analyses,
                    COALESCE(SUM(escalation_tokens_prompt), 0)     AS tokens_prompt,
                    COALESCE(SUM(escalation_tokens_completion), 0) AS tokens_completion
                FROM analysis_results
                {escalation_since_clause}
                GROUP BY escalation_model
                ORDER BY analyses DESC
                """
            ),
            *since_params,
        )

        by_model: list[dict[str, Any]] = []
        for r in primary_rows:
            d = dict(r)
            d["escalated"] = False
            by_model.append(d)
        for r in escalation_rows:
            d = dict(r)
            d["escalated"] = True
            by_model.append(d)

        total_prompt = sum(int(m["tokens_prompt"]) for m in by_model)
        total_completion = sum(int(m["tokens_completion"]) for m in by_model)
        total_analyses = sum(int(m["analyses"]) for m in primary_rows)
        total_escalations = sum(int(m["analyses"]) for m in escalation_rows)
        total_escalation_tokens = sum(
            int(m["tokens_prompt"]) + int(m["tokens_completion"])
            for m in escalation_rows
        )

        return {
            "total_analyses": total_analyses,
            "total_tokens_prompt": total_prompt,
            "total_tokens_completion": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_escalations": total_escalations,
            "total_escalation_tokens": total_escalation_tokens,
            "by_model": by_model,
        }

    async def get_daily_usage_stats(self, days: int = 14) -> list[dict[str, Any]]:
        """Return per-day, per-model token totals for the AI Usage tab's daily history.

        Buckets ``analysis_results`` by local calendar day (see
        :func:`_local_utc_offset_sql`), from ``analyzed_at``, over the
        trailing *days* days, respecting the same "Clear Stats" cutoff as
        :meth:`get_token_usage_stats`. One row per ``(day, model)`` pair —
        tier-1 and escalation usage are kept as separate rows (tagged via
        ``"escalated"``) rather than merged, so callers can price each
        model's tokens at that model's own rate before summing to a per-day
        total (mirrors how :meth:`get_token_usage_stats` prices ``by_model``
        rows). Days with no analysis activity are simply absent — this method
        does not zero-fill the range, keeping the result small.
        """
        if self._pool is None:
            return []

        reset_at = await self._get_ai_usage_reset_at()
        tz = _local_utc_offset_sql()
        cutoff = (
            (datetime.now(UTC).astimezone() - timedelta(days=days - 1))
            .date()
            .isoformat()
        )

        # Compared as text (to_char output), not cast to ::date — casting the
        # column to a typed date makes PostgreSQL's parameter-type inference
        # expect a native `date` object for `?` too, rejecting the plain ISO
        # date *string* `cutoff` actually passed in.
        conditions = [
            f"to_char(analyzed_at::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') >= ?"
        ]
        params: list[str] = [cutoff]
        if reset_at:
            conditions.append("analyzed_at > ?")
            params.append(reset_at)
        where = "WHERE " + " AND ".join(conditions)

        primary_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT to_char(analyzed_at::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') AS day, model,
                       COUNT(*)                            AS analyses,
                       COALESCE(SUM(tokens_prompt), 0)      AS tokens_prompt,
                       COALESCE(SUM(tokens_completion), 0)  AS tokens_completion
                FROM analysis_results
                {where}
                GROUP BY day, model
                """
            ),
            *params,
        )

        escalation_rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT to_char(analyzed_at::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') AS day,
                       escalation_model AS model,
                       COUNT(*)                                       AS analyses,
                       COALESCE(SUM(escalation_tokens_prompt), 0)     AS tokens_prompt,
                       COALESCE(SUM(escalation_tokens_completion), 0) AS tokens_completion
                FROM analysis_results
                {where} AND escalation_model != ''
                GROUP BY day, escalation_model
                """
            ),
            *params,
        )

        daily: list[dict[str, Any]] = []
        for r in primary_rows:
            d = dict(r)
            d["escalated"] = False
            daily.append(d)
        for r in escalation_rows:
            d = dict(r)
            d["escalated"] = True
            daily.append(d)

        daily.sort(key=lambda d: str(d["day"]), reverse=True)
        return daily

    # ------------------------------------------------------------------
    # Adaptive Learning (human feedback on AI verdicts — "smart brain")
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Scene Baseline (per-camera visual "smart brain" learning)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Analysis Queue
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Google Drive Upload Queue (see gdrive_client.py/gdrive_queue.py)
    # ------------------------------------------------------------------

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

    async def get_failed_gdrive_uploads(self, limit: int = 50) -> list[dict[str, Any]]:
        """Failed upload rows with their error message, newest-failure
        first — powers the Storage tab's failed-uploads list."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, clip_path, error_message, completed_at
                FROM gdrive_upload_queue
                WHERE status='failed'
                ORDER BY completed_at DESC
                LIMIT ?
                """
            ),
            limit,
        )
        return [dict(r) for r in rows]

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

    # ------------------------------------------------------------------
    # Local-only face enrollment (see vision.py, ai_face_recognition_enabled)
    # ------------------------------------------------------------------

    async def add_face_enrollment(
        self, name: str, embedding: list[float], approved: bool = True
    ) -> int:
        """Store a new enrolled household member's face embedding. Returns its id.

        *approved* controls whether this person counts toward the
        suspicious-flag bypass (see analyzer.py's ``_face_bypass_applies``)
        — defaults to True so the common "add a family member" flow works
        immediately, but can be set False (or flipped later via
        :meth:`set_face_enrollment_approved`) to enroll someone for
        recognition/labeling without granting them bypass trust.
        """
        if self._pool is None:
            return 0
        new_id = await self._pool.fetchval(
            _qm(
                "INSERT INTO face_enrollments (name, embedding, created_at, approved) "
                "VALUES (?, ?, ?, ?) RETURNING id"
            ),
            name,
            json.dumps(embedding),
            datetime.now(UTC).isoformat(),
            approved,
        )
        return new_id or 0

    async def list_face_enrollments(self) -> list[dict[str, Any]]:
        """Return all enrolled household members, with embeddings decoded to lists."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT id, name, embedding, created_at, approved "
            "FROM face_enrollments ORDER BY name"
        )
        results = []
        for r in rows:
            d = dict(r)
            d["embedding"] = json.loads(d["embedding"])
            results.append(d)
        return results

    async def delete_face_enrollment(self, enrollment_id: int) -> None:
        """Remove an enrolled household member by id."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("DELETE FROM face_enrollments WHERE id=?"), enrollment_id
        )

    async def set_face_enrollment_approved(
        self, enrollment_id: int, approved: bool
    ) -> None:
        """Flip whether an enrolled member counts toward the suspicious-flag
        bypass, without deleting/re-enrolling them."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE face_enrollments SET approved=? WHERE id=?"),
            approved,
            enrollment_id,
        )

    async def rename_face_enrollment(self, enrollment_id: int, name: str) -> None:
        """Correct an enrolled member's name without delete+re-enroll."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE face_enrollments SET name=? WHERE id=?"),
            name,
            enrollment_id,
        )

    # ------------------------------------------------------------------
    # Bulk-by-name operations (ADVANCED FEATURE — multi-frame enrollment)
    #
    # Enrolling several frames of the same person (see media_server.py's
    # /api/clips/{id}/frames — pulled from a clip they choose, rather than a
    # single photo) stores one row per selected frame under the same name,
    # so recognition can match whichever angle/lighting is closest to what a
    # camera actually saw. These bulk operations let the Biometrics tab
    # manage all of a person's photos as one unit (rename/approve/delete
    # everyone at once) instead of the row-by-row id-based methods above,
    # which would otherwise leave a person's enrollments in a confusing
    # partially-approved/partially-renamed state.
    # ------------------------------------------------------------------

    async def set_face_enrollments_approved_by_name(
        self, name: str, approved: bool
    ) -> None:
        """Flip bypass-approval for every enrollment sharing *name*."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE face_enrollments SET approved=? WHERE name=?"),
            approved,
            name,
        )

    async def rename_face_enrollments_by_name(
        self, old_name: str, new_name: str
    ) -> None:
        """Rename every enrollment currently sharing *old_name*."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE face_enrollments SET name=? WHERE name=?"),
            new_name,
            old_name,
        )

    async def delete_face_enrollments_by_name(self, name: str) -> None:
        """Remove every enrollment (every enrolled photo) sharing *name*."""
        if self._pool is None:
            return
        await self._pool.execute(_qm("DELETE FROM face_enrollments WHERE name=?"), name)
