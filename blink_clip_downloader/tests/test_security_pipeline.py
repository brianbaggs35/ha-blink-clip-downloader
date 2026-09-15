"""Tests for blink_downloader.security.pipeline.

The module is pure -- measurements in, verdict out -- so the cases here are
built from literals rather than from frames, which is the point of it being
separated out in the first place.
"""

from __future__ import annotations

from blink_downloader.security.assets import AssetLocation, AssetType, ProtectedAsset
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.pipeline import ClipMeasurements, assess_clip
from blink_downloader.security.tracks import build_tracks
from blink_downloader.security.vehicles import identify_protected_vehicle

FRAME = (640.0, 360.0)
MY_CAR: Box = (240.0, 170.0, 420.0, 290.0)
ZONE = Zone.from_config({"x_min": 0.38, "y_min": 0.47, "x_max": 0.66, "y_max": 0.81})


def _parked_car_tracks(frames: int = 4) -> list:
    return build_tracks(
        [("car", 0.95, MY_CAR, 1, i) for i in range(frames)], 2.0, FRAME
    )


def _measurements(**overrides) -> ClipMeasurements:
    base = {
        "camera": "Driveway",
        "tracks": _parked_car_tracks(),
        "frame_interval": 2.0,
        "frame_count": 4,
        "frames_analyzed": 4,
        "target_frames": 4,
    }
    base.update(overrides)
    return ClipMeasurements(**base)


def test_a_quiet_clip_produces_no_security_segment() -> None:
    """Nothing happened: a car sat in a driveway. An "evidence" section
    saying so would spend prompt budget telling the model that code found
    nothing, which reads as a hint that it should find something."""
    outcome = assess_clip(_measurements())

    assert outcome.events == []
    assert outcome.risk_score == 0.0
    assert outcome.prompt_segments == []


def test_an_asset_without_an_identification_is_left_alone() -> None:
    """A protected asset can be located without any vehicle having been
    identified -- the zone stands in for a car the detector never found.
    There is then no "which car is yours" story to tell, and no nearest
    vehicle to work out."""
    assert ZONE is not None
    asset = ProtectedAsset(
        name="Silver Kia",
        asset_type=AssetType.VEHICLE,
        camera="Driveway",
        zone=ZONE,
        box=ZONE.to_pixel_box(*FRAME),
        location=AssetLocation.ZONE,
    )
    outcome = assess_clip(_measurements(tracks=[], asset=asset))

    assert asset.identification is None
    assert outcome.nearest_vehicle is None
    assert outcome.prompt_segments == []


def test_an_identified_asset_gets_an_identity_segment() -> None:
    assert ZONE is not None
    tracks = _parked_car_tracks()
    identification = identify_protected_vehicle(tracks, FRAME, zone=ZONE)
    assert identification.protected is not None
    asset = ProtectedAsset(
        name="Silver Kia",
        asset_type=AssetType.VEHICLE,
        camera="Driveway",
        zone=ZONE,
        box=identification.protected.box,
        location=AssetLocation.DETECTED,
        identification=identification,
    )
    outcome = assess_clip(_measurements(tracks=tracks, asset=asset))

    assert outcome.prompt_segments
    assert "WHICH VEHICLE IS PROTECTED" in outcome.prompt_segments[0]
