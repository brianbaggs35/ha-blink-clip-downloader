"""Home Assistant WebSocket listener: Blink motion, and taps on alert buttons."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Internal HA Supervisor API on the isolated `hassio` Docker network — not
# exposed externally and does not terminate TLS, so ws:// is correct here.
_WS_URL = "ws://supervisor/core/websocket"  # NOSONAR
_RECONNECT_DELAY = 30  # seconds before reconnecting after a drop
# Fired by the companion app when a button on one of its notifications is
# tapped; the button's ``action`` string rides along in the event data.
NOTIFICATION_ACTION_EVENT = "mobile_app_notification_action"


class HAEventWatcher:
    """Subscribes to HA state_changed events and fires callbacks on Blink motion.

    When a ``binary_sensor.blink_*_motion`` entity flips to "on", the supplied
    *on_motion* callback is called with the human-readable camera name.

    When the same entity flips back to "off" (motion cleared), the optional
    *on_motion_cleared* callback is called, enabling a post-motion delayed
    download to capture the clip after Blink has had time to upload it.

    With *on_notification_action*, it also hears the companion app's
    notification buttons (the "Not a threat" button on alerts — see
    ``alert_actions.py``) and passes each tapped action string on. Either
    half can run without the other: *watch_motion* False subscribes to the
    button event alone, so turning off event-driven downloads does not also
    turn off the button.
    """

    def __init__(
        self,
        supervisor_token: str,
        on_motion: Callable[[str], None],
        on_motion_cleared: Callable[[str], None] | None = None,
        event_cameras: list[str] | None = None,
        *,
        watch_motion: bool = True,
        on_notification_action: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._token = supervisor_token
        self._on_motion = on_motion
        self._on_motion_cleared = on_motion_cleared
        self._watch_motion = watch_motion
        self._on_notification_action = on_notification_action
        # Handlers still running; held so they are not garbage-collected
        # mid-flight (the event loop keeps only weak references to tasks).
        self._action_tasks: set[asyncio.Task[object]] = set()
        # Lower-cased set of cameras to watch; empty = all Blink cameras.
        self._event_cameras: set[str] = (
            {c.lower() for c in event_cameras} if event_cameras else set()
        )
        self._running = False
        self._session: aiohttp.ClientSession | None = None
        self._msg_id = 0

    @property
    def has_work(self) -> bool:
        """True when there is anything to subscribe to at all."""
        return self._watch_motion or self._on_notification_action is not None

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
                _LOGGER.info("HA event watcher stopped")
                raise
            except Exception as exc:  # noqa: BLE001
                if self._running:
                    _LOGGER.warning("Event watcher disconnected (%s)", exc)

            # _connect_and_watch() can also return normally with no
            # exception at all -- _handle_ws_message() treats an
            # ERROR/CLOSE/CLOSED WebSocket message as a clean break out of
            # its receive loop, which is exactly what happens when HA Core
            # sends a graceful close frame instead of just dropping the TCP
            # connection (routine on a HA Core restart: config reload,
            # add-on update, automation edit -- not a rare edge case). The
            # backoff below must apply to *that* path too, or it busy-loops
            # full reconnect+auth handshakes against the Supervisor with no
            # delay at all until a real exception eventually interrupts it.
            if self._running:
                _LOGGER.warning("Reconnecting to HA WebSocket in %ds", _RECONNECT_DELAY)
                await asyncio.sleep(_RECONNECT_DELAY)

        _LOGGER.info("HA event watcher stopped")

    async def stop(self) -> None:
        """Stop the event watcher."""
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    def rename_camera(self, old_name: str, new_name: str) -> None:
        """Keep the optional event-camera allowlist aligned with Blink names."""
        old_lower = old_name.lower()
        if old_lower in self._event_cameras:
            self._event_cameras.remove(old_lower)
            self._event_cameras.add(new_name.lower())

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

            # Step 3: subscribe.
            for event_type in self._event_types():
                _LOGGER.info(
                    "HA WebSocket authenticated; subscribing to %s", event_type
                )
                self._msg_id += 1
                await ws.send_json(
                    {
                        "id": self._msg_id,
                        "type": "subscribe_events",
                        "event_type": event_type,
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
                event = data.get("event", {})
                if event.get("event_type") == NOTIFICATION_ACTION_EVENT:
                    self._handle_notification_action(event)
                else:
                    self._handle_state_changed(event)
            return False
        if msg.type in (
            aiohttp.WSMsgType.ERROR,
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
        ):
            _LOGGER.debug("WebSocket closed (type=%s)", msg.type)
            return True
        return False

    def _event_types(self) -> list[str]:
        """The events this watcher subscribes to, per what it was asked to do."""
        types = ["state_changed"] if self._watch_motion else []
        if self._on_notification_action is not None:
            types.append(NOTIFICATION_ACTION_EVENT)
        return types

    # ------------------------------------------------------------------
    # Internal: event parsing
    # ------------------------------------------------------------------

    def _handle_notification_action(self, event: dict) -> None:
        """Hand a tapped button's action string to its handler, off this loop.

        Run as a task so a slow database write never holds up the event
        stream — motion events arriving meanwhile still trigger fast polls.
        """
        handler = self._on_notification_action
        action = (event.get("data") or {}).get("action")
        if handler is None or not isinstance(action, str) or not action:
            return
        task: asyncio.Task[object] = asyncio.ensure_future(handler(action))
        self._action_tasks.add(task)
        task.add_done_callback(self._action_done)

    def _action_done(self, task: asyncio.Task[object]) -> None:
        self._action_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning(
                "Handling a notification action failed: %s", task.exception()
            )

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
