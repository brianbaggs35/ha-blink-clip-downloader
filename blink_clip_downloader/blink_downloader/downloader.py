"""Blink authentication and clip downloading via blinkpy."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from blinkpy import api as blink_api
from blinkpy.auth import Auth, BlinkTwoFARequiredError, UnauthorizedError
from blinkpy.blinkpy import Blink

from .blinkpy_compat import patch_oauth_signin_2fa_status
from .config import AppConfig
from .database import ClipDatabase
from .storage import StorageManager
from .tracker import ClipTracker

_LOGGER = logging.getLogger(__name__)

# Work around blinkpy issues #1233/#1230 (Blink's OAuth signin endpoint now
# returns HTTP 202 for accounts that need 2FA, which blinkpy doesn't yet
# recognise). See blinkpy_compat.py for details.
patch_oauth_signin_2fa_status()

AUTH_FILE = Path("/data/auth_credentials.json")
TWO_FA_FILE = Path("/data/two_fa_code.txt")
HARDWARE_ID_FILE = Path("/data/blink_hardware_id.txt")

# Blink returns up to 25 clips per page by default.
_PAGE_SIZE = 25
# Download stream chunk size (64 KiB).
_CHUNK_SIZE = 65_536


class TwoFARequired(Exception):
    """Raised when Blink requires 2FA and no code is available within the timeout."""


class AuthenticationError(Exception):
    """Raised on unrecoverable login failure."""


class BlinkDownloader:  # pylint: disable=too-many-instance-attributes
    """Handles Blink authentication and streaming clip downloads."""

    def __init__(
        self,
        config: AppConfig,
        storage: StorageManager,
        tracker: ClipTracker,
        db: ClipDatabase | None = None,
    ) -> None:
        self._config = config
        self._storage = storage
        self._tracker = tracker
        self._db = db
        self._blink: Blink | None = None
        self._session: aiohttp.ClientSession | None = None
        # Auth state exposed to the web UI.
        self.auth_state: str = "disconnected"
        self.auth_message: str = ""
        # Set by submit_two_fa_code(); cleared after each use.
        self._two_fa_event: asyncio.Event | None = None
        self._two_fa_code: str | None = None
        # 2FA submission/result tracking, exposed to the web UI so it can
        # tell whether a *specific* submission was accepted or rejected.
        self.two_fa_seq: int = 0
        self.two_fa_result_seq: int = 0
        self.two_fa_result_ok: bool | None = None

    # ------------------------------------------------------------------
    # Public: web-UI 2FA submission
    # ------------------------------------------------------------------

    def submit_two_fa_code(self, code: str) -> int:
        """Accept a sanitised 6-digit 2FA code from the web UI.

        Returns a sequence number the caller can compare against
        ``two_fa_result_seq``/``two_fa_result_ok`` to learn whether this
        specific submission was accepted or rejected.
        """
        self._two_fa_code = code.strip()
        self.two_fa_seq += 1
        if self._two_fa_event is not None:
            self._two_fa_event.set()
        return self.two_fa_seq

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Authenticate with Blink, reusing cached tokens when possible."""
        self.auth_state = "authenticating"
        self.auth_message = "Connecting to Blink…"
        session = self._get_session()

        login_data: dict[str, Any] = {
            "username": self._config.username,
            "password": self._config.password,
        }
        # Try with cached auth first
        use_cached = False
        if AUTH_FILE.exists():
            try:
                cached = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
                login_data.update(cached)
                # blinkpy's persisted login_attributes include the
                # username/password that were active when the token was
                # cached. Always prefer the values currently configured for
                # the add-on so a credential change (e.g. after Blink forces
                # a password reset) takes effect immediately instead of being
                # silently overwritten by the stale cached values.
                login_data["username"] = self._config.username
                login_data["password"] = self._config.password
                use_cached = True
                _LOGGER.debug("Loaded cached Blink auth credentials")
            except (KeyError, TypeError, ValueError):
                # json.JSONDecodeError is a ValueError subclass, already covered.
                _LOGGER.warning("Cached auth file is corrupt; will re-authenticate")

        # blinkpy's OAuth v2 flow identifies this installation to Blink with a
        # per-device "hardware_id" and generates a random one whenever an
        # Auth() instance isn't given one. _persist_auth() only writes
        # AUTH_FILE on a *successful* login, so without this every retry
        # after a failed/expired login would present Blink with a brand new
        # "device" using the same credentials -- a pattern that can trigger
        # Blink's fraud detection and cause otherwise-valid credentials (or a
        # freshly entered 2FA code) to be rejected. Persist a stable ID so
        # retries and add-on restarts all look like the same device.
        login_data["hardware_id"] = self._get_or_create_hardware_id(
            login_data.get("hardware_id")
        )

        auth = Auth(login_data=login_data, no_prompt=True, session=session)
        self._blink = Blink(session=session)
        self._blink.auth = auth

        try:
            started = await self._blink.start()
        except BlinkTwoFARequiredError:
            await self._handle_2fa()
            started = True
        except UnauthorizedError:
            # blinkpy >= 0.25's OAuth v2 flow doesn't raise this for bad
            # credentials (start() returns False instead) but handle it the
            # same way in case an older/newer blinkpy raises it directly.
            started = False
        except Exception as e:  # noqa: BLE001 pylint: disable=broad-exception-caught
            self.auth_state = "error"
            self.auth_message = "Authentication failed. Check your Blink credentials."
            _LOGGER.exception("Authentication error: %s", str(e))
            # Delete cached auth to force fresh login on next retry
            AUTH_FILE.unlink(missing_ok=True)
            raise

        # blinkpy >= 0.25's start() returns False (without raising) when the
        # OAuth login/refresh flow fails, leaving account_id unset. Treat
        # that the same as an invalid-credentials error: retry once with
        # fresh credentials if a cache was used, otherwise report a clear,
        # actionable authentication error.
        if not started or not self._blink.account_id:
            if use_cached:
                _LOGGER.warning(
                    "Cached auth tokens invalid or expired. "
                    "Retrying with fresh username/password."
                )
                AUTH_FILE.unlink(missing_ok=True)
                # Close stale session and create fresh one
                if self._session is not None:
                    await self._session.close()
                    self._session = None
                # Retry with fresh credentials and fresh session
                await asyncio.sleep(0.5)
                return await self.connect()

            # No cache was used and auth still failed → credentials are wrong
            # (or Blink requires additional verification).
            self.auth_state = "error"
            self.auth_message = "Authentication failed. Check your Blink credentials."
            message = (
                "Blink rejected the configured username/password, or the "
                "login could not be completed. Verify the credentials in "
                "the add-on configuration are correct and that the Blink "
                "account does not require additional verification, then "
                "restart the add-on."
            )
            _LOGGER.error(message)
            raise AuthenticationError(message)

        self.auth_state = "connected"
        self.auth_message = ""
        self._persist_auth()
        _LOGGER.info("Connected to Blink (account_id=%s)", self._blink.account_id)

    async def disconnect(self) -> None:
        """Close the local HTTP session on shutdown, without revoking the
        Blink auth token server-side.

        This deliberately does NOT call Blink's ``/client/{id}/logout``
        endpoint. Add-on restarts (Supervisor restart, watchdog, a config
        change) go through this same shutdown path, and revoking the token
        here defeats the whole point of persisting it in ``AUTH_FILE`` for
        reuse (see :meth:`connect`) — every restart would then be forced
        into a full username/password OAuth login instead of a quiet
        token-based reconnect. A full re-login is exactly the kind of
        account-wide auth event that can knock other clients on the same
        Blink account (e.g. Home Assistant's own Blink integration) into a
        temporarily unauthenticated state, surfacing as camera sensors
        (battery, etc.) going "unavailable" until that integration reloads.
        Leaving the token valid server-side means our own restarts never
        trigger that.
        """
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def account_id(self) -> str | None:
        """Return the currently authenticated Blink account ID, if any."""
        return getattr(self._blink, "account_id", None)

    # ------------------------------------------------------------------
    # Downloading
    # ------------------------------------------------------------------

    async def download_new_clips(self) -> list[dict[str, Any]]:
        """Fetch and download all clips not yet in the tracker.

        Returns a list of result dicts for each successfully downloaded clip.
        """
        if self._blink is None:
            raise RuntimeError("Call connect() before download_new_clips()")

        # Determine since-time: last download or 24 h ago on first run.
        since = self._tracker.last_download_time
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=24)

        clips = await self._fetch_clip_list(since)
        if not clips:
            _LOGGER.debug("No new clips from Blink API")
            return []

        new_clips = [
            c for c in clips if not self._tracker.is_downloaded(str(c.get("id", "")))
        ]
        if not new_clips:
            _LOGGER.debug("All %d clip(s) already downloaded", len(clips))
            return []

        has_backlog = len(new_clips) > self._config.max_clips_per_poll
        new_clips = new_clips[: self._config.max_clips_per_poll]
        _LOGGER.info("Downloading %d new clip(s)", len(new_clips))

        semaphore = asyncio.Semaphore(self._config.concurrent_downloads)
        tasks = [self._download_clip(clip, semaphore) for clip in new_clips]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[dict[str, Any]] = []
        for clip, result in zip(new_clips, raw_results):
            if isinstance(result, Exception):
                _LOGGER.error("Failed to download clip %s: %s", clip.get("id"), result)
            elif isinstance(result, dict):
                results.append(result)

        if has_backlog:
            # mark_downloaded() advances the tracker's cursor to "now" for
            # every clip downloaded above, but max_clips_per_poll left some
            # older clips in `clips` undownloaded. If the cursor were allowed
            # to advance, the next poll's since= filter would start after
            # those clips' creation time and the Blink API would never
            # return them again — permanently dropping backlog. Holding the
            # cursor at this poll's `since` means the next poll re-fetches
            # the same window; already-downloaded clips are filtered out via
            # is_downloaded() as usual, so only the real backlog remains.
            self._tracker.set_last_download_time(since)
            _LOGGER.info(
                "Backlog remains after this poll (max_clips_per_poll=%d); "
                "holding download cursor at %s",
                self._config.max_clips_per_poll,
                since.isoformat(),
            )

        self._tracker.save()
        self._persist_auth()
        return results

    # ------------------------------------------------------------------
    # Public: Sync Module local storage (USB drive)
    # ------------------------------------------------------------------

    async def download_local_storage_clips(self) -> list[dict[str, Any]]:
        """Download clips from every Blink Sync Module's USB local storage.

        Blink's API does not offer direct LAN access to the Sync Module's USB
        drive.  The workflow is:

        1. Refresh the manifest (list of clips on the USB drive).
        2. For each clip not yet in the tracker, call ``prepare_download()``
           which tells the Sync Module to upload the clip to Blink's cloud.
        3. Download the freshly-uploaded clip from the cloud URL.

        Returns a list of result dicts in the same format as
        :meth:`download_new_clips`, including ``"source": "local_storage"``
        so they appear correctly in the web UI and HA events.
        """
        if self._blink is None:
            return []

        results: list[dict[str, Any]] = []

        for sync_name, sync in self._blink.sync.items():
            manifest = await self._refresh_local_storage_manifest(sync_name, sync)
            if not manifest:
                continue

            quota_hit = await self._download_local_storage_manifest(manifest, results)
            if quota_hit:
                return results

        self._tracker.save()
        return results

    async def _refresh_local_storage_manifest(
        self, sync_name: str, sync: Any
    ) -> set | None:
        """Refresh and return a sync module's local-storage manifest.

        Returns None if this sync module has no USB storage, the refresh
        failed, or the manifest came back empty — the caller should skip it.
        """
        # BlinkOwl / BlinkLotus (standalone mini cameras) don't have USB.
        if not getattr(sync, "local_storage", False):
            _LOGGER.debug(
                "Sync module %r: local storage not active — skipping", sync_name
            )
            return None

        try:
            await sync.update_local_storage_manifest()
        except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
            _LOGGER.warning(
                "Could not refresh local-storage manifest for %r: %s", sync_name, exc
            )
            return None

        manifest = getattr(sync, "_local_storage", {}).get("manifest") or set()
        if not manifest:
            _LOGGER.debug("Sync module %r: local-storage manifest is empty", sync_name)
            return None

        _LOGGER.debug(
            "Sync module %r: %d clip(s) in local-storage manifest",
            sync_name,
            len(manifest),
        )
        return manifest

    async def _download_local_storage_manifest(
        self, manifest: set, results: list[dict[str, Any]]
    ) -> bool:
        """Download each not-yet-tracked clip in *manifest*, appending to *results*.

        Returns True if the storage quota was hit and downloading should stop.
        """
        for item in manifest:
            # Prefix "local_" keeps these IDs disjoint from cloud IDs.
            clip_id = f"local_{item.id}"

            if self._tracker.is_downloaded(clip_id):
                continue

            if self._storage.is_over_quota():
                _LOGGER.warning(
                    "Storage quota reached — stopping local-storage download"
                )
                self._tracker.save()
                return True

            result = await self._download_local_storage_item(clip_id, item)
            if result is not None:
                results.append(result)

        return False

    async def _download_local_storage_item(
        self, clip_id: str, item: Any
    ) -> dict[str, Any] | None:
        camera_name: str = item.name or "Unknown"

        # blinkpy stores created_at as a datetime.datetime object.
        ts = item.created_at
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                ts = datetime.now(timezone.utc)

        dest = self._storage.resolve_path(camera_name, ts, clip_id)
        dest.parent.mkdir(parents=True, exist_ok=True)

        if dest.exists():
            size = dest.stat().st_size
            self._tracker.mark_downloaded(clip_id, size)
            return None

        try:
            _LOGGER.info(
                "Preparing local-storage clip %s from %r (~%.1f KB)",
                item.id,
                camera_name,
                int(item.size or 0) / 1024,
            )
            await item.prepare_download(self._blink)
            success = await item.download_video(
                self._blink,
                str(dest),
                max_retries=self._config.retry_attempts,
            )
        except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
            _LOGGER.exception(
                "Error downloading local-storage clip %s: %s", item.id, exc
            )
            if dest.exists():
                dest.unlink(missing_ok=True)
            return None

        if not success:
            _LOGGER.warning(
                "Local-storage clip %s download failed (retries exhausted)", item.id
            )
            if dest.exists():
                dest.unlink(missing_ok=True)
            return None

        size = dest.stat().st_size if dest.exists() else 0
        self._tracker.mark_downloaded(clip_id, size)
        _LOGGER.info(
            "Downloaded local-storage clip %s from %r → %s (%d KB)",
            item.id,
            camera_name,
            dest,
            size // 1024,
        )

        result: dict[str, Any] = {
            "id": clip_id,
            "camera": camera_name,
            "path": str(dest),
            "timestamp": ts.isoformat(),
            "size_bytes": size,
            "network_id": 0,
            "duration": 0,
            "source": "local_storage",
        }
        if self._db:
            await self._db.add_clip(result)
        return result

    # ------------------------------------------------------------------
    # Internal: clip list
    # ------------------------------------------------------------------

    async def _fetch_clip_list(self, since: datetime) -> list[dict[str, Any]]:
        """Retrieve the paginated clip list from Blink.

        blinkpy >= 0.22 returns the parsed JSON dict directly from
        request_videos() (via auth.query → validate_response with
        json_resp=True).  Non-200 responses raise exceptions rather than
        returning an error response object.
        """
        clips: list[dict[str, Any]] = []
        since_epoch = since.timestamp()
        page = 0

        while True:
            try:
                data = await blink_api.request_videos(
                    self._blink, time=since_epoch, page=page
                )
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.warning("request_videos failed (page %d): %s", page, exc)
                break

            if not isinstance(data, dict):
                _LOGGER.warning(
                    "request_videos returned unexpected type %s; stopping pagination",
                    type(data).__name__,
                )
                break

            media: list[dict] = data.get("media") or []
            if not media:
                break
            clips.extend(media)
            # Stop paginating when we get a partial page.
            if len(media) < _PAGE_SIZE:
                break
            page += 1

        clips = self._apply_filters(clips)
        return clips

    def _apply_filters(self, clips: list[dict]) -> list[dict]:
        """Apply camera whitelist, motion-only, time-window, and deleted filters."""
        # Always skip clips marked as deleted by Blink.
        clips = [c for c in clips if not c.get("deleted", False)]

        if self._config.camera_filter:
            allowed = {c.lower() for c in self._config.camera_filter}
            clips = [c for c in clips if c.get("device_name", "").lower() in allowed]

        if self._config.motion_only:
            clips = [c for c in clips if c.get("source", "") == "pir"]

        if self._config.time_window_start or self._config.time_window_end:
            clips = [c for c in clips if self._in_time_window(c)]

        if self._config.min_clip_duration > 0:
            clips = [
                c
                for c in clips
                if int(c.get("duration", 0) or 0) >= self._config.min_clip_duration
            ]

        return clips

    def _in_time_window(self, clip: dict) -> bool:
        """Return True if the clip's creation time falls in the configured window.

        ``time_window_start > time_window_end`` denotes an overnight window
        that wraps past midnight (e.g. "22:00"-"06:00" for nighttime-only
        downloads).
        """
        raw = clip.get("created_at", "")
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            # time_window_start/end are documented as local HH:MM; the Blink
            # API returns created_at in UTC, so convert before comparing or
            # the window silently anchors to UTC instead of local wall-clock.
            clip_time = ts.astimezone().strftime("%H:%M")
        except (ValueError, AttributeError):
            return True  # Keep clip if we can't parse the time.

        start = self._config.time_window_start
        end = self._config.time_window_end

        if start and end:
            if start <= end:
                return start <= clip_time <= end
            return clip_time >= start or clip_time <= end
        if start:
            return clip_time >= start
        if end:
            return clip_time <= end
        return True

    # ------------------------------------------------------------------
    # Internal: single clip download
    # ------------------------------------------------------------------

    async def _download_clip(
        self, clip: dict, semaphore: asyncio.Semaphore
    ) -> dict[str, Any] | None:
        async with semaphore:
            clip_id = str(clip.get("id", ""))
            camera_name = clip.get("device_name", "unknown")
            # Blink API returns the video URL in the "media" field.
            url = clip.get("media", "")
            created_str = clip.get("created_at", "")

            try:
                timestamp = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                timestamp = datetime.now(timezone.utc)

            if not url:
                _LOGGER.warning("Clip %s has no media URL, skipping", clip_id)
                return None

            if self._storage.is_over_quota():
                _LOGGER.warning("Storage quota reached, skipping clip %s", clip_id)
                return None

            dest = self._storage.resolve_path(camera_name, timestamp, clip_id)
            dest.parent.mkdir(parents=True, exist_ok=True)

            # Already on disk but not in tracker (e.g. tracker was reset).
            if dest.exists():
                size = dest.stat().st_size
                self._tracker.mark_downloaded(clip_id, size)
                return {
                    "id": clip_id,
                    "camera": camera_name,
                    "path": str(dest),
                    "timestamp": timestamp.isoformat(),
                    "size_bytes": size,
                    "skipped": True,
                }

            full_url = self._resolve_url(url)
            size = await self._stream_to_file(full_url, dest)
            if size is None:
                return None

            self._tracker.mark_downloaded(clip_id, size)
            _LOGGER.info(
                "Downloaded %s from %r → %s (%d KB)",
                clip_id,
                camera_name,
                dest,
                size // 1024,
            )

            if self._config.download_thumbnails:
                await self._generate_thumbnail(dest, dest.with_suffix(".jpg"))

            # Normalise nullable Blink API fields before storing — the API
            # returns null (→ Python None) for duration/network_id on some
            # clip types (live-view, certain cameras).  Coerce to safe types
            # so downstream consumers (database, notifier, sensor) never see
            # NoneType where they expect int/str.
            result = {
                "id": clip_id,
                "camera": camera_name,
                "path": str(dest),
                "timestamp": timestamp.isoformat(),
                "size_bytes": size,
                "network_id": int(clip.get("network_id") or 0),
                "duration": int(clip.get("duration") or 0),
                "source": str(clip.get("source") or ""),
            }
            if self._db:
                await self._db.add_clip(result)
            return result

    def _resolve_url(self, url: str) -> str:
        """Prepend the Blink base URL to relative paths."""
        if url.startswith("http"):
            return url
        if self._blink is not None:
            try:
                blink_urls = getattr(self._blink, "urls", None)
                if blink_urls:
                    base = getattr(blink_urls, "base_url", None)
                    if base:
                        return f"{base}{url}"
            except AttributeError:
                pass
        return f"https://rest-prod.immedia-semi.com{url}"

    async def _stream_to_file(self, url: str, dest: Path) -> int | None:
        """Stream *url* to *dest*, retrying up to *retry_attempts* times.

        Returns the number of bytes written, or None on failure.
        """
        headers = getattr(getattr(self._blink, "auth", None), "header", {}) or {}
        session = self._get_session()

        for attempt in range(1, self._config.retry_attempts + 1):
            try:
                size = await self._stream_attempt(session, url, dest, headers, attempt)
                if size is not None:
                    return size

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                # aiohttp raises asyncio.TimeoutError (not a ClientError
                # subclass) when the ClientTimeout(total=...) above expires,
                # so it must be caught alongside ClientError or a slow/hung
                # server would skip retries entirely and leave a partial file
                # behind (only the loop-exit cleanup below removes it).
                # OSError (e.g. disk full, permission error) from the aiofiles
                # write must be caught the same way — otherwise a partial file
                # is left on disk and a later poll's `dest.exists()` pre-check
                # in _download_clip treats that truncated file as a completed
                # download, permanently.
                _LOGGER.warning(
                    "Download attempt %d/%d failed for %s: %s",
                    attempt,
                    self._config.retry_attempts,
                    url,
                    exc,
                )
                if dest.exists():
                    dest.unlink()

            if attempt < self._config.retry_attempts:
                await asyncio.sleep(self._config.retry_delay * attempt)

        # All attempts exhausted — clean up partial file.
        if dest.exists():
            dest.unlink()
        return None

    async def _stream_attempt(
        self,
        session: aiohttp.ClientSession,
        url: str,
        dest: Path,
        headers: dict[str, str],
        attempt: int,
    ) -> int | None:
        """Try a single download attempt; returns bytes written, or None on non-200."""
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "HTTP %d for %s (attempt %d/%d)",
                    resp.status,
                    url,
                    attempt,
                    self._config.retry_attempts,
                )
                # Non-200 gets the same retry treatment as a ClientError
                # instead of giving up immediately - a transient 5xx/429 from
                # Blink's API is exactly the kind of failure retry_attempts
                # exists to ride out.
                return None

            size = 0
            async with aiofiles.open(dest, "wb") as fh:
                async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                    await fh.write(chunk)
                    size += len(chunk)
            return size

    async def _generate_thumbnail(self, video_path: Path, thumb_path: Path) -> bool:
        """Extract the first frame of *video_path* as a JPEG thumbnail.

        Blink's media API doesn't reliably provide a per-clip thumbnail URL,
        so thumbnails are generated locally with ffmpeg instead. Returns True
        if *thumb_path* was created.
        """
        if thumb_path.exists() or not video_path.exists():
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "5",
                str(thumb_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            _LOGGER.warning("Could not run ffmpeg to generate thumbnail: %s", exc)
            return False

        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg timed out generating thumbnail for %s", video_path)
            # Timing out leaves the child process running; kill it and reap
            # it so it doesn't linger as a zombie/orphan.
            proc.kill()
            await proc.wait()
            return False

        if not thumb_path.exists():
            _LOGGER.warning("ffmpeg did not produce a thumbnail for %s", video_path)
            return False
        return True

    async def backfill_thumbnails(self, limit: int) -> int:
        """Generate missing ``.jpg`` thumbnails for already-downloaded clips.

        Used to fill in thumbnails for clips downloaded before
        ``download_thumbnails`` was enabled, and for clips re-imported by
        :func:`library_scanner.import_existing_clips` after a reinstall.
        Processes at most *limit* clips per call so a large backlog is
        filled in gradually across poll cycles rather than all at once.

        Returns the number of thumbnails generated.
        """
        if not self._config.download_thumbnails or self._db is None:
            return 0

        generated = 0
        for path_str in sorted(await self._db.get_all_file_paths()):
            if generated >= limit:
                break
            video_path = Path(path_str)
            thumb_path = video_path.with_suffix(".jpg")
            if thumb_path.exists() or not video_path.exists():
                continue
            if await self._generate_thumbnail(video_path, thumb_path):
                generated += 1
        return generated

    # ------------------------------------------------------------------
    # Internal: auth helpers
    # ------------------------------------------------------------------

    async def _handle_2fa(self) -> None:
        """Wait for a 2FA code from the web UI or the fallback file."""
        self.auth_state = "needs_2fa"
        self.auth_message = (
            "Enter the 6-digit verification code sent to your registered device."
        )
        _LOGGER.warning(
            "Blink requires 2FA. Enter the code in the web UI or write it to: %s",
            TWO_FA_FILE,
        )

        # Fresh event for this authentication attempt.
        self._two_fa_event = asyncio.Event()
        self._two_fa_code = None
        self.two_fa_result_seq = self.two_fa_seq
        self.two_fa_result_ok = None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.two_fa_timeout
        while loop.time() < deadline:
            code: str | None = None
            seq = self.two_fa_seq

            # --- Check web-UI submission first (code already set before event) ---
            if self._two_fa_event.is_set():
                code = self._two_fa_code or ""
                self._two_fa_event.clear()
                self._two_fa_code = None

            # --- File fallback (CLI / backwards compat) ---
            elif TWO_FA_FILE.exists():
                code = TWO_FA_FILE.read_text(encoding="utf-8").strip()
                TWO_FA_FILE.unlink(missing_ok=True)

            if code and self._blink:
                if await self._submit_2fa_code(code):
                    self.two_fa_result_seq = seq
                    self.two_fa_result_ok = True
                    self.auth_message = ""
                    return

                # Wrong/expired code — record the failure for the web UI and
                # keep waiting so the user can retry before the timeout.
                self.two_fa_result_seq = seq
                self.two_fa_result_ok = False
                self.auth_message = "Incorrect verification code. Please try again."
                _LOGGER.warning(
                    "Blink rejected the submitted 2FA code; awaiting retry."
                )
                continue

            # --- Wait for the event or poll timeout ---
            remaining = deadline - loop.time()
            try:
                await asyncio.wait_for(
                    self._two_fa_event.wait(),
                    timeout=min(2.0, max(0.01, remaining)),
                )
            except asyncio.TimeoutError:
                pass

        self.auth_state = "error"
        self.auth_message = (
            f"Verification code not provided within {self._config.two_fa_timeout:.0f}s."
        )
        raise TwoFARequired(
            f"2FA code was not provided within {self._config.two_fa_timeout:.0f}s. "
            f"Write the code to {TWO_FA_FILE} and restart the add-on."
        )

    async def _submit_2fa_code(self, code: str) -> bool:
        """Submit *code* to blinkpy.

        blinkpy >= 0.25 completes the OAuth v2 2FA flow (and full account
        setup) inside ``send_2fa_code()`` and returns ``True``/``False`` to
        indicate whether the code was accepted — a wrong or expired code
        returns ``False`` without raising. Calling ``start()`` again
        afterwards is unnecessary and would re-run the whole login flow, so
        we rely on this return value directly.
        """
        assert self._blink is not None
        try:
            return bool(await self._blink.send_2fa_code(code))
        except BlinkTwoFARequiredError:
            return False
        except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
            _LOGGER.warning("2FA verification failed: %s", exc)
            return False

    def _persist_auth(self) -> None:
        """Save the current auth token to disk for next startup."""
        if self._blink and self._blink.auth:
            try:
                attrs = self._blink.auth.login_attributes
                # Write to a temp file and rename over the target so a crash
                # mid-write can't leave a truncated/corrupt AUTH_FILE behind —
                # os.replace() is atomic on the same filesystem.
                tmp_path = AUTH_FILE.with_suffix(AUTH_FILE.suffix + ".tmp")
                tmp_path.write_text(json.dumps(attrs, indent=2))
                os.replace(tmp_path, AUTH_FILE)
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.warning("Could not persist auth credentials: %s", exc)

    @staticmethod
    def _get_or_create_hardware_id(cached_hardware_id: str | None) -> str:
        """Return a stable per-installation hardware ID for blinkpy's OAuth flow.

        Falls back to *cached_hardware_id* (from a previously persisted
        ``AUTH_FILE``) when ``HARDWARE_ID_FILE`` doesn't exist yet, and
        otherwise generates a fresh UUID. The chosen value is (re)written to
        ``HARDWARE_ID_FILE`` so it survives ``AUTH_FILE`` being deleted on a
        failed/expired login.
        """
        try:
            existing = HARDWARE_ID_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""

        hardware_id = existing or cached_hardware_id or str(uuid.uuid4()).upper()

        if hardware_id != existing:
            try:
                HARDWARE_ID_FILE.write_text(hardware_id, encoding="utf-8")
            except OSError as exc:
                _LOGGER.warning("Could not persist Blink hardware ID: %s", exc)

        return hardware_id

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # blinkpy's OAuth v2 / PKCE login flow makes several chained
            # requests to api.oauth.blink.com (authorize -> signin page ->
            # signin -> authorization code) and relies on cookies set by the
            # earlier steps being sent back on the later ones. aiohttp's
            # default CookieJar runs in "safe" mode and can silently refuse
            # to store/return some of those cookies, which makes the signin
            # POST fail validation -- blinkpy then logs "Login failed" with
            # no further detail, even with correct credentials (see
            # fronzbot/blinkpy#1229). An "unsafe" cookie jar matches a real
            # browser/app client and lets the whole OAuth flow share cookies
            # as intended.
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True)
            )
        return self._session
