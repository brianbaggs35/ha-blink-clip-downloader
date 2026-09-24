"""Shared pytest fixtures."""

from __future__ import annotations

import importlib
import io
import json
import os
import pkgutil
import sys
import tempfile
import types
from collections.abc import AsyncGenerator, Iterator
from functools import cache
from pathlib import Path, PurePath, PurePosixPath
from typing import Any

import pytest

import blink_downloader
from blink_downloader.config import AppConfig
from blink_downloader.database import ClipDatabase

# A local PostgreSQL 17 instance is expected to already be running (the
# Dockerfile bundles one for the real add-on; for local dev/CI, point this
# at any throwaway Postgres — see CONTRIBUTING.md). Overridable so CI can
# use a service-container DSN instead of localhost.
TEST_DB_DSN = os.environ.get(
    "TEST_DATABASE_DSN",
    "postgresql://postgres:postgres@localhost:5432/blink_clips_test",
)

# Every table in the schema — truncated before each test since, unlike
# SQLite's fresh-file-per-test, a single Postgres database is reused across
# the whole run. RESTART IDENTITY resets GENERATED ALWAYS AS IDENTITY
# sequences too, so tests asserting on specific autoincrement ids stay
# deterministic; CASCADE follows FK references (e.g. clips -> analysis_results).
_ALL_TABLES = (
    "clips, analysis_results, detected_objects, ai_usage_reset, "
    "analysis_queue, gdrive_upload_queue, "
    "camera_baselines, camera_duration_stats, camera_scene_baselines, "
    "analysis_feedback, face_enrollments, battery_history, "
    "security_events, camera_vehicle_signatures"
)


@pytest.fixture
async def db() -> AsyncGenerator[ClipDatabase]:
    d = ClipDatabase(TEST_DB_DSN)
    await d.init()
    assert d._pool is not None
    await d._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
    yield d
    await d.close()


# The Supervisor's persistent-storage mount, where the add-on keeps its
# state files.
DATA_ROOT = PurePosixPath("/data")


def is_data_path(value: object) -> bool:
    """True for a string or path naming ``/data`` or anything beneath it."""
    return isinstance(value, str | PurePath) and PurePosixPath(value).is_relative_to(
        DATA_ROOT
    )


@cache
def package_modules() -> tuple[types.ModuleType, ...]:
    """``blink_downloader`` and every module beneath it, imported."""
    return (
        blink_downloader,
        *(
            importlib.import_module(info.name)
            for info in pkgutil.walk_packages(
                blink_downloader.__path__, f"{blink_downloader.__name__}."
            )
        ),
    )


def attribute_owners(module: types.ModuleType) -> list[Any]:
    """*module* itself plus each class it defines (not ones it imports)."""
    return [
        module,
        *(
            value
            for value in vars(module).values()
            if isinstance(value, type) and value.__module__ == module.__name__
        ),
    ]


@cache
def data_path_attributes() -> tuple[tuple[Any, str, str | PurePath], ...]:
    """Every module- or class-level attribute in the package that holds a
    path under /data, as ``(owner, name, original value)``.

    Found by walking the package rather than listed, so a constant added
    later is covered without anyone remembering to add it here. A class
    attribute is taken from the class that defines it (a mixin, for
    MediaServer's), which is what every subclass inherits.
    """
    return tuple(
        (owner, name, value)
        for module in package_modules()
        for owner in attribute_owners(module)
        for name, value in vars(owner).items()
        if is_data_path(value)
    )


@pytest.fixture(scope="session")
def _data_dirs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("data")


@pytest.fixture(autouse=True)
def data_dir(_data_dirs: Path) -> Iterator[Path]:
    """A per-test stand-in for /data, which no test may touch.

    The package keeps its state at fixed /data paths. Wherever the suite can
    create /data (as root, or in a Home Assistant devcontainer), a file one
    test writes there changes what a later, unrelated test reads: a stale
    camera_identities.json turns a same-camera refresh into a replacement,
    and a trigger_download from the e2e backend cuts a poll wait short. CI's
    runners cannot create /data, so every write fails silently there and
    the suite stays green, which is why it went unnoticed.

    Each path keeps its place relative to /data, so two constants naming one
    file (app.TRIGGER_FILE and media_server/library.py's _TRIGGER_FILE) still
    agree. This directory is separate from the test's ``tmp_path``, so
    nothing appears in that one unexpectedly. A test's own ``patch(...)`` of
    one of these attributes still wins: it is applied after this fixture
    runs and undone before this fixture's teardown.
    """
    # mkdtemp inside one session directory, not tmp_path_factory.mktemp per
    # test: that lists every directory already in the base temp dir to pick
    # the next number, so one more per test added ~10s to a full run and
    # slowed every tmp_path created after it.
    root = Path(tempfile.mkdtemp(dir=_data_dirs))
    # Its own MonkeyPatch, not the shared `monkeypatch` fixture: requesting
    # that here would set it up ahead of every other fixture, so it would
    # be undone only after all their teardowns, which would then run against
    # whatever the test patched through it (db's close() hitting a test's
    # stub pool).
    with pytest.MonkeyPatch.context() as patcher:
        for owner, name, original in data_path_attributes():
            redirected = root / PurePosixPath(original).relative_to(DATA_ROOT)
            patcher.setattr(
                owner,
                name,
                redirected if isinstance(original, PurePath) else str(redirected),
            )
        yield root


