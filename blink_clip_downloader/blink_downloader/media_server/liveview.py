"""The Live View tab's API: start, watch and stop one live session.

Thin over :class:`~blink_downloader.live_view.LiveViewManager`, which owns
the session lifecycle — exactly one camera at a time, an idle timeout and a
hard cap. These routes also serve the rolling HLS playlist and segments,
because HA ingress proxies only HTTP and the raw TCP relay underneath is
unreachable from a browser regardless.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from aiohttp import web

from ..downloader import AUTH_FATAL_EXCEPTIONS
from ..live_view import CameraNotFoundError, LiveViewError
from .core import _MediaServerBase
from .support import (
    _INVALID_REQUEST_BODY,
    _LIVE_VIEW_NOT_AVAILABLE,
    _LIVEVIEW_FILENAME_RE,
    _json_object,
)

_LOGGER = logging.getLogger(__name__)


class LiveViewRoutesMixin(_MediaServerBase):
    """Live View sessions and their HLS output."""

    def _register_liveview_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        # Live View endpoints
        app.router.add_get("/api/liveview/cameras", self._handle_liveview_cameras)
        app.router.add_get("/api/liveview/status", self._handle_liveview_status)
        app.router.add_post("/api/liveview/start", self._handle_liveview_start)
        app.router.add_post("/api/liveview/stop", self._handle_liveview_stop)
        app.router.add_post("/api/liveview/heartbeat", self._handle_liveview_heartbeat)
        app.router.add_get(
            "/api/liveview/hls/{session_id}/{filename}",
            self._handle_liveview_hls_file,
        )

    async def _handle_liveview_cameras(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._live_view is None:
            raise web.HTTPServiceUnavailable(text=_LIVE_VIEW_NOT_AVAILABLE)
        return web.json_response({"cameras": self._live_view.list_cameras()})

    async def _handle_liveview_status(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._live_view is None:
            raise web.HTTPServiceUnavailable(text=_LIVE_VIEW_NOT_AVAILABLE)
        return web.json_response(asdict(self._live_view.get_status()))

    async def _handle_liveview_start(self, request: web.Request) -> web.Response:
        if self._live_view is None:
            raise web.HTTPServiceUnavailable(text=_LIVE_VIEW_NOT_AVAILABLE)
        body = await _json_object(request, _INVALID_REQUEST_BODY)
        camera = str(body.get("camera", "")).strip()
        if not camera:
            raise web.HTTPBadRequest(text="Missing camera")
        try:
            status = await self._live_view.start_session(camera)
        except AUTH_FATAL_EXCEPTIONS:
            raise web.HTTPServiceUnavailable(
                text="Blink session is reconnecting — try again shortly"
            )
        except CameraNotFoundError:
            raise web.HTTPNotFound(text="Camera not found")
        except LiveViewError as exc:
            raise web.HTTPBadGateway(text=str(exc))
        return web.json_response(asdict(status))

    async def _handle_liveview_stop(self, request: web.Request) -> web.Response:
        # Lenient body parsing, unlike /start above: a missing/garbage body
        # just means "stop whatever's active" (session_id=None), since stop
        # is meant to be safe to call defensively (e.g. on tab unload).
        if self._live_view is None:
            raise web.HTTPServiceUnavailable(text=_LIVE_VIEW_NOT_AVAILABLE)
        session_id = None
        try:
            body = await request.json()
            session_id = body.get("session_id") or None
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Live view stop: no/invalid request body: %s", exc)
        stopped = await self._live_view.stop_session(session_id)
        return web.json_response({"stopped": stopped})

    async def _handle_liveview_heartbeat(self, request: web.Request) -> web.Response:
        if self._live_view is None:
            raise web.HTTPServiceUnavailable(text=_LIVE_VIEW_NOT_AVAILABLE)
        body = await _json_object(request, _INVALID_REQUEST_BODY)
        session_id = str(body.get("session_id", "")).strip()
        if not session_id:
            raise web.HTTPBadRequest(text="Missing session_id")
        # A stale/unknown session_id is a routine race (the session may have
        # just ended via idle timeout) — reflected in "ok", not an error.
        return web.json_response({"ok": self._live_view.heartbeat(session_id)})

    async def _handle_liveview_hls_file(  # NOSONAR
        self, request: web.Request
    ) -> web.StreamResponse:
        if self._live_view is None:
            raise web.HTTPServiceUnavailable(text=_LIVE_VIEW_NOT_AVAILABLE)
        filename = request.match_info["filename"]
        if not _LIVEVIEW_FILENAME_RE.fullmatch(filename):
            raise web.HTTPNotFound()
        hls_dir = self._live_view.get_hls_dir(request.match_info["session_id"])
        if hls_dir is None:
            raise web.HTTPNotFound()
        file_path = hls_dir / filename
        try:
            file_path.resolve().relative_to(hls_dir.resolve())
        except ValueError:
            # Belt-and-suspenders on top of the filename allowlist above.
            raise web.HTTPNotFound()
        if not file_path.is_file():
            raise web.HTTPNotFound()
        content_type = (
            "application/vnd.apple.mpegurl"
            if filename.endswith(".m3u8")
            else "video/mp2t"
        )
        # Explicit Content-Type is required, not optional: Python's
        # mimetypes (aiohttp FileResponse's fallback) has no mapping for
        # .m3u8 and is ambiguous for .ts — without this override the file
        # would likely serve as application/octet-stream and risk breaking
        # Safari's native HLS handling. no-store: this is live content, must
        # never be cached.
        return web.FileResponse(
            file_path,
            headers={"Cache-Control": "no-store", "Content-Type": content_type},
        )
