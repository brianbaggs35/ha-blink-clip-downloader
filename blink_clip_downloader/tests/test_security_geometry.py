"""Tests for blink_downloader.security.geometry."""

from __future__ import annotations

import pytest

from blink_downloader.security.geometry import (
    TYPICAL_VEHICLE_WIDTH_FEET,
    Zone,
    box_area,
    box_center,
    box_foot_point,
    box_gap,
    box_iou,
    box_overlap_coefficient,
    clip_polygon_to_box,
    pixel_gap_to_feet,
    point_in_polygon,
    polygon_area,
)

# ----------------------------------------------------------------------
# box_gap
# ----------------------------------------------------------------------


def test_box_gap_horizontal_separation() -> None:
    assert box_gap((0, 0, 10, 10), (20, 0, 30, 10)) == pytest.approx(10.0)


def test_box_gap_diagonal_separation_is_hypotenuse() -> None:
    assert box_gap((0, 0, 10, 10), (13, 14, 20, 20)) == pytest.approx(5.0)


def test_box_gap_overlap_is_negative_shallower_axis() -> None:
    # 8px of horizontal overlap, 2px of vertical: the shallower axis wins.
    assert box_gap((0, 0, 10, 10), (2, 8, 12, 18)) == pytest.approx(-2.0)


def test_box_gap_touching_edges_is_zero() -> None:
    assert box_gap((0, 0, 10, 10), (10, 0, 20, 10)) == pytest.approx(0.0)


# ----------------------------------------------------------------------
# simple box helpers
# ----------------------------------------------------------------------


def test_box_center() -> None:
    assert box_center((0, 0, 10, 20)) == (5.0, 10.0)


def test_box_foot_point_is_bottom_centre() -> None:
    assert box_foot_point((10, 0, 30, 40)) == (20.0, 40.0)


def test_box_area() -> None:
    assert box_area((0, 0, 4, 5)) == pytest.approx(20.0)


def test_box_area_degenerate_box_is_zero() -> None:
    assert box_area((10, 10, 4, 4)) == pytest.approx(0.0)


def test_box_iou_identical_boxes() -> None:
    assert box_iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)


def test_box_iou_disjoint_boxes_is_zero() -> None:
    assert box_iou((0, 0, 10, 10), (20, 20, 30, 30)) == pytest.approx(0.0)


def test_box_iou_half_overlap() -> None:
    # 50 of 150 union.
    assert box_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_box_iou_zero_area_boxes_do_not_divide_by_zero() -> None:
    assert box_iou((5, 5, 5, 5), (5, 5, 5, 5)) == pytest.approx(0.0)


# ----------------------------------------------------------------------
# point_in_polygon
# ----------------------------------------------------------------------


def test_point_in_polygon_inside() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert point_in_polygon(5.0, 5.0, square) is True


def test_point_in_polygon_outside() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert point_in_polygon(15.0, 5.0, square) is False


def test_point_in_polygon_concave_notch_excluded() -> None:
    # An arrow-head whose notch cuts back to the middle of the shape.
    arrow = [(0.0, 0.0), (10.0, 5.0), (0.0, 10.0), (4.0, 5.0)]
    assert point_in_polygon(1.0, 5.0, arrow) is False
    assert point_in_polygon(6.0, 5.0, arrow) is True


# ----------------------------------------------------------------------
# pixel_gap_to_feet
# ----------------------------------------------------------------------


def test_pixel_gap_to_feet_uses_reference_width() -> None:
    # Half the reference width of a 6ft car is 3ft.
    assert pixel_gap_to_feet(50.0, 100.0) == pytest.approx(3.0)


def test_pixel_gap_to_feet_honours_custom_reference() -> None:
    assert pixel_gap_to_feet(100.0, 100.0, 3.0) == pytest.approx(3.0)


def test_pixel_gap_to_feet_without_scale_is_none() -> None:
    assert pixel_gap_to_feet(50.0, 0.0) is None


def test_typical_vehicle_width_matches_prompt_language() -> None:
    """The prompt tells the model a car is about six feet wide; the code
    computing distances must use the same figure or the two disagree."""
    assert TYPICAL_VEHICLE_WIDTH_FEET == 6.0


# ----------------------------------------------------------------------
# Zone
# ----------------------------------------------------------------------


