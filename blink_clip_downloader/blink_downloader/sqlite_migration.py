"""One-time import of a pre-5.0.0 SQLite clip library into PostgreSQL.

5.0.0 replaced the add-on's bundled SQLite database with a bundled
PostgreSQL server (see database.py) but never actually carried existing
data across — an upgrading install started from a genuinely empty
database, silently losing everything that only ever lived there (AI usage
stats, starred/tagged/archived status on old clips, per-clip source) even
though clip *files* on disk reappeared via library_scanner.py's unrelated
filesystem rescan. That rescan reconstructs only what's derivable from a
bare file (camera from its folder name, timestamp from its filename or
mtime) under a synthetic id — it has no way to know what actually
triggered the clip, so every reconstructed row has ``source=''``. This
module's import merges the old file's data into whatever's already
there (see database.py's import_legacy_sqlite_data), rather than assuming
an empty table, specifically so it still does real work for an install
that already went through that lossy reconstruction before this shipped —
which, in practice, is every install that already upgraded to 5.0.0.
Nothing here ever deletes the old SQLite file (only renames it, on success
— see migrate_legacy_sqlite), so this is safe to ship well after 5.0.0
itself: anyone who already upgraded still has their original data sitting
untouched at DEFAULT_LEGACY_DB_FILE.

Deliberately narrow in scope: only clips, analysis_results, and the AI
Usage tab's reset marker. face_enrollments/analysis_feedback/camera_* are
not imported — face recognition and biometrics are new in 5.0.0, so a
pre-5.0.0 file never has enrollments to lose, and per-camera learned
baselines are cheap to re-establish from scratch rather than risky to
carry across a schema that changed meaningfully more between versions than
clips/analysis_results did.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)

DEFAULT_LEGACY_DB_FILE = Path("/data/clip_library.db")


async def migrate_legacy_sqlite(
    db: ClipDatabase, sqlite_path: Path = DEFAULT_LEGACY_DB_FILE
) -> int:
    """Import clips/analysis_results/ai_usage_reset from *sqlite_path* into
    *db*, if the file exists — merging into any clips already present
    (see database.py's import_legacy_sqlite_data) rather than requiring an
    empty database, so this still does real work on an install that
    already went through library_scanner's lossy filesystem reconstruction
    before this shipped.

    Should run before library_scanner.import_existing_clips() in the
    startup sequence even so: any clip whose file hasn't been reconstructed
    yet (or won't be reconstructed again — a already-processed one isn't
    re-scanned) gets its real metadata from here first, rather than a
    later, lossier reconstruction.

    Never raises: any failure (corrupt file, permission error, unexpected
    schema) is logged and swallowed, exactly like import_existing_clips's
    own philosophy of never blocking startup over a best-effort recovery
    step. Returns the number of clips imported/backfilled (0 if nothing
    was done).
    """
    if not sqlite_path.exists():
        return 0
    try:
        legacy = await asyncio.get_running_loop().run_in_executor(
            None, _read_legacy_db, sqlite_path
        )
        if legacy is None:
            return 0
        clips, analysis_results, reset_at = legacy
        imported = await db.import_legacy_sqlite_data(clips, analysis_results, reset_at)
        if not imported:
            return 0

        _LOGGER.info(
            "Imported/backfilled %d clip(s) and %d analysis result(s) from "
            "the pre-5.0.0 SQLite library at %s",
            imported,
            len(analysis_results),
            sqlite_path,
        )
        try:
            sqlite_path.rename(sqlite_path.with_name(sqlite_path.name + ".migrated"))
        except OSError as exc:
            _LOGGER.warning(
                "Imported the legacy SQLite library but could not rename "
                "%s afterward (%s) — harmless, this just means the import "
                "is checked again (and immediately no-ops, since clips is "
                "no longer empty) on the next restart",
                sqlite_path,
                exc,
            )
        return imported
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Failed to import legacy SQLite library at %s — continuing "
            "with the existing (empty or partial) library; this will be "
            "retried on the next restart",
            sqlite_path,
        )
        return 0


def _read_legacy_db(
    sqlite_path: Path,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], str | None] | None:
    """Read clips/analysis_results/ai_usage_reset out of a pre-5.0.0
    SQLite file. Synchronous (stdlib sqlite3) — always called via
    run_in_executor, never directly from the event loop.

    Every column beyond each table's oldest/most fundamental ones (id,
    camera, file_path, timestamp, downloaded_at for clips; clip_id,
    camera, model, analyzed_at for analysis_results) is read defensively
    via _column() rather than assumed present: exactly which columns exist
    depends on which pre-5.0.0 version the file was last written by —
    escalation columns arrived in 4.0.0, and a few analysis_results
    columns (anomaly_score, escalation_provider, prompt_text, the
    face_bypass_* trio) arrived only in the same 5.0.0 development pass
    that replaced SQLite itself, so even a file from immediately before
    that switch may not have them yet. A column genuinely missing from the
    file falls back to that column's current schema default.
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "clips" not in tables:
            return None

        clip_cols = _table_columns(conn, "clips")
        clips = [
            _clip_row(row, clip_cols) for row in conn.execute("SELECT * FROM clips")
        ]

        analysis_results: list[tuple[Any, ...]] = []
        if "analysis_results" in tables:
            analysis_cols = _table_columns(conn, "analysis_results")
            analysis_results = [
                _analysis_result_row(row, analysis_cols)
                for row in conn.execute("SELECT * FROM analysis_results")
            ]

        reset_at: str | None = None
        if "ai_usage_reset" in tables:
            row = conn.execute(
                "SELECT reset_at FROM ai_usage_reset WHERE id = 1"
            ).fetchone()
            if row and row["reset_at"]:
                reset_at = str(row["reset_at"])

        return clips, analysis_results, reset_at
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _column(row: sqlite3.Row, cols: set[str], name: str, default: Any) -> Any:
    return row[name] if name in cols else default


