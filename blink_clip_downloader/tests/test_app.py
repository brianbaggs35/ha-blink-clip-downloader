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
    a._storage.apply_retention_policy_paths = MagicMock(return_value=[])
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
    app._storage.apply_retention_policy_paths.assert_called_once()


async def test_poll_cycle_retention_removes_orphaned_db_rows(app):
    """Files deleted by retention must have their DB row removed too, or the
    library keeps a dead entry for a clip that no longer exists on disk."""
    app._config.enable_library_db = True
    app._db.delete_clip_by_path = AsyncMock(return_value=True)
    app._storage.apply_retention_policy_paths = MagicMock(
        return_value=[Path("/share/blink-clips/old.mp4")]
    )

    await app._poll_cycle()

    app._db.delete_clip_by_path.assert_awaited_once_with("/share/blink-clips/old.mp4")


async def test_poll_cycle_logs_when_archiver_compresses_clips(app):
    app._archiver.run = AsyncMock(return_value=3)
    await app._poll_cycle()
    app._archiver.run.assert_awaited_once()


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


async def test_on_clips_downloaded_enqueues_for_analysis_when_configured(app):
    """When the AI analysis queue is wired up, every downloaded clip must be
    enqueued for analysis — the base test config has ai_analysis_enabled=False
    so _analysis_queue is normally None and this path is never exercised."""
    app._analysis_queue = MagicMock()
    app._analysis_queue.enqueue = AsyncMock()
    clips = [
        {"id": "1", "camera": "C", "path": "/p", "timestamp": "t", "size_bytes": 5}
    ]

    await app._on_clips_downloaded(clips)

    app._analysis_queue.enqueue.assert_awaited_once_with(clips[0])


async def test_on_clips_downloaded_analyzes_every_clip_at_the_burst_cap(app):
    """Exactly _MAX_AUTO_ANALYZE_BURST (5) clips in one batch is still the
    normal case, not a backlog — all of them get analyzed."""
    app._analysis_queue = MagicMock()
    app._analysis_queue.enqueue = AsyncMock()
    clips = [
        {
            "id": str(i),
            "camera": "C",
            "path": f"/p{i}",
            "timestamp": f"2026-01-01T00:0{i}:00",
            "size_bytes": 1,
        }
        for i in range(5)
    ]

    await app._on_clips_downloaded(clips)

    assert app._analysis_queue.enqueue.await_count == 5


async def test_on_clips_downloaded_caps_analysis_to_newest_clips_on_backlog(app):
    """A burst bigger than _MAX_AUTO_ANALYZE_BURST (5) — e.g. a fresh
    install's first poll, or catch-up after downtime — must still download
    and notify about every clip, but only auto-analyze the most recent 5;
    the rest stay in the library, un-analyzed, until requested on demand.
    Regression test: this used to enqueue every downloaded clip for
    analysis unconditionally, which could burn real API tokens re-analyzing
    a whole backlog of clips that were already days old."""
    app._analysis_queue = MagicMock()
    app._analysis_queue.enqueue = AsyncMock()
    # Deliberately out of chronological order, to prove the cap picks the
    # newest by timestamp rather than by list position.
    clips = [
        {
            "id": "old-1",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-01T00:00:00",
            "size_bytes": 1,
        },
        {
            "id": "new-1",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-07T00:05:00",
            "size_bytes": 1,
        },
        {
            "id": "old-2",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-02T00:00:00",
            "size_bytes": 1,
        },
        {
            "id": "new-2",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-07T00:04:00",
            "size_bytes": 1,
        },
        {
            "id": "old-3",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-03T00:00:00",
            "size_bytes": 1,
        },
        {
            "id": "new-3",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-07T00:03:00",
            "size_bytes": 1,
        },
        {
            "id": "new-4",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-07T00:02:00",
            "size_bytes": 1,
        },
        {
            "id": "new-5",
            "camera": "C",
            "path": "/p",
            "timestamp": "2026-01-07T00:01:00",
            "size_bytes": 1,
        },
    ]

    await app._on_clips_downloaded(clips)

    assert app._analysis_queue.enqueue.await_count == 5
    analyzed_ids = {c[0][0]["id"] for c in app._analysis_queue.enqueue.await_args_list}
    assert analyzed_ids == {"new-1", "new-2", "new-3", "new-4", "new-5"}
    # Every clip (analyzed or not) still gets its event/webhook/manifest.
    assert app._notifier.fire_event.await_count == len(clips)
    assert app._notifier.call_webhook.await_count == len(clips)


