"""Frame-level motion math: thumbnails, inter-frame diffs, trajectory hints.

Everything here turns a clip's already-extracted JPEG frames into plain
numbers — a small grayscale thumbnail, how much changed between consecutive
frames, where in the frame that change sat, and the short English phrases
:mod:`blink_downloader.analyzer` folds into its prompt from them. No model
inference, no I/O, no configuration: given the same bytes it returns the
same numbers.

Split out of the analyzer for the same reason ``model_catalog.py`` and
``moondream_finetune.py`` were — it is image arithmetic that happens to be
*used* during analysis, not part of deciding what a clip means, and it is
far easier to reason about (and test) as a handful of pure functions than as
static methods buried in a five-thousand-line analyzer class. Pillow is
imported lazily inside each function, exactly as before, so importing this
module costs nothing on an install where it is unused.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .security.geometry import point_in_polygon

# Fixed-size grayscale thumbnail used for the visual scene-baseline ("smart
# brain") comparison — see scene_thumbnail(). Small enough to
# store cheaply per-camera and compare quickly; large enough to notice a
# parked vehicle, a delivered package, or similar new object in frame.
SCENE_THUMBNAIL_SIZE: tuple[int, int] = (16, 16)

# Motion-trajectory hint ("smart brain" movement reasoning) — see
# motion_trajectory_hint() and
# zone_motion_fraction(), which share one grayscale-thumbnail
# pass at this size (see _grayscale_thumbnails/_maybe_compute_motion_thumbnails)
# over the frames actually selected for analysis. _select_best_frames()'s own
# motion-diff scoring (over the larger, pre-down-selection frame pool) uses
# the same 64x64 size but is a separate computation, not sharable with these.
MOTION_TRAJECTORY_THUMB_SIZE: tuple[int, int] = (64, 64)

# Fewer than this many selected frames makes entry/peak/exit direction too
# noisy to trust — no hint is emitted below this count.
MOTION_TRAJECTORY_MIN_FRAMES: int = 3

# Per-pixel average diff magnitude (0-255 raw byte scale, same as
# _select_best_frames()'s motion scoring — NOT normalized to 0.0-1.0) below
# which there's no real motion to characterize a direction for, just JPEG
# re-encoding noise between near-identical frames.
MOTION_TRAJECTORY_DIFF_FLOOR: float = 3.0

# A lateral centroid shift of at least this fraction of the thumbnail width
# is required to call a left/right direction (between the first and last
# frame) or a reversal/pacing pattern (between any two points in the
# sequence — see _has_lateral_reversal) — smaller shifts are treated as noise.
MOTION_TRAJECTORY_LATERAL_SHIFT_FRACTION: float = 0.15

# Ratio between the second half's and first half's average diff magnitude
# required to call an intensity trend (approaching/retreating proxy).
MOTION_TRAJECTORY_INTENSITY_RATIO: float = 1.3

# Outward padding applied to a configured car zone, as a fraction of the
# zone's own width/height, before measuring what share of a clip's motion
# fell "inside" it (see zone_motion_fraction). The zone itself
# is drawn tightly around the vehicle (VehicleZonePicker's only instruction
# is "draw ... the protected vehicle's zone"), so a person standing right
# beside the car — not overlapping its own footprint — generates motion
# pixels just outside that box. Without padding, that's exactly the
# near-miss case this heuristic most needs to catch: real-world testing
# showed a clip where someone stood essentially against a parked car still
# scored near-zero zone motion, and the resulting prompt hint ("away from
# the protected vehicle's usual spot") actively pushed the model toward a
# not-suspicious verdict. This only widens the *measurement* window, not
# the zone data stored/shown in the picker UI.
ZONE_MOTION_PAD_FRACTION: float = 0.2


def scene_thumbnail(frame: bytes) -> list[float] | None:
    """Reduce a frame to a small grayscale thumbnail for scene-baseline comparison.

    Returns a flat list of pixel values normalized to 0.0-1.0, or ``None``
    if the frame can't be decoded (e.g. Pillow unavailable or a corrupt
    JPEG) — callers treat that as "scene baseline unavailable for this
    clip" rather than an error.
    """
    try:
        import io as _io

        from PIL import Image as _Image

        with _Image.open(_io.BytesIO(frame)) as img:
            gray = img.convert("L").resize(
                SCENE_THUMBNAIL_SIZE, _Image.Resampling.LANCZOS
            )
            return [p / 255.0 for p in gray.tobytes()]
    except Exception:  # noqa: BLE001
        return None


def frame_motion_diffs(
    frames: list[bytes],
    zone_box: tuple[float, float, float, float] | None = None,
) -> list[float]:
    """Per-pixel inter-frame absolute difference for each consecutive pair.

    *zone_box* (see ``BaseAnalyzer._select_best_frames``), when given, sums
    only the pixels that fall inside it rather than the whole
    thumbnail — a polygon zone must already be reduced to its
    bounding box by the caller (see ``BaseAnalyzer._car_zone_bbox``), the same
    coarse approximation used everywhere else a zone feeds a ranking/
    proximity signal rather than exact geometry.
    """
    import io as _io

    from PIL import Image as _Image

    _THUMB = (64, 64)
    # tobytes() returns raw pixel bytes (L mode = 1 byte/pixel)
    # which avoids the untyped ImagingCore returned by getdata()
    thumbs: list[bytes] = [
        _Image.open(_io.BytesIO(f))
        .convert("L")
        .resize(_THUMB, _Image.Resampling.LANCZOS)
        .tobytes()
        for f in frames
    ]

    width, height = _THUMB
    pixels = width * height
    if zone_box is None:
        return [
            sum(abs(a - b) for a, b in zip(thumbs[i - 1], thumbs[i])) / pixels
            for i in range(1, len(thumbs))
        ]

    zx1 = max(0, min(width - 1, round(zone_box[0] * width)))
    zy1 = max(0, min(height - 1, round(zone_box[1] * height)))
    zx2 = max(zx1 + 1, min(width, round(zone_box[2] * width)))
    zy2 = max(zy1 + 1, min(height, round(zone_box[3] * height)))
    diffs: list[float] = []
    for i in range(1, len(thumbs)):
        total = 0
        for idx, (a, b) in enumerate(zip(thumbs[i - 1], thumbs[i])):
            x, y = idx % width, idx // width
            if zx1 <= x < zx2 and zy1 <= y < zy2:
                total += abs(a - b)
        diffs.append(total / pixels)
    return diffs


def grayscale_thumbnails(frames: list[bytes]) -> list[bytes]:
    """Decode, grayscale, and resize each frame to
    :data:`MOTION_TRAJECTORY_THUMB_SIZE`, returning raw ``L``-mode
    pixel bytes per frame (1 byte/pixel). Shared by
    :func:`frame_diff_magnitudes_and_centroids` and
    :func:`zone_motion_fraction`, which both need this identical
    transform on the identical (already down-selected, already
    vision-enhanced) frame set — see
    ``BaseAnalyzer._maybe_compute_motion_thumbnails``, which computes this once
    per clip instead of each of those recomputing its own copy.
    """
    import io as _io

    from PIL import Image as _Image

    return [
        _Image.open(_io.BytesIO(f))
        .convert("L")
        .resize(MOTION_TRAJECTORY_THUMB_SIZE, _Image.Resampling.LANCZOS)
        .tobytes()
        for f in frames
    ]


def frame_diff_magnitudes_and_centroids(
    thumbs: list[bytes],
) -> tuple[list[float], list[float]]:
    """Per-frame-pair diff magnitude and weighted-x centroid of the diff
    mask, given precomputed grayscale thumbnails (see
    :func:`grayscale_thumbnails`)."""
    width, height = MOTION_TRAJECTORY_THUMB_SIZE
    pixels = width * height

    diff_magnitudes: list[float] = []
    centroids_x: list[float] = []
    for i in range(1, len(thumbs)):
        total = 0
        weighted_x = 0
        for idx, (pa, pb) in enumerate(zip(thumbs[i - 1], thumbs[i])):
            d = abs(pa - pb)
            if d:
                total += d
                weighted_x += d * (idx % width)
        diff_magnitudes.append(total / pixels)
        centroids_x.append((weighted_x / total) if total else -1.0)

    return diff_magnitudes, centroids_x


def _classify_lateral_shift(centroids_x: list[float], width: int) -> str | None:
    valid_centroids = [cx for cx in centroids_x if cx >= 0]
    if len(valid_centroids) < 2:
        return None
    threshold = width * MOTION_TRAJECTORY_LATERAL_SHIFT_FRACTION
    if _has_lateral_reversal(valid_centroids, threshold):
        return "moving back and forth across the frame (may be pacing)"
    first_x, last_x = valid_centroids[0], valid_centroids[-1]
    if abs(last_x - first_x) < threshold:
        return None
    return (
        "moving left to right across the frame"
        if last_x > first_x
        else "moving right to left across the frame"
    )


def _has_lateral_reversal(centroids: list[float], threshold: float) -> bool:
    """True if the centroid sequence moves past *threshold* in one
    direction and then past *threshold* back the other way — genuine
    back-and-forth motion (e.g. someone pacing while casing a
    property), not just sensor noise around a roughly stationary
    point or one smooth pass in a single direction. Requires two
    separate excursions past the threshold in opposite directions,
    each measured from where the previous one ended, not from the
    clip's very first frame — comparing only the first and last
    centroid (the previous approach) misses this pattern entirely
    whenever the sequence happens to end up back near where it
    started.
    """
    last_sign = 0
    reference = centroids[0]
    for cx in centroids[1:]:
        delta = cx - reference
        if abs(delta) < threshold:
            continue
        sign = 1 if delta > 0 else -1
        if last_sign != 0 and sign != last_sign:
            return True
        last_sign = sign
        reference = cx
    return False


def _classify_intensity_trend(diff_magnitudes: list[float]) -> str | None:
    if len(diff_magnitudes) < 2:
        return None
    midpoint = len(diff_magnitudes) // 2
    first_half = diff_magnitudes[:midpoint] or diff_magnitudes[:1]
    second_half = diff_magnitudes[midpoint:]
    avg_first = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    if avg_second > avg_first * MOTION_TRAJECTORY_INTENSITY_RATIO:
        return "movement intensity increasing over time (may be approaching)"
    if avg_first > avg_second * MOTION_TRAJECTORY_INTENSITY_RATIO:
        return "movement intensity decreasing over time (may be retreating)"
    return None


def motion_trajectory_hint(thumbs: list[bytes] | None) -> str | None:
    """Classify a coarse movement direction/intensity trend across a
    clip's frames, given their precomputed grayscale thumbnails (see
    :func:`grayscale_thumbnails`, computed once by
    ``BaseAnalyzer._maybe_compute_motion_thumbnails`` and shared with
    :func:`zone_motion_fraction` rather than each recomputing its own).

    For each consecutive frame pair, computes the diff mask's weighted
    x centroid in addition to the overall diff magnitude — a shift in
    that centroid's x position across the sequence is a coarse
    "moving across frame" signal (including reversing direction at
    least once, a pacing/casing pattern a simple first-vs-last
    comparison would miss entirely); a rising diff magnitude trend
    (the moving region occupies more pixels as it nears the camera)
    is a coarse "may be approaching" proxy. Deliberately coarse and
    conservative — returns ``None`` (no hint) far more often than
    not, since ``BaseAnalyzer._build_prompt`` frames whatever is returned as a
    rough estimate, not a precise tracked path.

    Returns ``None`` when there are too few frames, no clear motion
    signal, thumbnails weren't available, or PIL is unavailable.
    """
    if thumbs is None or len(thumbs) < MOTION_TRAJECTORY_MIN_FRAMES:
        return None

    try:
        diff_magnitudes, centroids_x = frame_diff_magnitudes_and_centroids(thumbs)
        if not diff_magnitudes or max(diff_magnitudes) < MOTION_TRAJECTORY_DIFF_FLOOR:
            return None

        width = MOTION_TRAJECTORY_THUMB_SIZE[0]
        lateral = _classify_lateral_shift(centroids_x, width)
        if lateral is not None:
            return lateral

        return _classify_intensity_trend(diff_magnitudes)
    except Exception:  # noqa: BLE001
        return None


def zone_motion_fraction(
    thumbs: list[bytes] | None, zone: dict[str, Any]
) -> float | None:
    """Return the fraction (0.0-1.0) of this clip's total pixel motion
    that fell inside *zone* — either a normalised ``x_min``/``y_min``/
    ``x_max``/``y_max`` rectangle, or a
    ``{"shape": "polygon", "points": [[x, y], ...]}`` freeform outline
    (fractional 0-1 coordinates) — e.g. a user-drawn "car zone".

    *thumbs* are precomputed grayscale thumbnails (see
    :func:`grayscale_thumbnails`, shared with
    :func:`motion_trajectory_hint` via
    ``BaseAnalyzer._maybe_compute_motion_thumbnails`` rather than each
    recomputing its own). Buckets each diff pixel's magnitude into
    "inside the zone" vs. "outside" and reports what share of the
    clip's total motion energy fell inside it. This gives
    ``BaseAnalyzer._build_prompt`` a code-computed, structured signal —
    "most of what moved was actually in the car zone" vs. "the car
    zone barely moved; the motion is background activity elsewhere" —
    instead of asking the model to judge that from raw pixels alone.

    Returns ``None`` when *thumbs* is unavailable or has fewer than 2
    frames, *zone* is empty, or the clip has too little overall
    motion to attribute meaningfully.
    """
    if thumbs is None or len(thumbs) < 2 or not zone:
        return None

    try:
        width, height = MOTION_TRAJECTORY_THUMB_SIZE
        inside = _zone_membership_test(zone, width, height)

        total_motion = 0
        zone_motion = 0
        for i in range(1, len(thumbs)):
            for idx, (pa, pb) in enumerate(zip(thumbs[i - 1], thumbs[i])):
                d = abs(pa - pb)
                if not d:
                    continue
                total_motion += d
                if inside(idx % width, idx // width):
                    zone_motion += d

        pixels_per_pair = width * height
        if total_motion < MOTION_TRAJECTORY_DIFF_FLOOR * pixels_per_pair:
            return None

        return zone_motion / total_motion
    except Exception:  # noqa: BLE001
        return None


def _zone_membership_test(
    zone: dict[str, Any], width: int, height: int
) -> Callable[[int, int], bool]:
    """Build the "is this thumbnail pixel in the zone" test for *zone*.

    Resolved once per clip rather than per pixel, and returned as a closure
    so the hot loop above has a single call rather than a shape check on
    every one of the tens of thousands of pixels it walks.
    """
    if zone.get("shape") == "polygon":
        # Already precisely traced by the user, unlike a quick rectangle
        # drag — skip the tolerance padding rects get below and test
        # membership against the exact outline.
        poly_px = [(px * width, py * height) for px, py in zone.get("points", [])]
        return lambda x, y: point_in_polygon(x + 0.5, y + 0.5, poly_px)

    raw_x_min = zone.get("x_min", 0.0)
    raw_y_min = zone.get("y_min", 0.0)
    raw_x_max = zone.get("x_max", 1.0)
    raw_y_max = zone.get("y_max", 1.0)
    pad_x = max(0.0, raw_x_max - raw_x_min) * ZONE_MOTION_PAD_FRACTION
    pad_y = max(0.0, raw_y_max - raw_y_min) * ZONE_MOTION_PAD_FRACTION

    zx1 = max(0, min(width - 1, round((raw_x_min - pad_x) * width)))
    zy1 = max(0, min(height - 1, round((raw_y_min - pad_y) * height)))
    zx2 = max(zx1 + 1, min(width, round((raw_x_max + pad_x) * width)))
    zy2 = max(zy1 + 1, min(height, round((raw_y_max + pad_y) * height)))
    return lambda x, y: zx1 <= x < zx2 and zy1 <= y < zy2
