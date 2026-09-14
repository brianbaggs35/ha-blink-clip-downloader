-- Verbatim snapshot of the v5.5.2 database schema (_SCHEMA followed by
-- _MIGRATIONS, exactly as that release applied them at startup). Used by
-- tests/test_upgrade_from_previous_release.py to build a genuine
-- previous-release database and then run this release's init() over it.
-- Do not edit: it documents what shipped, not what we would write today.


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
    approved_faces_seen          BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_analysis_clip   ON analysis_results (clip_id);
CREATE INDEX IF NOT EXISTS idx_analysis_suspicious ON analysis_results (is_suspicious);
CREATE INDEX IF NOT EXISTS idx_analysis_notified ON analysis_results (clip_id, is_suspicious, confidence);

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
    error_message TEXT    DEFAULT ''
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

