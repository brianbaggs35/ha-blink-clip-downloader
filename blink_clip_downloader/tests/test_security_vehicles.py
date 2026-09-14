"""Tests for blink_downloader.security.vehicles.

The scenarios here are the ones that motivated the module: a neighbour's
car parked beside the protected one, and the protected car having been
driven away entirely.
"""

from __future__ import annotations

import pytest

from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.tracks import build_tracks, subject_tracks
from blink_downloader.security.vehicles import (
    CONFIDENT_THRESHOLD,
    SIGNATURE_MIN_SAMPLES,
    VehicleSignature,
    _normalize,
    describe_region,
    identify_protected_vehicle,
    nearest_vehicle_for_subjects,
    relative_side,
)

FRAME = (640.0, 360.0)
MY_CAR: Box = (300.0, 180.0, 460.0, 280.0)
NEIGHBOUR: Box = (470.0, 180.0, 630.0, 280.0)
ZONE = Zone.from_config({"x_min": 0.47, "y_min": 0.5, "x_max": 0.72, "y_max": 0.78})


def _tracks(entries: list[tuple[str, Box, int]], frames: int = 3) -> list:
    detections = []
    for label, box, track_id in entries:
        for i in range(frames):
            detections.append((label, 0.9, box, track_id, i))
    return build_tracks(detections, 2.0, FRAME)


# ----------------------------------------------------------------------
# describe_region / relative_side
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        ((0.0, 0.0, 40.0, 40.0), "the upper left of the frame"),
        ((600.0, 320.0, 640.0, 360.0), "the lower right of the frame"),
        ((300.0, 160.0, 340.0, 200.0), "the centre of the frame"),
        ((0.0, 160.0, 40.0, 200.0), "the left of the frame"),
        ((300.0, 0.0, 340.0, 40.0), "the upper centre of the frame"),
    ],
)
def test_describe_region(box: Box, expected: str) -> None:
    assert describe_region(box, FRAME) == expected


def test_describe_region_without_a_frame_size() -> None:
    assert describe_region(MY_CAR, (0.0, 0.0)) == "an unknown part of the frame"


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (NEIGHBOUR, "to its right"),
        ((100.0, 180.0, 260.0, 280.0), "to its left"),
        ((300.0, 300.0, 460.0, 350.0), "in front of it"),
        ((300.0, 20.0, 460.0, 90.0), "behind it"),
    ],
)
def test_relative_side(other: Box, expected: str) -> None:
    assert relative_side(other, MY_CAR) == expected


# ----------------------------------------------------------------------
# identification
# ----------------------------------------------------------------------


def test_no_vehicles_detected() -> None:
    result = identify_protected_vehicle([], FRAME, zone=ZONE)
    assert result.protected is None
    assert result.basis == "no vehicle detected in these frames"


def test_zone_picks_the_vehicle_that_fills_it_not_the_nearest_edge() -> None:
    """The regression this module exists for: the neighbour's car is only a
    few pixels from the zone boundary, but the protected car is the one
    actually inside it."""
    tracks = _tracks([("car", MY_CAR, 1), ("car", NEIGHBOUR, 2)])
    result = identify_protected_vehicle(tracks, FRAME, zone=ZONE)
    assert result.protected is not None
    assert result.protected.track_id == 1
    assert result.confident is True
    assert [c.track_id for c in result.others] == [2]


def test_protected_vehicle_absent_is_reported_as_absent() -> None:
    """Only the neighbour's car is in frame — the answer is "yours is not
    here", not "the neighbour's car is yours"."""
    tracks = _tracks([("car", NEIGHBOUR, 2)])
    result = identify_protected_vehicle(tracks, FRAME, zone=ZONE)
    assert result.protected is None
    assert result.others and result.others[0].track_id == 2
    assert "does not appear to be in these frames" in result.basis


def test_single_vehicle_without_zone_or_signature_is_accepted() -> None:
    tracks = _tracks([("car", MY_CAR, 1)])
    result = identify_protected_vehicle(tracks, FRAME)
    assert result.protected is not None
    assert result.basis == "the only vehicle visible in these frames"
    assert result.confident is False
    assert result.ambiguous is True


def test_several_vehicles_without_evidence_is_ambiguous_and_says_so() -> None:
    tracks = _tracks([("car", MY_CAR, 1), ("car", (0.0, 0.0, 600.0, 350.0), 2)])
    result = identify_protected_vehicle(tracks, FRAME)
    assert result.protected is not None
    assert result.protected.track_id == 2  # the largest
    assert result.confidence < CONFIDENT_THRESHOLD
    assert "draw a zone" in result.basis


