"""Main application orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archiver import ClipArchiver
from .config import AppConfig
from .database import ClipDatabase
from .digest import DailyDigest
from .downloader import AuthenticationError, BlinkDownloader, TwoFARequired
from .event_watcher import HAEventWatcher
from .library_scanner import import_existing_clips
from .manifest import ClipManifest
from .media_server import MediaServer
from .notifier import HANotifier
from .storage import StorageManager
from .tracker import ClipTracker

_LOGGER = logging.getLogger(__name__)

STATS_FILE = Path("/data/stats.json")
TRIGGER_FILE = Path("/data/trigger_download")


class BlinkClipDownloaderApp:  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """Co-ordinates polling, downloading, library, media server, and events."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

        self._storage = StorageManager(
            base_path=config.download_path,
            max_storage_gb=config.max_storage_gb,
            retention_days=config.retention_days,
            organize_by_camera=config.organize_by_camera,
            organize_by_date=config.organize_by_date,
            filename_format=config.filename_format,
        )
        self._tracker = ClipTracker()
        self._manifest = ClipManifest()
        self._db = ClipDatabase()
        self._notifier = HANotifier(
            supervisor_token=config.supervisor_token,
            enabled=config.notify_ha,
            title=config.ha_notification_title,
            webhook_url=config.webhook_url,
        )
        self._downloader = BlinkDownloader(
            config, self._storage, self._tracker, self._db
        )
        self._digest = DailyDigest(
            notifier=self._notifier,
            db=self._db,
            digest_time=config.digest_time,
            enabled=config.digest_enabled,
        )
        self._archiver = ClipArchiver(
            db=self._db,
            archive_dir=config.download_path / "archives",
            archive_after_days=config.archive_after_days,
            enabled=config.archive_enabled,
        )
        self._media_server = MediaServer(
            db=self._db,
            download_path=config.download_path,
            port=config.media_server_port,
            trigger_download=self._trigger_immediate_download,
            two_fa_callback=self._downloader.submit_two_fa_code,
            auth_state_getter=lambda: {
                "state": self._downloader.auth_state,
                "message": self._downloader.auth_message,
                "two_fa_result_seq": self._downloader.two_fa_result_seq,
                "two_fa_result_ok": self._downloader.two_fa_result_ok,
            },
        )
        self._event_watcher = HAEventWatcher(
            supervisor_token=config.supervisor_token,
            on_motion=self._on_blink_motion,
            on_motion_cleared=self._on_blink_motion_cleared,
            event_cameras=config.event_cameras,
        )

        self._running = False
        self._session_downloads = 0
        # Fast-poll state: epoch time until which we poll at fast_poll_interval.
        self._fast_poll_until: float = 0.0
        self._bg_tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        # Seconds between Blink auth retry attempts (override to 0 in unit tests).
        self._reconnect_interval: int = 60
        # Seconds between checks in startup-error waiting loop (override in tests).
        self._startup_poll_interval: float = 1.0

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start the main polling loop.

        The web server starts first so HA ingress is always reachable —
        even when the add-on has a configuration error or Blink auth fails.
        The process never calls sys.exit(); it stays alive and retries.
        """
        self._running = True
        self._storage.ensure_directory()

        _LOGGER.info("Blink Clip Downloader starting up")

        self._loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown)

        # Init database.
        if self._config.enable_library_db:
            await self._db.init()

            # Re-populate the library with any clip files left behind under
            # download_path from a previous installation (e.g. /data was
            # wiped on uninstall but /share/blink-clips was not).
            imported = await import_existing_clips(self._db, self._config.download_path)
            if imported:
                _LOGGER.info(
                    "Library re-import: added %d pre-existing clip(s)", imported
                )

        # ── Web server FIRST ─────────────────────────────────────────────────
        # Must start before any blocking auth call so HA ingress always finds
        # port 8099 listening (avoids the "App not running, Start?" loop).
        if self._config.enable_media_server:
            self._bg_tasks.append(
                asyncio.create_task(self._media_server.start(), name="media_server")
            )
            # Yield once so the server task begins binding before we continue.
            await asyncio.sleep(0)

        # ── Configuration error mode ─────────────────────────────────────────
        # options.json was missing or invalid.  Show the error on the Status
        # tab and keep the web server alive so the user can read it without SSH.
        if self._config.startup_error:
            _LOGGER.error(
                "Running in web-only mode — fix the add-on configuration and "
                "restart.  Error: %s",
                self._config.startup_error,
            )
            self._downloader.auth_state = "error"
            self._downloader.auth_message = (
                f"Configuration error: {self._config.startup_error}"
            )
            while self._running:
                await asyncio.sleep(self._startup_poll_interval)
            await self._shutdown()
            return

        _LOGGER.info("  Download path   : %s", self._config.download_path)
        _LOGGER.info("  Poll interval   : %d s", self._config.poll_interval)
        _LOGGER.info("  Retention       : %d days", self._config.retention_days)
        _LOGGER.info("  Quota           : %.1f GB", self._config.max_storage_gb)
        _LOGGER.info(
            "  Media server    : %s (port %d)",
            "on" if self._config.enable_media_server else "off",
            self._config.media_server_port,
        )
        _LOGGER.info(
            "  HA event watch  : %s", "on" if self._config.watch_ha_events else "off"
        )

        # ── Blink authentication with auto-retry ─────────────────────────────
        # Never exits on auth failure — retries every _reconnect_interval seconds
        # so a transient network blip or expired token heals itself.
        if not await self._connect_with_retry():
            # _running was cleared by SIGTERM while we were between retries.
            await self._shutdown()
            return

        self._tracker.increment_session_count()
        await self._notifier.update_sensor(
            "sensor.blink_downloader_status",
            "connected",
            {"friendly_name": "Blink Clip Downloader", "status": "connected"},
        )

        # Expose connection status (and initial disk stats) to the media server
        # status endpoint so the Storage card is populated right away.
        self._media_server.extra_status = {
            "connected": True,
            "account_id": self._downloader.account_id,
            "disk": self._storage.disk_stats(),
        }

        if self._config.watch_ha_events and self._config.supervisor_token:
            self._bg_tasks.append(
                asyncio.create_task(self._event_watcher.start(), name="event_watcher")
            )

        # Main poll loop.
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.exception("Unhandled error in poll cycle: %s", exc)

            if self._running:
                await self._wait_with_trigger_check()

        await self._shutdown()

    # ------------------------------------------------------------------
    # Blink connection with auto-retry
    # ------------------------------------------------------------------

    async def _connect_with_retry(self) -> bool:
        """Attempt Blink authentication, retrying on transient failures.

        Returns True when connected.  Returns False only when *_running* is
        cleared (SIGTERM / SIGINT) while waiting between attempts — the caller
        should then proceed to shutdown.

        The method intentionally never raises; the process stays alive (web
        server keeps running) between retries so HA ingress stays green.
        """
        attempt = 0
        while self._running:
            attempt += 1
            try:
                await self._downloader.connect()
                if attempt > 1:
                    _LOGGER.info("Connected to Blink after %d attempt(s)", attempt)
                return True
            except TwoFARequired as exc:
                _LOGGER.error(
                    "Blink 2FA timed out (attempt %d) — "
                    "open the Blink Clips panel to enter the code, "
                    "then the add-on will retry in %d s.",
                    attempt,
                    self._reconnect_interval,
                )
                await self._notifier.notify(str(exc), title="Blink 2FA Required")
            except AuthenticationError as exc:
                _LOGGER.error(
                    "Blink authentication failed (attempt %d): %s Retrying in %d s.",
                    attempt,
                    exc,
                    self._reconnect_interval,
                )
                if attempt == 1:
                    await self._notifier.notify(
                        str(exc), title="Blink Authentication Failed"
                    )
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.exception(
                    "Failed to connect to Blink (attempt %d): %s — retrying in %d s",
                    attempt,
                    exc,
                    self._reconnect_interval,
                )

            # Interruptible wait: check _running every second so SIGTERM is
            # responded to promptly even during a long retry interval.
            for _ in range(self._reconnect_interval):
                if not self._running:
                    return False
                await asyncio.sleep(1)

        return False

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def _poll_cycle(self) -> None:
        _LOGGER.debug("Poll cycle started")

        deleted = self._storage.apply_retention_policy()
        if deleted:
            _LOGGER.info("Retention removed %d file(s)", deleted)

        archived = await self._archiver.run()
        if archived:
            _LOGGER.info("Archiver compressed %d clip(s)", archived)

        if self._storage.is_over_quota():
            msg = (
                "Storage quota exceeded — skipping download. "
                "Delete old clips or raise the quota in settings."
            )
            _LOGGER.warning(msg)
            await self._notifier.notify(msg, title="Blink Downloader: Storage Full")
            await self._write_stats()
            return

        downloaded = await self._downloader.download_new_clips()

        # Augment with clips from the Sync Module's USB local storage.
        if self._config.download_local_storage:
            local_clips = await self._downloader.download_local_storage_clips()
            downloaded = [*downloaded, *local_clips]

        self._session_downloads += len(downloaded)

        if downloaded:
            await self._on_clips_downloaded(downloaded)

        await self._digest.check_and_send()

        # Always refresh disk stats so the web UI Storage section stays current
        # even when no new clips were downloaded this cycle.
        self._media_server.extra_status["disk"] = self._storage.disk_stats()

        await self._write_stats()
        _LOGGER.debug("Poll cycle finished (%d new clip(s))", len(downloaded))

    async def _on_clips_downloaded(self, clips: list[dict[str, Any]]) -> None:
        """Post-download: notifications, events, manifest, webhook, sensor."""
        cameras = sorted({c["camera"] for c in clips})
        count = len(clips)
        message = f"Downloaded {count} clip(s) from: {', '.join(cameras)}"
        _LOGGER.info(message)

        await self._notifier.notify(message)

        for clip in clips:
            if self._config.create_clip_manifest:
                self._manifest.append(clip)

            await self._notifier.fire_event(
                "blink_clip_downloaded",
                {
                    "clip_id": clip.get("id"),
                    "camera": clip.get("camera"),
                    "path": clip.get("path"),
                    "timestamp": clip.get("timestamp"),
                    "size_bytes": clip.get("size_bytes", 0),
                    "duration": clip.get("duration"),
                    "source": clip.get("source"),
                },
            )
            await self._notifier.call_webhook(clip)

        tracker_stats = self._tracker.stats
        disk = self._storage.disk_stats()
        last_dl = datetime.now(timezone.utc).isoformat()
        self._media_server.extra_status["last_download"] = last_dl
        # Keep disk stats fresh so the Storage card in the web UI is accurate.
        self._media_server.extra_status["disk"] = disk
        await self._notifier.update_sensor(
            "sensor.blink_downloader_status",
            str(tracker_stats.get("total_downloaded", 0)),
            {
                "friendly_name": "Blink Clip Downloader",
                "unit_of_measurement": "clips",
                "total_downloaded": tracker_stats.get("total_downloaded", 0),
                "session_downloads": self._session_downloads,
                "used_mb": disk.get("used_mb", 0),
                "free_gb": disk.get("free_gb", 0),
                "last_download": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # Fast-poll mode (triggered by HA motion events)
    # ------------------------------------------------------------------

    def _on_blink_motion(self, camera_name: str) -> None:
        """Called by HAEventWatcher when a Blink motion sensor fires."""
        self._fast_poll_until = time.monotonic() + self._config.fast_poll_duration
        _LOGGER.info(
            "Motion on %r — fast-poll mode active for %ds",
            camera_name,
            self._config.fast_poll_duration,
        )

    def _on_blink_motion_cleared(self, camera_name: str) -> None:
        """Called when a Blink motion sensor clears.

        Schedules a download poll after *post_motion_delay* seconds to give
        Blink time to encode and upload the clip before we try to fetch it.
        """
        delay = self._config.post_motion_delay
        _LOGGER.info(
            "Motion cleared on %r — will poll for new clip in %ds",
            camera_name,
            delay,
        )
        try:
            loop = self._loop or asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.debug(
                "No running event loop available to schedule post-motion delay"
            )
            return
        self._loop = loop
        loop.call_later(delay, self._activate_fast_poll)

    def _activate_fast_poll(self) -> None:
        """Activate fast-poll mode for one cycle (used by post-motion timer)."""
        self._fast_poll_until = time.monotonic() + self._config.fast_poll_duration
        _LOGGER.debug("Post-motion delay elapsed — fast-poll mode activated")

    async def _trigger_immediate_download(self) -> None:
        """Called by the media server's Sync Now button."""
        self._fast_poll_until = time.monotonic() + 30
        _LOGGER.info("Immediate download triggered via web UI")

    # ------------------------------------------------------------------
    # Wait / trigger helpers
    # ------------------------------------------------------------------

    async def _wait_with_trigger_check(self) -> None:
        """Wait for the next poll, honouring fast-poll mode and trigger files."""
        in_fast_mode = time.monotonic() < self._fast_poll_until
        interval = (
            self._config.fast_poll_interval
            if in_fast_mode
            else self._config.poll_interval
        )

        if in_fast_mode:
            _LOGGER.debug("Fast-poll mode: next poll in %ds", interval)

        remaining = interval
        while remaining > 0 and self._running:
            if TRIGGER_FILE.exists():
                try:
                    TRIGGER_FILE.unlink()
                except OSError:
                    pass
                _LOGGER.info("Manual trigger detected — polling now")
                return
            await asyncio.sleep(min(5, remaining))
            remaining -= 5

            # Re-check fast-poll; could have been activated mid-sleep.
            if time.monotonic() < self._fast_poll_until and not in_fast_mode:
                return

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def _write_stats(self) -> None:
        payload: dict[str, Any] = {
            "last_poll": datetime.now(timezone.utc).isoformat(),
            "session_downloads": self._session_downloads,
            **self._tracker.stats,
            "disk": self._storage.disk_stats(),
        }
        try:
            STATS_FILE.write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            _LOGGER.warning("Could not write stats file: %s", exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _handle_shutdown(self) -> None:
        _LOGGER.info("Shutdown signal received")
        self._running = False

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down gracefully …")

        # Cancel background tasks.
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

        await self._media_server.stop()
        await self._event_watcher.stop()
        await self._downloader.disconnect()
        await self._notifier.close()
        await self._db.close()
        self._tracker.save()

        _LOGGER.info(
            "Blink Clip Downloader stopped. Session downloads: %d",
            self._session_downloads,
        )