async def test_on_clips_downloaded_records_baseline_when_library_db_enabled(app):
    """With enable_library_db=True, each clip's camera/hour/duration is
    recorded into the anomaly-detection baseline."""
    app._config.enable_library_db = True
    app._db.record_clip_baseline = AsyncMock()
    clips = [
        {
            "id": "1",
            "camera": "Front Door",
            "path": "/p",
            "timestamp": "2024-06-01T08:30:00+00:00",
            "size_bytes": 5,
            "duration": 12,
        }
    ]

    await app._on_clips_downloaded(clips)

    app._db.record_clip_baseline.assert_awaited_once_with(
        camera="Front Door", hour=8, duration=12.0
    )


async def test_on_clips_downloaded_baseline_failure_is_swallowed(app, caplog):
    """A failure recording the anomaly baseline (e.g. DB error) must not
    interrupt the rest of the post-download flow (notifications, sensor
    update, etc.), but must still be logged rather than silently dropped."""
    app._config.enable_library_db = True
    app._db.record_clip_baseline = AsyncMock(side_effect=RuntimeError("db locked"))
    clips = [
        {"id": "1", "camera": "C", "path": "/p", "timestamp": "t", "size_bytes": 5}
    ]

    with caplog.at_level("DEBUG", logger="blink_downloader.app"):
        await app._on_clips_downloaded(clips)  # must not raise

    app._notifier.update_sensor.assert_awaited_once()
    assert "Could not record behavior baseline" in caplog.text


# ---------------------------------------------------------------------------
# _write_stats
# ---------------------------------------------------------------------------


def test_write_stats_creates_file(app, tmp_path):
    stats_path = tmp_path / "stats.json"
    with patch("blink_downloader.app.STATS_FILE", stats_path):
        app._write_stats()

    data = json.loads(stats_path.read_text())
    assert "last_poll" in data
    assert "total_downloaded" in data
    assert "disk" in data


def test_write_stats_handles_oserror(app, tmp_path):
    # Should not raise even if the file can't be written.
    with patch("blink_downloader.app.STATS_FILE", Path("/nonexistent/deep/stats.json")):
        app._write_stats()  # no exception


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


async def test_wait_logs_fast_poll_mode(app):
    """When fast-poll mode is active, the (short) fast_poll_interval is used
    and logged instead of the normal poll_interval."""
    app._config.fast_poll_interval = 0
    app._fast_poll_until = _time.monotonic() + 100
    app._running = True

    await app._wait_with_trigger_check()  # in_fast_mode branch, returns immediately


async def test_wait_ignores_trigger_file_unlink_oserror(app):
    """If the trigger file can't be deleted (e.g. permissions), the manual
    trigger must still be honored rather than raising."""
    app._config.poll_interval = 300
    app._running = True
    trigger = MagicMock()
    trigger.exists.return_value = True
    trigger.unlink.side_effect = OSError("permission denied")

    with patch("blink_downloader.app.TRIGGER_FILE", trigger):
        await app._wait_with_trigger_check()  # must not raise

    trigger.unlink.assert_called_once()


async def test_wait_returns_early_when_fast_poll_activated_mid_sleep(app, monkeypatch):
    """If motion activates fast-poll mode while _wait_with_trigger_check is
    already sleeping toward the (longer) normal poll_interval, it must notice
    on the next re-check and return early instead of sleeping out the full
    interval."""
    app._config.poll_interval = 10
    app._fast_poll_until = 0.0  # not in fast mode at the start of the wait
    app._running = True

    sleep_calls = 0

    async def _fake_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        # Simulate motion being detected partway through the wait.
        app._fast_poll_until = _time.monotonic() + 100

    monkeypatch.setattr("blink_downloader.app.asyncio.sleep", _fake_sleep)

    await app._wait_with_trigger_check()

    assert sleep_calls == 1


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_disconnects_and_saves_tracker(app):
    app._tracker.save = MagicMock()

    await app._shutdown()

    app._downloader.disconnect.assert_awaited_once()
    app._notifier.close.assert_awaited_once()
    app._tracker.save.assert_called_once()


