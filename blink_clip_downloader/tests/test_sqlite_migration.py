"""Tests for sqlite_migration.migrate_legacy_sqlite."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from blink_downloader.database import ClipDatabase
from blink_downloader.sqlite_migration import migrate_legacy_sqlite

_MODERN_SCHEMA = """
CREATE TABLE clips (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, file_path TEXT NOT NULL,
    timestamp TEXT NOT NULL, size_bytes INTEGER DEFAULT 0,
    duration INTEGER DEFAULT 0, source TEXT DEFAULT '',
    network_id INTEGER DEFAULT 0, starred INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]', downloaded_at TEXT NOT NULL,
    archived INTEGER DEFAULT 0, archive_path TEXT DEFAULT ''
);
CREATE TABLE analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL,
    camera TEXT NOT NULL, model TEXT NOT NULL,
    response_text TEXT DEFAULT '', is_suspicious INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.0, summary TEXT DEFAULT '',
    frame_count INTEGER DEFAULT 0, analysis_duration REAL DEFAULT 0.0,
    analyzed_at TEXT NOT NULL, tokens_prompt INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0, anomaly_score REAL DEFAULT 0.0,
    escalation_model TEXT DEFAULT '', escalation_tokens_prompt INTEGER DEFAULT 0,
    escalation_tokens_completion INTEGER DEFAULT 0,
    escalation_provider TEXT DEFAULT '', prompt_text TEXT DEFAULT '',
    face_bypass_applied INTEGER DEFAULT 0, face_bypass_names TEXT DEFAULT '',
    approved_faces_seen INTEGER DEFAULT 0
);
CREATE TABLE ai_usage_reset (id INTEGER PRIMARY KEY CHECK (id = 1), reset_at TEXT NOT NULL DEFAULT '');
"""

# A narrower, real-4.0.2-shaped schema: no starred/tags/archived on clips,
# no ai_usage_reset table at all, and analysis_results missing every column
# that arrived alongside biometrics/5.0.0 (anomaly_score, escalation_provider,
# prompt_text, the face_bypass_* trio) — face recognition didn't exist yet.
_LEGACY_402_SCHEMA = """
CREATE TABLE clips (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, file_path TEXT NOT NULL,
    timestamp TEXT NOT NULL, size_bytes INTEGER DEFAULT 0,
    duration INTEGER DEFAULT 0, source TEXT DEFAULT '',
    network_id INTEGER DEFAULT 0, downloaded_at TEXT NOT NULL
);
CREATE TABLE analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL,
    camera TEXT NOT NULL, model TEXT NOT NULL,
    response_text TEXT DEFAULT '', is_suspicious INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.0, summary TEXT DEFAULT '',
    frame_count INTEGER DEFAULT 0, analysis_duration REAL DEFAULT 0.0,
    analyzed_at TEXT NOT NULL, tokens_prompt INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0
);
"""


def _make_sqlite_db(path: Path, schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    conn.commit()
    return conn


async def test_no_file_returns_zero(db: ClipDatabase, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.db"
    assert await migrate_legacy_sqlite(db, missing) == 0


async def test_missing_clips_table_returns_zero(
    db: ClipDatabase, tmp_path: Path
) -> None:
    legacy_path = tmp_path / "unrelated.db"
    conn = _make_sqlite_db(legacy_path, "CREATE TABLE something_else (id INTEGER);")
    conn.close()
    assert await migrate_legacy_sqlite(db, legacy_path) == 0
    assert legacy_path.exists()  # untouched — nothing was imported


async def test_corrupt_file_does_not_raise(db: ClipDatabase, tmp_path: Path) -> None:
    legacy_path = tmp_path / "corrupt.db"
    legacy_path.write_bytes(b"not a sqlite file at all")
    assert await migrate_legacy_sqlite(db, legacy_path) == 0
    assert legacy_path.exists()


async def test_clips_without_an_analysis_results_table_still_imports(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """An old file can have a clips table with no analysis_results table at
    all (e.g. AI analysis was never enabled before upgrading) — clips
    should still import, just with no analysis history to bring along."""
    legacy_path = tmp_path / "clip_library.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.executescript(
        """
        CREATE TABLE clips (
            id TEXT PRIMARY KEY, camera TEXT NOT NULL, file_path TEXT NOT NULL,
            timestamp TEXT NOT NULL, size_bytes INTEGER DEFAULT 0,
            duration INTEGER DEFAULT 0, source TEXT DEFAULT '',
            network_id INTEGER DEFAULT 0, downloaded_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Driveway",
            "/share/blink-clips/c1.mp4",
            "2026-01-01T00:00:00+00:00",
            1024,
            10,
            "pir",
            1,
            "2026-01-01T00:00:05+00:00",
        ),
    )
    conn.commit()
    conn.close()

    imported = await migrate_legacy_sqlite(db, legacy_path)
    assert imported == 1

    clip = await db.get_clip("c1")
    assert clip is not None
    assert clip["source"] == "pir"
    assert await db.get_analysis_for_clip("c1") is None