@pytest.fixture
def tmp_download_dir(tmp_path: Path) -> Path:
    d = tmp_path / "clips"
    d.mkdir()
    return d


@pytest.fixture
def base_config(tmp_download_dir: Path) -> AppConfig:
    return AppConfig(
        username="test@example.com",
        password="hunter2",
        download_path=tmp_download_dir,
        poll_interval=60,
        retention_days=30,
        max_storage_gb=10.0,
        camera_filter=[],
        motion_only=False,
        time_window_start="",
        time_window_end="",
        min_clip_duration=0,
        download_thumbnails=False,
        filename_format="{camera}_{timestamp}",
        notify_ha=False,
        ha_notification_title="Test",
        log_level="debug",
        max_clips_per_poll=50,
        organize_by_camera=True,
        organize_by_date=True,
        concurrent_downloads=2,
        retry_attempts=1,
        retry_delay=0.0,
        supervisor_token="test_supervisor_token",
        webhook_url="",
        create_clip_manifest=False,
        two_fa_timeout=5.0,
        enable_library_db=False,
        enable_media_server=False,
        media_server_port=8099,
        watch_ha_events=False,
        fast_poll_duration=30,
        fast_poll_interval=5,
        post_motion_delay=10,
        event_cameras=[],
        digest_enabled=False,
        digest_time="08:00",
        archive_enabled=False,
        archive_after_days=60,
        ai_analysis_enabled=False,
        ai_provider="ollama",
        ollama_url="",
        ollama_model="",
        moondream_api_key="",
        ai_car_description="",
        ai_max_frames=3,
        ai_frame_interval=2.0,
        ai_schedule_start="",
        ai_schedule_end="",
        ai_batch_size=10,
        ai_check_interval=60,
        mobile_app_enabled=False,
        mobile_app_target="",
        smtp_enabled=False,
        smtp_host="",
        smtp_port=587,
        smtp_user="",
        smtp_password="",
        smtp_recipients=[],
        smtp_sender="",
        discord_enabled=False,
        discord_webhook_url="",
    )


@pytest.fixture
def sample_clip() -> dict:
    return {
        "id": 99001,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/networks/10/cameras/100/clip/99001.mp4",
        "thumbnail": "/api/v1/accounts/1/networks/10/cameras/100/thumbnail/99001",
        "created_at": "2024-06-01T08:30:00+00:00",
        "updated_at": "2024-06-01T08:30:05+00:00",
        "size": 1_048_576,
        "duration": 5,
        "source": "pir",
        "network_id": 10,
        "account_id": 1,
    }


@pytest.fixture
def options_file(tmp_path: Path) -> Path:
    opts = {
        "username": "user@test.com",
        "password": "pass123",
        "download_path": str(tmp_path / "clips"),
        "poll_interval": 120,
    }
    f = tmp_path / "options.json"
    f.write_text(json.dumps(opts))
    return f


@pytest.fixture
def real_cv2(monkeypatch: pytest.MonkeyPatch):
    """Install a small, genuinely-working stand-in for the ``cv2`` functions
    the vision module's pure-image helpers use.

    OpenCV is part of the optional ``vision`` extra and is deliberately not
    installed for the test suite (see pyproject.toml), so the heavier stages
    are exercised against ``MagicMock``. That is fine for plumbing, but it
    proves nothing about code whose whole job is arithmetic on pixels —
    whether two colours actually separate, whether a lighting shift actually
    cancels out. This fixture backs those few calls with numpy and Pillow
    instead, so the real code path runs on real image data.

    Colour conventions follow OpenCV's: images are BGR, and hue spans
    0-179 rather than 0-255.
    """
    import numpy as np
    from PIL import Image

    cv2: Any = types.ModuleType("cv2")
    cv2.IMREAD_COLOR = 1
    cv2.COLOR_BGR2HSV = 40
    cv2.COLOR_BGR2GRAY = 6

    def imdecode(buffer, _flag):
        try:
            with Image.open(io.BytesIO(bytes(buffer))) as img:
                return np.asarray(img.convert("RGB"))[:, :, ::-1].copy()
        except Exception:  # noqa: BLE001 - mirrors cv2's "returns None" contract
            return None

    def cvt_color(img, code):
        rgb = Image.fromarray(img[:, :, ::-1].copy())
        if code == cv2.COLOR_BGR2GRAY:
            return np.asarray(rgb.convert("L"))
        hsv = np.asarray(rgb.convert("HSV")).astype("float32")
        hsv[:, :, 0] = hsv[:, :, 0] * 179.0 / 255.0
        return hsv.astype("uint8")

    def calc_hist(images, channels, _mask, hist_size, ranges):
        img = images[0]
        first = img[:, :, channels[0]].ravel()
        second = img[:, :, channels[1]].ravel()
        hist, _, _ = np.histogram2d(
            first,
            second,
            bins=hist_size,
            range=[[ranges[0], ranges[1]], [ranges[2], ranges[3]]],
        )
        return hist.astype("float32")

    def resize(img, size):
        width, height = size
        return np.asarray(
            Image.fromarray(img).resize((width, height), Image.Resampling.BILINEAR)
        )

    cv2.imdecode = imdecode
    cv2.cvtColor = cvt_color
    cv2.calcHist = calc_hist
    cv2.resize = resize
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    return cv2
