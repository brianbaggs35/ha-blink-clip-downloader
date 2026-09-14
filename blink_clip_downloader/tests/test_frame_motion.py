"""Tests for blink_downloader.frame_motion.

These moved here wholesale when the frame-motion math was lifted out of
``analyzer.py`` — the assertions are unchanged, only the names they call.
"""

from __future__ import annotations

import pytest

from blink_downloader import frame_motion


def _real_jpeg(shade: int = 100, width: int = 64) -> bytes:
    """Build a genuinely decodable solid-color JPEG for PIL-dependent tests."""
    import io

    from PIL import Image

    img = Image.new("RGB", (width, 64), color=(shade, shade, shade))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _real_jpeg_with_bar(
    bar_x: int,
    width: int = 64,
    height: int = 64,
    bg: int = 20,
    fg: int = 220,
    bar_width: int = 10,
) -> bytes:
    """Build a decodable JPEG: a bright vertical bar on a dark background.

    Used to construct frame sequences with a controllable, real motion
    signal for :func:`frame_motion.motion_trajectory_hint` tests — moving
    ``bar_x`` across frames simulates lateral movement, and varying ``fg``
    at a fixed ``bar_x`` simulates a growing/shrinking motion intensity
    trend without a lateral shift.
    """
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=(bg, bg, bg))
    draw = ImageDraw.Draw(img)
    x0 = max(0, bar_x)
    x1 = min(width - 1, bar_x + bar_width)
    draw.rectangle([x0, 0, x1, height - 1], fill=(fg, fg, fg))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _thumbs(frames: list[bytes]) -> list[bytes]:
    """Precompute grayscale thumbnails the way _maybe_compute_motion_thumbnails
    does in production — motion_trajectory_hint/zone_motion_fraction take
    precomputed thumbnails rather than raw frame bytes."""
    return frame_motion.grayscale_thumbnails(frames)


def test_classify_lateral_shift_none_with_fewer_than_two_valid_centroids() -> None:
    assert frame_motion._classify_lateral_shift([-1.0, 5.0, -1.0], width=100) is None


def test_classify_intensity_trend_none_with_fewer_than_two_magnitudes() -> None:
    assert frame_motion._classify_intensity_trend([1.0]) is None


def test_classify_intensity_trend_none_when_roughly_steady() -> None:
    """Neither half is meaningfully louder than the other — no trend."""
    assert frame_motion._classify_intensity_trend([10.0, 10.0, 10.5, 10.0]) is None