async def test_shutdown_isolates_step_failures(app):
    """A failing cleanup step must not skip the remaining ones (esp. db.close/tracker.save)."""
    app._media_server.stop = AsyncMock(side_effect=RuntimeError("boom"))
    app._event_watcher.stop = AsyncMock(side_effect=RuntimeError("boom"))
    app._downloader.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
    app._db.close = AsyncMock(side_effect=RuntimeError("boom"))
    app._tracker.save = MagicMock(side_effect=RuntimeError("boom"))

    await app._shutdown()

    app._media_server.stop.assert_awaited_once()
    app._event_watcher.stop.assert_awaited_once()
    app._downloader.disconnect.assert_awaited_once()
    app._notifier.close.assert_awaited_once()
    app._db.close.assert_awaited_once()
    app._tracker.save.assert_called_once()


async def test_shutdown_closes_analysis_queue_analyzer_and_dispatcher_when_present(
    app,
):
    """When AI analysis is configured, _shutdown() must also stop the
    analysis queue and close the analyzer — these are skipped entirely when
    ai_analysis_enabled=False (the base test config), so this path is
    otherwise never exercised."""
    app._tracker.save = MagicMock()
    app._analysis_queue = MagicMock()
    app._analysis_queue.stop = MagicMock()
    app._analyzer = MagicMock()
    app._analyzer.close = AsyncMock()
    app._analyzer.escalation_analyzer = None

    await app._shutdown()

    app._analysis_queue.stop.assert_called_once()
    app._analyzer.close.assert_awaited_once()


async def test_shutdown_closes_escalation_analyzer_when_present(app):
    """When cross-provider escalation is configured, _shutdown() must also
    close the tier-2 escalation analyzer (app.py only holds a reference to
    the tier-1 analyzer, so this can't be exercised by analyzer.close() alone)."""
    app._tracker.save = MagicMock()
    app._analyzer = MagicMock()
    app._analyzer.close = AsyncMock()
    app._analyzer.escalation_analyzer = MagicMock()
    app._analyzer.escalation_analyzer.close = AsyncMock()

    await app._shutdown()

    app._analyzer.close.assert_awaited_once()
    app._analyzer.escalation_analyzer.close.assert_awaited_once()


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


async def test_run_startup_error_marks_disconnected(app):
    """Config-error mode must report connected=False (not an absent/None
    key) via extra_status, so the sidebar badge shows "Disconnected"
    instead of an indefinite "Unknown"."""
    import dataclasses

    app._config = dataclasses.replace(
        app._config,
        startup_error="options.json not found",
        enable_media_server=False,
    )
    app._startup_poll_interval = 0
    app._storage.ensure_directory = MagicMock()

    task = asyncio.create_task(app.run())
    await asyncio.sleep(0)
    app._handle_shutdown()
    await asyncio.wait_for(task, timeout=2.0)

    assert app._media_server.extra_status.get("connected") is False


async def test_run_invalid_credentials_marks_disconnected(app):
    """AuthenticationError during startup must report connected=False via
    extra_status — before this fix, extra_status only ever gained a
    "connected" key on a *successful* connect, so a bad-credentials add-on
    reported "Unknown" instead of "Disconnected" for its entire lifetime."""
    app._downloader.connect = AsyncMock(
        side_effect=AuthenticationError("Blink rejected the configured credentials")
    )
    app._startup_poll_interval = 0
    app._storage.ensure_directory = MagicMock()

    task = asyncio.create_task(app.run())
    await asyncio.sleep(0)
    app._handle_shutdown()
    await asyncio.wait_for(task, timeout=2.0)

    assert app._media_server.extra_status.get("connected") is False


async def test_run_successful_connect_marks_connected(app, tmp_path):
    """A normal successful startup still flips extra_status["connected"]
    to True via _finish_startup, confirming the new default-False set at
    the top of run() doesn't shadow the existing success path."""
    from blink_downloader.tracker import ClipTracker

    app._storage.ensure_directory = MagicMock()
    app._tracker = ClipTracker(tmp_path / "tracker.json")

    async def _fake_poll():
        app._running = False

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = AsyncMock()

    await app.run()

    assert app._media_server.extra_status.get("connected") is True


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


