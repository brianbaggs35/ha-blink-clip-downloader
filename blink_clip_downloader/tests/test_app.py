"""Tests for blink_downloader.app."""

from __future__ import annotations

import asyncio
import json
import time as _time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.app import BlinkClipDownloaderApp
from blink_downloader.downloader import AuthenticationError, TwoFARequired


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(base_config):
    a = BlinkClipDownloaderApp(base_config)
    # Replace heavy collaborators with lightweight mocks.
    a._downloader.connect = AsyncMock()
    a._downloader.disconnect = AsyncMock()
    a._downloader.download_new_clips = AsyncMock(return_value=[])
    a._notifier.notify = AsyncMock(return_value=True)
    a._notifier.fire_event = AsyncMock(return_value=True)
    a._notifier.update_sensor = AsyncMock(return_value=True)
    a._notifier.call_webhook = AsyncMock(return_value=True)
    a._notifier.close = AsyncMock()
    a._storage.apply_retention_policy = MagicMock(return_value=0)
    a._storage.is_over_quota = MagicMock(return_value=False)
    a._storage.disk_stats = MagicMock(return_value={"used_mb": 1.0, "free_gb": 99.0})
    # _shutdown() always runs at the end of run(); mock save() so it doesn't
    # try to write to /data/downloaded_clips.json in the test environment.
    a._tracker.save = MagicMock()
    return a


# ---------------------------------------------------------------------------
# _poll_cycle
# ---------------------------------------------------------------------------


async def test_poll_cycle_no_new_clips(app):
    await app._poll_cycle()
    app._downloader.download_new_clips.assert_awaited_once()
    app._notifier.notify.assert_not_awaited()


async def test_poll_cycle_with_new_clips(app):
    clips = [
        {
            "id": "1",
            "camera": "Porch",
            "path": "/share/blink-clips/1.mp4",
            "timestamp": "2024-06-01T08:30:00+00:00",
            "size_bytes": 1024,
        }
    ]
    app._downloader.download_new_clips = AsyncMock(return_value=clips)

    await app._poll_cycle()

    app._notifier.notify.assert_awaited_once()
    assert app._session_downloads == 1


async def test_poll_cycle_skipped_clips_trigger_no_notifications_or_analysis(app):
    """Clips re-linked into the tracker (already on disk) are not "new".

    This happens when the tracker/database falls behind the files under
    download_path (e.g. after restoring an older HA backup) — the downloader
    re-links them instead of re-downloading, but they must not be treated as
    freshly downloaded clips or they'd trigger AI re-analysis and burn tokens
    on clips that were likely already analyzed before the restore.
    """
    clips = [
        {
            "id": "1",
            "camera": "Porch",
            "path": "/share/blink-clips/1.mp4",
            "timestamp": "2024-06-01T08:30:00+00:00",
            "size_bytes": 1024,
            "skipped": True,
        }
    ]
    app._downloader.download_new_clips = AsyncMock(return_value=clips)
    app._on_clips_downloaded = AsyncMock()

    await app._poll_cycle()

    app._on_clips_downloaded.assert_not_awaited()
    app._notifier.notify.assert_not_awaited()
    assert app._session_downloads == 0


async def test_poll_cycle_mix_of_skipped_and_new_clips(app):
    clips = [
        {
            "id": "1",
            "camera": "Porch",
            "path": "/p/1.mp4",
            "timestamp": "t",
            "size_bytes": 1,
            "skipped": True,
        },
        {
            "id": "2",
            "camera": "Porch",
            "path": "/p/2.mp4",
            "timestamp": "t",
            "size_bytes": 1,
        },
    ]
    app._downloader.download_new_clips = AsyncMock(return_value=clips)
    app._on_clips_downloaded = AsyncMock()

    await app._poll_cycle()

    app._on_clips_downloaded.assert_awaited_once_with([clips[1]])
    assert app._session_downloads == 1


async def test_poll_cycle_quota_exceeded_skips_download(app):
    app._storage.is_over_quota = MagicMock(return_value=True)

    await app._poll_cycle()

    app._downloader.download_new_clips.assert_not_awaited()
    app._notifier.notify.assert_awaited_once()
    notify_call = app._notifier.notify.call_args
    assert (
        "quota" in notify_call[0][0].lower() or "storage" in notify_call[0][0].lower()
    )


async def test_poll_cycle_calls_retention(app):
    await app._poll_cycle()
    app._storage.apply_retention_policy.assert_called_once()


