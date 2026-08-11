"""Per-camera battery state tracking and low-battery alerting."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .database import ClipDatabase
from .notification_channels import NotificationDispatcher

_LOGGER = logging.getLogger(__name__)


class BatteryMonitor:
    """Checks every camera's battery state once per poll cycle.

    Every reading is recorded in ``battery_history`` unconditionally (see
    :meth:`check_and_alert`) — the Status tab's battery strip/history works
    regardless of whether alerting is turned on. Only a genuine ok-to-low
    transition, and only when *alerts_enabled*, triggers a notification;
    recording and alerting are deliberately independent so turning alerts
    off never also blanks the Status tab.
    """

    def __init__(
        self,
        db: ClipDatabase,
        dispatcher: NotificationDispatcher,
        get_battery_snapshot: Callable[[], list[dict[str, Any]]],
        alerts_enabled: bool,
    ) -> None:
        self._db = db
        self._dispatcher = dispatcher
        self._get_battery_snapshot = get_battery_snapshot
        self._alerts_enabled = alerts_enabled

    async def check_and_alert(self) -> None:
        """Record each camera's current battery reading; alert on a new low.

        ``ClipDatabase.add_battery_reading`` returns ``True`` only for a
        genuine transition from a *previously recorded, different* state —
        never for a camera's very first-ever reading, even if it's already
        low — so enabling alerts for the first time on a fleet with an
        already-low camera doesn't immediately spam a notification for
        something that was never actually observed to change.
        """
        for reading in self._get_battery_snapshot():
            camera = reading["camera"]
            battery_state = reading["battery_state"]
            transitioned = await self._db.add_battery_reading(
                camera,
                battery_state,
                reading.get("battery_level"),
                reading.get("battery_voltage"),
            )
            if transitioned and battery_state == "low":
                _LOGGER.info("%s battery is now low", camera)
                if self._alerts_enabled:
                    await self._dispatcher.dispatch_battery_alert(
                        camera, battery_state, reading.get("battery_voltage")
                    )