async def test_connect_with_retry_interruptible_wait_returns_false(app, monkeypatch):
    """The interruptible per-second wait between retries must notice
    _running being cleared mid-wait (not just at the top of the outer while
    loop) and return False promptly rather than sleeping out the full
    reconnect_interval."""
    app._running = True
    app._reconnect_interval = 5

    async def _connect():
        raise RuntimeError("fail")

    app._downloader.connect = _connect

    sleep_calls = 0

    async def _fake_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            app._running = False

    monkeypatch.setattr("blink_downloader.app.asyncio.sleep", _fake_sleep)

    result = await app._connect_with_retry()

    assert result is False
    assert sleep_calls == 1


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


async def test_run_poll_cycle_exception_is_caught_and_loop_continues(app, tmp_path):
    """An unhandled exception from _poll_cycle must be logged and swallowed,
    not crash the whole run() loop — a transient failure in one poll cycle
    shouldn't take down the add-on."""
    from blink_downloader.tracker import ClipTracker

    app._storage.ensure_directory = MagicMock()
    app._tracker = ClipTracker(tmp_path / "tracker.json")

    call_count = 0

    async def _fake_poll():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    async def _fake_wait():
        app._running = False

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = _fake_wait

    await app.run()

    assert call_count == 1
    app._downloader.disconnect.assert_awaited_once()


async def test_run_starts_media_server_event_watcher_and_analysis_queue(app, tmp_path):
    """run() must launch the media server, HA event watcher, and analysis
    queue as background tasks when their respective config flags are on —
    the base test config disables all three (enable_media_server=False,
    watch_ha_events=False, ai_analysis_enabled=False) so this path is
    otherwise never exercised."""
    import dataclasses

    from blink_downloader.tracker import ClipTracker

    app._config = dataclasses.replace(
        app._config,
        enable_media_server=True,
        watch_ha_events=True,
        supervisor_token="test_supervisor_token",
    )
    app._storage.ensure_directory = MagicMock()
    app._tracker = ClipTracker(tmp_path / "tracker.json")

    app._media_server.start = AsyncMock()
    app._media_server.stop = AsyncMock()
    app._event_watcher.start = AsyncMock()
    app._event_watcher.stop = AsyncMock()
    app._analysis_queue = MagicMock()
    app._analysis_queue.start = AsyncMock()
    app._analysis_queue.stop = MagicMock()

    async def _fake_poll():
        # Let the just-launched background tasks get a turn before we stop
        # and _shutdown() cancels them.
        await asyncio.sleep(0)
        app._running = False

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = AsyncMock()

    await app.run()

    app._media_server.start.assert_awaited_once()
    app._event_watcher.start.assert_awaited_once()
    app._analysis_queue.start.assert_awaited_once()
    app._analysis_queue.stop.assert_called_once()


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


async def test_trigger_immediate_download_activates_fast_poll(app):
    """The media server's Sync Now button calls this to force a near-term
    poll rather than waiting out the full poll_interval."""
    before = _time.monotonic()
    app._trigger_immediate_download()
    assert app._fast_poll_until >= before + 29


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


def test_on_blink_motion_cleared_handles_no_running_event_loop(app):
    """When called with no _loop cached and no event loop running (this is a
    plain sync test — there's genuinely no loop running here), the
    RuntimeError from asyncio.get_running_loop() must be caught rather than
    propagating out to the HAEventWatcher callback."""
    app._loop = None
    app._config.post_motion_delay = 15

    app._on_blink_motion_cleared("Garage")  # must not raise

    assert app._loop is None


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
    from tests.conftest import _ALL_TABLES, TEST_DB_DSN

    app._config = dataclasses.replace(app._config, enable_library_db=True)
    app._db = ClipDatabase(TEST_DB_DSN)
    # "Fresh ClipDatabase" (simulating a reinstall) means empty tables, not a
    # new connection — unlike SQLite's per-file isolation, this suite's
    # Postgres database is shared, so start this test from a clean slate.
    await app._db.init()
    assert app._db._pool is not None  # noqa: SLF001
    await app._db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")  # noqa: SLF001
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
        # The library reimport now runs as a background task (so it never
        # delays the media server from binding — see app.py). Let it finish
        # before we stop the run loop, since _shutdown() cancels bg tasks
        # rather than waiting for them.
        for t in app._bg_tasks:
            if t.get_name() == "library_reimport":
                await t
        app._running = False

    app._poll_cycle = _fake_poll
    app._wait_with_trigger_check = AsyncMock()

    await app.run()

    check_db = ClipDatabase(TEST_DB_DSN)
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

    assert app._db._pool is None  # never initialised  # noqa: SLF001


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


