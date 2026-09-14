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
    pixel_gap_to_feet,
    point_in_polygon,
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
