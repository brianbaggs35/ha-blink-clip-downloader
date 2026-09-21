"""The Status tab's API: what the add-on and the cameras are doing.

Camera list, library and per-camera statistics, the activity chart, the
tag vocabulary, and battery state and history.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .core import _MediaServerBase

_LOGGER = logging.getLogger(__name__)


class StatusRoutesMixin(_MediaServerBase):
    """Cameras, statistics, activity and battery state."""

    def _register_status_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/cameras", self._handle_cameras)
        app.router.add_get("/api/stats", self._handle_stats)
        app.router.add_get("/api/activity", self._handle_activity)
        app.router.add_get("/api/battery/status", self._handle_battery_status)
        app.router.add_get(
            "/api/battery/history/{camera}", self._handle_battery_history
        )
        app.router.add_get("/api/tags", self._handle_tags)

    async def _handle_cameras(self, _request: web.Request) -> web.Response:
        """Per-camera clip stats, backing the Library nav/filter sidebar.

        get_camera_stats() is purely clip-history-based (GROUP BY over
        clips), so a camera the account currently reports but that has
        never had a clip downloaded -- just installed, or renamed/replaced
        before this add-on's identity-tracking (downloader.py) ever saw it
        under any name, so there is nothing for it to migrate -- would
        otherwise never appear here at all, even though it's a completely
        real, live camera. The AI/Vehicles tabs' camera-configs endpoint
        already unions in the live camera list for exactly this reason
        (see _handle_ai_camera_configs_get); do the same here so a
        brand-new camera is at least visible (with zero stats) instead of
        unreachable from the nav sidebar until its first clip downloads.
        Purely additive -- a camera with real clip history (including one
        no longer live, e.g. after a rename this add-on never observed) is
        untouched, since those historical clips are still real and worth
        keeping reachable; nothing is ever removed here.
        """
        camera_stats = await self._db.get_camera_stats()
        existing_lower = {str(row.get("camera", "")).lower() for row in camera_stats}
        live_names = self._list_camera_names() if self._list_camera_names else []
        for name in live_names:
            if name.lower() in existing_lower:
                continue
            camera_stats.append(
                {
                    "camera": name,
                    "total": 0,
                    "size_bytes": 0,
                    "today": 0,
                    "this_week": 0,
                    "last_seen": "",
                }
            )
            existing_lower.add(name.lower())
        return web.json_response(camera_stats)

    async def _handle_battery_status(self, _request: web.Request) -> web.Response:
        """Current battery state for every camera with a recorded reading.

        Deliberately a separate namespace from /api/cameras above, not
        folded into it — that endpoint is derived from get_camera_stats()
        and only lists cameras with at least one downloaded clip, which is
        the wrong scope for battery status (should reflect every camera
        Blink reports, regardless of clip history).

        battery_history rows are camera-name-keyed and only ever migrated
        to a new name when this add-on directly observes the rename (see
        ClipDatabase.rename_camera) -- a rename from before that tracking
        ever saw the old name (e.g. renamed the moment a brand-new camera
        was installed, before its first poll under the default name) has
        nothing to migrate the old row away from, so it would otherwise
        sit here forever, looking like a real extra camera. Same fix as
        _handle_ai_camera_configs_get: filter against the live camera
        list, but only when it's non-empty, so a startup window before
        Blink has connected (list_camera_names() briefly []) can't be
        misread as "every camera is gone" and hide them all. Rows aren't
        deleted, just excluded from this response -- a false-positive
        exclusion self-heals the instant the live list is accurate again.
        """
        readings = await self._db.get_latest_battery_state()
        live_names = self._list_camera_names() if self._list_camera_names else []
        if live_names:
            live_names_lower = {str(n).lower() for n in live_names}
            readings = [
                r
                for r in readings
                if str(r.get("camera", "")).lower() in live_names_lower
            ]
        return web.json_response(readings)

    async def _handle_battery_history(self, request: web.Request) -> web.Response:
        camera = request.match_info["camera"]
        return web.json_response(await self._db.get_battery_history(camera))

    async def _handle_stats(self, request: web.Request) -> web.Response:
        stats = await self._db.get_stats()
        # extra_status is MediaServer's own dict (populated by app.py after
        # each poll cycle).  Do NOT read from request.app — that is aiohttp's
        # internal Application dict and is never populated with disk_stats.
        disk_raw = self.extra_status.get("disk")
        if disk_raw:
            stats["disk"] = disk_raw
        stats.update(self.extra_status)
        return web.json_response(stats)

    async def _handle_activity(self, request: web.Request) -> web.Response:
        try:
            # A zero/negative `days` shifts get_activity_data()'s cutoff to
            # today or into the future, silently returning no data instead of
            # erroring — clamp the lower bound like the other paginated
            # endpoints in this file (_handle_list_clips, _handle_ai_suspicious)
            # already do for limit/offset.
            days = max(1, min(int(request.rel_url.query.get("days", 7)), 30))
        except ValueError:
            days = 7
        data = await self._db.get_activity_data(days)
        return web.json_response(data)

    async def _handle_tags(self, _request: web.Request) -> web.Response:
        tags = await self._db.get_distinct_tags()
        return web.json_response(tags)
