"""Tests for BatteryMonitor."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from blink_downloader.battery_monitor import BatteryMonitor


def _make_monitor(
    snapshot: list[dict[str, Any]],
    alerts_enabled: bool = True,
    add_battery_reading_result: bool | list[bool] = False,
) -> tuple[BatteryMonitor, MagicMock, MagicMock]:
    db = MagicMock()
    if isinstance(add_battery_reading_result, list):
        db.add_battery_reading = AsyncMock(side_effect=add_battery_reading_result)
    else:
        db.add_battery_reading = AsyncMock(return_value=add_battery_reading_result)

    dispatcher = MagicMock()
    dispatcher.dispatch_battery_alert = AsyncMock()

    monitor = BatteryMonitor(
        db=db,
        dispatcher=dispatcher,
        get_battery_snapshot=lambda: snapshot,
        alerts_enabled=alerts_enabled,
    )
    return monitor, db, dispatcher


async def test_check_and_alert_records_every_reading() -> None:
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "ok",
            "battery_level": 3,
            "battery_voltage": 165,
        },
        {
            "camera": "Backyard",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 105,
        },
    ]
    monitor, db, _ = _make_monitor(snapshot, alerts_enabled=False)

    await monitor.check_and_alert()

    assert db.add_battery_reading.await_count == 2
    db.add_battery_reading.assert_any_await("Front Door", "ok", 3, 165)
    db.add_battery_reading.assert_any_await("Backyard", "low", 0, 105)


async def test_check_and_alert_empty_snapshot_does_nothing() -> None:
    monitor, db, dispatcher = _make_monitor([])

    await monitor.check_and_alert()

    db.add_battery_reading.assert_not_awaited()
    dispatcher.dispatch_battery_alert.assert_not_awaited()


async def test_check_and_alert_alerts_on_low_transition_when_enabled() -> None:
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 105,
        }
    ]
    monitor, _, dispatcher = _make_monitor(
        snapshot, alerts_enabled=True, add_battery_reading_result=True
    )

    await monitor.check_and_alert()

    dispatcher.dispatch_battery_alert.assert_awaited_once_with("Front Door", "low", 105)


async def test_check_and_alert_no_alert_when_alerts_disabled() -> None:
    """Recording still happens (see test_check_and_alert_records_every_reading)
    even when the master alerts_enabled switch is off — only dispatch is gated."""
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 105,
        }
    ]
    monitor, db, dispatcher = _make_monitor(
        snapshot, alerts_enabled=False, add_battery_reading_result=True
    )

    await monitor.check_and_alert()

    db.add_battery_reading.assert_awaited_once()
    dispatcher.dispatch_battery_alert.assert_not_awaited()


async def test_check_and_alert_no_alert_on_first_ever_reading() -> None:
    """add_battery_reading returns False for a camera's first-ever reading,
    even if it's already low — must not immediately alert."""
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 105,
        }
    ]
    monitor, _, dispatcher = _make_monitor(
        snapshot, alerts_enabled=True, add_battery_reading_result=False
    )

    await monitor.check_and_alert()

    dispatcher.dispatch_battery_alert.assert_not_awaited()


async def test_check_and_alert_no_alert_on_recovery() -> None:
    """A transition back to "ok" is a real transition (returns True) but
    must never fire a low-battery alert."""
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "ok",
            "battery_level": 3,
            "battery_voltage": 165,
        }
    ]
    monitor, _, dispatcher = _make_monitor(
        snapshot, alerts_enabled=True, add_battery_reading_result=True
    )

    await monitor.check_and_alert()

    dispatcher.dispatch_battery_alert.assert_not_awaited()


async def test_check_and_alert_no_alert_when_state_unchanged() -> None:
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 105,
        }
    ]
    monitor, _, dispatcher = _make_monitor(
        snapshot, alerts_enabled=True, add_battery_reading_result=False
    )

    await monitor.check_and_alert()

    dispatcher.dispatch_battery_alert.assert_not_awaited()


async def test_check_and_alert_handles_multiple_cameras_independently() -> None:
    snapshot = [
        {
            "camera": "Front Door",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 105,
        },
        {
            "camera": "Backyard",
            "battery_state": "ok",
            "battery_level": 3,
            "battery_voltage": 165,
        },
        {
            "camera": "Garage",
            "battery_state": "low",
            "battery_level": 0,
            "battery_voltage": 100,
        },
    ]
    # Front Door transitions to low (alert), Backyard transitions to ok (no
    # alert), Garage is a first-ever reading already low (no alert).
    monitor, _, dispatcher = _make_monitor(
        snapshot,
        alerts_enabled=True,
        add_battery_reading_result=[True, True, False],
    )

    await monitor.check_and_alert()

    dispatcher.dispatch_battery_alert.assert_awaited_once_with("Front Door", "low", 105)
