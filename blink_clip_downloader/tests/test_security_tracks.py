"""Tests for blink_downloader.security.tracks."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.tracks import (
    ANIMAL_LABELS,
    CARRYABLE_LABELS,
    PERSON_LABEL,
    SUBJECT_LABELS,
    VEHICLE_LABELS,
    ObjectTrack,
    TrackPoint,
    build_tracks,
    person_tracks,
    subject_tracks,
)

FRAME = (640.0, 360.0)


def _track(
    boxes: Sequence[Box],
    *,
    label: str = PERSON_LABEL,
    track_id: int | None = 1,
    interval: float = 2.0,
    confidence: float = 0.9,
    frames: list[int] | None = None,
    frame_size: tuple[float, float] = FRAME,
    tracked: bool = True,
) -> ObjectTrack:
    indices = frames if frames is not None else list(range(len(boxes)))
    return ObjectTrack(
        label=label,
        track_id=track_id,
        points=[
            TrackPoint(frame_index=i, offset=i * interval, box=b, confidence=confidence)
            for i, b in zip(indices, boxes)
        ],
        frame_size=frame_size,
        tracked=tracked,
    )


# ----------------------------------------------------------------------
# label vocabulary
# ----------------------------------------------------------------------


def test_label_sets_are_disjoint_where_they_must_be() -> None:
    assert not SUBJECT_LABELS & VEHICLE_LABELS
    assert not SUBJECT_LABELS & CARRYABLE_LABELS
    assert ANIMAL_LABELS == SUBJECT_LABELS - {PERSON_LABEL}


# ----------------------------------------------------------------------
# build_tracks
# ----------------------------------------------------------------------


def test_build_tracks_groups_by_track_id_and_label() -> None:
    detections = [
        ("person", 0.9, (0.0, 0.0, 10.0, 20.0), 1, 0),
        ("person", 0.8, (5.0, 0.0, 15.0, 20.0), 1, 1),
        ("car", 0.95, (100.0, 0.0, 200.0, 60.0), 2, 0),
    ]
    tracks = build_tracks(detections, 2.0, FRAME)
    assert len(tracks) == 2
    person = next(t for t in tracks if t.label == "person")
    assert person.frame_count == 2
    assert person.tracked is True


def test_build_tracks_label_flip_starts_a_new_track() -> None:
    """A track id that changes class mid-clip must not produce one object
    that was a dog and then a person."""
    detections = [
        ("dog", 0.7, (0.0, 0.0, 10.0, 10.0), 3, 0),
        ("person", 0.7, (0.0, 0.0, 10.0, 10.0), 3, 1),
    ]
    tracks = build_tracks(detections, 2.0, FRAME)
    assert sorted(t.label for t in tracks) == ["dog", "person"]


def test_build_tracks_untracked_detections_group_by_label_and_are_flagged() -> None:
    detections = [
        ("person", 0.9, (0.0, 0.0, 10.0, 20.0), None, 0),
        ("person", 0.9, (40.0, 0.0, 50.0, 20.0), None, 1),
    ]
    (track,) = build_tracks(detections, 2.0, FRAME)
    assert track.tracked is False
    assert track.track_id is None
    assert track.frame_count == 2


def test_build_tracks_offsets_come_from_frame_index_and_interval() -> None:
    detections = [("person", 0.9, (0.0, 0.0, 1.0, 1.0), 1, 3)]
    (track,) = build_tracks(detections, 2.5, FRAME)
    assert track.first_offset == pytest.approx(7.5)


def test_build_tracks_sorts_points_by_frame_even_when_input_is_shuffled() -> None:
    detections = [
        ("person", 0.9, (30.0, 0.0, 40.0, 20.0), 1, 2),
        ("person", 0.9, (0.0, 0.0, 10.0, 20.0), 1, 0),
    ]
    (track,) = build_tracks(detections, 2.0, FRAME)
    assert [p.frame_index for p in track.points] == [0, 2]


def test_build_tracks_orders_tracks_by_first_appearance() -> None:
    detections = [
        ("person", 0.9, (0.0, 0.0, 10.0, 10.0), 2, 4),
        ("dog", 0.9, (0.0, 0.0, 10.0, 10.0), 1, 1),
    ]
    tracks = build_tracks(detections, 2.0, FRAME)
    assert [t.label for t in tracks] == ["dog", "person"]


def test_build_tracks_empty_input() -> None:
    assert build_tracks([], 2.0, FRAME) == []


def test_subject_and_person_filters() -> None:
    detections = [
        ("person", 0.9, (0.0, 0.0, 1.0, 1.0), 1, 0),
        ("dog", 0.9, (0.0, 0.0, 1.0, 1.0), 2, 0),
        ("car", 0.9, (0.0, 0.0, 1.0, 1.0), 3, 0),
    ]
    tracks = build_tracks(detections, 2.0, FRAME)
    assert sorted(t.label for t in subject_tracks(tracks)) == ["dog", "person"]
    assert [t.label for t in person_tracks(tracks)] == ["person"]


# ----------------------------------------------------------------------
# basic shape
# ----------------------------------------------------------------------


def test_dwell_and_frame_count() -> None:
    track = _track([(0, 0, 10, 20)] * 4)
    assert track.dwell_seconds == pytest.approx(6.0)
    assert track.frame_count == 4


def test_frame_count_counts_distinct_frames_only() -> None:
    track = _track([(0, 0, 10, 20)] * 3, frames=[0, 0, 1])
    assert track.frame_count == 2


def test_mean_confidence() -> None:
    track = ObjectTrack(
        label="person",
        track_id=1,
        points=[
            TrackPoint(0, 0.0, (0, 0, 1, 1), 0.4),
            TrackPoint(1, 2.0, (0, 0, 1, 1), 0.8),
        ],
        frame_size=FRAME,
    )
    assert track.mean_confidence == pytest.approx(0.6)


def test_peak_height_fraction() -> None:
    track = _track([(0, 0, 10, 90), (0, 0, 10, 180)])
    assert track.peak_height_fraction == pytest.approx(0.5)


def test_peak_height_fraction_zero_height_frame() -> None:
    track = _track([(0, 0, 10, 90)], frame_size=(640.0, 0.0))
    assert track.peak_height_fraction == 0.0


def test_peak_height_fraction_is_capped_at_one() -> None:
    track = _track([(0, 0, 10, 900)])
    assert track.peak_height_fraction == pytest.approx(1.0)


def test_peak_area_fraction() -> None:
    track = _track([(0.0, 0.0, 64.0, 36.0)])
    assert track.peak_area_fraction == pytest.approx(0.01)


def test_peak_area_fraction_zero_area_frame() -> None:
    track = _track([(0, 0, 10, 10)], frame_size=(0.0, 0.0))
    assert track.peak_area_fraction == 0.0


def test_continuity_perfect_when_seen_every_frame() -> None:
    track = _track([(0, 0, 10, 20)] * 4)
    assert track.continuity(2.0) == pytest.approx(1.0)


def test_continuity_drops_when_track_is_lost_and_reacquired() -> None:
    track = _track([(0, 0, 10, 20)] * 3, frames=[0, 2, 4])
    assert track.continuity(2.0) == pytest.approx(0.6)


def test_continuity_of_single_sighting_is_one() -> None:
    assert _track([(0, 0, 10, 20)]).continuity(2.0) == pytest.approx(1.0)


def test_continuity_with_zero_interval_is_one() -> None:
    assert _track([(0, 0, 10, 20)] * 3).continuity(0.0) == pytest.approx(1.0)


# ----------------------------------------------------------------------
# motion
# ----------------------------------------------------------------------


def test_path_length_is_in_frame_widths() -> None:
    track = _track([(0, 0, 10, 10), (320, 0, 330, 10)])
    assert track.path_length == pytest.approx(0.5)


def test_path_length_single_point_is_zero() -> None:
    assert _track([(0, 0, 10, 10)]).path_length == 0.0


def test_path_length_zero_width_frame_is_zero() -> None:
    track = _track([(0, 0, 10, 10), (100, 0, 110, 10)], frame_size=(0.0, 360.0))
    assert track.path_length == 0.0


def test_average_speed() -> None:
    track = _track([(0, 0, 10, 10), (320, 0, 330, 10)], interval=2.0)
    assert track.average_speed == pytest.approx(0.25)


def test_average_speed_stationary_track_is_zero() -> None:
    assert _track([(0, 0, 10, 10)]).average_speed == 0.0


def test_net_drift() -> None:
    track = _track([(0, 0, 10, 10), (64, 36, 74, 46)])
    dx, dy = track.net_drift
    assert dx == pytest.approx(0.1)
    assert dy == pytest.approx(0.05625)


def test_net_drift_zero_width_frame() -> None:
    track = _track([(0, 0, 10, 10), (64, 36, 74, 46)], frame_size=(0.0, 0.0))
    assert track.net_drift == (0.0, 0.0)


def test_area_ratio_growth() -> None:
    track = _track([(0, 0, 10, 10), (0, 0, 20, 20)])
    assert track.area_ratio == pytest.approx(4.0)


def test_area_ratio_degenerate_first_box_is_one() -> None:
    track = _track([(0, 0, 0, 0), (0, 0, 20, 20)])
    assert track.area_ratio == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("boxes", "expected"),
    [
        ([(0, 0, 10, 10)], "seen once"),
        ([(0, 0, 10, 10), (0, 0, 30, 30)], "moving toward the camera"),
        ([(0, 0, 30, 30), (0, 0, 10, 10)], "moving away from the camera"),
        ([(0, 0, 10, 10), (2, 0, 12, 10)], "staying in roughly one place"),
        ([(0, 0, 10, 10), (200, 0, 210, 10)], "moving right across the frame"),
        ([(200, 0, 210, 10), (0, 0, 10, 10)], "moving left"),
        ([(0, 0, 10, 10), (0, 200, 10, 210)], "moving down the frame"),
        ([(0, 200, 10, 210), (0, 0, 10, 10)], "moving up the frame"),
    ],
)
def test_direction(boxes: list[Box], expected: str) -> None:
    assert _track(boxes).direction == expected


def test_max_speed_increase_detects_a_sudden_rush() -> None:
    steady = _track([(0, 0, 10, 10), (64, 0, 74, 10), (128, 0, 138, 10)])
    lurching = _track([(0, 0, 10, 10), (10, 0, 20, 10), (300, 0, 310, 10)])
    assert steady.max_speed_increase == pytest.approx(0.0)
    assert lurching.max_speed_increase > steady.max_speed_increase


def test_max_speed_increase_ignores_slowing_down() -> None:
    """Everyone slows to a stop on reaching a car; only speeding up is
    unusual enough to mean anything."""
    stopping = _track([(0, 0, 10, 10), (300, 0, 310, 10), (302, 0, 312, 10)])
    assert stopping.max_speed_increase == pytest.approx(0.0)


def test_max_speed_increase_needs_three_sightings() -> None:
    assert _track([(0, 0, 10, 10), (200, 0, 210, 10)]).max_speed_increase == 0.0


def test_max_speed_increase_zero_width_frame() -> None:
    track = _track([(0, 0, 10, 10)] * 3, frame_size=(0.0, 360.0))
    assert track.max_speed_increase == 0.0


def test_max_speed_increase_ignores_duplicate_offsets() -> None:
    """Two sightings in the same frame give no time to divide by, so they
    contribute no leg rather than an infinite speed."""
    track = _track([(0, 0, 10, 10)] * 3, frames=[0, 0, 1])
    assert track.max_speed_increase == 0.0


# ----------------------------------------------------------------------
# approach profiles
# ----------------------------------------------------------------------


def test_approach_profile_measures_ground_distance() -> None:
    """Everything on the same ground line, so the gap is purely horizontal
    and the feet-based measure matches the edge-based one."""
    target: Box = (500.0, 0.0, 600.0, 100.0)
    track = _track([(0, 0, 10, 100), (200, 0, 210, 100), (450, 0, 460, 100)])
    profile = track.approach_to(target)
    assert profile.first_gap == pytest.approx(495.0)
    assert profile.min_gap == pytest.approx(45.0)
    assert profile.min_gap_offset == pytest.approx(4.0)
    assert profile.approached is True
    assert profile.retreated is False


def test_approach_profile_ignores_foreground_traffic() -> None:
    """A passer-by whose box touches the target while standing well in
    front of it is not close to it, and must not read as though they are."""
    target: Box = (200.0, 0.0, 400.0, 100.0)
    passing = _track([(180, 100, 220, 200)]).approach_to(target)
    beside = _track([(180, 20, 220, 100)]).approach_to(target)
    # Both touch the target's outline in the image...
    assert passing.min_box_gap <= 0
    assert beside.min_box_gap <= 0
    # ...but only one of them is actually standing next to it.
    assert beside.min_gap == pytest.approx(0.0)
    assert passing.min_gap > 200.0


def test_approach_profile_retreat_after_getting_close() -> None:
    target: Box = (500.0, 0.0, 600.0, 100.0)
    track = _track([(400, 0, 410, 100), (450, 0, 460, 100), (100, 0, 110, 100)])
    profile = track.approach_to(target)
    assert profile.retreated is True
    assert profile.retreat_fraction == pytest.approx((395 - 45) / 395)


def test_approach_profile_records_the_deepest_overlap_separately() -> None:
    target: Box = (100.0, 0.0, 200.0, 100.0)
    track = _track([(0, 0, 10, 100), (120, 10, 180, 100)])
    profile = track.approach_to(target)
    assert profile.min_gap == pytest.approx(0.0)
    assert profile.min_box_gap < 0
    assert profile.min_box_gap_index == 1
    assert profile.approach_fraction == pytest.approx(1.0)


def test_approach_profile_starting_on_top_of_the_target() -> None:
    """A first gap of zero leaves nothing to close, so the approach fraction
    is zero rather than a division by zero."""
    target: Box = (0.0, 0.0, 100.0, 100.0)
    track = _track([(0, 0, 100, 100), (500, 0, 510, 100)])
    profile = track.approach_to(target)
    assert profile.approach_fraction == 0.0
    assert profile.retreat_fraction == pytest.approx(1.0)


def test_approach_profile_ending_on_top_of_the_target() -> None:
    target: Box = (0.0, 0.0, 100.0, 100.0)
    track = _track([(500, 0, 510, 100), (0, 0, 100, 100)])
    profile = track.approach_to(target)
    assert profile.retreat_fraction == 0.0


# ----------------------------------------------------------------------
# zones
# ----------------------------------------------------------------------


def _zone() -> Zone:
    zone = Zone.from_config({"x_min": 0.5, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0})
    assert zone is not None
    return zone


def test_zone_membership_uses_the_subjects_feet() -> None:
    zone = Zone.from_config({"x_min": 0.0, "y_min": 0.8, "x_max": 1.0, "y_max": 1.0})
    assert zone is not None
    # Box centre sits above the zone; the feet are inside it.
    track = _track([(0.0, 200.0, 40.0, 320.0)])
    assert track.zone_membership(zone) == [True]


def test_zone_membership_is_cached_per_zone() -> None:
    track = _track([(0, 0, 10, 10)])
    zone = _zone()
    first = track.zone_membership(zone)
    assert track.zone_membership(zone) is first


def test_zone_membership_zero_sized_frame_is_all_false() -> None:
    track = _track([(0, 0, 10, 10)] * 2, frame_size=(0.0, 0.0))
    assert track.zone_membership(_zone()) == [False, False]


def test_entered_zone_requires_an_outside_then_inside_transition() -> None:
    entering = _track([(0.0, 0.0, 20.0, 100.0), (400.0, 0.0, 420.0, 100.0)])
    already_inside = _track([(400.0, 0.0, 420.0, 100.0)] * 2)
    assert entering.entered_zone(_zone()) is True
    assert already_inside.entered_zone(_zone()) is False


def test_zone_dwell_spans_first_to_last_in_zone_sighting() -> None:
    track = _track(
        [
            (0.0, 0.0, 20.0, 100.0),
            (400.0, 0.0, 420.0, 100.0),
            (500.0, 0.0, 520.0, 100.0),
        ]
    )
    assert track.zone_dwell(_zone()) == pytest.approx(2.0)


def test_zone_dwell_is_zero_when_never_in_zone() -> None:
    track = _track([(0.0, 0.0, 20.0, 100.0)])
    assert track.zone_dwell(_zone()) == 0.0


def test_in_zone() -> None:
    assert _track([(400.0, 0.0, 420.0, 100.0)]).in_zone(_zone()) is True
    assert _track([(0.0, 0.0, 20.0, 100.0)]).in_zone(_zone()) is False
