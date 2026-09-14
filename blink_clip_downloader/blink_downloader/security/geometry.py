"""Coordinate math shared by the vision and security layers.

Two coordinate spaces appear throughout this add-on and must not be mixed
up:

* **Normalized** (0.0-1.0) — what a user-drawn zone is stored as (see the
  Vehicles tab's ``car_zone``), so a zone keeps meaning at any frame
  resolution.
* **Pixel** — what the object detector returns, sized to whatever
  resolution the analyzed frames happen to be.

:class:`Zone` owns the conversion between them; every ``box_*`` helper
below is scale-independent and simply requires both of its arguments to be
in the *same* space.

Pure stdlib by design — :mod:`blink_downloader.vision` imports from here
rather than the other way round, so none of this drags in torch/opencv.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: ``(x1, y1, x2, y2)`` in either coordinate space, top-left origin.
Box = tuple[float, float, float, float]

#: Reference real-world vehicle width (feet), used to turn a pixel gap into
#: an approximate distance by treating a detected vehicle's own pixel width
#: as the scale. The same "a typical car is about 6 feet wide" reference
#: ``analyzer.py``'s ``_car_protection_segment`` already gives the AI model,
#: so code-computed distances and the model's own written 1 ft / 3 ft rules
#: reason about one consistent scale instead of two invented separately.
TYPICAL_VEHICLE_WIDTH_FEET = 6.0


def box_gap(a: Box, b: Box) -> float:
    """Return the edge-to-edge gap between boxes *a* and *b*.

    Zero or positive when the boxes don't overlap (the straight-line
    distance between their nearest edges). Negative when they overlap, with
    magnitude equal to the smaller of the two axes' overlap depth — more
    negative means more deeply overlapping.
    """
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(bx1 - ax2, ax1 - bx2, 0.0)
    dy = max(by1 - ay2, ay1 - by2, 0.0)
    if not dx and not dy:
        overlap_x = min(ax2, bx2) - max(ax1, bx1)
        overlap_y = min(ay2, by2) - max(ay1, by1)
        return -min(overlap_x, overlap_y)
    return math.hypot(dx, dy)


def box_center(box: Box) -> tuple[float, float]:
    """Return the centre point of *box*."""
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def box_foot_point(box: Box) -> tuple[float, float]:
    """Return the bottom-centre of *box* — where a subject meets the ground.

    Zone membership is judged from this point rather than the box centre:
    a person standing just outside a driveway zone still has their *centre*
    well above the ground and can drift into an overhead zone that their
    feet never enter, which would read as "entered the zone" when they
    plainly did not.
    """
    x1, _y1, x2, y2 = box
    return ((x1 + x2) / 2.0, y2)


#: How much more ground distance a pixel of *vertical* separation represents
#: than a pixel of horizontal separation, for objects sharing a ground plane
#: in a typical downward-angled security camera view. A person walking along
#: the pavement in front of a parked car has box edges that touch — they
#: overlap in the image — while standing several feet closer to the camera;
#: the only thing that gives them away in 2D is how much lower in frame
#: their feet are. A deliberately coarse constant, not a homography: the
#: goal is to stop foreground traffic reading as "inches from the vehicle",
#: which it plainly is not, and any factor in the 2-3 range achieves that.
GROUND_DEPTH_WEIGHT = 2.5


def ground_gap(
    subject: Box, asset: Box, depth_weight: float = GROUND_DEPTH_WEIGHT
) -> float:
    """Approximate ground-plane separation between a subject and an asset.

    Measured from where the subject's feet are to the nearest point of the
    asset's own ground line (its bottom edge), with vertical separation
    weighted up by *depth_weight* to account for perspective. Always
    non-negative: unlike :func:`box_gap` this asks "how far apart are they
    standing", a question that has no negative answer, and leaves "do their
    outlines overlap" to ``box_gap``.
    """
    fx, fy = box_foot_point(subject)
    dx = max(asset[0] - fx, fx - asset[2], 0.0)
    dy = abs(fy - asset[3]) * depth_weight
    return math.hypot(dx, dy)


def box_area(box: Box) -> float:
    """Return the area of *box*, clamped at zero for degenerate boxes."""
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def box_iou(a: Box, b: Box) -> float:
    """Return the intersection-over-union of *a* and *b* (0.0-1.0)."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if intersection <= 0.0:
        return 0.0
    # Safe to divide: a non-zero intersection means both boxes are at least
    # that large, so the union is at least the intersection.
    return intersection / (box_area(a) + box_area(b) - intersection)