# ---------------------------------------------------------------------------
# _on_clips_downloaded
# ---------------------------------------------------------------------------


async def test_on_clips_downloaded_fires_event_per_clip(app):
    clips = [
        {
            "id": "a",
            "camera": "Cam1",
            "path": "/p/a.mp4",
            "timestamp": "t",
            "size_bytes": 10,
        },
        {
            "id": "b",
            "camera": "Cam2",
            "path": "/p/b.mp4",
            "timestamp": "t",
            "size_bytes": 20,
        },
    ]
    await app._on_clips_downloaded(clips)
    assert app._notifier.fire_event.await_count == 2


async def test_on_clips_downloaded_lists_cameras_in_notification(app):
    clips = [
        {"id": "1", "camera": "Alpha", "path": "/x", "timestamp": "t", "size_bytes": 1},
        {"id": "2", "camera": "Beta", "path": "/y", "timestamp": "t", "size_bytes": 1},
    ]
    await app._on_clips_downloaded(clips)
    notify_msg = app._notifier.notify.call_args[0][0]
    assert "Alpha" in notify_msg
    assert "Beta" in notify_msg


async def test_on_clips_downloaded_updates_sensor(app):
    clips = [
        {"id": "1", "camera": "C", "path": "/p", "timestamp": "t", "size_bytes": 5}
    ]
    await app._on_clips_downloaded(clips)
    app._notifier.update_sensor.assert_awaited_once()
    entity_id = app._notifier.update_sensor.call_args[0][0]
    assert entity_id == "sensor.blink_downloader_status"


async def test_on_clips_downloaded_calls_webhook(app):
    clips = [
        {"id": "1", "camera": "C", "path": "/p", "timestamp": "t", "size_bytes": 5}
    ]
    await app._on_clips_downloaded(clips)
    app._notifier.call_webhook.assert_awaited_once()


async def test_on_clips_downloaded_appends_manifest(app):
    app._config.create_clip_manifest = True
    app._manifest.append = MagicMock()
    clips = [
        {"id": "1", "camera": "C", "path": "/p", "timestamp": "t", "size_bytes": 5}
    ]
    await app._on_clips_downloaded(clips)
    app._manifest.append.assert_called_once_with(clips[0])


async def test_on_clips_downloaded_skips_manifest_when_disabled(app):
    app._config.create_clip_manifest = False
    app._manifest.append = MagicMock()
    clips = [
        {"id": "1", "camera": "C", "path": "/p", "timestamp": "t", "size_bytes": 5}
    ]
    await app._on_clips_downloaded(clips)
    app._manifest.append.assert_not_called()


# ---------------------------------------------------------------------------
# _write_stats
# ---------------------------------------------------------------------------


async def test_write_stats_creates_file(app, tmp_path):
    stats_path = tmp_path / "stats.json"
    with patch("blink_downloader.app.STATS_FILE", stats_path):
        await app._write_stats()

    data = json.loads(stats_path.read_text())
    assert "last_poll" in data
    assert "total_downloaded" in data
    assert "disk" in data


async def test_write_stats_handles_oserror(app, tmp_path):
    # Should not raise even if the file can't be written.
    with patch("blink_downloader.app.STATS_FILE", Path("/nonexistent/deep/stats.json")):
        await app._write_stats()  # no exception


# ---------------------------------------------------------------------------
# _wait_with_trigger_check
# ---------------------------------------------------------------------------


async def test_trigger_file_causes_early_return(app, tmp_path):
    trigger = tmp_path / "trigger"
    trigger.write_text("")
    app._config.poll_interval = 300
    app._running = True

    with patch("blink_downloader.app.TRIGGER_FILE", trigger):
        await app._wait_with_trigger_check()

    assert not trigger.exists()


async def test_no_trigger_waits_full_interval(app):
    # Use a very short interval so the test is fast.
    app._config.poll_interval = 0
    app._running = True
    # Should return quickly without error.
    await app._wait_with_trigger_check()


async def test_running_false_exits_wait_early(app):
    app._config.poll_interval = 300
    app._running = False
    # Should return immediately because _running is False.
    await app._wait_with_trigger_check()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_disconnects_and_saves_tracker(app):
    app._tracker.save = MagicMock()

    await app._shutdown()

    app._downloader.disconnect.assert_awaited_once()
    app._notifier.close.assert_awaited_once()
    app._tracker.save.assert_called_once()


