"""The Security Feed tab's API: a grid of near-live camera snapshots.

Independent of Live View by design — it reads blinkpy's cached camera
image rather than opening a session, so the two tabs never contend for the
one live slot. Its layout settings persist to /data like the other
per-feature settings files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _SETTINGS_WRITE_FAILED,
    _json_object,
)

_LOGGER = logging.getLogger(__name__)


class SecurityFeedRoutesMixin(_MediaServerBase):
    """Near-live camera snapshot tiles and their layout."""

    _SECURITY_FEED_SETTINGS_FILE = Path("/data/security_feed_settings.json")

    _SECURITY_FEED_MIN_COLUMNS = 1

    # Past 3 per row, a tile shrinks too small to make out what a snapshot
    # actually shows — 3 itself is already a "fits, but not ideal" upper
    # bound, not a comfortably-recommended default (see
    # _SECURITY_FEED_DEFAULT_COLUMNS below).
    _SECURITY_FEED_MAX_COLUMNS = 3

    _SECURITY_FEED_MIN_REFRESH_SECONDS = 5

    _SECURITY_FEED_MAX_REFRESH_SECONDS = 300

    # 2, not the 3-tile max, so a fresh install defaults to the
    # comfortably-readable end of the range rather than the "fits, but
    # cramped" end.
    _SECURITY_FEED_DEFAULT_COLUMNS = 2

    _SECURITY_FEED_DEFAULT_REFRESH_SECONDS = 15

    def _register_securityfeed_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        # Security Feed endpoints
        app.router.add_get(
            "/api/security-feed/cameras", self._handle_security_feed_cameras
        )
        app.router.add_get(
            "/api/security-feed/settings", self._handle_security_feed_settings_get
        )
        app.router.add_put(
            "/api/security-feed/settings", self._handle_security_feed_settings_put
        )
        app.router.add_get(
            "/api/security-feed/snapshot/{camera}",
            self._handle_security_feed_snapshot,
        )

    async def _handle_security_feed_cameras(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._list_camera_names is None:
            raise web.HTTPServiceUnavailable(text="Security Feed is not available")
        return web.json_response({"cameras": self._list_camera_names()})

    def _read_security_feed_settings(self) -> dict[str, Any]:
        settings = {
            # Empty means "show every camera" — same convention as
            # ai_car_cameras (see config.py), not a distinct sentinel.
            "cameras": [],
            "columns": self._SECURITY_FEED_DEFAULT_COLUMNS,
            "refresh_seconds": self._SECURITY_FEED_DEFAULT_REFRESH_SECONDS,
        }
        if self._SECURITY_FEED_SETTINGS_FILE.exists():
            try:
                data = json.loads(self._SECURITY_FEED_SETTINGS_FILE.read_text())
                if not isinstance(data, dict):
                    raise TypeError("settings must be a JSON object")
                settings["cameras"] = [str(c) for c in data.get("cameras", [])]
                settings["columns"] = self._clamp_security_feed_columns(
                    data.get("columns", self._SECURITY_FEED_DEFAULT_COLUMNS)
                )
                settings["refresh_seconds"] = self._clamp_security_feed_refresh(
                    data.get(
                        "refresh_seconds", self._SECURITY_FEED_DEFAULT_REFRESH_SECONDS
                    )
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not read Security Feed settings file: %s", exc)
        # Same fix as _handle_battery_status/_handle_ai_camera_configs_get: a
        # camera renamed without this add-on directly observing the rename
        # (see ClipDatabase.rename_camera) has nothing to migrate this
        # curated selection away from, so a stale name would otherwise sit
        # in it forever -- invisible in the Customize picker (built from the
        # live list) but still silently narrowing displayedCameras below
        # what the user actually asked for, with no fallback since the
        # *other* selected cameras keep the filtered list non-empty. Only
        # filter when the live list is non-empty, so a startup window
        # before Blink has connected yet can't be misread as "every
        # selected camera is gone" and wipe the selection.
        live_names = self._list_camera_names() if self._list_camera_names else []
        if live_names and settings["cameras"]:
            live_names_lower = {str(n).lower() for n in live_names}
            settings["cameras"] = [
                c for c in settings["cameras"] if str(c).lower() in live_names_lower
            ]
        return settings

    async def _handle_security_feed_settings_get(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        settings = self._read_security_feed_settings()
        return web.json_response(settings)

    async def _handle_security_feed_settings_put(
        self, request: web.Request
    ) -> web.Response:
        body = await _json_object(request)
        cameras = body.get("cameras", [])
        if not isinstance(cameras, list):
            raise web.HTTPBadRequest(text="cameras must be a list")
        settings = {
            "cameras": [str(c) for c in cameras],
            "columns": self._clamp_security_feed_columns(
                body.get("columns", self._SECURITY_FEED_DEFAULT_COLUMNS)
            ),
            "refresh_seconds": self._clamp_security_feed_refresh(
                body.get("refresh_seconds", self._SECURITY_FEED_DEFAULT_REFRESH_SECONDS)
            ),
        }
        try:
            self._SECURITY_FEED_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
        except OSError as exc:
            _LOGGER.warning("Could not save Security Feed settings: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc
        return web.json_response(settings)

    @classmethod
    def _clamp_security_feed_columns(cls, value: Any) -> int:
        try:
            columns = int(value)
        except (TypeError, ValueError):
            return cls._SECURITY_FEED_DEFAULT_COLUMNS
        return max(
            cls._SECURITY_FEED_MIN_COLUMNS, min(cls._SECURITY_FEED_MAX_COLUMNS, columns)
        )

    @classmethod
    def _clamp_security_feed_refresh(cls, value: Any) -> int:
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return cls._SECURITY_FEED_DEFAULT_REFRESH_SECONDS
        return max(
            cls._SECURITY_FEED_MIN_REFRESH_SECONDS,
            min(cls._SECURITY_FEED_MAX_REFRESH_SECONDS, seconds),
        )

    async def _handle_security_feed_snapshot(
        self, request: web.Request
    ) -> web.Response:
        if self._get_camera_snapshot is None:
            raise web.HTTPServiceUnavailable(text="Security Feed is not available")
        camera = request.match_info["camera"]
        image = await self._get_camera_snapshot(camera)
        if image is None:
            raise web.HTTPNotFound(text="No snapshot available for this camera")
        # Live content — must never be cached, same reasoning as the Live
        # View HLS route.
        return web.Response(
            body=image,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    def _migrate_security_feed_settings(self, old_name: str, new_name: str) -> None:
        settings = self._read_security_feed_settings()
        cameras = settings.get("cameras", [])
        renamed_cameras = [
            new_name if str(camera).lower() == old_name.lower() else camera
            for camera in cameras
        ]
        if renamed_cameras == cameras:
            return
        settings["cameras"] = list(dict.fromkeys(renamed_cameras))
        try:
            self._SECURITY_FEED_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
        except OSError as exc:
            _LOGGER.warning(
                "Could not save security feed settings after camera rename: %s",
                exc,
            )
            raise