def test_learned_signature_identifies_the_car_with_no_zone_drawn() -> None:
    signature = VehicleSignature(
        box=(300 / 640, 180 / 360, 460 / 640, 280 / 360),
        histogram=(1.0, 0.0, 0.0),
        sample_count=SIGNATURE_MIN_SAMPLES,
    )
    tracks = _tracks([("car", MY_CAR, 1), ("car", NEIGHBOUR, 2)])
    result = identify_protected_vehicle(
        tracks,
        FRAME,
        signature=signature,
        histograms={1: (1.0, 0.0, 0.0), 2: (0.0, 1.0, 0.0)},
    )
    assert result.protected is not None
    assert result.protected.track_id == 1
    assert result.protected.appearance_similarity == pytest.approx(1.0)
    assert "learned parking spot" in result.basis
    assert "colours match" in result.basis


def test_unestablished_signature_is_ignored() -> None:
    signature = VehicleSignature(
        box=(470 / 640, 180 / 360, 630 / 640, 280 / 360), sample_count=1
    )
    tracks = _tracks([("car", MY_CAR, 1), ("car", NEIGHBOUR, 2)])
    result = identify_protected_vehicle(tracks, FRAME, zone=ZONE, signature=signature)
    assert result.protected is not None
    assert result.protected.track_id == 1
    assert result.protected.position_similarity == 0.0


def test_signature_without_a_histogram_still_matches_on_position() -> None:
    signature = VehicleSignature(
        box=(300 / 640, 180 / 360, 460 / 640, 280 / 360),
        sample_count=SIGNATURE_MIN_SAMPLES,
    )
    tracks = _tracks([("car", MY_CAR, 1)])
    result = identify_protected_vehicle(tracks, FRAME, signature=signature)
    assert result.protected is not None
    assert result.protected.appearance_similarity == 0.0
    assert "colours match" not in result.basis


def test_two_equally_good_candidates_reduce_confidence() -> None:
    """Side-by-side cars both half-filling the zone must not produce a
    confident answer just because one edged the other out."""
    left: Box = (280.0, 200.0, 400.0, 270.0)
    right: Box = (400.0, 200.0, 520.0, 270.0)
    wide = Zone.from_config({"x_min": 0.4, "y_min": 0.5, "x_max": 0.78, "y_max": 0.78})
    tracks = _tracks([("car", left, 1), ("car", right, 2)])
    result = identify_protected_vehicle(tracks, FRAME, zone=wide)
    assert result.protected is not None
    assert result.confident is False


def test_identification_to_dict() -> None:
    tracks = _tracks([("car", MY_CAR, 1), ("car", NEIGHBOUR, 2)])
    payload = identify_protected_vehicle(tracks, FRAME, zone=ZONE).to_dict()
    assert payload["identified"] is True
    assert payload["confident"] is True
    assert payload["protected"]["track_id"] == 1
    assert len(payload["others"]) == 1


def test_identification_to_dict_when_nothing_matched() -> None:
    payload = identify_protected_vehicle([], FRAME, zone=ZONE).to_dict()
    assert payload["identified"] is False
    assert payload["protected"] is None


# ----------------------------------------------------------------------
# signatures
# ----------------------------------------------------------------------


def test_signature_from_observation_starts_at_one_sample() -> None:
    signature = VehicleSignature.from_observation((0.1, 0.1, 0.2, 0.2), (1.0, 0.0))
    assert signature.sample_count == 1
    assert signature.established is False


def test_signature_blend_moves_slowly_toward_the_new_observation() -> None:
    signature = VehicleSignature(box=(0.0, 0.0, 0.4, 0.4), histogram=(1.0, 0.0))
    blended = signature.blend((1.0, 1.0, 1.0, 1.0), (0.0, 1.0))
    # A first blend takes the full 1/(0+1) weight, capped at 0.25.
    assert blended.box[0] == pytest.approx(0.25)
    assert blended.histogram == pytest.approx((0.75, 0.25))
    assert blended.sample_count == 1


def test_signature_blend_weight_decays_with_sample_count() -> None:
    early = VehicleSignature(box=(0.0, 0.0, 0.4, 0.4), sample_count=3)
    late = VehicleSignature(box=(0.0, 0.0, 0.4, 0.4), sample_count=50)
    target: Box = (1.0, 1.0, 1.0, 1.0)
    assert early.blend(target, ()).box[0] > late.blend(target, ()).box[0]


def test_signature_blend_adopts_a_histogram_of_a_different_length() -> None:
    signature = VehicleSignature(box=(0.0, 0.0, 0.4, 0.4), histogram=(1.0, 0.0))
    assert signature.blend((0.0, 0.0, 0.4, 0.4), (0.5, 0.5, 0.5)).histogram == (
        0.5,
        0.5,
        0.5,
    )


def test_signature_blend_keeps_the_old_histogram_when_none_is_supplied() -> None:
    signature = VehicleSignature(box=(0.0, 0.0, 0.4, 0.4), histogram=(1.0, 0.0))
    assert signature.blend((0.0, 0.0, 0.4, 0.4), ()).histogram == (1.0, 0.0)