# ---------------------------------------------------------------------------
# run() – 2FA failure path: retries, never exits
# ---------------------------------------------------------------------------


async def test_run_2fa_required_sends_notification_and_retries(app):
    """2FA timeout sends an HA notification; app stays alive and retries."""
    app._reconnect_interval = 0  # no sleep between retries in tests
    attempt = 0

    async def _connect():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise TwoFARequired("needs code")
        # Second attempt: stop so the test finishes
        app._running = False
        raise RuntimeError("test stop")

    app._downloader.connect = _connect
    app._storage.ensure_directory = MagicMock()

    await app.run()

    app._notifier.notify.assert_awaited_once()
    title = app._notifier.notify.call_args.kwargs.get("title", "")
    assert "2FA" in title
    assert attempt == 2  # retried at least once


# ---------------------------------------------------------------------------
# run() – connect error path: retries, never exits
# ---------------------------------------------------------------------------


async def test_run_generic_connect_error_retries(app):
    """Connection errors cause retry, not immediate exit."""
    app._reconnect_interval = 0
    attempt = 0

    async def _connect():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise RuntimeError("network down")
        app._running = False
        raise RuntimeError("test stop")

    app._downloader.connect = _connect
    app._storage.ensure_directory = MagicMock()

    await app.run()

    app._downloader.download_new_clips.assert_not_awaited()
    assert attempt == 2


# ---------------------------------------------------------------------------
# run() – startup_error mode: web server stays up, connect() never called
# ---------------------------------------------------------------------------


async def test_run_startup_error_never_connects(app):
    """With startup_error set the app enters web-only mode without calling connect()."""
    import dataclasses

    app._config = dataclasses.replace(
        app._config,
        startup_error="options.json not found",
        enable_media_server=False,
    )
    app._startup_poll_interval = 0  # instant loop in tests
    app._storage.ensure_directory = MagicMock()

    task = asyncio.create_task(app.run())
    await asyncio.sleep(0)  # let run() reach the wait loop
    app._handle_shutdown()  # trigger graceful stop
    await asyncio.wait_for(task, timeout=2.0)

    app._downloader.connect.assert_not_awaited()


async def test_run_startup_error_sets_auth_state(app):
    """startup_error mode sets downloader auth_state to 'error' for the web UI."""
    import dataclasses

    app._config = dataclasses.replace(
        app._config,
        startup_error="missing credentials",
        enable_media_server=False,
    )
    app._startup_poll_interval = 0
    app._storage.ensure_directory = MagicMock()

    task = asyncio.create_task(app.run())
    await asyncio.sleep(0)
    app._handle_shutdown()
    await asyncio.wait_for(task, timeout=2.0)

    assert app._downloader.auth_state == "error"
    assert "missing credentials" in app._downloader.auth_message


# ---------------------------------------------------------------------------
# _connect_with_retry() unit tests
# ---------------------------------------------------------------------------


async def test_connect_with_retry_succeeds_first_attempt(app):
    app._running = True
    result = await app._connect_with_retry()
    assert result is True
    app._downloader.connect.assert_awaited_once()


async def test_connect_with_retry_retries_on_error(app):
    """Retries until connect() succeeds."""
    app._running = True
    app._reconnect_interval = 0
    attempt = 0

    async def _connect():
        nonlocal attempt
        attempt += 1
        if attempt < 3:
            raise RuntimeError("transient failure")

    app._downloader.connect = _connect

    result = await app._connect_with_retry()
    assert result is True
    assert attempt == 3


async def test_connect_with_retry_returns_false_on_sigterm(app):
    """Returns False immediately when _running is cleared during a retry wait."""
    app._running = True
    app._reconnect_interval = 0

    async def _connect():
        app._running = False
        raise RuntimeError("fail")

    app._downloader.connect = _connect

    result = await app._connect_with_retry()
    assert result is False


async def test_connect_with_retry_notifies_on_two_fa_timeout(app):
    """TwoFARequired triggers an HA notification before retrying."""
    app._running = True
    app._reconnect_interval = 0
    attempt = 0

    async def _connect():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise TwoFARequired("timeout")
        app._running = False
        raise RuntimeError("stop")

    app._downloader.connect = _connect

    await app._connect_with_retry()

    app._notifier.notify.assert_awaited_once()
    assert "2FA" in app._notifier.notify.call_args.kwargs.get("title", "")


