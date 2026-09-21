"""The Security Events tab's API: the deterministic event timeline.

Reads what :mod:`blink_downloader.security` derived and the database
stored. Nothing is computed here — the rules that decide what counts as an
event live where they can be tested without a GPU.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..database import SUSPICIOUS_PERIODS
from .core import _MediaServerBase
from .support import (
    _paging,
)

_LOGGER = logging.getLogger(__name__)


class SecurityEventsRoutesMixin(_MediaServerBase):
    """The security-event timeline, its stats and per-clip events."""

    def _register_security_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/security/timeline", self._handle_security_timeline)
        app.router.add_get("/api/security/stats", self._handle_security_stats)
        app.router.add_get(
            "/api/security/events/{clip_id}", self._handle_security_events
        )

    async def _handle_security_timeline(self, request: web.Request) -> web.Response:
        """Security events across every camera, one row per clip.

        Collapsed per clip rather than per event — see
        :meth:`ClipDatabase.get_security_timeline` for why a raw event list
        makes a worse timeline than no timeline at all.
        """
        q = request.rel_url.query
        limit, offset = _paging(q, default_limit=50, max_limit=200, min_limit=1)
        period = q.get("period")
        if period not in SUSPICIOUS_PERIODS:
            period = None
        return web.json_response(
            await self._db.get_security_timeline(
                limit=limit,
                offset=offset,
                camera=q.get("camera") or None,
                min_severity=q.get("severity") or None,
                period=period,
            )
        )

    async def _handle_security_stats(self, request: web.Request) -> web.Response:
        """Severity counts over a recent window, for the Security tab header."""
        try:
            days = max(1, min(int(request.rel_url.query.get("days", 7)), 90))
        except ValueError:
            days = 7
        return web.json_response(await self._db.get_security_stats(days=days))

    async def _handle_security_events(self, request: web.Request) -> web.Response:
        """Every structured event behind one clip's assessment."""
        clip_id = request.match_info["clip_id"]
        return web.json_response(
            {"events": await self._db.get_security_events(clip_id)}
        )