async def test_poll_cycle_logs_when_thumbnails_backfilled(app):
    app._downloader.backfill_thumbnails = AsyncMock(return_value=2)
    await app._poll_cycle()
    app._downloader.backfill_thumbnails.assert_awaited_once()


# ---------------------------------------------------------------------------
# __init__ — AI analyzer wiring: camera_configs.json (web UI) merged with
# options.json, then passed to create_analyzer(). This is the machinery
# that gives each camera its own perspective/description/car-camera flag.
# ---------------------------------------------------------------------------


def _build_app_with_camera_configs(
    base_config, tmp_path, cfg_entries, **config_overrides
):
    """Construct a BlinkClipDownloaderApp with AI enabled and a fake
    camera_configs.json, capturing the kwargs passed to create_analyzer()."""
    import dataclasses

    cfg_file = tmp_path / "camera_configs.json"
    if cfg_entries is not None:
        cfg_file.write_text(json.dumps(cfg_entries))

    config = dataclasses.replace(
        base_config,
        ai_analysis_enabled=True,
        ollama_url="http://localhost:11434",
        **config_overrides,
    )

    with (
        patch("blink_downloader.app.Path", return_value=cfg_file),
        patch("blink_downloader.app.create_analyzer") as mock_create_analyzer,
    ):
        mock_create_analyzer.return_value = MagicMock()
        app = BlinkClipDownloaderApp(config)

    return app, mock_create_analyzer


def test_init_wires_vision_pipeline_from_config(base_config, tmp_path) -> None:
    """The optional CV pipeline config fields must reach the analyzer via
    attach_vision_pipeline(), so enabling a toggle in options.json actually
    turns the corresponding vision.py stage on."""
    from blink_downloader.vision import VisionPipeline

    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        None,
        ai_enhanced_detection_enabled=True,
        ai_object_detection_model="yolo11s.pt",
        ai_face_recognition_enabled=True,
    )

    mock_analyzer = mock_create_analyzer.return_value
    mock_analyzer.attach_vision_pipeline.assert_called_once()
    pipeline = mock_analyzer.attach_vision_pipeline.call_args.args[0]
    assert isinstance(pipeline, VisionPipeline)
    config = pipeline._config  # noqa: SLF001
    assert config.enhanced_detection_enabled is True
    assert config.object_detection_model == "yolo11s.pt"
    assert config.face_recognition_enabled is True


def test_init_camera_configs_ui_file_populates_descriptions_and_car_cameras(
    base_config, tmp_path
):
    """Each camera's own description and car-camera flag from the web UI
    AI tab must reach the analyzer, giving cameras independent perspectives
    (e.g. a driveway camera watching the car vs. a front-door camera
    watching for package theft)."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "Watches the driveway and the owner's car",
                "custom_prompt": "",
                "is_car_camera": True,
            },
            {
                "camera": "Front Door",
                "description": "Watches for package theft and unauthorized entry",
                "custom_prompt": "Flag anyone lingering near the porch.",
                "is_car_camera": False,
            },
        ],
    )

    kwargs = mock_create_analyzer.call_args.kwargs
    assert kwargs["camera_descriptions"] == {
        "Driveway": "Watches the driveway and the owner's car",
        "Front Door": "Watches for package theft and unauthorized entry",
    }
    assert kwargs["camera_prompts"] == {
        "Front Door": "Flag anyone lingering near the porch."
    }
    assert kwargs["car_cameras"] == ["Driveway"]


def test_init_camera_configs_car_zone_reaches_analyzer(base_config, tmp_path) -> None:
    """A valid car_zone rectangle in camera_configs.json must reach
    create_analyzer() as a normalised float dict keyed by camera name."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": True,
                "car_zone": {
                    "x_min": "0.2",
                    "y_min": 0.3,
                    "x_max": 0.8,
                    "y_max": 0.9,
                },
            },
            {
                "camera": "Front Door",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": False,
            },
        ],
    )

    kwargs = mock_create_analyzer.call_args.kwargs
    assert kwargs["car_zones"] == {
        "Driveway": {"x_min": 0.2, "y_min": 0.3, "x_max": 0.8, "y_max": 0.9}
    }


