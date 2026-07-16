"""Main application orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis_queue import AnalysisQueue
from .analyzer import BaseAnalyzer, create_analyzer
from .archiver import ClipArchiver
from .config import AppConfig
from .database import ClipDatabase
from .vision import VisionConfig, VisionPipeline
from .digest import DailyDigest
from .downloader import AuthenticationError, BlinkDownloader, TwoFARequired
from .event_watcher import HAEventWatcher
from .library_scanner import import_existing_clips
from .manifest import ClipManifest
from .media_server import MediaServer
from .notification_channels import NotificationDispatcher
from .notifier import HANotifier
from .storage import StorageManager
from .tracker import ClipTracker

_LOGGER = logging.getLogger(__name__)

STATS_FILE = Path("/data/stats.json")
TRIGGER_FILE = Path("/data/trigger_download")

# How many missing clip thumbnails to generate per poll cycle. Keeps ffmpeg
# CPU usage low on constrained hardware while gradually backfilling large
# libraries (e.g. after enabling download_thumbnails or after a reinstall).
# Each is a single `-frames:v 1` extract (well under a second on typical
# hardware, see _generate_thumbnail), sequential and bounded, so raising
# this from the original 5 still leaves plenty of margin in a 300s+ poll
# cycle — 5 meant a few-hundred-clip backlog took hours to fully catch up,
# which read as "thumbnails aren't working" rather than "still catching up".
_THUMBNAIL_BACKFILL_BATCH = 15

# How many clips from a single downloaded batch get auto-queued for (paid)
# AI analysis. download_new_clips() already caps *downloads* per poll via
# max_clips_per_poll (default 50) to bound bandwidth/disk I/O, but every
# downloaded clip used to get enqueued for analysis unconditionally — fine
# for routine polling (usually 1-2 clips), but a genuine backlog burst (a
# fresh install's first poll pulling a busy 24h window, or catch-up after
# the add-on being down for a while) could enqueue dozens of clips for
# analysis in one shot, burning real API tokens on clips that are already
# hours or days old by the time anyone looks at them. Only the most recent
# few are worth analyzing automatically; older ones in the same burst still
# download and appear in the library normally, just not auto-analyzed —
# analyzable on demand via "Analyze Now" like any other unanalyzed clip.
_MAX_AUTO_ANALYZE_BURST = 5


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

        # --- AI Analysis (optional) ---
        self._analyzer: BaseAnalyzer | None = None
        self._analysis_queue: AnalysisQueue | None = None
        # Constructed unconditionally (not gated by ai_analysis_enabled) so the
        # web UI's "Send Test Email" button works even before AI analysis —
        # and therefore real suspicious-activity alerts — is turned on.
        self._alert_dispatcher = NotificationDispatcher(
            supervisor_token=config.supervisor_token,
            mobile_app_target=config.mobile_app_target,
            mobile_app_enabled=config.mobile_app_enabled,
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            smtp_user=config.smtp_user,
            smtp_password=config.smtp_password,
            smtp_recipients=config.smtp_recipients,
            smtp_sender=config.smtp_sender,
            smtp_enabled=config.smtp_enabled,
            discord_webhook_url=config.discord_webhook_url,
            discord_enabled=config.discord_enabled,
        )

        if config.ai_analysis_enabled:
            self._setup_ai_analysis(config)

        self._media_server = MediaServer(
            db=self._db,
            port=config.media_server_port,
            trigger_download=self._trigger_immediate_download,
            two_fa_callback=self._downloader.submit_two_fa_code,
            auth_state_getter=lambda: {
                "state": self._downloader.auth_state,
                "message": self._downloader.auth_message,
                "two_fa_result_seq": self._downloader.two_fa_result_seq,
                "two_fa_result_ok": self._downloader.two_fa_result_ok,
            },
            analyzer=self._analyzer,
            analysis_queue=self._analysis_queue,
            notification_dispatcher=self._alert_dispatcher,
            moondream_api_key=config.moondream_api_key,
            prompt_debug_enabled=config.ai_prompt_debug_enabled,
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
    # AI analysis setup
    # ------------------------------------------------------------------

    def _load_camera_configs_from_ui(
        self,
    ) -> tuple[dict[str, str], dict[str, str], list[str], dict[str, dict[str, float]]]:
        """Load per-camera settings from the web UI config file.

        camera_configs.json is the primary source of truth for descriptions,
        custom prompts, and car-camera flags.
        """
        camera_descriptions: dict[str, str] = {}
        camera_prompts: dict[str, str] = {}
        car_cameras_from_ui: list[str] = []
        car_zones: dict[str, dict[str, Any]] = {}

        cam_desc_file = Path("/data/camera_configs.json")
        if not cam_desc_file.exists():
            return camera_descriptions, camera_prompts, car_cameras_from_ui, car_zones

        try:
            cam_cfgs = json.loads(cam_desc_file.read_text())
            for cam_cfg in cam_cfgs:
                if not isinstance(cam_cfg, dict) or not cam_cfg.get("camera"):
                    continue
                cam = str(cam_cfg["camera"])
                if cam_cfg.get("description"):
                    camera_descriptions[cam] = str(cam_cfg["description"])
                if cam_cfg.get("custom_prompt"):
                    camera_prompts[cam] = str(cam_cfg["custom_prompt"])
                if cam_cfg.get("is_car_camera"):
                    car_cameras_from_ui.append(cam)
                zone = MediaServer._normalize_car_zone(cam_cfg.get("car_zone"))
                if zone is not None:
                    car_zones[cam] = zone
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not load %s, starting with no per-camera AI "
                "descriptions/prompts/car-camera settings: %s",
                cam_desc_file,
                exc,
            )

        return camera_descriptions, camera_prompts, car_cameras_from_ui, car_zones

    def _load_vehicle_settings_from_ui(self) -> str | None:
        """Load the protected-vehicle description from the web UI config file.

        vehicle_settings.json is the source of truth once written (see
        MediaServer._handle_vehicle_settings_get/_put) — falling back to the
        ai_car_description option in options.json only until the file has
        been written for the first time. Returns None (not "") when the file
        doesn't exist or is unreadable, so the caller can tell "not written
        yet" apart from "written as an empty string" and fall back to
        options.json only for the former.
        """
        settings_file = Path("/data/vehicle_settings.json")
        if not settings_file.exists():
            return None
        try:
            data = json.loads(settings_file.read_text())
            return str(data.get("car_description", ""))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not load %s, falling back to the configured "
                "ai_car_description option: %s",
                settings_file,
                exc,
            )
            return None

    def _load_finetune_model_from_ui(self) -> str | None:
        """Load the activated Moondream fine-tune checkpoint id, if any.

        finetune_state.json (written by
        MediaServer._handle_finetune_activate when a checkpoint is
        activated from the AI tab's Fine-Tune card) is the source of truth
        once written — falling back to the moondream_finetune_model option
        in options.json only until a checkpoint has been activated for the
        first time. Without this, an activated checkpoint would silently
        revert to whatever (or nothing) was last saved in options.json on
        every add-on restart, defeating the point of activating it.
        Returns None (not "") when the file doesn't exist or is
        unreadable, matching _load_vehicle_settings_from_ui's contract.
        """
        state_file = Path("/data/finetune_state.json")
        if not state_file.exists():
            return None
        try:
            data = json.loads(state_file.read_text())
            return str(data.get("active_model_id", ""))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not load %s, falling back to the configured "
                "moondream_finetune_model option: %s",
                state_file,
                exc,
            )
            return None

    @staticmethod
    def _merge_camera_config_fallbacks(
        config: AppConfig,
        camera_descriptions: dict[str, str],
        camera_prompts: dict[str, str],
    ) -> None:
        """Fall back to options.json values for cameras not yet configured.

        Preserves backward compatibility for cameras not configured in the
        web UI. Mutates the dicts in place.
        """
        for item in config.ai_camera_descriptions:
            cam = str(item.get("camera", ""))
            desc = str(item.get("description", ""))
            if cam and desc and cam not in camera_descriptions:
                camera_descriptions[cam] = desc
        for item in config.ai_camera_prompts:
            cam = str(item.get("camera", ""))
            pmt = str(item.get("prompt", ""))
            if cam and pmt and cam not in camera_prompts:
                camera_prompts[cam] = pmt

    def _setup_ai_analysis(self, config: AppConfig) -> None:
        (
            camera_descriptions,
            camera_prompts,
            car_cameras_from_ui,
            car_zones,
        ) = self._load_camera_configs_from_ui()
        self._merge_camera_config_fallbacks(config, camera_descriptions, camera_prompts)

        # Derive car_cameras: web UI checkboxes take priority; fall back
        # to ai_car_cameras from options.json for backward compatibility.
        car_cameras: list[str] | None = (
            car_cameras_from_ui or config.ai_car_cameras or None
        )

        # vehicle_settings.json (Vehicles tab) takes priority once written;
        # fall back to ai_car_description from options.json until then.
        car_description = self._load_vehicle_settings_from_ui()
        if car_description is None:
            car_description = config.ai_car_description

        # finetune_state.json takes priority once a checkpoint has been
        # activated from the AI tab; fall back to moondream_finetune_model
        # from options.json until then.
        moondream_finetune_model = self._load_finetune_model_from_ui()
        if moondream_finetune_model is None:
            moondream_finetune_model = config.moondream_finetune_model

        self._analyzer = create_analyzer(
            ai_provider=config.ai_provider,
            prompt=config.ai_prompt,
            car_description=car_description,
            max_frames=config.ai_max_frames,
            frame_interval=config.ai_frame_interval,
            suspicious_keywords=config.ai_suspicious_keywords,
            camera_prompts=camera_prompts or None,
            camera_descriptions=camera_descriptions,
            frame_strategy=config.ai_frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones or None,
            ollama_url=config.ollama_url,
            ollama_model=config.ollama_model,
            ollama_cloud_api_key=config.ollama_cloud_api_key,
            moondream_api_key=config.moondream_api_key,
            moondream_finetune_model=moondream_finetune_model,
            anthropic_api_key=config.anthropic_api_key,
            anthropic_model=config.anthropic_model,
            openai_api_key=config.openai_api_key,
            openai_model=config.openai_model,
            escalation_provider=config.ai_escalation_provider,
            escalation_model=config.ai_escalation_model,
            store_prompt_debug=config.ai_prompt_debug_enabled,
        )
        if self._analyzer is None:
            return

        self._analyzer.attach_scene_baseline_db(self._db)
        self._analyzer.attach_vision_pipeline(
            VisionPipeline(
                VisionConfig(
                    enhanced_detection_enabled=config.ai_enhanced_detection_enabled,
                    object_detection_model=config.ai_object_detection_model,
                    face_recognition_enabled=config.ai_face_recognition_enabled,
                ),
                db=self._db,
            )
        )
        self._analysis_queue = AnalysisQueue(
            analyzer=self._analyzer,
            db=self._db,
            dispatcher=self._alert_dispatcher,
            schedule_start=config.ai_schedule_start,
            schedule_end=config.ai_schedule_end,
            batch_size=config.ai_batch_size,
            check_interval=config.ai_check_interval,
            min_confidence=config.ai_min_confidence,
        )

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

        # Reported to the sidebar's connection badge via /api/stats. Default
        # to False (not None/absent) from the first moment the media server
        # can answer requests, so a client that loads mid-startup-error (e.g.
        # invalid credentials, which never reaches _finish_startup below)
        # sees "Disconnected" rather than an indefinite "Unknown" — the
        # frontend only distinguishes those two when this key is present.
        self._media_server.extra_status["connected"] = False

        _LOGGER.info("Blink Clip Downloader starting up")

        self._loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._loop.add_signal_handler(sig, self._handle_shutdown)

        # Init database. Schema setup is cheap (a few CREATE TABLE IF NOT
        # EXISTS statements) so it's fine to await inline here.
        if self._config.enable_library_db:
            await self._db.init()

        # ── Web server FIRST ─────────────────────────────────────────────────
        # Must start before any blocking auth call so HA ingress always finds
        # port 8099 listening (avoids the "App not running, Start?" loop).
        if self._config.enable_media_server:
            self._bg_tasks.append(
                asyncio.create_task(self._media_server.start(), name="media_server")
            )
            # Yield once so the server task begins binding before we continue.
            await asyncio.sleep(0)

        if self._config.enable_library_db:
            # Re-populate the library with any clip files left behind under
            # download_path from a previous installation (e.g. /data was
            # wiped on uninstall but /share/blink-clips was not). Runs as a
            # background task — a large library means an unbounded number of
            # rglob()/stat() syscalls, and awaiting it inline here used to
            # delay the media server task above from ever getting scheduled,
            # leaving HA ingress with nothing listening on port 8099 (seen as
            # a 504) for however long the filesystem walk took.
            self._bg_tasks.append(
                asyncio.create_task(self._reimport_library(), name="library_reimport")
            )

        # ── Configuration error mode ─────────────────────────────────────────
        # options.json was missing or invalid.  Show the error on the Status
        # tab and keep the web server alive so the user can read it without SSH.
        if self._config.startup_error:
            await self._run_config_error_mode()
            return

        self._log_startup_config()

        # ── Blink authentication with auto-retry ─────────────────────────────
        # Never exits on auth failure — retries every _reconnect_interval seconds
        # so a transient network blip or expired token heals itself.
        if not await self._connect_with_retry():
            # _running was cleared by SIGTERM while we were between retries.
            await self._shutdown()
            return

        await self._finish_startup()

        # Main poll loop.
        while self._running:
            try:
                await self._poll_cycle()
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.exception("Unhandled error in poll cycle: %s", exc)

            if self._running:
                await self._wait_with_trigger_check()

        await self._shutdown()

    async def _run_config_error_mode(self) -> None:
        """Keep the web server alive so the config error is visible in the UI."""
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

    def _log_startup_config(self) -> None:
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

    async def _finish_startup(self) -> None:
        """Mark connected, publish status, and start event/analysis background tasks."""
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

        if self._analysis_queue:
            _model_by_provider = {
                "ollama": self._config.ollama_model,
                "ollama_cloud": self._config.ollama_model,
                "moondream_cloud": self._config.moondream_finetune_model,
                "anthropic": self._config.anthropic_model,
                "openai": self._config.openai_model,
            }
            _LOGGER.info(
                "  AI analysis    : on (provider=%s, model=%s)",
                self._config.ai_provider,
                _model_by_provider.get(self._config.ai_provider) or "(auto)",
            )
            self._bg_tasks.append(
                asyncio.create_task(self._analysis_queue.start(), name="analysis_queue")
            )

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
                await self._handle_invalid_credentials(exc)
                return False
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.exception(
                    "Failed to connect to Blink (attempt %d): %s — retrying in %d s",
                    attempt,
                    exc,
                    self._reconnect_interval,
                )

            # Interruptible wait: check _running every second so SIGTERM is
            # responded to promptly even during a long retry interval.
            if not await self._interruptible_wait(self._reconnect_interval):
                return False

        return False

    async def _handle_invalid_credentials(self, exc: AuthenticationError) -> None:
        # Invalid credentials — retrying with the same credentials
        # would trigger Blink's lockout protection. Stop immediately
        # and wait for the add-on to be restarted with fixed credentials.
        _LOGGER.error(
            "Blink authentication failed — invalid credentials. "
            "Fix the username/password in the add-on configuration "
            "and restart. Not retrying to prevent account lockout."
        )
        await self._notifier.notify(str(exc), title="Blink Authentication Failed")
        self._downloader.auth_state = "error"
        self._downloader.auth_message = (
            "Invalid credentials — fix username/password and restart the add-on."
        )
        while self._running:
            await asyncio.sleep(self._startup_poll_interval)

    async def _interruptible_wait(self, seconds: int) -> bool:
        """Sleep up to *seconds*, checking _running every second.

        Returns False if _running was cleared before the wait completed.
        """
        for _ in range(seconds):
            if not self._running:
                return False
            await asyncio.sleep(1)
        return True

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def _poll_cycle(self) -> None:
        _LOGGER.debug("Poll cycle started")

        deleted_paths = self._storage.apply_retention_policy_paths()
        if deleted_paths:
            _LOGGER.info("Retention removed %d file(s)", len(deleted_paths))
            if self._config.enable_library_db:
                # apply_retention_policy_paths() only touches the
                # filesystem — without this, a deleted clip's row (and its
                # tags/analysis results) would linger in the DB forever,
                # pointing at a file that no longer exists.
                for path in deleted_paths:
                    await self._db.delete_clip_by_path(str(path))

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
            self._write_stats()
            return

        downloaded = await self._downloader.download_new_clips()

        # Augment with clips from the Sync Module's USB local storage.
        if self._config.download_local_storage:
            local_clips = await self._downloader.download_local_storage_clips()
            downloaded = [*downloaded, *local_clips]

        # Clips already present on disk get re-linked into the tracker (see
        # BlinkDownloader._download_clip) rather than re-downloaded, which
        # happens whenever the tracker/database falls behind the files under
        # download_path — most commonly after restoring an older HA backup.
        # These are not new content, so they must not trigger notifications,
        # webhooks, or (critically) AI analysis — that would silently re-run
        # and re-bill analysis for clips that were likely already analyzed
        # before the restore.
        new_clips = [c for c in downloaded if not c.get("skipped")]
        rediscovered = len(downloaded) - len(new_clips)
        if rediscovered:
            _LOGGER.info(
                "%d clip(s) already on disk were re-linked into the tracker "
                "without notifications or AI analysis (tracker/database "
                "reset, e.g. after a backup restore)",
                rediscovered,
            )

        self._session_downloads += len(new_clips)

        if new_clips:
            await self._on_clips_downloaded(new_clips)

        # Gradually fill in thumbnails for clips downloaded before
        # download_thumbnails was enabled (or re-imported after a reinstall).
        backfilled = await self._downloader.backfill_thumbnails(
            _THUMBNAIL_BACKFILL_BATCH
        )
        if backfilled:
            _LOGGER.debug("Generated %d missing thumbnail(s)", backfilled)

        await self._digest.check_and_send()

        # Always refresh disk stats so the web UI Storage section stays current
        # even when no new clips were downloaded this cycle.
        self._media_server.extra_status["disk"] = self._storage.disk_stats()

        self._write_stats()
        _LOGGER.debug("Poll cycle finished (%d new clip(s))", len(downloaded))

    async def _on_clips_downloaded(self, clips: list[dict[str, Any]]) -> None:
        """Post-download: notifications, events, manifest, webhook, sensor."""
        cameras = sorted({c["camera"] for c in clips})
        count = len(clips)
        message = f"Downloaded {count} clip(s) from: {', '.join(cameras)}"
        _LOGGER.info(message)

        await self._notifier.notify(message)

        analyze_ids: set[Any] | None = None
        if len(clips) > _MAX_AUTO_ANALYZE_BURST:
            newest = sorted(clips, key=lambda c: c.get("timestamp") or "")[
                -_MAX_AUTO_ANALYZE_BURST:
            ]
            analyze_ids = {c.get("id") for c in newest}
            _LOGGER.info(
                "Downloaded a burst of %d clips at once (backlog catch-up or "
                "first run) — auto-analyzing only the %d most recent; the "
                "rest are in the library and analyzable on demand via "
                "Analyze Now.",
                len(clips),
                _MAX_AUTO_ANALYZE_BURST,
            )

        for clip in clips:
            analyze = analyze_ids is None or clip.get("id") in analyze_ids
            await self._handle_downloaded_clip(clip, analyze=analyze)

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

    async def _handle_downloaded_clip(
        self, clip: dict[str, Any], analyze: bool = True
    ) -> None:
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

        # analyze=False for backlog clips beyond _MAX_AUTO_ANALYZE_BURST (see
        # _on_clips_downloaded) — still downloaded, notified, and stored
        # normally, just not queued for (paid) AI analysis.
        if analyze and self._analysis_queue:
            await self._analysis_queue.enqueue(clip)

        # Record clip in behavior baseline so anomaly scoring improves over time.
        # This runs regardless of whether AI analysis is enabled.
        if self._config.enable_library_db:
            try:
                ts = str(clip.get("timestamp") or "")
                hour = (
                    datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
                    if ts
                    else datetime.now(timezone.utc).hour
                )
                await self._db.record_clip_baseline(
                    camera=str(clip.get("camera") or ""),
                    hour=hour,
                    duration=float(clip.get("duration") or 0),
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Could not record behavior baseline for clip %s: %s",
                    clip.get("id"),
                    exc,
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

    def _trigger_immediate_download(self) -> None:
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

    def _write_stats(self) -> None:
        payload: dict[str, Any] = {
            "last_poll": datetime.now(timezone.utc).isoformat(),
            "session_downloads": self._session_downloads,
            **self._tracker.stats,
            "disk": self._storage.disk_stats(),
        }
        try:
            # Write to a temp file and rename over the target so a crash
            # mid-write can't leave a truncated/corrupt STATS_FILE behind —
            # os.replace() is atomic on the same filesystem. Mirrors
            # BlinkDownloader._persist_auth() / ClipTracker.save().
            tmp_path = STATS_FILE.with_suffix(STATS_FILE.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2))
            os.replace(tmp_path, STATS_FILE)
        except OSError as exc:
            _LOGGER.warning("Could not write stats file: %s", exc)

    async def _reimport_library(self) -> None:
        imported = await import_existing_clips(self._db, self._config.download_path)
        if imported:
            _LOGGER.info("Library re-import: added %d pre-existing clip(s)", imported)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _handle_shutdown(self) -> None:
        _LOGGER.info("Shutdown signal received")
        self._running = False

    async def _shutdown_step(self, name: str, coro) -> None:
        # Each cleanup step is isolated so one failure (e.g. media_server.stop()
        # raising) can't skip the rest — in particular db.close() and
        # tracker.save() must always run or we risk a corrupt DB handle or a
        # lost session count on the next start.
        try:
            await coro
        except Exception:
            _LOGGER.exception("Error during shutdown step %r", name)

    async def _shutdown(self) -> None:
        _LOGGER.info("Shutting down gracefully …")

        # Cancel background tasks.
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

        await self._shutdown_step("media_server.stop", self._media_server.stop())
        await self._shutdown_step("event_watcher.stop", self._event_watcher.stop())
        if self._analysis_queue:
            self._analysis_queue.stop()
        if self._analyzer:
            await self._shutdown_step("analyzer.close", self._analyzer.close())
            if self._analyzer.escalation_analyzer is not None:
                await self._shutdown_step(
                    "analyzer.escalation.close",
                    self._analyzer.escalation_analyzer.close(),
                )
        if self._alert_dispatcher:
            await self._shutdown_step(
                "alert_dispatcher.close", self._alert_dispatcher.close()
            )
        await self._shutdown_step(
            "downloader.disconnect", self._downloader.disconnect()
        )
        await self._shutdown_step("notifier.close", self._notifier.close())
        await self._shutdown_step("db.close", self._db.close())
        try:
            self._tracker.save()
        except Exception:
            _LOGGER.exception("Error during shutdown step 'tracker.save'")

        _LOGGER.info(
            "Blink Clip Downloader stopped. Session downloads: %d",
            self._session_downloads,
        )
