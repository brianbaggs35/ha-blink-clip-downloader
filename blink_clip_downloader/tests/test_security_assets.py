"""Tests for blink_downloader.security.assets."""

from __future__ import annotations

import pytest

from blink_downloader.security.assets import (
    AssetLocation,
    AssetType,
    ProtectedAsset,
    resolve_vehicle_asset,
)
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.tracks import build_tracks
from blink_downloader.security.vehicles import SIGNATURE_MIN_SAMPLES, VehicleSignature

FRAME = (640.0, 360.0)
MY_CAR: Box = (300.0, 180.0, 460.0, 280.0)
NEIGHBOUR: Box = (470.0, 180.0, 630.0, 280.0)
ZONE = Zone.from_config({"x_min": 0.47, "y_min": 0.5, "x_max": 0.72, "y_max": 0.78})


def _tracks(entries: list[tuple[str, Box, int]]) -> list:
    detections = []
    for label, box, track_id in entries:
        for i in range(3):
            detections.append((label, 0.9, box, track_id, i))
    return build_tracks(detections, 2.0, FRAME)


# ----------------------------------------------------------------------
# ProtectedAsset
# ----------------------------------------------------------------------


def test_asset_without_a_box_is_unlocated() -> None:
    asset = ProtectedAsset(name="x", asset_type=AssetType.VEHICLE, camera="cam")
    assert asset.located is False
    assert asset.detected is False
    assert asset.present is False
    assert asset.gap_feet(10.0) is None


def test_asset_confident_without_an_identification() -> None:
    """Non-vehicle assets carry no identification, and must not be treated
    as tentatively identified because of it."""
    asset = ProtectedAsset(name="front door", asset_type=AssetType.DOOR, camera="cam")
    assert asset.confident is True


@pytest.mark.parametrize(
    ("asset_type", "expected"),
    [
        (AssetType.VEHICLE, 6.0),
        (AssetType.DOOR, 3.0),
        (AssetType.GARAGE, 9.0),
        (AssetType.MAILBOX, 1.0),
        (AssetType.CAMERA, 0.5),
        (AssetType.OTHER, 3.0),
        (AssetType.WINDOW, 3.0),
        (AssetType.PACKAGE_AREA, 3.0),
        (AssetType.GATE, 4.0),
        (AssetType.BICYCLE, 5.5),
        (AssetType.EQUIPMENT, 3.0),
    ],
)
def test_asset_width_feet_by_type(asset_type: AssetType, expected: float) -> None:
    asset = ProtectedAsset(name="x", asset_type=asset_type, camera="cam")
    assert asset.width_feet == expected


def test_gap_feet_scales_by_the_assets_own_pixel_width() -> None:
    asset = ProtectedAsset(
        name="car", asset_type=AssetType.VEHICLE, camera="cam", box=(0, 0, 120, 60)
    )
    assert asset.gap_feet(60.0) == pytest.approx(3.0)


def test_present_covers_detected_and_zone_but_not_absent() -> None:
    def asset(location: AssetLocation) -> ProtectedAsset:
        return ProtectedAsset(
            name="x", asset_type=AssetType.VEHICLE, camera="cam", location=location
        )

    assert asset(AssetLocation.DETECTED).present is True
    assert asset(AssetLocation.ZONE).present is True
    assert asset(AssetLocation.ZONE_ABSENT).present is False
    assert asset(AssetLocation.UNKNOWN).present is False


# ----------------------------------------------------------------------
# resolve_vehicle_asset
# ----------------------------------------------------------------------


def test_no_description_means_no_protected_vehicle() -> None:
    assert (
        resolve_vehicle_asset("cam", "", _tracks([("car", MY_CAR, 1)]), FRAME) is None
    )


def test_detected_vehicle_becomes_the_asset_box() -> None:
    asset = resolve_vehicle_asset(
        "cam", "blue sedan", _tracks([("car", MY_CAR, 1)]), FRAME, zone=ZONE
    )
    assert asset is not None
    assert asset.location is AssetLocation.DETECTED
    assert asset.box == MY_CAR
    assert asset.present is True
    assert asset.identification is not None


def test_no_vehicle_detected_at_all_falls_back_to_the_zone() -> None:
    """The detector may simply have missed a car parked in shadow, so the
    zone still stands in for it and physical rules keep working."""
    asset = resolve_vehicle_asset("cam", "blue sedan", [], FRAME, zone=ZONE)
    assert asset is not None
    assert asset.location is AssetLocation.ZONE
    assert asset.box == ZONE.to_pixel_box(*FRAME)  # type: ignore[union-attr]
    assert asset.present is True
    assert asset.detected is False


def test_other_vehicles_present_but_none_matching_marks_the_asset_absent() -> None:
    asset = resolve_vehicle_asset(
        "cam", "blue sedan", _tracks([("car", NEIGHBOUR, 2)]), FRAME, zone=ZONE
    )
    assert asset is not None
    assert asset.location is AssetLocation.ZONE_ABSENT
    assert asset.present is False
    assert asset.located is True


def test_no_zone_and_no_detection_leaves_the_asset_unlocated() -> None:
    asset = resolve_vehicle_asset("cam", "blue sedan", [], FRAME)
    assert asset is not None
    assert asset.location is AssetLocation.UNKNOWN
    assert asset.located is False


def test_ambiguous_identification_makes_the_asset_unconfident() -> None:
    asset = resolve_vehicle_asset(
        "cam",
        "blue sedan",
        _tracks([("car", MY_CAR, 1), ("car", (0.0, 0.0, 600.0, 350.0), 2)]),
        FRAME,
    )
    assert asset is not None
    assert asset.confident is False


def test_signature_is_forwarded_to_identification() -> None:
    signature = VehicleSignature(
        box=(470 / 640, 180 / 360, 630 / 640, 280 / 360),
        sample_count=SIGNATURE_MIN_SAMPLES,
    )
    asset = resolve_vehicle_asset(
        "cam",
        "blue sedan",
        _tracks([("car", MY_CAR, 1), ("car", NEIGHBOUR, 2)]),
        FRAME,
        signature=signature,
    )
    assert asset is not None
    assert asset.identification is not None
    assert asset.identification.protected is not None
    assert asset.identification.protected.track_id == 2


def test_zone_is_ignored_for_the_asset_box_when_the_frame_size_is_unknown() -> None:
    asset = resolve_vehicle_asset("cam", "blue sedan", [], (0.0, 0.0), zone=ZONE)
    assert asset is not None
    assert asset.box is None
    assert asset.location is AssetLocation.UNKNOWN


def test_every_asset_type_has_a_width() -> None:
    """A type with no reference width would fall back to OTHER's silently."""
    from blink_downloader.security.assets import _ASSET_WIDTH_FEET

    assert set(_ASSET_WIDTH_FEET) == set(AssetType)
