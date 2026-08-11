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
from datetime import UTC, datetime, timedelta

from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer

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
    "analysis_feedback, face_enrollments"
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


async def _seed(db: ClipDatabase) -> None:
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


async def _main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8199
    dsn = os.environ.get("BLINK_DB_DSN")
    if not dsn:
        raise SystemExit("BLINK_DB_DSN must be set to a reachable Postgres DSN")

    db = ClipDatabase(dsn=dsn)
    await db.init()
    assert db._pool is not None
    await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
    await _seed(db)

    server = MediaServer(db=db, port=port)
    await server.start()
    print(f"Standalone e2e server ready on http://localhost:{port}/", flush=True)

    # Runs until killed — Playwright's webServer config owns this process's
    # lifecycle (see frontend/playwright.config.ts).
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