async def test_empty_clips_table_returns_zero(db: ClipDatabase, tmp_path: Path) -> None:
    """A pre-5.0.0 file can exist with a clips table that has zero rows —
    e.g. an install upgraded before it ever downloaded a clip. Nothing to
    import, so this is a no-op, and the file is left alone rather than
    renamed as if a real import happened."""
    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _MODERN_SCHEMA)
    conn.commit()
    conn.close()
    assert await migrate_legacy_sqlite(db, legacy_path) == 0
    assert legacy_path.exists()


async def test_imports_clips_and_analysis_results(
    db: ClipDatabase, tmp_path: Path
) -> None:
    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _MODERN_SCHEMA)
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Driveway",
            "/share/blink-clips/c1.mp4",
            "2026-01-01T00:00:00+00:00",
            1024,
            10,
            "pir",
            1,
            1,  # starred
            '["package"]',
            "2026-01-01T00:00:05+00:00",
            0,
            "",
        ),
    )
    conn.execute(
        "INSERT INTO analysis_results "
        "(clip_id, camera, model, response_text, is_suspicious, confidence, "
        " summary, frame_count, analysis_duration, analyzed_at, tokens_prompt, "
        " tokens_completion, anomaly_score, escalation_model, "
        " escalation_tokens_prompt, escalation_tokens_completion, "
        " escalation_provider, prompt_text, face_bypass_applied, "
        " face_bypass_names, approved_faces_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Driveway",
            "llava:7b",
            "A person walks by.",
            1,
            0.82,
            "Person walking",
            3,
            1.5,
            "2026-01-01T00:00:10+00:00",
            120,
            40,
            0.3,
            "gpt-4o",
            80,
            20,
            "openai",
            "prompt text here",
            0,
            "",
            0,
        ),
    )
    conn.commit()
    conn.close()

    imported = await migrate_legacy_sqlite(db, legacy_path)
    assert imported == 1

    clip = await db.get_clip("c1")
    assert clip is not None
    assert clip["camera"] == "Driveway"
    assert clip["starred"] is True
    assert clip["tags"] == ["package"]

    analysis = await db.get_analysis_for_clip("c1")
    assert analysis is not None
    assert analysis["model"] == "llava:7b"
    assert analysis["escalation_provider"] == "openai"

    # Successful import renames the old file so this never re-runs.
    assert not legacy_path.exists()
    assert legacy_path.with_name(legacy_path.name + ".migrated").exists()


async def test_legacy_402_schema_defaults_missing_columns(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """A real pre-5.0.0 file has neither starred/tags/archived on clips nor
    any of the biometrics-era analysis_results columns — the import must
    still succeed, falling back to each column's modern default."""
    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _LEGACY_402_SCHEMA)
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Front Door",
            "/share/blink-clips/c1.mp4",
            "2026-01-01T00:00:00+00:00",
            2048,
            8,
            "pir",
            1,
            "2026-01-01T00:00:05+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO analysis_results "
        "(clip_id, camera, model, response_text, is_suspicious, confidence, "
        " summary, frame_count, analysis_duration, analyzed_at, tokens_prompt, "
        " tokens_completion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Front Door",
            "gpt-4o-mini",
            "Nothing unusual.",
            0,
            0.1,
            "Empty driveway",
            2,
            0.9,
            "2026-01-01T00:00:10+00:00",
            50,
            10,
        ),
    )
    conn.commit()
    conn.close()

    imported = await migrate_legacy_sqlite(db, legacy_path)
    assert imported == 1

    clip = await db.get_clip("c1")
    assert clip is not None
    assert clip["starred"] is False
    assert clip["tags"] == []
    assert clip["archived"] is False

    analysis = await db.get_analysis_for_clip("c1")
    assert analysis is not None
    assert analysis["escalation_provider"] == ""
    assert analysis["face_bypass_applied"] is False
    assert analysis["approved_faces_seen"] is False