def test_init_camera_configs_malformed_car_zone_is_ignored(
    base_config, tmp_path
) -> None:
    """A car_zone missing a required key must be skipped rather than crash
    startup or reach the analyzer as a broken/partial rectangle."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": True,
                "car_zone": {"x_min": 0.2, "y_min": 0.3, "x_max": 0.8},
            }
        ],
    )

    assert mock_create_analyzer.call_args.kwargs["car_zones"] is None


def test_init_camera_configs_inverted_car_zone_is_ignored(
    base_config, tmp_path
) -> None:
    """An inverted rectangle (x_min >= x_max) — e.g. from hand-editing
    camera_configs.json outside the web UI — must be rejected the same way
    the PUT handler rejects it, not silently reach the live analyzer as a
    degenerate zone. Both paths share MediaServer._normalize_car_zone so
    they can't drift out of sync."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": True,
                "car_zone": {"x_min": 0.9, "y_min": 0.3, "x_max": 0.1, "y_max": 0.9},
            }
        ],
    )

    assert mock_create_analyzer.call_args.kwargs["car_zones"] is None


def test_init_camera_configs_options_json_fills_gaps_not_covered_by_ui(
    base_config, tmp_path
):
    """options.json ai_camera_descriptions/ai_camera_prompts only apply to
    cameras the web UI file doesn't already cover (backward compatibility)."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "UI description",
                "custom_prompt": "",
                "is_car_camera": False,
            }
        ],
        ai_camera_descriptions=[
            {
                "camera": "Driveway",
                "description": "options.json description (should be ignored)",
            },
            {"camera": "Backyard", "description": "options.json backyard description"},
        ],
    )

    kwargs = mock_create_analyzer.call_args.kwargs
    assert kwargs["camera_descriptions"] == {
        "Driveway": "UI description",
        "Backyard": "options.json backyard description",
    }


def test_init_camera_prompts_options_json_fills_gaps_not_covered_by_ui(
    base_config, tmp_path
):
    """options.json ai_camera_prompts mirrors the descriptions fallback: it
    only fills in cameras the web UI file doesn't already cover, and
    malformed entries in the UI file (no "camera" key) are skipped."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "",
                "custom_prompt": "UI prompt",
                "is_car_camera": False,
            },
            {"description": "missing camera key, should be skipped"},
            "not even a dict",
        ],
        ai_camera_prompts=[
            {"camera": "Driveway", "prompt": "options.json prompt (should be ignored)"},
            {"camera": "Backyard", "prompt": "options.json backyard prompt"},
        ],
    )

    kwargs = mock_create_analyzer.call_args.kwargs
    assert kwargs["camera_prompts"] == {
        "Driveway": "UI prompt",
        "Backyard": "options.json backyard prompt",
    }


def test_init_car_cameras_ui_checkboxes_take_priority_over_options(
    base_config, tmp_path
):
    """A non-empty web-UI car-camera selection overrides options.json's
    legacy ai_car_cameras list entirely (not merged)."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        [
            {
                "camera": "Driveway",
                "description": "",
                "custom_prompt": "",
                "is_car_camera": True,
            }
        ],
        ai_car_cameras=["Front Door"],
    )

    assert mock_create_analyzer.call_args.kwargs["car_cameras"] == ["Driveway"]


def test_init_car_cameras_falls_back_to_options_when_ui_file_has_none_checked(
    base_config, tmp_path
):
    """No web-UI file at all falls back to options.json's ai_car_cameras."""
    _app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config,
        tmp_path,
        None,  # no camera_configs.json written
        ai_car_cameras=["Front Door"],
    )

    assert mock_create_analyzer.call_args.kwargs["car_cameras"] == ["Front Door"]