def test_frame_motion_diffs_zone_box_restricts_to_zone() -> None:
    """A zone covering only the destination side of the bar's motion
    captures less total diff than the unrestricted whole-frame score —
    confirms the zone actually restricts which pixels count, not just
    threads the parameter through unused."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    full_diffs = frame_motion.frame_motion_diffs(frames)
    zone_diffs = frame_motion.frame_motion_diffs(frames, (38 / 64, 0.0, 1.0, 1.0))
    assert 0 < zone_diffs[0] < full_diffs[0]


def test_frame_motion_diffs_zone_box_none_matches_default() -> None:
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    assert frame_motion.frame_motion_diffs(frames) == frame_motion.frame_motion_diffs(
        frames, None
    )


def test_scene_thumbnail_returns_normalized_pixel_values() -> None:
    thumb = frame_motion.scene_thumbnail(_real_jpeg(128, width=200))
    assert thumb is not None
    assert len(thumb) == 16 * 16
    assert all(0.0 <= v <= 1.0 for v in thumb)
    assert all(abs(v - 128 / 255) < 0.05 for v in thumb)


def test_scene_thumbnail_returns_none_for_invalid_data() -> None:
    assert frame_motion.scene_thumbnail(b"not a real jpeg") is None


def test_scene_thumbnail_returns_none_for_empty_bytes() -> None:
    assert frame_motion.scene_thumbnail(b"") is None


# ---------------------------------------------------------------------------
# attach_database / analyze_clip integration
# ---------------------------------------------------------------------------


def test_motion_trajectory_hint_insufficient_frames() -> None:
    frames = [_real_jpeg(100), _real_jpeg(100)]
    assert frame_motion.motion_trajectory_hint(_thumbs(frames)) is None


def test_motion_trajectory_hint_none_thumbs() -> None:
    assert frame_motion.motion_trajectory_hint(None) is None


def test_motion_trajectory_hint_no_motion() -> None:
    frames = [_real_jpeg(100)] * 4
    assert frame_motion.motion_trajectory_hint(_thumbs(frames)) is None


def test_motion_trajectory_hint_left_to_right() -> None:
    frames = [
        _real_jpeg_with_bar(2),
        _real_jpeg_with_bar(18),
        _real_jpeg_with_bar(34),
        _real_jpeg_with_bar(50),
    ]
    hint = frame_motion.motion_trajectory_hint(_thumbs(frames))
    assert hint == "moving left to right across the frame"


def test_motion_trajectory_hint_right_to_left() -> None:
    frames = [
        _real_jpeg_with_bar(50),
        _real_jpeg_with_bar(34),
        _real_jpeg_with_bar(18),
        _real_jpeg_with_bar(2),
    ]
    hint = frame_motion.motion_trajectory_hint(_thumbs(frames))
    assert hint == "moving right to left across the frame"


def test_motion_trajectory_hint_oscillating() -> None:
    """A bar that moves right across several steps, then reverses back
    left in one big jump, is a distinct pacing/casing signal a simple
    first-vs-last centroid comparison would miss: each step's centroid is
    the *midpoint* of that step's travel, not the bar's raw position, so
    the sequence must actually rise then fall to be detected as a
    reversal (a naive there-and-back-through-identical-positions test
    produces identical midpoints for every leg and no signal at all —
    this uses different waypoints on the way out vs. the way back so the
    midpoint sequence itself reverses)."""
    frames = [
        _real_jpeg_with_bar(2),
        _real_jpeg_with_bar(18),
        _real_jpeg_with_bar(34),
        _real_jpeg_with_bar(50),
        _real_jpeg_with_bar(2),
    ]
    hint = frame_motion.motion_trajectory_hint(_thumbs(frames))
    assert hint == "moving back and forth across the frame (may be pacing)"


def test_motion_trajectory_hint_intensity_increasing() -> None:
    frames = [
        _real_jpeg_with_bar(27, fg=60),
        _real_jpeg_with_bar(27, fg=100),
        _real_jpeg_with_bar(27, fg=160),
        _real_jpeg_with_bar(27, fg=230),
    ]
    hint = frame_motion.motion_trajectory_hint(_thumbs(frames))
    assert hint == "movement intensity increasing over time (may be approaching)"


def test_motion_trajectory_hint_intensity_decreasing() -> None:
    frames = [
        _real_jpeg_with_bar(27, fg=230),
        _real_jpeg_with_bar(27, fg=160),
        _real_jpeg_with_bar(27, fg=100),
        _real_jpeg_with_bar(27, fg=60),
    ]
    hint = frame_motion.motion_trajectory_hint(_thumbs(frames))
    assert hint == "movement intensity decreasing over time (may be retreating)"


def test_motion_trajectory_hint_returns_none_on_processing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A processing error after thumbnails are already in hand (e.g. a
    corrupted thumbnail list) returns None rather than raising - the
    try/except inside _compute_motion_trajectory_hint stays as a
    defensive safety net even though the PIL decode itself now happens
    earlier, in _grayscale_thumbnails."""
    monkeypatch.setattr(
        frame_motion,
        "frame_diff_magnitudes_and_centroids",
        lambda thumbs: (_ for _ in ()).throw(ValueError("boom")),
    )
    frames = [_real_jpeg(100)] * 4
    assert frame_motion.motion_trajectory_hint(_thumbs(frames)) is None


def test_grayscale_thumbnails_returns_thumbnail_per_frame() -> None:
    frames = [_real_jpeg(100), _real_jpeg_with_bar(20)]
    thumbs = frame_motion.grayscale_thumbnails(frames)
    assert len(thumbs) == 2
    width, height = 64, 64
    assert all(len(t) == width * height for t in thumbs)


