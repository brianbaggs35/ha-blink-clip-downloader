"""The pages and endpoints that are not part of any one tab.

The SPA itself, its favicon and static assets, the health probe, and the
Blink authentication state the UI polls while a login or 2FA is pending.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _INVALID_REQUEST_BODY,
    _STATIC_DIR,
)

_LOGGER = logging.getLogger(__name__)


class AppShellMixin(_MediaServerBase):
    """The SPA, its assets, the health probe and auth state."""

    def _register_core_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/favicon.svg", self._handle_favicon)
        assets_dir = _STATIC_DIR / "assets"
        if assets_dir.is_dir():
            app.router.add_static("/assets", assets_dir)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/auth/status", self._handle_auth_status)
        app.router.add_post("/api/auth/2fa", self._handle_two_fa)

    async def _handle_index(self, request: web.Request) -> web.Response:  # NOSONAR
        # HA ingress sends X-Ingress-Path so the JS can prefix all API calls.
        # For direct port access the header is absent and the prefix is empty.
        # The header value is attacker-controlled on any deployment where a
        # client can set arbitrary request headers, so it must never be
        # interpolated into the page verbatim: json.dumps() produces a
        # properly quote/backslash-escaped JS string literal, and the
        # "</" -> "<\/" swap additionally prevents a value like
        # "</script><script>..." from closing out the surrounding <script>
        # tag early.
        index_file = _STATIC_DIR / "index.html"
        if not index_file.exists():
            raise web.HTTPInternalServerError(
                text=(
                    "Frontend build not found at "
                    f"{index_file}. Run `npm run build` in frontend/ (see "
                    "CONTRIBUTING.md) — the Docker image builds this "
                    "automatically, so this only happens in a bare checkout."
                )
            )
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        safe_literal = json.dumps(ingress_path).replace("</", "<\\/")
        html = index_file.read_text().replace("'__HAROOT__'", safe_literal)
        return web.Response(text=html, content_type="text/html")

    async def _handle_favicon(  # NOSONAR
        self, _request: web.Request
    ) -> web.StreamResponse:
        favicon = _STATIC_DIR / "favicon.svg"
        if not favicon.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(
            favicon, headers={"Cache-Control": "public, max-age=86400"}
        )

    async def _handle_health(self, _request: web.Request) -> web.Response:  # NOSONAR
        return web.json_response({"status": "ok"})

    async def _handle_auth_status(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._auth_state_getter:
            status = self._auth_state_getter()
        else:
            status = {"state": "connected", "message": ""}
        return web.json_response(status)

    async def _handle_two_fa(self, request: web.Request) -> web.Response:
        if not self._two_fa_callback:
            raise web.HTTPServiceUnavailable(text="2FA not available")
        try:
            body = await request.json()
            code = str(body.get("code", "")).strip()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_REQUEST_BODY)
        if not code.isdigit() or len(code) != 6:
            raise web.HTTPBadRequest(text="Code must be exactly 6 digits")
        seq = self._two_fa_callback(code)
        return web.json_response({"submitted": True, "seq": seq})