def test_init_corrupt_camera_configs_file_falls_back_to_options_json(
    base_config, tmp_path, caplog
):
    """A corrupt camera_configs.json must not crash startup — options.json
    values should still reach the analyzer, and the failure must be logged
    rather than silently swallowed (this is the only place a user would
    learn why their per-camera settings stopped applying)."""
    import dataclasses

    cfg_file = tmp_path / "camera_configs.json"
    cfg_file.write_text("{not valid json")

    config = dataclasses.replace(
        base_config,
        ai_analysis_enabled=True,
        ollama_url="http://localhost:11434",
        ai_camera_descriptions=[
            {"camera": "Driveway", "description": "fallback description"}
        ],
    )

    with (
        patch("blink_downloader.app.Path", return_value=cfg_file),
        patch("blink_downloader.app.create_analyzer") as mock_create_analyzer,
        caplog.at_level("WARNING"),
    ):
        mock_create_analyzer.return_value = MagicMock()
        BlinkClipDownloaderApp(config)

    assert mock_create_analyzer.call_args.kwargs["camera_descriptions"] == {
        "Driveway": "fallback description"
    }
    assert "Could not load" in caplog.text


def _build_app_with_vehicle_settings(
    base_config, tmp_path, vehicle_settings_content, **config_overrides
):
    """Construct a BlinkClipDownloaderApp with AI enabled and a fake
    vehicle_settings.json, capturing the kwargs passed to create_analyzer().
    Routes "/data/camera_configs.json" and "/data/vehicle_settings.json" to
    separate tmp_path files so each can be controlled independently, unlike
    _build_app_with_camera_configs's single fixed-return-value patch."""
    import dataclasses

    cam_cfg_file = tmp_path / "camera_configs.json"
    vehicle_file = tmp_path / "vehicle_settings.json"
    if vehicle_settings_content is not None:
        vehicle_file.write_text(json.dumps(vehicle_settings_content))

    def _fake_path(path_str):
        return (
            vehicle_file if path_str == "/data/vehicle_settings.json" else cam_cfg_file
        )

    config = dataclasses.replace(
        base_config,
        ai_analysis_enabled=True,
        ollama_url="http://localhost:11434",
        **config_overrides,
    )

    with (
        patch("blink_downloader.app.Path", side_effect=_fake_path),
        patch("blink_downloader.app.create_analyzer") as mock_create_analyzer,
    ):
        mock_create_analyzer.return_value = MagicMock()
        app = BlinkClipDownloaderApp(config)

    return app, mock_create_analyzer


def test_init_vehicle_settings_ui_file_overrides_options_json(
    base_config, tmp_path
) -> None:
    """A protected-vehicle description saved via the Vehicles tab
    (vehicle_settings.json) must survive a restart and reach the analyzer,
    taking priority over options.json's ai_car_description — the same "web
    UI file overrides config.yaml option once written" contract
    camera_configs.json already has (see CLAUDE.md)."""
    _app, mock_create_analyzer = _build_app_with_vehicle_settings(
        base_config,
        tmp_path,
        {"car_description": "Silver Kia Forte"},
        ai_car_description="options.json description (should be ignored)",
    )

    assert (
        mock_create_analyzer.call_args.kwargs["car_description"] == "Silver Kia Forte"
    )


def test_init_vehicle_settings_falls_back_to_options_when_file_missing(
    base_config, tmp_path
) -> None:
    """No vehicle_settings.json yet (never saved from the Vehicles tab)
    falls back to options.json's ai_car_description, matching the "falls
    back to config.yaml only until first written" contract."""
    _app, mock_create_analyzer = _build_app_with_vehicle_settings(
        base_config,
        tmp_path,
        None,  # no vehicle_settings.json written
        ai_car_description="options.json description",
    )

    assert (
        mock_create_analyzer.call_args.kwargs["car_description"]
        == "options.json description"
    )


def _build_app_with_finetune_state(
    base_config, tmp_path, finetune_state_content, **config_overrides
):
    """Same pattern as _build_app_with_vehicle_settings, but for the
    activated-Moondream-checkpoint state file."""
    import dataclasses

    cam_cfg_file = tmp_path / "camera_configs.json"
    state_file = tmp_path / "finetune_state.json"
    if finetune_state_content is not None:
        state_file.write_text(json.dumps(finetune_state_content))

    def _fake_path(path_str):
        return state_file if path_str == "/data/finetune_state.json" else cam_cfg_file

    config = dataclasses.replace(
        base_config,
        ai_analysis_enabled=True,
        ollama_url="http://localhost:11434",
        **config_overrides,
    )

    with (
        patch("blink_downloader.app.Path", side_effect=_fake_path),
        patch("blink_downloader.app.create_analyzer") as mock_create_analyzer,
    ):
        mock_create_analyzer.return_value = MagicMock()
        app = BlinkClipDownloaderApp(config)

    return app, mock_create_analyzer