async def test_connect_with_retry_stops_on_authentication_error(app):
    """AuthenticationError sends a notification and stops retrying to prevent account lockout."""
    app._running = True
    app._reconnect_interval = 0
    app._startup_poll_interval = 0.0
    attempt = 0

    async def _connect():
        nonlocal attempt
        attempt += 1
        raise AuthenticationError("Blink rejected the configured credentials")

    app._downloader.connect = _connect

    # Simulate _running being cleared (e.g. SIGTERM) so the wait loop exits.
    async def _stop_running() -> None:
        await asyncio.sleep(0)
        app._running = False

    asyncio.create_task(_stop_running())
    result = await app._connect_with_retry()

    assert result is False
    assert attempt == 1  # did NOT retry — would risk account lockout
    app._notifier.notify.assert_awaited_once()
    assert "Authentication" in app._notifier.notify.call_args.kwargs.get("title", "")
    assert app._downloader.auth_state == "error"


# ---------------------------------------------------------------------------
# run() – single successful iteration
# ---------------------------------------------------------------------------


async def test_run_one_iteration_then_stop(app, tmp_path):
    """run() polls once then exits because _running is set to False."""
    app._storage.ensure_directory = MagicMock()
    # Give the tracker a writable file so _shutdown() can save it.
    from blink_downloader.tracker import ClipTracker

    app._tracker = ClipTracker(tmp_path / "tracker.json")
    poll_count = 0

    async def _fake_poll():
        nonlocal poll_count
        poll_count += 1
        app._running = False  # Stop after first cycle

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = AsyncMock()

    await app.run()

    assert poll_count == 1
    app._downloader.connect.assert_awaited_once()
    app._downloader.disconnect.assert_awaited_once()


# ---------------------------------------------------------------------------
# Signal handler
# ---------------------------------------------------------------------------


def test_handle_shutdown_sets_running_false(app):
    app._running = True
    app._handle_shutdown()
    assert app._running is False


# ---------------------------------------------------------------------------
# Fast-poll / motion helpers
# ---------------------------------------------------------------------------


def test_on_blink_motion_sets_fast_poll_until(app):
    app._config.fast_poll_duration = 60
    before = _time.monotonic()
    app._on_blink_motion("Front Door")
    assert app._fast_poll_until >= before + 59


def test_activate_fast_poll_sets_fast_poll_until(app):
    app._config.fast_poll_duration = 30
    before = _time.monotonic()
    app._activate_fast_poll()
    assert app._fast_poll_until >= before + 29


def test_on_blink_motion_cleared_schedules_timer(app):
    """_on_blink_motion_cleared should call loop.call_later without raising."""
    loop = MagicMock()
    app._loop = loop
    app._config.post_motion_delay = 15

    app._on_blink_motion_cleared("Garage")

    loop.call_later.assert_called_once_with(15, app._activate_fast_poll)


# ---------------------------------------------------------------------------
# Disk stats propagation to media server extra_status (v2.5.4)
# ---------------------------------------------------------------------------


async def test_poll_cycle_updates_disk_stats_in_extra_status(app):
    """_poll_cycle must refresh extra_status['disk'] even when no clips downloaded."""
    fake_disk = {
        "used_mb": 42.0,
        "free_gb": 8.5,
        "used_bytes": 0,
        "free_bytes": 0,
        "total_bytes": 0,
        "total_gb": 0.0,
        "quota_bytes": 0,
        "quota_gb": 0,
    }
    app._storage.disk_stats = MagicMock(return_value=fake_disk)
    app._downloader.download_new_clips = AsyncMock(return_value=[])

    await app._poll_cycle()

    assert app._media_server.extra_status.get("disk") == fake_disk


async def test_on_clips_downloaded_updates_disk_stats_in_extra_status(app):
    """After a successful download batch, extra_status['disk'] must be set."""
    fake_disk = {
        "used_mb": 100.0,
        "free_gb": 5.0,
        "used_bytes": 0,
        "free_bytes": 0,
        "total_bytes": 0,
        "total_gb": 0.0,
        "quota_bytes": 0,
        "quota_gb": 0,
    }
    app._storage.disk_stats = MagicMock(return_value=fake_disk)

    clips = [
        {"id": "1", "camera": "Cam", "path": "/p", "timestamp": "t", "size_bytes": 1}
    ]
    await app._on_clips_downloaded(clips)

    assert app._media_server.extra_status.get("disk") == fake_disk


