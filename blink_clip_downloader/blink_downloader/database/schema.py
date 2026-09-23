"""The DDL this add-on's PostgreSQL database is built and upgraded with.

Two strings, applied in order on every startup by
:meth:`~blink_downloader.database.ClipDatabase.init`. They are kept apart
from the queries that read these tables because they are edited on a
different occasion — a schema change, not a feature — and because getting
the split between them wrong is the one mistake here that silently breaks
upgrades rather than failing loudly. See ``_MIGRATIONS``' own comment.
"""

from __future__ import annotations

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
    risk_override_applied        BOOLEAN DEFAULT FALSE,
    -- JSON array of {label, score} from the optional audio stage (see
    -- vision/audio.py). A string rather than its own table because
    -- there are at most three per clip and nothing ever queries them
    -- individually -- unlike detected_objects, which the security
    -- layer joins against per box.
    audio_labels                 TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_analysis_clip   ON analysis_results (clip_id);
CREATE INDEX IF NOT EXISTS idx_analysis_suspicious ON analysis_results (is_suspicious);
CREATE INDEX IF NOT EXISTS idx_analysis_notified ON analysis_results (clip_id, is_suspicious, confidence);

-- Per-object detections from the optional computer-vision pipeline's
-- ObjectDetector (see vision/detection.py, ai_enhanced_detection_enabled) — powers
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
-- histogram_pipeline records which frame pipeline produced `histogram`, so a
-- future change to that pipeline can invalidate the stored vector instead of
-- silently comparing vectors measured two different ways. Nothing reads it at
-- runtime; it exists purely so _MIGRATIONS can tell already-stored rows apart
-- from freshly-written ones exactly once. See the 6.0.5 entry there.
CREATE TABLE IF NOT EXISTS camera_vehicle_signatures (
    camera             TEXT PRIMARY KEY,
    box                TEXT NOT NULL,
    histogram          TEXT NOT NULL DEFAULT '[]',
    histogram_pipeline TEXT NOT NULL DEFAULT 'raw',
    sample_count       INTEGER DEFAULT 0,
    updated_at         TEXT NOT NULL
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
    -- BaseAnalyzer.transient_error in analyzer/base.py. Left at 0 for a clip
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
-- (see vision/faces.py, ai_face_recognition_enabled). embedding is a JSON-encoded
-- list of floats (a 512-dim facenet-pytorch InceptionResnetV1 embedding) —
-- stored as TEXT rather than a native array/vector type for the same reason
-- `tags` above is TEXT: simplicity over compactness for a table that will
-- only ever hold a handful of rows. Never leaves this database. thumbnail is
-- a small JPEG of the enrolled face, so the Biometrics tab can show whose
-- photo each row is; NULL for a row enrolled before 6.0.7, which only ever
-- stored the embedding. frame_width is the width of the clip frame the face
-- was captured from (NULL for an uploaded photo, and before 6.0.7): a face
-- enrolled at one size matches poorly at another, so the tab flags a photo
-- whose width no longer matches ai_face_recognition_resolution. camera is
-- the camera whose clip it came from (NULL for an uploaded photo, and
-- before 6.0.7), so a person's photos can be seen to cover each camera they
-- are recognized on; a camera rename carries it like every other table's.
CREATE TABLE IF NOT EXISTS face_enrollments (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       TEXT    NOT NULL,
    embedding  TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    approved   BOOLEAN NOT NULL DEFAULT TRUE,
    thumbnail  BYTEA,
    frame_width INTEGER,
    camera     TEXT
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
-- design in analyzer/base.py's docs explicitly warns against. So this data
-- is surfaced for a human to review and act on (e.g. re-enrolling someone
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
ALTER TABLE face_enrollments ADD COLUMN IF NOT EXISTS thumbnail BYTEA;
ALTER TABLE face_enrollments ADD COLUMN IF NOT EXISTS frame_width INTEGER;
ALTER TABLE face_enrollments ADD COLUMN IF NOT EXISTS camera TEXT;
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
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS audio_labels TEXT DEFAULT '';
ALTER TABLE detected_objects ADD COLUMN IF NOT EXISTS offset_seconds DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE detected_objects ADD COLUMN IF NOT EXISTS frame_width DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE detected_objects ADD COLUMN IF NOT EXISTS frame_height DOUBLE PRECISION DEFAULT 0.0;
-- 6.0.5: object detection and every model stage beside it now scan raw frames
-- rather than CLAHE-enhanced ones (see vision/pipeline.py's
-- _run_detection_stages), and a learned vehicle colour fingerprint is measured
-- from those same frames. The two are not interchangeable: measured on real
-- night footage, the same car's raw and enhanced fingerprints score ~0.72-0.88
-- cosine against each other, while two genuinely different cars score ~0.69 —
-- so an already-learned signature would go on judging its own vehicle by a
-- yardstick that no longer matches, and blend() only walks it back a few
-- percent per clip. Clearing the vector (and nothing else — the learned
-- parking position and sample count are measured the same way as before and
-- stay) lets the next confident sighting relearn it in one step, via blend()'s
-- own length-mismatch path.
--
-- Idempotent in three steps, and the ordering is load-bearing: the column is
-- added defaulting to 'enhanced', which is what every row predating this
-- migration is; the update then clears exactly those rows and marks them; and
-- the default finally flips to 'raw' so everything written afterwards is
-- already correct. A fresh database gets 'raw' from _SCHEMA above and is
-- matched by none of it.
ALTER TABLE camera_vehicle_signatures ADD COLUMN IF NOT EXISTS histogram_pipeline TEXT NOT NULL DEFAULT 'enhanced';
UPDATE camera_vehicle_signatures SET histogram = '[]', histogram_pipeline = 'raw' WHERE histogram_pipeline <> 'raw';
ALTER TABLE camera_vehicle_signatures ALTER COLUMN histogram_pipeline SET DEFAULT 'raw';
"""