def _clip_row(row: sqlite3.Row, cols: set[str]) -> tuple[Any, ...]:
    return (
        str(row["id"]),
        str(row["camera"]),
        str(row["file_path"]),
        str(row["timestamp"]),
        int(row["size_bytes"] or 0),
        int(row["duration"] or 0),
        str(row["source"] or ""),
        int(row["network_id"] or 0),
        bool(_column(row, cols, "starred", 0)),
        str(_column(row, cols, "tags", "[]") or "[]"),
        str(row["downloaded_at"] or ""),
        bool(_column(row, cols, "archived", 0)),
        str(_column(row, cols, "archive_path", "") or ""),
    )


def _analysis_result_row(row: sqlite3.Row, cols: set[str]) -> tuple[Any, ...]:
    return (
        str(row["clip_id"]),
        str(row["camera"]),
        str(row["model"]),
        str(_column(row, cols, "response_text", "") or ""),
        bool(_column(row, cols, "is_suspicious", 0)),
        float(_column(row, cols, "confidence", 0.0) or 0.0),
        str(_column(row, cols, "summary", "") or ""),
        int(_column(row, cols, "frame_count", 0) or 0),
        float(_column(row, cols, "analysis_duration", 0.0) or 0.0),
        str(row["analyzed_at"] or ""),
        int(_column(row, cols, "tokens_prompt", 0) or 0),
        int(_column(row, cols, "tokens_completion", 0) or 0),
        float(_column(row, cols, "anomaly_score", 0.0) or 0.0),
        str(_column(row, cols, "escalation_model", "") or ""),
        int(_column(row, cols, "escalation_tokens_prompt", 0) or 0),
        int(_column(row, cols, "escalation_tokens_completion", 0) or 0),
        str(_column(row, cols, "escalation_provider", "") or ""),
        str(_column(row, cols, "prompt_text", "") or ""),
        bool(_column(row, cols, "face_bypass_applied", 0)),
        str(_column(row, cols, "face_bypass_names", "") or ""),
        bool(_column(row, cols, "approved_faces_seen", 0)),
    )
