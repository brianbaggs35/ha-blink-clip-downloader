"""Blink authentication and clip downloading via blinkpy."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from blinkpy import api as blink_api
from blinkpy.auth import Auth, BlinkTwoFARequiredError, UnauthorizedError
from blinkpy.blinkpy import Blink

from .config import AppConfig
from .database import ClipDatabase
from .storage import StorageManager
from .tracker import ClipTracker

_LOGGER = logging.getLogger(__name__)

AUTH_FILE = Path("/data/auth_credentials.json")
TWO_FA_FILE = Path("/data/two_fa_code.txt")

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

    # ------------------------------------------------------------------
    # Public: web-UI 2FA submission
    # ------------------------------------------------------------------

    def submit_two_fa_code(self, code: str) -> None:
        """Accept a sanitised 6-digit 2FA code from the web UI."""
        self._two_fa_code = code.strip()
        if self._two_fa_event is not None:
            self._two_fa_event.set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Authenticate with Blink, reusing cached tokens when possible."""
        self.auth_state = "authenticating"
        self.auth_message = "Connecting to Blink…"
        session = await self._get_session()

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
                use_cached = True
                _LOGGER.debug("Loaded cached Blink auth credentials")
            except (json.JSONDecodeError, KeyError):
                _LOGGER.warning("Cached auth file is corrupt; will re-authenticate")

        auth = Auth(login_data=login_data, no_prompt=True, session=session)
        self._blink = Blink(session=session)
        self._blink.auth = auth

        try:
            await self._blink.start()
        except BlinkTwoFARequiredError:
            await self._handle_2fa()
        except UnauthorizedError as e:
            # Cached tokens or credentials are invalid
            if use_cached:
                _LOGGER.warning(
                    "Cached auth tokens invalid or expired. "
                    "Retrying with fresh username/password."
                )
                AUTH_FILE.unlink(missing_ok=True)
                # Retry with fresh credentials (recursive call, non-cached)
                await asyncio.sleep(0.5)
                return await self.connect()
            # No cache was used and auth still failed → credentials are wrong
            self.auth_state = "error"
            self.auth_message = "Authentication failed. Check your Blink credentials."
            _LOGGER.error("Authentication failed: %s", str(e))
            raise
        except Exception as e:  # noqa: BLE001 pylint: disable=broad-exception-caught
            self.auth_state = "error"
            self.auth_message = "Authentication failed. Check your Blink credentials."
            _LOGGER.error("Authentication error: %s", str(e))
            # Delete cached auth to force fresh login on next retry
            AUTH_FILE.unlink(missing_ok=True)
            raise

        # Validate blink object is properly initialized after start().
        # In some blinkpy versions, start() may not raise but still fail
        # to initialize, leaving attributes like urls as None.
        if not hasattr(self._blink, "account_id") or not self._blink.account_id:
            _LOGGER.error(
                "Blink authentication failed silently; start() completed "
                "but account_id is not set. This may indicate invalid "
                "credentials or a Blink API issue."
            )
            AUTH_FILE.unlink(missing_ok=True)
            self.auth_state = "error"
            self.auth_message = "Authentication failed. Check your Blink credentials."
            raise RuntimeError(
                "Blink authentication failed; check credentials and Blink API status."
            )

        self.auth_state = "connected"
        self.auth_message = ""
        self._persist_auth()
        _LOGGER.info("Connected to Blink (account_id=%s)", self._blink.account_id)

    async def disconnect(self) -> None:
        """Log out from Blink and close the HTTP session."""
        if self._blink:
            try:
                await blink_api.request_logout(self._blink)
            except Exception:  # noqa: BLE001 pylint: disable=broad-exception-caught
                pass
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
            # BlinkOwl / BlinkLotus (standalone mini cameras) don't have USB.
            if not getattr(sync, "local_storage", False):
                _LOGGER.debug(
                    "Sync module %r: local storage not active — skipping",
                    sync_name,
                )
                continue

            try:
                await sync.update_local_storage_manifest()
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.warning(
                    "Could not refresh local-storage manifest for %r: %s",
                    sync_name,
                    exc,
                )
                continue

            manifest = getattr(sync, "_local_storage", {}).get("manifest") or set()
            if not manifest:
                _LOGGER.debug(
                    "Sync module %r: local-storage manifest is empty", sync_name
                )
                continue

            _LOGGER.debug(
                "Sync module %r: %d clip(s) in local-storage manifest",
                sync_name,
                len(manifest),
            )

            for item in manifest:
                # Prefix "local_" keeps these IDs disjoint from cloud IDs.
                clip_id = f"local_{item.id}"

                if self._tracker.is_downloaded(clip_id):
                    continue

                if self._storage.is_over_quota():
                    _LOGGER.warning(
                        "Storage quota reached — stopping local-storage download"
                    )
                    return results

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
                    continue

                try:
                    _LOGGER.info(
                        "Preparing local-storage clip %s from %r (~%.1f KB)",
                        item.id,
                        camera_name,
                        (item.size or 0) / 1024,
                    )
                    await item.prepare_download(self._blink)
                    success = await item.download_video(
                        self._blink,
                        str(dest),
                        max_retries=self._config.retry_attempts,
                    )
                except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                    _LOGGER.error(
                        "Error downloading local-storage clip %s: %s", item.id, exc
                    )
                    if dest.exists():
                        dest.unlink(missing_ok=True)
                    continue

                if not success:
                    _LOGGER.warning(
                        "Local-storage clip %s download failed (retries exhausted)",
                        item.id,
                    )
                    if dest.exists():
                        dest.unlink(missing_ok=True)
                    continue

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
                results.append(result)

        return results

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
        """Return True if the clip's creation time falls in the configured window."""
        raw = clip.get("created_at", "")
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            clip_time = ts.strftime("%H:%M")
        except (ValueError, AttributeError):
            return True  # Keep clip if we can't parse the time.

        start = self._config.time_window_start
        end = self._config.time_window_end

        if start and clip_time < start:
            return False
        if end and clip_time > end:
            return False
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
                thumb_url = clip.get("thumbnail", "")
                if thumb_url:
                    thumb_dest = dest.with_suffix(".jpg")
                    await self._stream_to_file(self._resolve_url(thumb_url), thumb_dest)

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
        # Safely handle cases where urls or base_url might be None.
        try:
            if hasattr(self._blink, "urls") and self._blink.urls:
                base = getattr(self._blink.urls, "base_url", None)
                if base:
                    return f"{base}{url}"
        except AttributeError:
            pass
        # Fallback to default Blink base URL if urls is not available.
        return f"https://rest-prod.immedia-semi.com{url}"

    async def _stream_to_file(self, url: str, dest: Path) -> int | None:
        """Stream *url* to *dest*, retrying up to *retry_attempts* times.

        Returns the number of bytes written, or None on failure.
        """
        headers = getattr(getattr(self._blink, "auth", None), "header", {}) or {}
        session = await self._get_session()

        for attempt in range(1, self._config.retry_attempts + 1):
            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning(
                            "HTTP %d for %s (attempt %d)", resp.status, url, attempt
                        )
                        return None

                    size = 0
                    async with aiofiles.open(dest, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                            await fh.write(chunk)
                            size += len(chunk)
                    return size

            except aiohttp.ClientError as exc:
                _LOGGER.warning(
                    "Download attempt %d/%d failed for %s: %s",
                    attempt,
                    self._config.retry_attempts,
                    url,
                    exc,
                )
                if attempt < self._config.retry_attempts:
                    await asyncio.sleep(self._config.retry_delay * attempt)

        # All attempts exhausted — clean up partial file.
        if dest.exists():
            dest.unlink()
        return None

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

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.two_fa_timeout
        while loop.time() < deadline:
            # --- Check web-UI submission first (code already set before event) ---
            if self._two_fa_event.is_set():
                code = self._two_fa_code or ""
                self._two_fa_event.clear()
                self._two_fa_code = None
                if code and self._blink:
                    await self._blink.send_2fa_code(code)
                    # After 2FA submission, call start() again to complete init.
                    await self._blink.start()
                    return

            # --- File fallback (CLI / backwards compat) ---
            if TWO_FA_FILE.exists():
                code = TWO_FA_FILE.read_text(encoding="utf-8").strip()
                if code and self._blink:
                    TWO_FA_FILE.unlink(missing_ok=True)
                    await self._blink.send_2fa_code(code)
                    # After 2FA submission, call start() again to complete init.
                    await self._blink.start()
                    return

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

    def _persist_auth(self) -> None:
        """Save the current auth token to disk for next startup."""
        if self._blink and self._blink.auth:
            try:
                attrs = self._blink.auth.login_attributes
                AUTH_FILE.write_text(json.dumps(attrs, indent=2))
            except Exception as exc:  # noqa: BLE001 pylint: disable=broad-exception-caught
                _LOGGER.warning("Could not persist auth credentials: %s", exc)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