def test_zone_from_config_none_and_empty() -> None:
    assert Zone.from_config(None) is None
    assert Zone.from_config({}) is None


def test_zone_from_config_rectangle() -> None:
    zone = Zone.from_config({"x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.6})
    assert zone is not None
    assert zone.bounds == (0.1, 0.2, 0.5, 0.6)
    assert zone.is_polygon is False


def test_zone_from_config_rectangle_missing_keys_defaults_to_full_frame() -> None:
    zone = Zone.from_config({"x_min": 0.25})
    assert zone is not None
    assert zone.bounds == (0.25, 0.0, 1.0, 1.0)


def test_zone_from_config_polygon_bounds_are_its_bounding_box() -> None:
    zone = Zone.from_config(
        {"shape": "polygon", "points": [[0.2, 0.3], [0.8, 0.1], [0.5, 0.9]]}
    )
    assert zone is not None
    assert zone.is_polygon is True
    assert zone.bounds == (0.2, 0.1, 0.8, 0.9)


def test_zone_from_config_polygon_without_points_is_none() -> None:
    assert Zone.from_config({"shape": "polygon", "points": []}) is None


def test_zone_contains_rectangle() -> None:
    zone = Zone.from_config({"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 0.5})
    assert zone is not None
    assert zone.contains(0.25, 0.25) is True
    assert zone.contains(0.75, 0.25) is False


def test_zone_contains_polygon_uses_exact_outline() -> None:
    triangle = Zone.from_config(
        {"shape": "polygon", "points": [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]}
    )
    assert triangle is not None
    assert triangle.contains(0.1, 0.1) is True
    # Inside the bounding box but outside the triangle itself.
    assert triangle.contains(0.9, 0.9) is False


def test_zone_to_pixel_box_scales_to_frame() -> None:
    zone = Zone.from_config({"x_min": 0.5, "y_min": 0.25, "x_max": 1.0, "y_max": 0.75})
    assert zone is not None
    assert zone.to_pixel_box(640, 360) == (320.0, 90.0, 640.0, 270.0)


# ----------------------------------------------------------------------
# polygon_area / clip_polygon_to_box
# ----------------------------------------------------------------------

#: A unit square traced clockwise and counter-clockwise; the area is the
#: same either way, since winding order is a drawing artefact of whichever
#: direction the user dragged.
_SQUARE_CW = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
_SQUARE_CCW = list(reversed(_SQUARE_CW))


@pytest.mark.parametrize("ring", [_SQUARE_CW, _SQUARE_CCW])
def test_polygon_area_ignores_winding_order(
    ring: list[tuple[float, float]],
) -> None:
    assert polygon_area(ring) == pytest.approx(100.0)


def test_polygon_area_of_a_triangle() -> None:
    assert polygon_area([(0.0, 0.0), (4.0, 0.0), (0.0, 3.0)]) == pytest.approx(6.0)


@pytest.mark.parametrize(
    "ring", [[], [(0.0, 0.0)], [(0.0, 0.0), (1.0, 1.0)]], ids=["none", "one", "two"]
)
def test_polygon_area_needs_three_vertices(ring: list[tuple[float, float]]) -> None:
    assert polygon_area(ring) == 0.0


def test_clip_polygon_to_box_trims_to_the_overlap() -> None:
    clipped = clip_polygon_to_box(_SQUARE_CW, (5.0, 5.0, 20.0, 20.0))
    assert polygon_area(clipped) == pytest.approx(25.0)


def test_clip_polygon_to_box_keeps_a_fully_enclosed_polygon() -> None:
    clipped = clip_polygon_to_box(_SQUARE_CW, (-5.0, -5.0, 25.0, 25.0))
    assert polygon_area(clipped) == pytest.approx(100.0)


def test_clip_polygon_to_box_of_a_disjoint_box_is_empty() -> None:
    assert clip_polygon_to_box(_SQUARE_CW, (50.0, 50.0, 60.0, 60.0)) == []


@pytest.mark.parametrize(
    "box",
    [(10.0, 10.0, 10.0, 20.0), (10.0, 10.0, 20.0, 10.0)],
    ids=["zero-width", "zero-height"],
)
def test_clip_polygon_to_box_of_a_degenerate_box_is_empty(
    box: tuple[float, float, float, float],
) -> None:
    assert clip_polygon_to_box(_SQUARE_CW, box) == []


def test_clip_polygon_to_box_of_a_degenerate_ring_is_empty() -> None:
    assert clip_polygon_to_box([(0.0, 0.0), (1.0, 1.0)], (0.0, 0.0, 5.0, 5.0)) == []


def test_clip_polygon_to_box_handles_a_concave_shape() -> None:
    """A C, clipped to a band across its middle, leaves the two arms. They
    come back joined by a zero-area seam, which contributes nothing to the
    area -- the only thing anything reads this result for."""
    c_shape = [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 2.0),
        (2.0, 2.0),
        (2.0, 8.0),
        (10.0, 8.0),
        (10.0, 10.0),
        (0.0, 10.0),
    ]
    assert polygon_area(c_shape) == pytest.approx(10 * 10 - 8 * 6)
    clipped = clip_polygon_to_box(c_shape, (0.0, 0.0, 10.0, 5.0))
    assert polygon_area(clipped) == pytest.approx(10 * 2 + 2 * 3)


# ----------------------------------------------------------------------
# box_overlap_coefficient
# ----------------------------------------------------------------------


def test_box_overlap_coefficient_is_one_when_the_smaller_box_is_enclosed() -> None:
    assert box_overlap_coefficient((2, 2, 4, 4), (0, 0, 10, 10)) == pytest.approx(1.0)


def test_box_overlap_coefficient_is_zero_when_disjoint() -> None:
    assert box_overlap_coefficient((0, 0, 1, 1), (5, 5, 6, 6)) == 0.0


def test_box_overlap_coefficient_of_a_degenerate_box_is_zero() -> None:
    assert box_overlap_coefficient((0, 0, 0, 0), (0, 0, 10, 10)) == 0.0


# ----------------------------------------------------------------------
# Zone.overlap_with_box / Zone.covered_share_of / Zone.to_pixel_points
# ----------------------------------------------------------------------

#: A right triangle filling the top-left half of the frame. Its bounding
#: box is the whole frame, which is exactly how a bounding-box shortcut
#: goes wrong.
_TRIANGLE = {"shape": "polygon", "points": [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]}


def test_zone_overlap_with_box_uses_the_outline_not_the_bounds() -> None:
    zone = Zone.from_config(_TRIANGLE)
    assert zone is not None
    assert zone.to_pixel_box(100, 100) == (0.0, 0.0, 100.0, 100.0)
    # A box in the excluded corner: full marks against the bounds, none
    # against the shape the user actually drew.
    corner = (80.0, 80.0, 95.0, 95.0)
    assert box_overlap_coefficient(corner, zone.to_pixel_box(100, 100)) == 1.0
    assert zone.overlap_with_box(corner, 100, 100) == 0.0


def test_zone_overlap_with_box_matches_the_plain_arithmetic_for_rectangles() -> None:
    zone = Zone.from_config({"x_min": 0.2, "y_min": 0.2, "x_max": 0.6, "y_max": 0.6})
    assert zone is not None
    box = (30.0, 30.0, 90.0, 50.0)
    assert zone.overlap_with_box(box, 100, 100) == box_overlap_coefficient(
        box, zone.to_pixel_box(100, 100)
    )


def test_zone_overlap_with_box_halves_a_box_split_by_the_hypotenuse() -> None:
    zone = Zone.from_config(_TRIANGLE)
    assert zone is not None
    # A square straddling the diagonal is cut exactly in half by it.
    assert zone.overlap_with_box((25.0, 25.0, 75.0, 75.0), 100, 100) == pytest.approx(
        0.5
    )


@pytest.mark.parametrize(
    ("width", "height"), [(0, 100), (100, 0)], ids=["no-width", "no-height"]
)
def test_zone_overlap_with_box_needs_a_real_frame(width: int, height: int) -> None:
    zone = Zone.from_config(_TRIANGLE)
    assert zone is not None
    assert zone.overlap_with_box((0.0, 0.0, 10.0, 10.0), width, height) == 0.0
    assert zone.covered_share_of((0.0, 0.0, 10.0, 10.0), width, height) == 0.0


def test_zone_overlap_with_box_of_a_degenerate_box_is_zero() -> None:
    zone = Zone.from_config(_TRIANGLE)
    assert zone is not None
    assert zone.overlap_with_box((10.0, 10.0, 10.0, 10.0), 100, 100) == 0.0
    assert zone.covered_share_of((10.0, 10.0, 10.0, 10.0), 100, 100) == 0.0


def test_zone_covered_share_of_measures_the_boxs_own_area() -> None:
    zone = Zone.from_config(_TRIANGLE)
    assert zone is not None
    # Wholly inside the triangle.
    assert zone.covered_share_of((5.0, 5.0, 15.0, 15.0), 100, 100) == pytest.approx(1.0)
    # Straddling the hypotenuse.
    assert zone.covered_share_of((40.0, 40.0, 60.0, 60.0), 100, 100) == pytest.approx(
        0.5
    )
    # In the excluded corner.
    assert zone.covered_share_of((80.0, 80.0, 95.0, 95.0), 100, 100) == 0.0


def test_zone_covered_share_of_rectangle_is_the_plain_ratio() -> None:
    zone = Zone.from_config({"x_min": 0.0, "y_min": 0.0, "x_max": 0.5, "y_max": 1.0})
    assert zone is not None
    # Half the box's width sits inside the left-hand half of the frame.
    assert zone.covered_share_of((25.0, 0.0, 75.0, 10.0), 100, 100) == pytest.approx(
        0.5
    )


def test_zone_to_pixel_points_gives_a_rectangle_its_four_corners() -> None:
    zone = Zone.from_config({"x_min": 0.0, "y_min": 0.5, "x_max": 1.0, "y_max": 1.0})
    assert zone is not None
    assert zone.to_pixel_points(100, 100) == [
        (0.0, 50.0),
        (100.0, 50.0),
        (100.0, 100.0),
        (0.0, 100.0),
    ]


def test_zone_to_pixel_points_scales_a_polygon() -> None:
    zone = Zone.from_config(_TRIANGLE)
    assert zone is not None
    assert zone.to_pixel_points(640, 360) == [(0.0, 0.0), (640.0, 0.0), (0.0, 360.0)]


# ----------------------------------------------------------------------
# A lasso that crosses itself must not switch the zone off
# ----------------------------------------------------------------------

#: A figure-of-eight: the Vehicles tab traces freeform zones with a lasso,
#: which a user can cross over itself, and whose two loops run in opposite
#: directions. Shoelace measures that as enclosing nothing at all.
_BOWTIE = {
    "shape": "polygon",
    "points": [[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]],
}


def test_a_self_crossing_outline_falls_back_to_its_bounds() -> None:
    """Measuring it as empty would silently switch car protection off for
    that camera, which is worse than the coarse answer it had before."""
    zone = Zone.from_config(_BOWTIE)
    assert zone is not None
    assert polygon_area(zone.to_pixel_points(100, 100)) == 0.0
    # The ray-casting membership test copes with the crossing on its own:
    # the left and right lobes are inside, the pinch between them is not.
    assert zone.contains(0.2, 0.5) is True
    assert zone.contains(0.5, 0.5) is False

    # ... so the area-based tests fall back to the bounds rather than
    # reporting that nothing is ever in this zone.
    middle = (40.0, 40.0, 60.0, 60.0)
    assert zone.overlap_with_box(middle, 100, 100) == pytest.approx(1.0)
    assert zone.covered_share_of(middle, 100, 100) == pytest.approx(1.0)


def test_overlap_never_exceeds_one_for_a_partly_cancelling_outline() -> None:
    """A trace that crosses itself only partly still under-reports its own
    area, which left unchecked turns a ratio into something above 1.0 and
    lets one candidate outscore a perfect match."""
    # A ring whose tail doubles back inside itself and crosses an edge.
    zone = Zone.from_config(
        {
            "shape": "polygon",
            "points": [
                [0.0, 0.0],
                [1.0, 0.0],
                [1.0, 1.0],
                [0.0, 1.0],
                [0.0, 0.2],
                [0.8, 0.2],
                [0.8, 0.8],
                [0.05, 0.8],
            ],
        }
    )
    assert zone is not None
    for box in [
        (10.0, 10.0, 90.0, 90.0),
        (0.0, 0.0, 100.0, 100.0),
        (30.0, 30.0, 50.0, 50.0),
        (0.0, 20.0, 80.0, 80.0),
    ]:
        assert 0.0 <= zone.overlap_with_box(box, 100, 100) <= 1.0
        assert 0.0 <= zone.covered_share_of(box, 100, 100) <= 1.0
