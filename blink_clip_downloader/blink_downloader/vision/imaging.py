"""Pure image arithmetic the stages share.

Frame dimensions, cropping a box out of a frame, the colour histogram a
vehicle signature is built from, and how much a region changed between two
frames. No model inference and no I/O: given the same bytes these return
the same numbers, which is what makes them testable on their own and safe
to call from any stage.

Kept apart from any one stage because more than one uses them — the
detector, the vehicle-signature learning in :mod:`.pipeline`, and the
zone reference all do — for the same reason
``blink_downloader.frame_motion`` was lifted out of the analyzer.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ..security import Box
from . import runtime

_LOGGER = logging.getLogger(__name__)


def _frame_dimensions(frame: bytes) -> tuple[int, int] | None:
    """Decode *frame* far enough to read its ``(width, height)``.

    Everything the security layer computes — zone membership, ground
    distance, how much of the frame a subject fills — needs the frame's
    real resolution, which the detector's pixel boxes alone don't carry.
    ``None`` when the frame can't be decoded, which every caller treats as
    "no geometry available for this clip".
    """
    with runtime._native_import_lock:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

    img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    height, width = img.shape[:2]
    return (width, height)


def _crop_region(img: Any, box: Box) -> Any:
    """Clamp *box* to *img*'s bounds and return that region of it.

    Always at least one pixel wide and tall. A detector box can sit partly
    (or, after the tracker extrapolates one, entirely) outside the frame,
    and an empty crop would fail further down in ways that read as a decode
    error rather than as the bad box it actually is.
    """
    height, width = img.shape[:2]
    x1 = max(0, min(width - 1, int(box[0])))
    y1 = max(0, min(height - 1, int(box[1])))
    x2 = max(x1 + 1, min(width, int(box[2])))
    y2 = max(y1 + 1, min(height, int(box[3])))
    return img[y1:y2, x1:x2]


def _vehicle_histogram(frame: bytes, box: Box) -> tuple[float, ...]:
    """Return a coarse colour fingerprint of *box*'s contents in *frame*.

    A 16×4 hue/saturation histogram of the cropped region, area-normalized.
    Hue and saturation rather than raw RGB so the same car scores similarly
    in morning sun and under a porch light; coarse bins because the job is
    "is this the silver hatchback or the red pickup", not fine-grained
    recognition. Empty when the crop can't be produced, which callers treat
    as "no appearance evidence" rather than as a mismatch.
    """
    with runtime._native_import_lock:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

    img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return ()
    hsv = cv2.cvtColor(_crop_region(img, box), cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 4], [0, 180, 0, 256])
    return tuple(float(v) for v in (hist.flatten() / float(hist.sum())))


#: Side length the asset's region is resampled to before before/after
#: comparison. Small enough that the comparison is about structure rather
#: than sensor noise or a pixel of camera shake, large enough to notice a
#: dent-sized change in a vehicle-sized crop.
_CHANGE_PATCH = 64


def _region_appearance_change(before: bytes, after: bytes, box: Box) -> float | None:
    """How much *box*'s contents changed between two frames (0.0-1.0).

    The cheap, model-free half of "did the protected vehicle itself change
    during this clip" — a new dent, a thrown object left on the bonnet, a
    door left open. Both crops are contrast-normalized before comparison so
    that a cloud passing, a floodlight switching on, or the camera's own
    auto-exposure does not register as damage; what survives is structural
    difference in that one region.

    This is evidence, never a verdict: it says the region looks different,
    not that anything was damaged. ``None`` when either crop can't be
    produced.
    """
    with runtime._native_import_lock:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

    crops: list[Any] = []
    for data in (before, after):
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(_crop_region(img, box), cv2.COLOR_BGR2GRAY)
        try:
            resized = np.asarray(
                cv2.resize(gray, (_CHANGE_PATCH, _CHANGE_PATCH)), dtype="float32"
            )
        except Exception:  # noqa: BLE001
            # Every other stage in this module treats a failure as missing
            # evidence rather than an error, and this one is the least
            # important of them: no before/after comparison simply means the
            # impact rule has one fewer input.
            return None
        if resized.shape != (_CHANGE_PATCH, _CHANGE_PATCH):
            return None
        std = float(resized.std())
        # A region with no variance at all — a blown-out or fully black
        # crop — would divide by zero here and reach the impact rule as
        # NaN, where every threshold comparison silently evaluates False.
        # Dividing by one instead leaves it flat, which reads correctly as
        # "nothing structural changed".
        crops.append((resized - float(resized.mean())) / (std if std > 0 else 1.0))

    difference = float(np.abs(crops[0] - crops[1]).mean())
    # Two contrast-normalized, structurally unrelated crops differ by about
    # 1.1 on average, so halving maps "completely different" onto roughly
    # 0.55 and leaves headroom above it rather than saturating at 1.0.
    return min(1.0, difference / 2.0)


def _select_scan_frames(
    frames: list[bytes], cap: int, interval: float
) -> tuple[list[bytes], float]:
    """Pick an evenly-spaced subset of *frames* for the temporal scan.

    Object detection runs over this set rather than over the handful of
    frames chosen for the AI prompt, because those are picked by motion
    (entry, peak, exit) and are deliberately *not* evenly spaced — which
    makes "frame index × interval" the wrong clip time for them, and every
    duration, speed and trajectory derived from it wrong too. An even
    subset keeps the arithmetic honest at a bounded cost: *cap* frames of
    detection, whatever the clip's length.

    A *cap* of zero means the caller has already narrowed the pool to the
    prompt's own frames (``ai_temporal_scan_frames: 0``, documented as
    disabling the wider scan) — there is nothing left to subsample, so the
    list is returned as given.

    Returns the frames alongside the real seconds between them.
    """
    if cap <= 0 or len(frames) <= cap:
        return frames, interval
    step = math.ceil(len(frames) / cap)
    return frames[::step], interval * step