async def test_merges_into_a_clip_already_reconstructed_by_library_scanner(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """The realistic case for anyone who already upgraded to 5.0.0 before
    this migration existed: library_scanner already rebuilt this exact
    clip from its bare file under a synthetic id, with none of its real
    metadata (source='', no starred/tags, downloaded_at stamped at rescan
    time). The old SQLite row for the *same file* must backfill that
    existing row in place — matched by file_path, since the ids differ —
    not get skipped just because clips already has data, and not create a
    second, duplicate row either.
    """
    await db.add_clip(
        {
            "id": "import-abc123",
            "camera": "driveway",  # reconstructed from a lowercase folder name
            "path": "/share/blink-clips/c1.mp4",
            "timestamp": "2026-01-01T00:00:00+00:00",  # derived from file mtime
            "size_bytes": 1024,
            "duration": 10,  # from a real ffprobe of the file — kept as-is
            "source": "",  # library_scanner can never know this
            "network_id": 0,
        }
    )
    # Analysis run for real against this clip since upgrading (well after
    # the old SQLite data below) — must remain linked to the same id.
    await db.add_analysis_result(
        {
            "clip_id": "import-abc123",
            "camera": "driveway",
            "model": "gpt-4o-mini",
            "response_text": "",
            "is_suspicious": False,
            "confidence": 0.1,
            "summary": "Recent re-analysis",
            "frame_count": 1,
            "analysis_duration": 0.5,
            "analyzed_at": "2026-07-16T00:00:00+00:00",
        }
    )

    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _MODERN_SCHEMA)
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "original-blink-id",
            "Driveway",
            "/share/blink-clips/c1.mp4",  # same file as the reconstructed clip
            "2025-12-31T23:59:00+00:00",  # the real, API-reported timestamp
            1024,
            10,
            "pir",
            1,
            1,  # starred
            '["package"]',
            "2025-12-31T23:59:05+00:00",  # the real download time
            0,
            "",
        ),
    )
    conn.execute(
        "INSERT INTO analysis_results "
        "(clip_id, camera, model, response_text, is_suspicious, confidence, "
        " summary, frame_count, analysis_duration, analyzed_at, tokens_prompt, "
        " tokens_completion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "original-blink-id",
            "Driveway",
            "llava:7b",
            "A person walks by.",
            1,
            0.82,
            "Person walking",
            3,
            1.5,
            "2025-12-31T23:59:10+00:00",  # the real, pre-upgrade analysis
            120,
            40,
        ),
    )
    conn.commit()
    conn.close()

    imported = await migrate_legacy_sqlite(db, legacy_path)
    assert imported == 1

    # Still only one clip for this file — no duplicate under the old id.
    assert await db.get_clip("original-blink-id") is None
    clip = await db.get_clip("import-abc123")
    assert clip is not None
    assert clip["camera"] == "Driveway"  # backfilled from the old row
    assert clip["source"] == "pir"  # the whole point of this fix
    assert clip["starred"] is True
    assert clip["tags"] == ["package"]
    assert clip["downloaded_at"] == "2025-12-31T23:59:05+00:00"
    # Untouched — the fresh reconstruction's own probe/stat is trusted.
    assert clip["duration"] == 10

    all_clips = await db.get_clips(limit=100)
    assert len(all_clips) == 1

    # Both the pre-upgrade (imported, remapped) and post-upgrade (already
    # real) analyses are present under the one preserved id.
    assert db._pool is not None  # noqa: SLF001
    history = await db._pool.fetch(  # noqa: SLF001
        "SELECT model FROM analysis_results WHERE clip_id = $1 ORDER BY analyzed_at",
        "import-abc123",
    )
    assert [row["model"] for row in history] == ["llava:7b", "gpt-4o-mini"]


async def test_orphaned_analysis_result_is_skipped(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """SQLite's own FOREIGN KEY was never enforced by default, so an old
    file can plausibly have an analysis_results row for a clip_id that no
    longer has a matching clips row — that row must be skipped rather than
    failing the whole import (Postgres *does* enforce the FK)."""
    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _MODERN_SCHEMA)
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Driveway",
            "/share/blink-clips/c1.mp4",
            "2026-01-01T00:00:00+00:00",
            1,
            1,
            "",
            0,
            0,
            "[]",
            "2026-01-01T00:00:00+00:00",
            0,
            "",
        ),
    )
    conn.execute(
        "INSERT INTO analysis_results "
        "(clip_id, camera, model, analyzed_at) VALUES (?,?,?,?)",
        ("deleted-clip", "Driveway", "llava:7b", "2026-01-01T00:00:10+00:00"),
    )
    conn.commit()
    conn.close()

    imported = await migrate_legacy_sqlite(db, legacy_path)
    assert imported == 1
    assert await db.get_analysis_for_clip("deleted-clip") is None


async def test_ai_usage_reset_marker_imported(db: ClipDatabase, tmp_path: Path) -> None:
    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _MODERN_SCHEMA)
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Driveway",
            "/share/blink-clips/c1.mp4",
            "2026-01-01T00:00:00+00:00",
            1,
            1,
            "",
            0,
            0,
            "[]",
            "2026-01-01T00:00:00+00:00",
            0,
            "",
        ),
    )
    conn.execute(
        "INSERT INTO ai_usage_reset (id, reset_at) VALUES (1, ?)",
        ("2026-02-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    assert await migrate_legacy_sqlite(db, legacy_path) == 1
    reset_at = await db._get_ai_usage_reset_at()  # noqa: SLF001
    assert reset_at == "2026-02-01T00:00:00+00:00"


async def test_rename_failure_after_successful_import_is_non_fatal(
    db: ClipDatabase, tmp_path: Path
) -> None:
    legacy_path = tmp_path / "clip_library.db"
    conn = _make_sqlite_db(legacy_path, _MODERN_SCHEMA)
    conn.execute(
        "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "Driveway",
            "/share/blink-clips/c1.mp4",
            "2026-01-01T00:00:00+00:00",
            1,
            1,
            "",
            0,
            0,
            "[]",
            "2026-01-01T00:00:00+00:00",
            0,
            "",
        ),
    )
    conn.commit()
    conn.close()

    with patch(
        "blink_downloader.sqlite_migration.Path.rename",
        side_effect=OSError("read-only filesystem"),
    ):
        imported = await migrate_legacy_sqlite(db, legacy_path)
    assert imported == 1
    assert await db.get_clip("c1") is not None
