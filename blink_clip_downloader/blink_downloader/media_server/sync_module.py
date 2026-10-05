"""The Sync Module tab's API: arming and disarming.

Both the whole system and one camera at a time, through narrow callables
the downloader supplies, so this module never touches blinkpy directly.

The route says *which* by name, since that is what Home Assistant's
generated YAML has. The tab also sends the ids it was shown
(``network_id``, ``camera_id`` in the body): a name changes when the camera
is renamed and two sync modules can hold cameras with the same name, an id
does neither.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _SYNC_MODULES_NOT_AVAILABLE,
    _json_object,
)

_LOGGER = logging.getLogger(__name__)


class SyncModuleRoutesMixin(_MediaServerBase):
    """Sync module state, and arming a system or a camera."""

    def _register_syncmodule_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/sync-modules", self._handle_sync_modules_get)
        app.router.add_post(
            "/api/sync-modules/{name}/arm", self._handle_sync_module_arm
        )
        app.router.add_post(
            "/api/sync-modules/cameras/{camera}/arm",
            self._handle_sync_module_camera_arm,
        )

    async def _handle_sync_modules_get(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        """Every sync module on the account, with its own info/armed state
        and each of its cameras' armed/online/battery state — the Sync
        Module tab's one and only read endpoint.

        No `await` here: `_get_sync_module_snapshot` just reads blinkpy's
        already-cached in-memory state (kept fresh by the regular poll
        cycle's `refresh_camera_state()`, not by this endpoint), so there's
        genuinely nothing to await. `async def` is required anyway — aiohttp
        rejects a plain sync callable as a route handler.
        """
        if self._get_sync_module_snapshot is None:
            return web.json_response([])
        return web.json_response(self._get_sync_module_snapshot())

    async def _handle_arm_request(
        self,
        request: web.Request,
        match_key: str,
        label: str,
        arm_fn: Callable[..., Awaitable[bool | None]] | None,
        identifiers: tuple[str, ...],
    ) -> web.Response:
        """Shared body for _handle_sync_module_arm/_handle_sync_module_camera_arm.

        *match_key* is the route's match_info key ("name" for a sync
        module, "camera" for a camera); *label* is the noun used in the
        404 message; *identifiers* are the optional body fields that name
        the target by something other than its name, passed on to *arm_fn*
        as keyword arguments when present.
        """
        name = request.match_info[match_key]
        body = await _json_object(request)
        armed = body.get("armed")
        # Not bool(...): a body without "armed", or with a typo in it, used
        # to mean disarm, and the string "false" meant arm.
        if not isinstance(armed, bool):
            raise web.HTTPBadRequest(text='"armed" must be true or false')
        hints = {key: _identifier(body, key) for key in identifiers}
        hints = {key: value for key, value in hints.items() if value is not None}
        if arm_fn is None:
            raise web.HTTPServiceUnavailable(text=_SYNC_MODULES_NOT_AVAILABLE)
        result = await arm_fn(name, armed, **hints)
        if result is None:
            raise web.HTTPNotFound(text=f'{label} "{name}" not found')
        if not result:
            raise web.HTTPBadGateway(
                text=f"Blink did not accept the {'arm' if armed else 'disarm'} request — try again"
            )
        return web.json_response({"armed": armed})

    async def _handle_sync_module_arm(self, request: web.Request) -> web.Response:
        return await self._handle_arm_request(
            request, "name", "Sync module", self._arm_sync_module, ("network_id",)
        )

    async def _handle_sync_module_camera_arm(
        self, request: web.Request
    ) -> web.Response:
        return await self._handle_arm_request(
            request,
            "camera",
            "Camera",
            self._arm_camera,
            ("camera_id", "network_id"),
        )


def _identifier(body: dict[str, Any], key: str) -> str | None:
    """An optional id from the request body, as the string blinkpy compares.

    Absent or empty means "not given". Anything that is not a string or a
    number is a malformed request rather than an id nothing matches.
    """
    value = body.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise web.HTTPBadRequest(text=f'"{key}" must be a string or a number')
    return str(value)