def test_zone_motion_fraction_concentrated_in_zone() -> None:
    """A zone covering the whole frame captures ~100% of the clip's motion."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction == pytest.approx(1.0, abs=0.02)


def test_zone_motion_fraction_outside_zone() -> None:
    """A zone that never overlaps either bar position captures ~0% of motion."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 20 / 64, "y_min": 0.0, "x_max": 40 / 64, "y_max": 1.0}
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction == pytest.approx(0.0, abs=0.02)


def test_zone_motion_fraction_partial_overlap() -> None:
    """A zone covering only the destination bar position captures roughly
    half the motion — the leading and trailing edges are similar in size."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 44 / 64, "y_min": 0.0, "x_max": 60 / 64, "y_max": 1.0}
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction is not None
    assert 0.3 < fraction < 0.7


def test_zone_motion_fraction_none_thumbs() -> None:
    assert frame_motion.zone_motion_fraction(None, {"x_min": 0}) is None


def test_zone_motion_fraction_insufficient_frames() -> None:
    assert (
        frame_motion.zone_motion_fraction(_thumbs([_real_jpeg(100)]), {"x_min": 0})
        is None
    )


def test_zone_motion_fraction_empty_zone() -> None:
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    assert frame_motion.zone_motion_fraction(_thumbs(frames), {}) is None


def test_zone_motion_fraction_no_motion() -> None:
    frames = [_real_jpeg(100)] * 3
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    assert frame_motion.zone_motion_fraction(_thumbs(frames), zone) is None


def test_zone_motion_fraction_returns_none_on_processing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A processing error after thumbnails are already in hand returns
    None rather than raising - the try/except inside
    _zone_motion_fraction stays as a defensive safety net even though the
    PIL decode itself now happens earlier, in _grayscale_thumbnails."""
    monkeypatch.setattr(
        frame_motion,
        "point_in_polygon",
        lambda x, y, points: (_ for _ in ()).throw(ValueError("boom")),
    )
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"shape": "polygon", "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]}
    assert frame_motion.zone_motion_fraction(_thumbs(frames), zone) is None


def test_zone_motion_fraction_rect_zone_with_no_shape_key_still_works() -> None:
    """Zones saved before the polygon feature existed have no `shape` key —
    must still be treated as a rectangle, not silently misread."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    assert "shape" not in zone
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction == pytest.approx(1.0, abs=0.02)


def test_zone_motion_fraction_polygon_covering_whole_frame() -> None:
    """A polygon covering the whole frame captures ~100% of the motion,
    same as the equivalent whole-frame rectangle."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {
        "shape": "polygon",
        "points": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
    }
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction == pytest.approx(1.0, abs=0.02)


def test_zone_motion_fraction_polygon_outside_motion() -> None:
    """A polygon that never overlaps either bar position captures ~0%,
    same as the equivalent rectangle."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {
        "shape": "polygon",
        "points": [
            [20 / 64, 0.0],
            [40 / 64, 0.0],
            [40 / 64, 1.0],
            [20 / 64, 1.0],
        ],
    }
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction == pytest.approx(0.0, abs=0.02)


def test_zone_motion_fraction_polygon_triangle_partial_overlap() -> None:
    """A genuinely non-rectangular (triangular) zone still produces a
    sane, bounded fraction — confirms the point-in-polygon path isn't just
    exercised on axis-aligned squares standing in for rectangles."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {
        "shape": "polygon",
        "points": [[40 / 64, 0.0], [60 / 64, 0.0], [50 / 64, 1.0]],
    }
    fraction = frame_motion.zone_motion_fraction(_thumbs(frames), zone)
    assert fraction is not None
    assert 0.0 <= fraction <= 1.0


def test_zone_motion_fraction_polygon_empty_points_returns_none() -> None:
    """An empty points list should never actually reach here in practice —
    _normalize_car_zone requires >= 3 points — but must fail safely (via
    the broad except) rather than crash the whole analysis."""
    frames = [_real_jpeg_with_bar(5), _real_jpeg_with_bar(45)]
    zone = {"shape": "polygon", "points": []}
    assert frame_motion.zone_motion_fraction(_thumbs(frames), zone) is None


# ---------------------------------------------------------------------------
# _point_in_polygon — ray-casting test used by _zone_motion_fraction's
# polygon path
# ---------------------------------------------------------------------------
