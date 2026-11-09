"""How many of each thing a clip held (``object_counts``).

The rows here are shaped like ``detected_objects`` rows: one per box per
sampled frame. Every case is a way the count went wrong, or a way the fix
for one of them could go wrong in turn.
"""

from __future__ import annotations

from typing import Any

import pytest

from blink_downloader.object_counts import (
    SAME_PLACE_IOU,
    summarize_detected_objects,
)

#: Six parking spaces side by side, none overlapping its neighbour.
_SPACES = [(float(i * 100), 200.0, float(i * 100 + 90), 260.0) for i in range(6)]


def _row(
    label: str,
    frame: int,
    box: tuple[float, float, float, float],
    track_id: int | None,
    confidence: float = 0.8,
) -> dict[str, Any]:
    return {
        "label": label,
        "confidence": confidence,
        "track_id": track_id,
        "frame_index": frame,
        "box_x1": box[0],
        "box_y1": box[1],
        "box_x2": box[2],
        "box_y2": box[3],
    }


def _count(rows: list[dict[str, Any]], label: str = "car") -> int:
    return {r["label"]: r["count"] for r in summarize_detected_objects(rows)}[label]


def test_six_parked_cars_are_six_when_the_tracker_renames_two_of_them() -> None:
    """The report behind this module: 8 cars shown for a clip holding 6.

    Two of the six go undetected for a few frames (someone walks in front
    of them) and come back under new track ids. Nothing moved.
    """
    rows = []
    for frame in range(12):
        for space, box in enumerate(_SPACES):
            hidden = space in (1, 4) and frame in (4, 5, 6)
            if hidden:
                continue
            renamed = space in (1, 4) and frame > 6
            rows.append(
                _row("car", frame, box, track_id=space + (10 if renamed else 0))
            )

    summary = summarize_detected_objects(rows)
    assert summary == [
        {
            "label": "car",
            "count": 6,
            "detections": 12 * 6 - 6,
            "max_confidence": pytest.approx(0.8),
        }
    ]


def test_a_car_renamed_more_than_once_is_still_one_car() -> None:
    box = _SPACES[0]
    rows = [_row("car", frame, box, track_id=frame // 2) for frame in range(8)]
    assert _count(rows) == 1


def test_a_box_that_wobbles_between_frames_is_still_the_same_parked_car() -> None:
    rows = [
        _row("car", 0, (100.0, 100.0, 200.0, 160.0), track_id=1),
        _row("car", 3, (104.0, 98.0, 207.0, 163.0), track_id=2),
    ]
    assert _count(rows) == 1


def test_two_cars_in_one_frame_are_two_however_much_they_overlap() -> None:
    """Seen together is the one thing that proves two objects, so tracks
    that share a frame are never joined: one car parked close behind
    another overlaps it heavily from a low camera."""
    front, behind = (100.0, 100.0, 200.0, 160.0), (110.0, 95.0, 205.0, 150.0)
    rows = [
        _row("car", 0, front, track_id=1),
        _row("car", 0, behind, track_id=2),
        _row("car", 1, front, track_id=1),
    ]
    assert _count(rows) == 2


def test_a_car_that_leaves_and_one_that_parks_elsewhere_are_two() -> None:
    rows = [_row("car", f, _SPACES[0], track_id=1) for f in range(3)]
    rows += [_row("car", f, _SPACES[3], track_id=2) for f in range(4, 7)]
    assert _count(rows) == 2


def test_overlap_just_short_of_the_bar_does_not_join_two_cars() -> None:
    # Shifted so intersection-over-union lands just under SAME_PLACE_IOU.
    a = (0.0, 0.0, 100.0, 100.0)
    b = (34.0, 0.0, 134.0, 100.0)
    assert 0.49 < 66 / 134 < SAME_PLACE_IOU
    rows = [_row("car", 0, a, track_id=1), _row("car", 2, b, track_id=2)]
    assert _count(rows) == 2


def test_a_renamed_car_does_not_swallow_the_one_parked_beside_it() -> None:
    """Joining goes by the box nearest in time and never across a shared
    frame, so a chain of joins cannot merge two cars that stood together."""
    left, right = _SPACES[0], _SPACES[1]
    rows = [
        _row("car", 0, left, track_id=1),
        _row("car", 0, right, track_id=2),
        _row("car", 2, left, track_id=3),
        _row("car", 2, right, track_id=4),
    ]
    assert _count(rows) == 2


def test_a_track_joins_the_group_it_overlaps_most() -> None:
    near, nearer = (0.0, 0.0, 100.0, 100.0), (20.0, 0.0, 120.0, 100.0)
    rows = [
        _row("car", 0, near, track_id=1),
        _row("car", 1, (300.0, 0.0, 400.0, 100.0), track_id=2),
        _row("car", 2, nearer, track_id=3),
        # Overlaps both earlier groups; it is the second one moved a little.
        _row("car", 4, (22.0, 0.0, 122.0, 100.0), track_id=4),
    ]
    assert _count(rows) == 2


def test_people_are_never_joined() -> None:
    """A second person standing where the first stood is a second person,
    and the security events count people by these same ids."""
    spot = (100.0, 50.0, 140.0, 200.0)
    rows = [_row("person", f, spot, track_id=1) for f in range(2)]
    rows += [_row("person", f, spot, track_id=2) for f in range(4, 6)]
    assert _count(rows, "person") == 2


def test_the_most_seen_in_one_frame_is_the_floor() -> None:
    rows = [_row("car", 0, box, track_id=None) for box in _SPACES[:3]]
    rows.append(_row("car", 1, _SPACES[0], track_id=1))
    assert _count(rows) == 3


def test_labels_are_ordered_by_count_then_boxes_then_name() -> None:
    rows = [
        _row("dog", 0, _SPACES[0], track_id=1, confidence=0.4),
        _row("cat", 0, _SPACES[1], track_id=1),
        _row("car", 0, _SPACES[2], track_id=1),
        _row("car", 1, _SPACES[2], track_id=1, confidence=0.9),
        _row("truck", 0, _SPACES[3], track_id=1),
        _row("truck", 0, _SPACES[4], track_id=2),
    ]
    summary = summarize_detected_objects(rows)
    assert [r["label"] for r in summary] == ["truck", "car", "cat", "dog"]
    assert summary[1]["max_confidence"] == pytest.approx(0.9)


def test_missing_frame_and_confidence_read_as_zero() -> None:
    row = _row("car", 0, _SPACES[0], track_id=None)
    row["frame_index"] = None
    row["confidence"] = None
    assert summarize_detected_objects([row]) == [
        {"label": "car", "count": 1, "detections": 1, "max_confidence": 0.0}
    ]


def test_no_rows_is_no_summary() -> None:
    assert summarize_detected_objects([]) == []
