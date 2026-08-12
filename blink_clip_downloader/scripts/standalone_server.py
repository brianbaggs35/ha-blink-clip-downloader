#!/usr/bin/env python3
"""Boots a standalone MediaServer, seeded with a handful of fake clips,
against a real Postgres database — for Playwright's interaction tests
(frontend/e2e/) to exercise real frontend+backend+DB behavior without
Home Assistant, blinkpy, or a full Docker image build involved.

This mirrors how the add-on can genuinely run standalone in production
(the `enable_media_server` option — see DOCS.md), just pointed at a
throwaway database instead of the bundled one. Not used by the root
`e2e/` smoke tests, which boot the real container instead — this script
is for the frontend/e2e/ interaction-test suite only.

Usage:
    BLINK_DB_DSN=postgresql://postgres:postgres@localhost:5432/blink_clips_e2e \
        python3 scripts/standalone_server.py [port]

Requires `npm run build` (from frontend/) to have already produced
blink_downloader/static/, and a reachable, already-created Postgres
database at BLINK_DB_DSN (ClipDatabase.init() creates the schema, but not
the database itself).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from blink_downloader import media_server
from blink_downloader.analyzer import ClipAnalyzer
from blink_downloader.archiver import ClipArchiver
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer


# MediaServer's per-feature settings files (camera_configs.json,
# vehicle_settings.json, etc.) are hardcoded to live under /data — the HA
# Supervisor's persistent-storage mount, always present and writable in
# the real add-on container. This script runs directly on the host, where
# /data doesn't exist (and creating it would need root) — so these class
# attributes are redirected to a throwaway temp dir before MediaServer is
# constructed, same idea as pointing BLINK_DB_DSN at a throwaway database
# instead of the bundled one. Genuinely required for any standalone (no
# Supervisor) deployment, not just for tests.
def _redirect_data_files(data_dir: Path) -> None:
    media_server.MediaServer._SECURITY_FEED_SETTINGS_FILE = (
        data_dir / "security_feed_settings.json"
    )
    media_server.MediaServer._CAMERA_CONFIGS_FILE = data_dir / "camera_configs.json"
    media_server.MediaServer._VEHICLE_SETTINGS_FILE = data_dir / "vehicle_settings.json"
    media_server.MediaServer._FINETUNE_STATE_FILE = data_dir / "finetune_state.json"


# Port 1 is a reserved/privileged port nothing ever listens on, so
# health_check() fails fast (connection refused) rather than hanging on
# a real timeout — this is a real ClipAnalyzer, not a mock, just pointed
# at a guaranteed-closed port. Unlocks the AI and AI Usage tabs (both
# gated on `analyzer is not None` server-side) for e2e testing without
# needing a real Ollama/cloud provider.
_UNREACHABLE_OLLAMA_URL = "http://127.0.0.1:1"

_CAMERAS = ("Front Door", "Backyard", "Garage")
# Real values the Library tab's source filter actually recognizes (see
# LibraryPage.vue's SOURCE_OPTIONS) — not arbitrary strings, so the
# source-filter e2e test asserts against real, meaningful filtering.
_SOURCES = ("pir", "liveview", "snapshot")
_CLIP_COUNT = 12

# Mirrors tests/conftest.py's `db` fixture: truncated before seeding so
# every run starts from the same deterministic state regardless of what a
# previous local run left behind — CI's Postgres service is ephemeral
# anyway, but a developer re-running this against a persistent local
# Postgres needs the same guarantee.
_ALL_TABLES = (
    "clips, analysis_results, ai_usage_reset, analysis_queue, "
    "gdrive_upload_queue, "
    "camera_baselines, camera_duration_stats, camera_scene_baselines, "
    "analysis_feedback, face_enrollments, battery_history"
)


def _clip(
    clip_id: str, camera: str, source: str, hours_ago: int, now: datetime
) -> dict:
    return {
        "id": clip_id,
        "camera": camera,
        "path": f"/share/blink-clips/{camera}/{clip_id}.mp4",
        "timestamp": (now - timedelta(hours=hours_ago)).isoformat(),
        "size_bytes": 2_000_000 + hours_ago * 10_000,
        "duration": 8,
        "source": source,
        "network_id": 10,
    }


def _distribution_clips(now: datetime) -> list[dict]:
    # 12 clips: an even 4-per-camera and 4-per-source split, so filtering
    # by either has a known, deterministic expected count to assert
    # against rather than just "some non-empty subset". Read-only as far
    # as the e2e specs are concerned — nothing should star/tag/delete
    # these, or every count-based assertion (e.g. "exactly 3 starred")
    # becomes dependent on test execution order.
    return [
        _clip(
            f"e2e-clip-{i:03d}",
            _CAMERAS[i % len(_CAMERAS)],
            _SOURCES[i % len(_SOURCES)],
            i,
            now,
        )
        for i in range(_CLIP_COUNT)
    ]


# A dedicated camera/id namespace for tests that star/tag/edit a clip —
# kept separate from the distribution clips above (and from each other)
# so a mutating test can never change what a filter-count assertion sees,
# regardless of what order the e2e spec files happen to run in.
_SCRATCH_CAMERA = "Test Scratch"
_SCRATCH_CLIP_IDS = ("e2e-scratch-star", "e2e-scratch-tag")

# Archived clips for the Storage tab. Reusing real distribution cameras
# (rather than a new camera name) is deliberate: get_camera_stats() and
# get_stats()'s total_count both filter WHERE archived=FALSE, so these
# are invisible to every existing Library/Vehicles/Status per-camera or
# total-clips assertion regardless of which camera they're on — but
# reusing real cameras (that also have non-archived clips) means they
# still show up as options in the Archived Clips camera filter dropdown,
# which only lists cameras from that same archived=FALSE-filtered query.
# Two clips share one archive_path (exercises the per-camera-subheader
# grouping within one expanded archive, and camera-filtering an archive
# group down to a subset of its clips); a third has its own archive_path
# with a single clip (its own archive group, dedicated to the delete
# test so removing it doesn't shrink the count the other assertions
# depend on — same isolation principle as _SCRATCH_CLIP_IDS above).
_ARCHIVE_PATH_MULTI = "/archives/2024-01-e2e.zip"
_ARCHIVE_PATH_SOLO = "/archives/2024-02-e2e.zip"
_ARCHIVE_CLIPS = (
    ("e2e-archive-front", "Front Door", _ARCHIVE_PATH_MULTI, 200),
    ("e2e-archive-back", "Backyard", _ARCHIVE_PATH_MULTI, 202),
    ("e2e-archive-solo", "Garage", _ARCHIVE_PATH_SOLO, 204),
)

# A clip old enough to be eligible once the e2e ClipArchiver's
# archive_after_days=5 (see _main) is applied, for the "Run Archiving Now"
# test. Two independent constraints pin this to a narrow window: it must be
# older than 120 hours (archive_after_days=5) while every *other* fixture
# (distribution clips 0-11 hours, scratch clips ~100-101 hours, _ARCHIVE_CLIPS
# already archived=True regardless of age) stays under that so only this one
# clip is swept in — but it must also stay under 168 hours, or the Library
# tab's own default date filter (dateRange='week', LibraryPage.vue) would
# hide it from the Library grid entirely, which would silently break
# library-filters.spec.ts's TOTAL_CLIPS count (that filter isn't applied by
# /api/cameras or /api/stats, only by the Library tab's own clip list, which
# is what made this so non-obvious — confirmed by direct API queries before
# landing on 140h here). 140 clears both with a comfortable margin either way.
_PENDING_ARCHIVE_CLIP_ID = "e2e-pending-archive"
_PENDING_ARCHIVE_HOURS_AGO = 140  # ~5.8 days

# A clip with a simulated failed Google Drive upload, for the Storage tab's
# Failed Uploads / retry test. Deliberately its own clip rather than reusing
# one of _ARCHIVE_CLIPS above — "e2e-archive-solo" is already earmarked for
# the archive-delete test, and this needs to stay untouched by that.
_FAILED_UPLOAD_CLIP_ID = "e2e-failed-upload"


async def _seed(db: ClipDatabase, archive_source_dir: Path) -> None:
    now = datetime.now(UTC)
    for clip in _distribution_clips(now):
        await db.add_clip(clip)
    # Starred/tagged distribution clips spread across different cameras
    # give the filter tests real, known-count subsets to assert against.
    for clip_id in ("e2e-clip-000", "e2e-clip-003", "e2e-clip-006"):
        await db.star_clip(clip_id, True)
    for clip_id in ("e2e-clip-001", "e2e-clip-004"):
        await db.set_tags(clip_id, ["delivery"])

    for i, clip_id in enumerate(_SCRATCH_CLIP_IDS):
        await db.add_clip(_clip(clip_id, _SCRATCH_CAMERA, "pir", 100 + i, now))

    for clip_id, camera, archive_path, hours_ago in _ARCHIVE_CLIPS:
        await db.add_clip(_clip(clip_id, camera, "pir", hours_ago, now))
        await db.mark_archived(clip_id, archive_path)

    # Both new clips below use _SCRATCH_CAMERA (not a real distribution
    # camera) and a non-"pir" source, deliberately — every distribution
    # camera's count, the "pir" source-filter count, and the total clip
    # count are all asserted as exact numbers elsewhere (status.spec.ts,
    # library-filters.spec.ts); landing on Test Scratch with a different
    # source only requires updating that camera's own count + the total,
    # not every one of those per-camera/per-source assertions individually.
    # A real file on disk is required for the pending-archive clip —
    # _archive_month treats a clip whose source file is missing as
    # unrecoverable and deletes its row instead of archiving it, which
    # would make this clip vanish rather than prove "Run Archiving Now"
    # actually archived something.
    archive_source_dir.mkdir(parents=True, exist_ok=True)
    pending_source = archive_source_dir / f"{_PENDING_ARCHIVE_CLIP_ID}.mp4"
    pending_source.write_bytes(b"fake video data for e2e archiving test")
    pending_clip = _clip(
        _PENDING_ARCHIVE_CLIP_ID,
        _SCRATCH_CAMERA,
        "snapshot",
        _PENDING_ARCHIVE_HOURS_AGO,
        now,
    )
    pending_clip["path"] = str(pending_source)
    await db.add_clip(pending_clip)

    # A clip with a failed Google Drive upload, for the Failed Uploads /
    # retry test — the retry endpoints only need self._db (see
    # media_server.py's _handle_gdrive_queue_failed/_handle_gdrive_retry),
    # so no real GDriveClient/GDriveUploadQueue needs to be wired in here.
    await db.add_clip(
        _clip(_FAILED_UPLOAD_CLIP_ID, _SCRATCH_CAMERA, "snapshot", 50, now)
    )
    await db.enqueue_for_gdrive_upload(
        _FAILED_UPLOAD_CLIP_ID,
        _SCRATCH_CAMERA,
        f"/share/blink-clips/{_SCRATCH_CAMERA}/{_FAILED_UPLOAD_CLIP_ID}.mp4",
    )
    await db.update_gdrive_queue_status(
        _FAILED_UPLOAD_CLIP_ID, "failed", error="Simulated failure for e2e testing"
    )

    # Battery history for the Status tab's battery strip/history modal
    # tests — Front Door ends up "ok", Backyard ends up "low" with one
    # prior recovered episode so the history modal has something to show.
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.add_battery_reading("Backyard", "ok", 3, 170)
    await db.add_battery_reading("Backyard", "low", 0, 108)
    await db.add_battery_reading("Backyard", "ok", 3, 172)
    await db.add_battery_reading("Backyard", "low", 0, 104)


async def _main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8199
    dsn = os.environ.get("BLINK_DB_DSN")
    if not dsn:
        raise SystemExit("BLINK_DB_DSN must be set to a reachable Postgres DSN")

    data_dir = Path(tempfile.mkdtemp(prefix="blink-e2e-data-"))
    _redirect_data_files(data_dir)

    db = ClipDatabase(dsn=dsn)
    await db.init()
    assert db._pool is not None
    await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
    archive_dir = data_dir / "archives"
    await _seed(db, data_dir / "pending-archive-source")

    analyzer = ClipAnalyzer(
        ollama_url=_UNREACHABLE_OLLAMA_URL, model="llava", prompt="Describe this clip."
    )
    # archive_after_days=5 (not the config.yaml default of 60) — see
    # _PENDING_ARCHIVE_HOURS_AGO's comment above for exactly why 5, not a
    # rounder-looking number.
    archiver = ClipArchiver(
        db=db, archive_dir=archive_dir, archive_after_days=5, enabled=True
    )
    server = MediaServer(db=db, port=port, analyzer=analyzer, archiver=archiver)
    await server.start()
    print(f"Standalone e2e server ready on http://localhost:{port}/", flush=True)

    # Runs until killed — Playwright's webServer config owns this process's
    # lifecycle (see frontend/playwright.config.ts).
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
