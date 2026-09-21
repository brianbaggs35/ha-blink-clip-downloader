"""The Automations tab's API: writing into Home Assistant, and test sends.

Creating an automation, script or scene through Core's own config API, and
the notification test buttons for each configured channel. Both exist to
prove the round trip works from inside the add-on's own UI.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..ha_config import HAConfigError
from .core import _MediaServerBase
from .support import (
    _NOTIFICATIONS_NOT_CONFIGURED,
    _json_object,
)

_LOGGER = logging.getLogger(__name__)


class AutomationRoutesMixin(_MediaServerBase):
    """Creating HA config objects, and notification tests."""

    def _register_automations_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""

        app.router.add_post("/api/ha/config/create", self._handle_ha_config_create)
        app.router.add_post("/api/notifications/test-email", self._handle_test_email)
        app.router.add_post(
            "/api/notifications/test-discord", self._handle_test_discord
        )
        app.router.add_post("/api/notifications/test-mobile", self._handle_test_mobile)
        app.router.add_post(
            "/api/notifications/test-ha", self._handle_test_ha_notification
        )

    async def _handle_ha_config_create(self, request: web.Request) -> web.Response:
        """Create one automation/script/scene in Home Assistant.

        The builder sends the YAML it is already showing, plus the kind and
        a stable object id, so pressing Create twice updates the same object
        rather than making a second one. Everything the config API cannot
        create keeps its copy/download buttons instead — see ha_config.py.
        """
        if self._ha_config_writer is None:
            raise web.HTTPServiceUnavailable(
                text=(
                    "Creating configuration needs Home Assistant's API, "
                    "which this add-on only has when Home Assistant runs it."
                )
            )
        body = await _json_object(request)
        kind = str(body.get("kind", "") or "")
        object_id = str(body.get("object_id", "") or "")
        text = str(body.get("yaml", "") or "")
        try:
            name = await self._ha_config_writer.create(kind, object_id, text)
        except HAConfigError as exc:
            # A considered refusal, not a crash: the UI shows the message.
            return web.json_response({"created": False, "message": str(exc)})
        # The name Home Assistant lists it under, not an entity id — see
        # ha_config.created_name for why those are not the same thing.
        return web.json_response({"created": True, "name": name})

    async def _handle_test_email(self, _request: web.Request) -> web.Response:
        """Send a one-off test email using the configured SMTP settings."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": _NOTIFICATIONS_NOT_CONFIGURED}
            )
        ok, message = await self._notification_dispatcher.send_test_email()
        return web.json_response({"success": ok, "message": message})

    async def _handle_test_discord(self, _request: web.Request) -> web.Response:
        """Send a one-off test message to the configured Discord webhook."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": _NOTIFICATIONS_NOT_CONFIGURED}
            )
        ok, message = await self._notification_dispatcher.send_test_discord()
        return web.json_response({"success": ok, "message": message})

    async def _handle_test_mobile(self, _request: web.Request) -> web.Response:
        """Send a one-off test mobile_app push notification."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": _NOTIFICATIONS_NOT_CONFIGURED}
            )
        ok, message = await self._notification_dispatcher.send_test_mobile()
        return web.json_response({"success": ok, "message": message})

    async def _handle_test_ha_notification(self, _request: web.Request) -> web.Response:
        """Send a one-off test Home Assistant persistent notification."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": _NOTIFICATIONS_NOT_CONFIGURED}
            )
        ok, message = await self._notification_dispatcher.send_test_ha_notification()
        return web.json_response({"success": ok, "message": message})