# ---------------------------------------------------------------------------
# Local storage integration in poll cycle (v2.5.5)
# ---------------------------------------------------------------------------


async def test_poll_cycle_calls_local_storage_when_enabled(app):
    """When download_local_storage=True, the poll cycle fetches USB clips."""
    app._config.download_local_storage = True
    local_clip = {
        "id": "local_1",
        "camera": "Front Door",
        "path": "/p/local_1.mp4",
        "timestamp": "2024-06-01T08:00:00+00:00",
        "size_bytes": 500_000,
        "source": "local_storage",
    }
    app._downloader.download_local_storage_clips = AsyncMock(return_value=[local_clip])

    await app._poll_cycle()

    app._downloader.download_local_storage_clips.assert_awaited_once()
    assert app._session_downloads == 1  # local clip counted


async def test_poll_cycle_skips_local_storage_when_disabled(app):
    """When download_local_storage=False, local-storage method is never called."""
    app._config.download_local_storage = False
    app._downloader.download_local_storage_clips = AsyncMock(return_value=[])

    await app._poll_cycle()

    app._downloader.download_local_storage_clips.assert_not_awaited()


# ---------------------------------------------------------------------------
# Library re-import on startup (v2.6.6)
# ---------------------------------------------------------------------------


async def test_run_imports_existing_clips_with_library_db_enabled(app, tmp_path):
    """run() re-populates a fresh library DB from files left under download_path.

    Simulates a reinstall: /data/clip_library.db is gone (fresh ClipDatabase)
    but download_path still has clips from before the uninstall.
    """
    import dataclasses

    from blink_downloader.database import ClipDatabase
    from blink_downloader.tracker import ClipTracker

    app._config = dataclasses.replace(app._config, enable_library_db=True)
    app._db = ClipDatabase(tmp_path / "lib.db")
    app._storage.ensure_directory = MagicMock()
    app._tracker = ClipTracker(tmp_path / "tracker.json")

    clip_dir = app._config.download_path / "Front_Door" / "2024-06-01"
    clip_dir.mkdir(parents=True)
    clip_path = clip_dir / "Front_Door_20240601_080000.mp4"
    clip_path.write_bytes(b"old-clip")

    poll_count = 0

    async def _fake_poll():
        nonlocal poll_count
        poll_count += 1
        app._running = False

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = AsyncMock()

    await app.run()

    check_db = ClipDatabase(tmp_path / "lib.db")
    await check_db.init()
    try:
        paths = await check_db.get_all_file_paths()
    finally:
        await check_db.close()

    assert str(clip_path) in paths


async def test_run_skips_import_when_library_db_disabled(app, tmp_path):
    """When enable_library_db is False, no DB is created or scanned."""
    app._storage.ensure_directory = MagicMock()
    from blink_downloader.tracker import ClipTracker

    app._tracker = ClipTracker(tmp_path / "tracker.json")

    clip_dir = app._config.download_path / "Front_Door" / "2024-06-01"
    clip_dir.mkdir(parents=True)
    (clip_dir / "Front_Door_20240601_080000.mp4").write_bytes(b"old-clip")

    poll_count = 0

    async def _fake_poll():
        nonlocal poll_count
        poll_count += 1
        app._running = False

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = AsyncMock()

    await app.run()

    assert app._db._db is None  # never initialised


async def test_poll_cycle_local_storage_clips_trigger_notification(app):
    """Local-storage clips trigger the same HA notification as cloud clips."""
    app._config.download_local_storage = True
    local_clip = {
        "id": "local_2",
        "camera": "Garage",
        "path": "/p",
        "timestamp": "t",
        "size_bytes": 1,
        "source": "local_storage",
    }
    app._downloader.download_local_storage_clips = AsyncMock(return_value=[local_clip])

    await app._poll_cycle()

    app._notifier.notify.assert_awaited_once()
    notify_msg = app._notifier.notify.call_args[0][0]
    assert "Garage" in notify_msg


# ---------------------------------------------------------------------------
# Thumbnail backfill in poll cycle
# ---------------------------------------------------------------------------


async def test_poll_cycle_backfills_thumbnails(app):
    from blink_downloader.app import _THUMBNAIL_BACKFILL_BATCH

    app._downloader.backfill_thumbnails = AsyncMock(return_value=0)

    await app._poll_cycle()

    app._downloader.backfill_thumbnails.assert_awaited_once_with(
        _THUMBNAIL_BACKFILL_BATCH
    )