def test_signature_appearance_similarity_handles_mismatched_vectors() -> None:
    signature = VehicleSignature(box=(0.0, 0.0, 1.0, 1.0), histogram=(1.0, 0.0))
    assert signature.appearance_similarity(()) == 0.0
    assert signature.appearance_similarity((1.0, 0.0, 0.0)) == 0.0


def test_signature_appearance_similarity_of_zero_vectors() -> None:
    signature = VehicleSignature(box=(0.0, 0.0, 1.0, 1.0), histogram=(0.0, 0.0))
    assert signature.appearance_similarity((0.0, 0.0)) == 0.0


def test_signature_to_dict_round_trips_values() -> None:
    signature = VehicleSignature((0.1, 0.2, 0.3, 0.4), (0.5, 0.5), 7)
    assert signature.to_dict() == {
        "box": [0.1, 0.2, 0.3, 0.4],
        "histogram": [0.5, 0.5],
        "sample_count": 7,
    }


# ----------------------------------------------------------------------
# nearest_vehicle_for_subjects
# ----------------------------------------------------------------------


def _identification_with_both_cars():
    tracks = _tracks([("car", MY_CAR, 1), ("car", NEIGHBOUR, 2)])
    return identify_protected_vehicle(tracks, FRAME, zone=ZONE)


def test_nearest_vehicle_detects_a_neighbour_at_their_own_car() -> None:
    identification = _identification_with_both_cars()
    subjects = subject_tracks(
        build_tracks([("person", 0.9, (600.0, 200.0, 640.0, 300.0), 9, 0)], 2.0, FRAME)
    )
    assert nearest_vehicle_for_subjects(subjects, identification) == "other"


def test_nearest_vehicle_detects_someone_at_the_protected_car() -> None:
    identification = _identification_with_both_cars()
    subjects = subject_tracks(
        build_tracks([("person", 0.9, (260.0, 200.0, 290.0, 300.0), 9, 0)], 2.0, FRAME)
    )
    assert nearest_vehicle_for_subjects(subjects, identification) == "protected"


def test_nearest_vehicle_between_the_two_is_unanswerable() -> None:
    identification = _identification_with_both_cars()
    subjects = subject_tracks(
        build_tracks([("person", 0.9, (462.0, 200.0, 468.0, 300.0), 9, 0)], 2.0, FRAME)
    )
    assert nearest_vehicle_for_subjects(subjects, identification) is None


def test_nearest_vehicle_uses_whichever_subject_got_closest() -> None:
    identification = _identification_with_both_cars()
    subjects = subject_tracks(
        build_tracks(
            [
                ("person", 0.9, (0.0, 0.0, 20.0, 60.0), 8, 0),
                ("person", 0.9, (600.0, 200.0, 640.0, 300.0), 9, 0),
            ],
            2.0,
            FRAME,
        )
    )
    assert nearest_vehicle_for_subjects(subjects, identification) == "other"


def test_nearest_vehicle_needs_another_vehicle_to_compare_against() -> None:
    tracks = _tracks([("car", MY_CAR, 1)])
    identification = identify_protected_vehicle(tracks, FRAME, zone=ZONE)
    subjects = subject_tracks(
        build_tracks([("person", 0.9, (0.0, 0.0, 20.0, 60.0), 9, 0)], 2.0, FRAME)
    )
    assert nearest_vehicle_for_subjects(subjects, identification) is None


def test_nearest_vehicle_without_subjects_or_identification() -> None:
    identification = _identification_with_both_cars()
    assert nearest_vehicle_for_subjects([], identification) is None
    assert (
        nearest_vehicle_for_subjects(
            subject_tracks(
                build_tracks(
                    [("person", 0.9, (0.0, 0.0, 20.0, 60.0), 9, 0)], 2.0, FRAME
                )
            ),
            identify_protected_vehicle([], FRAME, zone=ZONE),
        )
        is None
    )


def test_normalize_without_a_frame_size_returns_the_box_unchanged() -> None:
    """Defensive: dividing by a zero dimension would be worse than leaving
    the box in pixel space for the similarity check to score poorly."""
    assert _normalize(MY_CAR, (0.0, 0.0)) == MY_CAR


def test_a_zone_is_ignored_when_the_frame_size_is_unknown() -> None:
    """A zone only means anything once there is a frame to scale it onto —
    without one it would score every candidate zero and report the
    protected vehicle as absent, when the truth is that nothing could be
    measured."""
    tracks = _tracks([("car", MY_CAR, 1)])
    result = identify_protected_vehicle(tracks, (0.0, 0.0), zone=ZONE)
    assert result.protected is not None
    assert result.basis == "the only vehicle visible in these frames"
