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
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from blink_downloader import gdrive_client, media_server
from blink_downloader.analyzer import ClipAnalyzer
from blink_downloader.archiver import ClipArchiver
from blink_downloader.database import ClipDatabase
from blink_downloader.gdrive_client import GDriveClient
from blink_downloader.live_view import LiveViewManager
from blink_downloader.media_server import MediaServer


class _ExpectedE2ENoiseFilter(logging.Filter):
    """Hide expected optional-fixture warnings from Playwright's live output."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.name == "blink_downloader.media_server":
            return not (
                message.startswith("ffmpeg exited ") and " for e2e-clip-" in message
            )
        if record.name == "blink_downloader.analyzer":
            return not (
                message.startswith("ffmpeg exited ")
                and " for /share/blink-clips/" in message
            )
        if record.name == "blink_downloader.vision":
            return not message.startswith(
                "facenet_pytorch package is not installed, face recognition unavailable:"
            )
        return True


def _configure_e2e_logging() -> None:
    """Keep expected standalone-fixture noise out of CI without hiding errors."""
    if os.environ.get("BLINK_E2E") != "1":
        return
    noise_filter = _ExpectedE2ENoiseFilter()
    for logger_name in (
        "blink_downloader.media_server",
        "blink_downloader.analyzer",
        "blink_downloader.vision",
    ):
        logging.getLogger(logger_name).addFilter(noise_filter)


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
    media_server.MediaServer._VEHICLE_ZONE_SNAPSHOTS_DIR = (
        data_dir / "vehicle_zone_snapshots"
    )
    # gdrive_client.py's CREDENTIALS_FILE/SETTINGS_FILE are bare
    # module-level constants — referenced as globals throughout that
    # module (SETTINGS_FILE.write_text(...), etc.), not MediaServer class
    # attributes like the others above — so they're redirected on the
    # gdrive_client module itself, same reasoning as
    # _force_face_recognition_available() patching media_server directly
    # below. Must happen before GDriveClient() is constructed in _main():
    # its __init__ reads both files immediately via _load_settings()/
    # _load_credentials().
    gdrive_client.CREDENTIALS_FILE = data_dir / "google_drive_credentials.json"
    gdrive_client.SETTINGS_FILE = data_dir / "google_drive_settings.json"


def _force_face_recognition_available() -> None:
    """Biometrics' nav tab — and BiometricsPage.vue's whole enroll form — are
    hidden/disabled whenever GET /api/ai/faces reports available=false,
    which vision.is_face_recognition_available() genuinely does here:
    facenet_pytorch is part of the optional CV-pipeline extra (see
    pyproject.toml), not this lightweight test environment. Patched to
    always return True (media_server.py imported the name directly, via
    `from .vision import ... is_face_recognition_available`, so it must be
    patched on the *media_server* module, not vision — same reasoning as
    _redirect_data_files patching MediaServer's own class attributes
    above) so the tab and its CRUD (list/rename/approve/remove the
    enrollments seeded below) are e2e-reachable. The actual embedding step
    (FaceEmbedder.embed(), called only from the enroll endpoint) still
    independently discovers facenet_pytorch is missing and gracefully
    returns "no face detected" either way — a real, already-handled
    response, not something this patch papers over — so an enrollment can
    never actually (falsely) succeed here.
    """
    media_server.is_face_recognition_available = lambda: True


# Port 1 is a reserved/privileged port nothing ever listens on, so
# health_check() fails fast (connection refused) rather than hanging on
# a real timeout — this is a real ClipAnalyzer, not a mock, just pointed
# at a guaranteed-closed port. Unlocks the AI and AI Usage tabs (both
# gated on `analyzer is not None` server-side) for e2e testing without
# needing a real Ollama/cloud provider.
_UNREACHABLE_OLLAMA_URL = "http://127.0.0.1:1"


class _FakeBlinkAuth:
    """Deterministic Blink auth/2FA state machine for browser E2E tests."""

    _VALID_CODE = "123456"
    _ERROR_CODE = "999999"

    def __init__(self) -> None:
        self.state = "connected"
        self.message = ""
        self._result_seq = 0
        self._result_ok: bool | None = None

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "message": self.message,
            "two_fa_result_seq": self._result_seq,
            "two_fa_result_ok": self._result_ok,
        }

    def submit_two_fa(self, code: str) -> int:
        self._result_seq += 1
        if code == self._VALID_CODE:
            self.state = "connected"
            self.message = ""
            self._result_ok = True
        elif code == self._ERROR_CODE:
            self.state = "error"
            self.message = "Blink authentication failed (simulated E2E error)."
            self._result_ok = False
        else:
            self.state = "needs_2fa"
            self.message = "Incorrect verification code. Please try again."
            self._result_ok = False
        return self._result_seq


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

# A real, valid (not just placeholder bytes) short video, on _SCRATCH_CAMERA
# so it doesn't inflate any real camera's count. Unlocks two things that
# otherwise fail/404 in this environment: Biometrics' "enroll from a clip"
# frame picker (_handle_clip_frames genuinely shells out to ffmpeg against
# the clip's file_path — placeholder bytes just fail extraction instead of
# exercising the real success path) and VehicleZonePicker's background
# image (its drawing surface only initializes once a real <img> load event
# fires on a clip thumbnail — reuses the exact camera the existing "car
# camera" e2e test already marks, Test Scratch, so no new test dependency).
_BIOMETRICS_CLIP_ID = "e2e-biometrics-source"
_BIOMETRICS_CLIP_DURATION = 3  # seconds — kept in sync with the generated video

# Its own dedicated clip for the bulk-delete test — bulkDelete() is
# destructive, so this can't reuse _SCRATCH_CLIP_IDS (star/tag tests need
# those to keep existing) or a "distribution" clip (whose camera/source/
# starred/tagged counts other tests assert as exact numbers). Only one
# clip: exercising bulkDelete's Promise.all over a single id already
# covers the same code path a multi-id selection would.
_DELETE_TEST_CLIP_ID = "e2e-scratch-delete"


async def _generate_test_video(path: Path, duration: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size=64x64:rate=5",
        "-pix_fmt",
        "yuv420p",
        str(path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


# Security Feed unlock: MediaServer's list_camera_names/get_camera_snapshot
# are narrow callables (see media_server.py's __init__), deliberately not
# routed through blinkpy/LiveViewManager — a fake camera list plus a real
# (tiny, Pillow-generated) JPEG per camera unlocks Security Feed for e2e
# testing the same cheap way the AI tab is unlocked by a real ClipAnalyzer
# pointed at an unreachable port, no actual camera hardware needed. Garage
# deliberately returns no snapshot (None) so its tile exercises the "No
# snapshot available yet" placeholder path instead of every camera taking
# the same happy path.
_SECURITY_FEED_NO_SNAPSHOT_CAMERA = "Garage"


def _list_camera_names() -> list[str]:
    return list(_CAMERAS)


def _fake_snapshot_jpeg() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 3), color=(80, 120, 160)).save(buf, format="JPEG")
    return buf.getvalue()


async def _camera_snapshot(camera: str) -> bytes | None:
    if camera == _SECURITY_FEED_NO_SNAPSHOT_CAMERA:
        return None
    return _fake_snapshot_jpeg()


# Live View unlock: MediaServer's live_view is a real LiveViewManager here
# (unlike list_camera_names/get_camera_snapshot above, it isn't optional —
# every /api/liveview/* route 503s without one), given a fake get_camera
# whose init_livestream() always raises. Actually starting a session needs
# a real Blink live-stream feeding a real ffmpeg process — completely out
# of reach here — but this still exercises everything up to that point for
# real: the camera picker, selecting a camera, the "starting" state, and a
# genuine (not mocked) LiveViewError being caught and surfaced as a toast,
# through live_view.py's real _create_session code path.
class _FakeLiveViewCamera:
    async def init_livestream(self) -> None:
        # A small, deliberate delay — a real blinkpy call to Blink's cloud
        # takes a real network round trip, giving LiveViewPage.vue's
        # "starting" placeholder a real window to render before the error
        # arrives. Without this, the fake camera fails near-instantly
        # (no real I/O happening at all), racing ahead of the frontend's
        # own render cycle and making that placeholder's e2e coverage
        # flaky/unreliable to assert on.
        await asyncio.sleep(0.3)
        raise NotImplementedError("e2e fake camera has no live view support")


# dict[str, Any] (not dict[str, _FakeLiveViewCamera]), matching
# tests/test_live_view.py's own _get_camera_from fixture helper — needed so
# _live_view_get_camera's inferred return type is Any (satisfies
# LiveViewManager's real BlinkCamera | None signature) rather than pyright
# tracking the concrete fake class and rejecting the mismatch.
_LIVE_VIEW_CAMERA_REGISTRY: dict[str, Any] = {
    name: _FakeLiveViewCamera() for name in _CAMERAS
}


def _live_view_get_camera(name: str):
    return _LIVE_VIEW_CAMERA_REGISTRY.get(name)


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

    # See _BIOMETRICS_CLIP_ID's comment above for why this needs a *real*
    # video, not just a placeholder file like the archiving clip above.
    biometrics_dir = archive_source_dir.parent / "biometrics-source"
    biometrics_dir.mkdir(parents=True, exist_ok=True)
    biometrics_source = biometrics_dir / f"{_BIOMETRICS_CLIP_ID}.mp4"
    await _generate_test_video(biometrics_source, _BIOMETRICS_CLIP_DURATION)
    biometrics_source.with_suffix(".jpg").write_bytes(_fake_snapshot_jpeg())
    # source="snapshot" (not "pir"): keeps this off library-filters.spec.ts's
    # "pir" source-filter count. hours_ago=12 (not 0-11, where every
    # distribution clip already lives — see _distribution_clips): keeps it
    # from inserting into the *global* newest-first ordering those clips'
    # relative prev/next relationships depend on (library-modal.spec.ts),
    # while staying inside EnrollFromClipPicker's default 24h lookback and
    # still sorting newest-on-Test-Scratch (ahead of the ~100h-old scratch
    # clips), so it's auto-selected with no extra click needed.
    biometrics_clip = _clip(_BIOMETRICS_CLIP_ID, _SCRATCH_CAMERA, "snapshot", 12, now)
    biometrics_clip["path"] = str(biometrics_source)
    biometrics_clip["duration"] = _BIOMETRICS_CLIP_DURATION
    await db.add_clip(biometrics_clip)

    # source="snapshot" (not "pir"), same reasoning as the biometrics clip
    # just above: keeps this off library-filters.spec.ts's "pir" source-
    # filter count. library-selection.spec.ts's bulk-delete test removes
    # this before status.spec.ts or storage.spec.ts run (declaration order
    # is execution order given workers: 1), so it never shows up in either
    # of their own Test-Scratch/total-clip counts. hours_ago=30, not
    # something under 24: EnrollFromClipPicker's default lookback window is
    # the last 24 hours on the selected camera (also Test Scratch), and
    # biometrics.spec.ts (which runs first alphabetically, well before this
    # clip is deleted) asserts exactly one clip shows up there — the
    # biometrics-source clip itself, at hours_ago=12.
    await db.add_clip(_clip(_DELETE_TEST_CLIP_ID, _SCRATCH_CAMERA, "snapshot", 30, now))

    # Enrolled household members, seeded directly rather than through the
    # UI's enroll flow — that needs facenet_pytorch, not installed in this
    # lightweight test environment (see is_face_recognition_available()).
    # Real embedding vectors aren't needed: nothing in list/rename/approve/
    # delete inspects embedding *values* — only enroll does, and that's
    # blocked either way here. "Alex E2E" has two enrollments with
    # different approved states (exercises the mixedApproval badge);
    # "Jordan E2E" is fully approved.
    await db.add_face_enrollment("Alex E2E", [0.1, 0.2, 0.3], approved=True)
    await db.add_face_enrollment("Alex E2E", [0.4, 0.5, 0.6], approved=False)
    await db.add_face_enrollment("Jordan E2E", [0.7, 0.8, 0.9], approved=True)
    # A third, dedicated to the rename-then-remove test — kept apart from
    # Alex (a static mixedApproval reference) and Jordan (dedicated to the
    # approve-toggle test) so those two tests' assertions never have to
    # account for this one's mutations.
    await db.add_face_enrollment("Casey E2E", [0.15, 0.25, 0.35], approved=True)

    # Battery history for the Status tab's battery strip/history modal
    # tests — Front Door ends up "ok", Backyard ends up "low" with one
    # prior recovered episode so the history modal has something to show.
    await db.add_battery_reading("Front Door", "ok", 3, 165)
    await db.add_battery_reading("Backyard", "ok", 3, 170)
    await db.add_battery_reading("Backyard", "low", 0, 108)
    await db.add_battery_reading("Backyard", "ok", 3, 172)
    await db.add_battery_reading("Backyard", "low", 0, 104)


async def _main() -> None:
    _configure_e2e_logging()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8199
    dsn = os.environ.get("BLINK_DB_DSN")
    if not dsn:
        raise SystemExit("BLINK_DB_DSN must be set to a reachable Postgres DSN")

    data_dir = Path(tempfile.mkdtemp(prefix="blink-e2e-data-"))
    _redirect_data_files(data_dir)
    _force_face_recognition_available()

    db = ClipDatabase(dsn=dsn)
    await db.init()
    assert db._pool is not None
    await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
    archive_dir = data_dir / "archives"
    await _seed(db, data_dir / "pending-archive-source")

    analyzer = ClipAnalyzer(
        ollama_url=_UNREACHABLE_OLLAMA_URL, model="llava", prompt="Describe this clip."
    )
    fake_auth = _FakeBlinkAuth()
    # archive_after_days=5 (not the config.yaml default of 60) — see
    # _PENDING_ARCHIVE_HOURS_AGO's comment above for exactly why 5, not a
    # rounder-looking number.
    archiver = ClipArchiver(
        db=db, archive_dir=archive_dir, archive_after_days=5, enabled=True
    )
    # Not started (no .start() call) — its sweep loop only matters for a
    # session that actually got past _create_session, which the fake
    # camera above can never reach; start_session/stop_session/get_status
    # don't depend on the sweep loop running.
    live_view = LiveViewManager(
        get_camera=_live_view_get_camera, list_camera_names=_list_camera_names
    )
    # A real (but unconfigured/uncredentialed) GDriveClient — constructing
    # one does nothing beyond loading the two files redirected above (both
    # absent, so it starts out blank/disconnected). This unlocks the
    # Storage tab's settings-save flow for e2e coverage; every endpoint
    # that needs an actual connection (status beyond "disconnected", quota,
    # folders, backup-now, upload) still correctly reports not-connected or
    # fails fast rather than making a real call to Google — same
    # real-dependency-that-fails-gracefully idea as everywhere else in this
    # script. _handle_gdrive_connect (the one path that *would* call out to
    # Google) stays unreachable without both a client id and secret saved,
    # which nothing here does.
    gdrive = GDriveClient()
    server = MediaServer(
        db=db,
        port=port,
        two_fa_callback=fake_auth.submit_two_fa,
        auth_state_getter=fake_auth.status,
        analyzer=analyzer,
        archiver=archiver,
        list_camera_names=_list_camera_names,
        get_camera_snapshot=_camera_snapshot,
        live_view=live_view,
        gdrive_client=gdrive,
    )
    await server.start()
    print(f"Standalone e2e server ready on http://localhost:{port}/", flush=True)

    # Runs until killed — Playwright's webServer config owns this process's
    # lifecycle (see frontend/playwright.config.ts).
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
