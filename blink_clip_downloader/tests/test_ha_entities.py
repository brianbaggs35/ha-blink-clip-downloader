"""Tests for blink_downloader.ha_entities."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from blink_downloader.ha_entities import (
    BATTERY_LOW_EVENT,
    CLIP_ANALYZED_EVENT,
    CLOUD_STORAGE_SENSOR,
    LOCAL_STORAGE_SENSOR,
    HAEntityPublisher,
    clip_analyzed_event_data,
    cloud_storage_state,
    local_storage_state,
)

_GB = 1024**3


def _disk(**overrides: Any) -> dict[str, Any]:
    disk: dict[str, Any] = {
        "used_bytes": 5 * _GB,
        "used_mb": 5120.0,
        "free_bytes": 50 * _GB,
        "free_gb": 50.0,
        "total_bytes": 100 * _GB,
        "total_gb": 100.0,
        "quota_bytes": 0,
        "quota_gb": 0,
    }
    disk.update(overrides)
    return disk


# ---------------------------------------------------------------------------
# local_storage_state
# ---------------------------------------------------------------------------


def test_local_storage_measures_against_the_quota_when_one_is_set() -> None:
    state, attrs = local_storage_state(_disk(quota_bytes=10 * _GB, quota_gb=10.0))
    assert state == "50.0"
    assert attrs["basis"] == "quota"
    assert attrs["percent_used"] == 50.0
    assert attrs["clips_used_gb"] == 5.0
    assert attrs["unit_of_measurement"] == "%"


def test_local_storage_measures_disk_fill_when_no_quota_is_set() -> None:
    # 100 GB disk with 50 GB free is 50% full regardless of how much of
    # that is clips.
    state, attrs = local_storage_state(_disk())
    assert state == "50.0"
    assert attrs["basis"] == "disk"


def test_local_storage_is_unknown_when_there_is_no_disk_to_measure() -> None:
    state, attrs = local_storage_state(_disk(total_bytes=0, free_bytes=0))
    assert state == "unknown"
    assert "percent_used" not in attrs


def test_local_storage_never_reports_over_100_percent() -> None:
    state, _ = local_storage_state(_disk(quota_bytes=1 * _GB, used_bytes=4 * _GB))
    assert state == "100.0"


def test_local_storage_tolerates_missing_keys() -> None:
    state, attrs = local_storage_state({})
    assert state == "unknown"
    assert attrs["basis"] == "disk"


# ---------------------------------------------------------------------------
# cloud_storage_state
# ---------------------------------------------------------------------------


def _quota(limit: int | None, usage: int, in_drive: int = 0) -> SimpleNamespace:
    return SimpleNamespace(limit=limit, usage=usage, usage_in_drive=in_drive)


def test_cloud_storage_reports_the_used_percentage() -> None:
    state, attrs = cloud_storage_state(
        "google_drive",
        configured=True,
        connected=True,
        quota=_quota(100 * _GB, 75 * _GB, 20 * _GB),
        queue={"pending": 3, "failed": 1, "completed": 40},
    )
    assert state == "75.0"
    assert attrs["provider"] == "google_drive"
    assert attrs["used_gb"] == 75.0
    assert attrs["total_gb"] == 100.0
    assert attrs["free_gb"] == 25.0
    assert attrs["clips_used_gb"] == 20.0
    assert attrs["pending_uploads"] == 3
    assert attrs["failed_uploads"] == 1
    assert attrs["uploaded_clips"] == 40
    assert attrs["unlimited"] is False


def test_cloud_storage_is_unknown_rather_than_zero_when_not_connected() -> None:
    state, attrs = cloud_storage_state(
        "none", configured=False, connected=False, quota=None
    )
    assert state == "unknown"
    assert attrs["connected"] is False
    assert "percent_used" not in attrs


def test_cloud_storage_reports_unlimited_workspace_accounts_as_unknown() -> None:
    state, attrs = cloud_storage_state(
        "google_drive", configured=True, connected=True, quota=_quota(None, 9 * _GB)
    )
    assert state == "unknown"
    assert attrs["unlimited"] is True
    assert attrs["used_gb"] == 9.0
    assert "percent_used" not in attrs


def test_cloud_storage_carries_the_upload_pause_state() -> None:
    _, attrs = cloud_storage_state(
        "google_drive",
        configured=True,
        connected=True,
        quota=_quota(10 * _GB, 1 * _GB),
        queue={"uploads_paused": True, "pause_reason": "quota exceeded"},
    )
    assert attrs["uploads_paused"] is True
    assert attrs["pause_reason"] == "quota exceeded"


def test_cloud_storage_handles_a_zero_limit_without_dividing_by_it() -> None:
    state, attrs = cloud_storage_state(
        "google_drive", configured=True, connected=True, quota=_quota(0, 0)
    )
    assert state == "unknown"
    assert attrs["unlimited"] is True


def test_cloud_storage_reports_a_negative_limit_as_unknown() -> None:
    """A malformed answer should not come back as a negative percentage."""
    state, attrs = cloud_storage_state(
        "google_drive", configured=True, connected=True, quota=_quota(-1, 5 * _GB)
    )
    assert state == "unknown"
    assert "percent_used" not in attrs


# ---------------------------------------------------------------------------
# clip_analyzed_event_data
# ---------------------------------------------------------------------------


def test_clip_analyzed_event_data_flattens_the_result() -> None:
    result = SimpleNamespace(
        clip_id="abc",
        camera="Driveway",
        is_suspicious=True,
        confidence=0.87654,
        summary="A person approached the car",
        risk_score=61.25,
        severity="elevated",
        event_type="asset_proximity",
        evidence_quality=0.4321,
        approved_faces_seen=False,
        model="claude-haiku-4-5",
        response_text="a very long raw model response",
        prompt_text="an even longer prompt",
    )
    data = clip_analyzed_event_data(result, {"path": "/share/blink-clips/a.mp4"})
    assert data == {
        "clip_id": "abc",
        "camera": "Driveway",
        "is_suspicious": True,
        "confidence": 0.877,
        "summary": "A person approached the car",
        "risk_score": 61.2,
        "severity": "elevated",
        "event_type": "asset_proximity",
        "evidence_quality": 0.43,
        "face_recognized": False,
        "model": "claude-haiku-4-5",
        "path": "/share/blink-clips/a.mp4",
    }
    # The two big text fields stay out of the recorder.
    assert "response_text" not in data
    assert "prompt_text" not in data


def test_clip_analyzed_event_data_tolerates_a_missing_clip_row() -> None:
    data = clip_analyzed_event_data(SimpleNamespace(clip_id="x", camera="Cam"), None)
    assert data["path"] == ""
    assert data["is_suspicious"] is False
    assert data["confidence"] == 0.0


# ---------------------------------------------------------------------------
# HAEntityPublisher
# ---------------------------------------------------------------------------


def _publisher() -> tuple[HAEntityPublisher, MagicMock]:
    notifier = MagicMock()
    notifier.update_sensor = AsyncMock(return_value=True)
    notifier.fire_event = AsyncMock(return_value=True)
    return HAEntityPublisher(notifier), notifier


async def test_publisher_writes_both_sensors_under_their_documented_names() -> None:
    publisher, notifier = _publisher()

    assert await publisher.publish_local_storage(_disk()) is True
    assert notifier.update_sensor.await_args.args[0] == LOCAL_STORAGE_SENSOR

    assert (
        await publisher.publish_cloud_storage(
            "google_drive",
            configured=True,
            connected=True,
            quota=_quota(10 * _GB, 5 * _GB),
        )
        is True
    )
    assert notifier.update_sensor.await_args.args[0] == CLOUD_STORAGE_SENSOR
    assert notifier.update_sensor.await_args.args[1] == "50.0"


async def test_publisher_fires_the_analysis_and_battery_events() -> None:
    publisher, notifier = _publisher()

    await publisher.fire_clip_analyzed(
        SimpleNamespace(clip_id="c1", camera="Porch", is_suspicious=True), {}
    )
    assert notifier.fire_event.await_args.args[0] == CLIP_ANALYZED_EVENT
    assert notifier.fire_event.await_args.args[1]["clip_id"] == "c1"

    await publisher.fire_battery_low(
        {
            "camera": "Porch",
            "battery_state": "low",
            "battery_level": 1,
            "battery_voltage": 140,
        }
    )
    assert notifier.fire_event.await_args.args[0] == BATTERY_LOW_EVENT
    assert notifier.fire_event.await_args.args[1] == {
        "camera": "Porch",
        "battery_state": "low",
        "battery_level": 1,
        "battery_voltage": 140,
    }


async def test_publisher_fires_battery_event_with_nothing_but_a_camera_name() -> None:
    publisher, notifier = _publisher()
    await publisher.fire_battery_low({"camera": "Shed"})
    payload = notifier.fire_event.await_args.args[1]
    assert payload["battery_level"] is None
    assert payload["battery_voltage"] is None
    assert payload["battery_state"] == ""
