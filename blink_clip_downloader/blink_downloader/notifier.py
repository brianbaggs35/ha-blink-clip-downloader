"""Home Assistant notification and event sender."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_HA_API = "http://supervisor/core/api"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class HANotifier:
    """Sends persistent notifications, events, and sensor updates to HA."""

    def __init__(
        self,
        supervisor_token: str,
        enabled: bool,
        title: str,
        webhook_url: str = "",
    ) -> None:
        self._token = supervisor_token
        self._enabled = enabled
        self._title = title
        self._webhook_url = webhook_url
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def notify(self, message: str, title: str | None = None) -> bool:
        """Create a persistent notification in Home Assistant."""
        if not self._enabled or not self._token:
            return False
        return await self._post(
            f"{_HA_API}/services/persistent_notification/create",
            {"message": message, "title": title or self._title},
        )

    async def fire_event(self, event_type: str, event_data: dict[str, Any]) -> bool:
        """Fire a custom HA event."""
        if not self._token:
            return False
        return await self._post(
            f"{_HA_API}/events/{event_type}",
            event_data,
        )

    async def update_sensor(
        self, entity_id: str, state: str, attributes: dict[str, Any]
    ) -> bool:
        """Write a virtual sensor state via the HA REST API."""
        if not self._token:
            return False
        return await self._post(
            f"{_HA_API}/states/{entity_id}",
            {"state": state, "attributes": attributes},
        )

    async def call_webhook(self, payload: dict[str, Any]) -> bool:
        """POST *payload* to the user-configured webhook URL (fire-and-forget)."""
        if not self._webhook_url:
            return False
        try:
            session = await self._get_session()
            async with session.post(
                self._webhook_url,
                json=payload,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status < 400:
                    return True
                _LOGGER.warning(
                    "Webhook %s returned HTTP %d", self._webhook_url, resp.status
                )
                return False
        except aiohttp.ClientError as exc:
            _LOGGER.warning("Webhook request failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        # No default headers here: this session is shared with call_webhook(),
        # which posts to an arbitrary user-configured URL. The Supervisor
        # token is attached per-request in _post() instead, so it's only ever
        # sent to the HA API and never leaked to a third-party webhook.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _post(self, url: str, payload: dict[str, Any]) -> bool:
        try:
            session = await self._get_session()
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    return True
                _LOGGER.warning("POST %s returned HTTP %d", url, resp.status)
                return False
        except aiohttp.ClientError as exc:
            _LOGGER.warning("Failed to POST %s: %s", url, exc)
            return False