def test_init_finetune_state_ui_file_overrides_options_json(
    base_config, tmp_path
) -> None:
    """An activated fine-tune checkpoint (finetune_state.json, written by
    MediaServer._handle_finetune_activate) must survive a restart and reach
    the analyzer, taking priority over options.json's
    moondream_finetune_model — without this, activating a checkpoint from
    the AI tab would silently revert to the base model on every restart."""
    _app, mock_create_analyzer = _build_app_with_finetune_state(
        base_config,
        tmp_path,
        {"active_model_id": "moondream3-preview/abc123@50"},
        moondream_finetune_model="options.json model (should be ignored)",
    )

    assert (
        mock_create_analyzer.call_args.kwargs["moondream_finetune_model"]
        == "moondream3-preview/abc123@50"
    )


def test_init_finetune_state_falls_back_to_options_when_file_missing(
    base_config, tmp_path
) -> None:
    """No finetune_state.json yet (no checkpoint ever activated) falls back
    to options.json's moondream_finetune_model."""
    _app, mock_create_analyzer = _build_app_with_finetune_state(
        base_config,
        tmp_path,
        None,  # no finetune_state.json written
        moondream_finetune_model="options.json model",
    )

    assert (
        mock_create_analyzer.call_args.kwargs["moondream_finetune_model"]
        == "options.json model"
    )


def test_init_corrupt_vehicle_settings_file_falls_back_to_options_json(
    base_config, tmp_path, caplog
) -> None:
    """A corrupt vehicle_settings.json must not crash startup — options.json
    should still reach the analyzer, mirroring camera_configs.json's
    corrupt-file handling — and the failure must be logged, not silently
    swallowed."""
    vehicle_file = tmp_path / "vehicle_settings.json"
    vehicle_file.write_text("{not valid json")

    def _fake_path(path_str):
        return (
            vehicle_file
            if path_str == "/data/vehicle_settings.json"
            else tmp_path / "camera_configs.json"
        )

    import dataclasses

    config = dataclasses.replace(
        base_config,
        ai_analysis_enabled=True,
        ollama_url="http://localhost:11434",
        ai_car_description="options.json fallback description",
    )

    with (
        patch("blink_downloader.app.Path", side_effect=_fake_path),
        patch("blink_downloader.app.create_analyzer") as mock_create_analyzer,
        caplog.at_level("WARNING"),
    ):
        mock_create_analyzer.return_value = MagicMock()
        BlinkClipDownloaderApp(config)

    assert (
        mock_create_analyzer.call_args.kwargs["car_description"]
        == "options.json fallback description"
    )
    assert "Could not load" in caplog.text


def test_init_attaches_scene_baseline_db_and_creates_analysis_queue(
    base_config, tmp_path
):
    """When create_analyzer() succeeds, the app must wire up the scene
    baseline DB and background analysis queue so the smart-brain baseline
    and long-clip frame doubling actually run."""
    app, mock_create_analyzer = _build_app_with_camera_configs(
        base_config, tmp_path, None
    )

    mock_create_analyzer.return_value.attach_scene_baseline_db.assert_called_once_with(
        app._db
    )
    assert app._analysis_queue is not None
    assert app._alert_dispatcher is not None


def test_init_analyzer_disabled_skips_queue_creation(base_config, tmp_path):
    """create_analyzer() returning None (e.g. missing ollama_url) must leave
    the analysis queue unset rather than wiring up a queue around a
    nonexistent analyzer. The notification dispatcher is still constructed
    unconditionally so the web UI's "Send Test Email" button works even
    before AI analysis is configured."""
    import dataclasses

    config = dataclasses.replace(
        base_config,
        ai_analysis_enabled=True,
        ollama_url="",  # ClipAnalyzer requires this; create_analyzer returns None
    )
    app = BlinkClipDownloaderApp(config)

    assert app._analyzer is None
    assert app._analysis_queue is None
    assert app._alert_dispatcher is not None