def point_in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test.

    *points* is a closed ring of ``(x, y)`` vertices in the same coordinate
    space as *x*/*y* — the algorithm itself is scale-independent, so callers
    may pass either pixel or fractional coordinates as long as both sides
    agree.
    """
    inside = False
    x2, y2 = points[-1]
    for x1, y1 in points:
        if (y1 > y) != (y2 > y):
            x_intersect = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < x_intersect:
                inside = not inside
        x2, y2 = x1, y1
    return inside


def pixel_gap_to_feet(
    gap: float,
    reference_width: float,
    reference_width_feet: float = TYPICAL_VEHICLE_WIDTH_FEET,
) -> float | None:
    """Convert a pixel *gap* to approximate feet using a reference object.

    *reference_width* is the pixel width of an object of known real-world
    width (a detected vehicle, by default). Returns ``None`` when that
    width isn't usable, since without a scale reference a pixel gap carries
    no distance meaning at all — callers must say "indeterminate" rather
    than inventing a number.
    """
    if reference_width <= 0:
        return None
    return (gap / reference_width) * reference_width_feet


@dataclass(frozen=True)
class Zone:
    """A user-drawn region of a camera's field of view, in normalized coords.

    Built from the same ``car_zone`` dict the Vehicles tab persists — either
    a plain ``x_min``/``y_min``/``x_max``/``y_max`` rectangle or
    ``{"shape": "polygon", "points": [[x, y], ...]}``. ``bounds`` is always
    populated (a polygon's axis-aligned bounding box), so callers needing
    only a coarse reference never have to care which shape they were given.
    """

    bounds: Box
    points: tuple[tuple[float, float], ...] = ()

    @classmethod
    def from_config(cls, zone: dict[str, Any] | None) -> Zone | None:
        """Build a zone from a stored config dict, or ``None`` if unusable.

        An absent zone, an empty dict, or a polygon with no points all
        return ``None`` — every caller treats that as "no zone configured
        for this camera", which is the overwhelmingly common case.
        """
        if not zone:
            return None
        if zone.get("shape") == "polygon":
            raw = zone.get("points") or []
            points = tuple((float(p[0]), float(p[1])) for p in raw)
            if not points:
                return None
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return cls(bounds=(min(xs), min(ys), max(xs), max(ys)), points=points)
        return cls(
            bounds=(
                float(zone.get("x_min", 0.0)),
                float(zone.get("y_min", 0.0)),
                float(zone.get("x_max", 1.0)),
                float(zone.get("y_max", 1.0)),
            )
        )

    @property
    def is_polygon(self) -> bool:
        """True when this zone was drawn as a freeform outline."""
        return bool(self.points)

    def contains(self, x: float, y: float) -> bool:
        """True if the normalized point ``(x, y)`` falls inside this zone.

        A polygon is tested against its exact traced outline; a rectangle
        against its bounds. Both are inclusive of the boundary itself only
        as far as the underlying test is — the distinction never matters at
        the scale these zones are drawn at.
        """
        if self.points:
            return point_in_polygon(x, y, list(self.points))
        x1, y1, x2, y2 = self.bounds
        return x1 <= x <= x2 and y1 <= y <= y2

    def to_pixel_box(self, width: float, height: float) -> Box:
        """Return this zone's bounding box scaled to a *width* × *height* frame."""
        x1, y1, x2, y2 = self.bounds
        return (x1 * width, y1 * height, x2 * width, y2 * height)
