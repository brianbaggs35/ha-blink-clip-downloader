"""Home Assistant WebSocket listener that triggers instant downloads on motion."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

_WS_URL = "ws://supervisor/core/websocket"
_RECONNECT_DELAY = 30  # seconds before reconnecting after a drop


class HAEventWatcher:
    """Subscribes to HA state_changed events and fires callbacks on Blink motion.

    When a ``binary_sensor.blink_*_motion`` entity flips to "on", the supplied
    *on_motion* callback is called with the human-readable camera name.

    When the same entity flips back to "off" (motion cleared), the optional
    *on_motion_cleared* callback is called, enabling a post-motion delayed
    download to capture the clip after Blink has had time to upload it.
    """

    def __init__(
        self,
        supervisor_token: str,
        on_motion: Callable[[str], None],
        on_motion_cleared: Callable[[str], None] | None = None,
        event_cameras: list[str] | None = None,
    ) -> None:
        self._token = supervisor_token
        self._on_motion = on_motion
        self._on_motion_cleared = on_motion_cleared
        # Lower-cased set of cameras to watch; empty = all Blink cameras.
        self._event_cameras: set[str] = (
            {c.lower() for c in event_cameras} if event_cameras else set()
        )
        self._running = False
        self._session: aiohttp.ClientSession | None = None
        self._msg_id = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect to HA WebSocket and watch for motion events (auto-reconnects)."""
        self._running = True
        _LOGGER.info("HA event watcher starting")

        while self._running:
            try:
                await self._connect_and_watch()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                if self._running:
                    _LOGGER.warning(
                        "Event watcher disconnected (%s) — reconnecting in %ds",
                        exc,
                        _RECONNECT_DELAY,
                    )
                    await asyncio.sleep(_RECONNECT_DELAY)

        _LOGGER.info("HA event watcher stopped")

    async def stop(self) -> None:
        """Stop the event watcher."""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Internal: WebSocket session
    # ------------------------------------------------------------------

    async def _connect_and_watch(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        async with self._session.ws_connect(
            _WS_URL,
            heartbeat=30,
        ) as ws:
            _LOGGER.debug("WebSocket connected to %s", _WS_URL)

            # Step 1: receive auth_required.
            raw = await ws.receive_json()
            if raw.get("type") != "auth_required":
                raise ValueError(f"Expected auth_required, got: {raw.get('type')}")

            # Step 2: authenticate.
            await ws.send_json({"type": "auth", "access_token": self._token})
            raw = await ws.receive_json()
            if raw.get("type") != "auth_ok":
                raise ValueError(f"HA WebSocket auth failed: {raw}")

            _LOGGER.info("HA WebSocket authenticated; subscribing to state_changed")

            # Step 3: subscribe.
            self._msg_id += 1
            await ws.send_json(
                {
                    "id": self._msg_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )

            # Step 4: consume events.
            async for msg in ws:
                if not self._running or self._handle_ws_message(msg):
                    break

    def _handle_ws_message(self, msg: aiohttp.WSMessage) -> bool:
        """Process one WebSocket message. Returns True if the connection should close."""
        if msg.type == aiohttp.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                return False
            if data.get("type") == "event":
                self._handle_state_changed(data.get("event", {}))
            return False
        if msg.type in (
            aiohttp.WSMsgType.ERROR,
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
        ):
            _LOGGER.debug("WebSocket closed (type=%s)", msg.type)
            return True
        return False

    # ------------------------------------------------------------------
    # Internal: event parsing
    # ------------------------------------------------------------------

    def _handle_state_changed(self, event: dict) -> None:
        if event.get("event_type") != "state_changed":
            return

        data = event.get("data", {})
        entity_id: str = data.get("entity_id", "")
        new_state: dict = data.get("new_state") or {}

        camera_name = self.extract_blink_camera(entity_id)
        if camera_name is None:
            return

        if self._event_cameras and camera_name.lower() not in self._event_cameras:
            _LOGGER.debug(
                "Ignoring motion on %r (not in event_cameras whitelist)", camera_name
            )
            return

        state = new_state.get("state", "")
        if state == "on":
            _LOGGER.info(
                "Motion detected on Blink camera %r — triggering fast poll", camera_name
            )
            self._on_motion(camera_name)
        elif state == "off" and self._on_motion_cleared is not None:
            _LOGGER.info(
                "Motion cleared on Blink camera %r — scheduling post-motion download",
                camera_name,
            )
            self._on_motion_cleared(camera_name)

    # ------------------------------------------------------------------
    # Public utility (also used by tests)
    # ------------------------------------------------------------------

    @staticmethod
    def extract_blink_camera(entity_id: str) -> str | None:
        """Extract a human-readable camera name from a Blink motion entity_id.

        ``binary_sensor.blink_front_door_motion`` → ``"front door"``

        Returns None if the entity is not a Blink motion sensor.
        """
        prefix = "binary_sensor.blink_"
        suffix = "_motion"
        if not entity_id.startswith(prefix):
            return None
        inner = entity_id[len(prefix) :]
        if not inner.endswith(suffix):
            return None
        slug = inner[: -len(suffix)]
        return slug.replace("_", " ") if slug else None
