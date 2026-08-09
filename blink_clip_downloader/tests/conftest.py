"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

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
    "clips, analysis_results, ai_usage_reset, analysis_queue, "
    "gdrive_upload_queue, "
    "camera_baselines, camera_duration_stats, camera_scene_baselines, "
    "analysis_feedback, face_enrollments"
)


@pytest.fixture
async def db() -> AsyncGenerator[ClipDatabase, None]:
    d = ClipDatabase(TEST_DB_DSN)
    await d.init()
    assert d._pool is not None
    await d._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
    yield d
    await d.close()


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
