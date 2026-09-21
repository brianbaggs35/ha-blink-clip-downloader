"""Stage 2: object detection and tracking (Ultralytics YOLO + ByteTrack).

The stage that turns pixels into the boxes everything downstream reasons
about — and the one whose output the structured security layer, the clip
modal's object chips and the prompt's detected-classes line all read. It
also renders the two hints those boxes support: what was detected and
where it sat relative to the protected vehicle, and how subjects moved
across the sampled frames.

Read ``_MIN_DETECTION_CONFIDENCE``'s comment before touching thresholds
here; the interaction between ultralytics' own 0.1 default and ByteTrack's
second association stage is not what it looks like.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from ..security import (
    SUBJECT_LABELS,
    VEHICLE_LABELS,
    Box,
    Zone,
    box_gap,
)
from ..security.geometry import pixel_gap_to_feet
from . import imaging, runtime

_LOGGER = logging.getLogger(__name__)


# COCO class names (Ultralytics' default label set) this pipeline surfaces
# in prompts. Anything else YOLO detects (furniture, traffic lights, sports
# balls, ...) isn't relevant to a home-security judgment and is filtered
# out to keep the OBJECT DETECTION hint short and focused.
# The label vocabulary itself lives in security/tracks.py, which is where
# every rule that reasons about people, animals and vehicles reads it from —
# one definition, not two that can drift apart. This module adds only the
# carryable classes and the bicycle, which the detector surfaces for the
# prompt but which no security rule treats as a subject or a vehicle.
#: Confidence a detection must clear to be kept at all, unless the tracker
#: itself vouched for it with a track id.
#:
#: Ultralytics' ``model.track()`` defaults ``conf`` to 0.1 — deliberately,
#: and not as a detection threshold: ByteTrack's second association stage
#: wants weak boxes so it can re-attach them to tracks it already trusts.
#: It is overridable (``kwargs["conf"] = 0.1 if kwargs.get("conf") is None``
#: in Model.track, ultralytics 8.4.155), and is deliberately left alone:
#: raising it would deny the tracker exactly the weak boxes the escape
#: hatch below exists to convert back into recall. Filtering after the
#: fact, on whether the tracker vouched for a box, keeps both. ByteTrack
#: never *starts* a track from one (``new_track_thresh: 0.25`` in
#: bytetrack.yaml), so a sub-0.25 box with no id is a box ultralytics itself
#: would not have believed.
#:
#: Those boxes reach us in bulk, because ultralytics skips filtering results
#: entirely on any frame where the tracker returns no activated track
#: ("if len(tracks) == 0: continue" in trackers/track.py) — which, on frames
#: sampled seconds apart, is most of them: ByteTrack auto-activates a new
#: track only on its first frame, and a subject that has moved cannot be
#: re-matched by IoU. The raw 0.1-threshold predictions are then left in
#: place with no ids at all. Measured on a synthetic clip of one person
#: crossing an empty backdrop: spurious "person" boxes at 0.13-0.22 and a
#: phantom "car" at 0.12, on frames whose backdrop alone detects nothing.
#:
#: Storing those cost real accuracy: the clip modal counted them ("2 dogs"
#: for one dog), they entered the prompt's detected-classes line, and a
#: single junk "person" was enough to suppress the no-subject grounding in
#: _no_subject_sentence.
#:
#: A box the tracker *did* id is kept whatever its score: that one has been
#: matched to an established track, which is ByteTrack vouching for it, and
#: is exactly the recall the low threshold exists to buy.
_MIN_DETECTION_CONFIDENCE = 0.25


_VEHICLE_CLASSES = VEHICLE_LABELS


_SUBJECT_CLASSES = SUBJECT_LABELS


_RELEVANT_CLASSES = (
    SUBJECT_LABELS
    | VEHICLE_LABELS
    | frozenset({"bicycle", "backpack", "handbag", "suitcase"})
)


@dataclass
class DetectedObject:
    """One detected object in one analyzed frame."""

    label: str
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixel coords
    track_id: int | None
    frame_index: int


class ObjectDetector:
    """YOLO object detection + ByteTrack tracking (Ultralytics).

    The model is loaded once (and downloaded on first use, cached under
    /data — see ``runtime._YOLO_MODEL_CACHE_DIR`` above) and reused across clips —
    reloading it per clip would make every analysis pay a multi-second
    cold-start cost. Inference runs in a thread executor so the asyncio
    event loop is never blocked, mirroring ``MoondreamLocalAnalyzer``'s
    pattern in ``analyzer/moondream_provider.py`` for the same reason.

    Note on tracking: Ultralytics' ``.track(persist=True)`` is designed for
    continuous video frames. This pipeline only ever hands it the handful
    of frames already sampled for AI analysis (seconds apart, not
    consecutive video frames), so track IDs here are best-effort — useful
    for noticing "the same object across two sampled frames" but not a
    substitute for true continuous tracking.
    """

    def __init__(self, model_name: str = "yolo26n.pt") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not runtime.torch_cpu_compatible():
            raise runtime.CPUIncompatibleError(runtime._CPU_INCOMPATIBLE_MESSAGE)
        # Must be set before the ultralytics import below: it probes
        # ~/.config/Ultralytics for a writable settings dir as a side effect
        # of import (not lazily), and this container's $HOME isn't writable
        # by the add-on's runtime user. Finding it unwritable, ultralytics
        # falls back to /tmp on its own — but does so by printing raw,
        # unformatted lines straight to stdout (bypassing our logging setup
        # entirely, unlabeled and timestamp-free among otherwise-structured
        # log output) and using an ephemeral directory that doesn't survive
        # a container recreation. Pointing it at the same persistent,
        # known-writable directory the model weights already cache under
        # avoids both problems in one step: no fallback needed, so no
        # notice, and the settings file persists like the weights do.
        os.environ.setdefault("YOLO_CONFIG_DIR", runtime._YOLO_MODEL_CACHE_DIR)
        # The whole body below (not just the import) runs under the shared
        # lock: this method only ever executes once per process (ensure_ready
        # already guards repeat calls), so the only cost is a one-time wait
        # if another stage's first load is in flight at the same moment -
        # see runtime._native_import_lock's own comment for why that's necessary.
        with runtime._native_import_lock:
            from ultralytics import YOLO  # type: ignore[import-not-found]

            # A bare filename (the default "yolo11n.pt", or any custom
            # ai_object_detection_model naming a standard pretrained
            # checkpoint) downloads into YOLO()'s cwd if given as-is;
            # resolving it against a persistent absolute directory first
            # makes that download (and every reload after a container
            # recreation) reuse the same file instead of silently
            # re-fetching it. A caller-supplied path that already has a
            # directory component is left untouched.
            model_path = self._model_name
            if os.path.basename(model_path) == model_path:
                os.makedirs(runtime._YOLO_MODEL_CACHE_DIR, exist_ok=True)
                model_path = os.path.join(runtime._YOLO_MODEL_CACHE_DIR, model_path)
            _LOGGER.info("Loading YOLO object-detection model '%s'", self._model_name)
            self._model = YOLO(model_path)
            _LOGGER.info("YOLO model '%s' ready", self._model_name)

    async def ensure_ready(self) -> bool:
        """Ensure the model is loaded. Returns True when ready."""
        if self._model is not None:
            return True
        async with self._get_lock():
            if self._model is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                _LOGGER.warning(
                    "ultralytics package is not installed, object detection "
                    "unavailable: %s. Install it with: pip install ultralytics",
                    exc,
                )
                return False
            except runtime.CPUIncompatibleError as exc:
                _LOGGER.warning("Object detection unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load YOLO model")
                return False

    def _detect_in_frame(
        self, img: Any, idx: int, persist: bool
    ) -> list[DetectedObject]:
        results = self._model.track(
            img, persist=persist, tracker="bytetrack.yaml", verbose=False
        )
        if not results:
            return []
        result = results[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        names = result.names
        ids = boxes.id
        detections: list[DetectedObject] = []
        for i in range(len(boxes)):
            label = names.get(int(boxes.cls[i]), "")
            if label not in _RELEVANT_CLASSES:
                continue
            track_id = int(ids[i]) if ids is not None else None
            confidence = float(boxes.conf[i])
            # See _MIN_DETECTION_CONFIDENCE: an un-tracked box under the
            # threshold is one the tracker itself would not have opened a
            # track for, and on most frames these arrive unfiltered.
            if track_id is None and confidence < _MIN_DETECTION_CONFIDENCE:
                continue
            x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i])
            detections.append(
                DetectedObject(
                    label=label,
                    confidence=confidence,
                    box=(x1, y1, x2, y2),
                    track_id=track_id,
                    frame_index=idx,
                )
            )
        return detections

    def _detect_sync(self, frames: list[bytes]) -> list[DetectedObject]:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

        detections: list[DetectedObject] = []
        # persist=False on the first successfully-decoded frame of *this*
        # call forces Ultralytics to build fresh ByteTrack state
        # (ultralytics.trackers.track.on_predict_start: `if
        # hasattr(predictor, "trackers") and persist: return` — a call with
        # persist=False falls through and rebuilds the tracker even when
        # one already exists) instead of silently continuing whatever
        # tracker state this shared, long-lived model instance was left in
        # by the previous clip it analyzed (see this class's own
        # docstring: the model is reused across clips, potentially from a
        # completely different camera or hours/days apart). Without this,
        # track IDs — and therefore _build_tracking_hint's "same person
        # lingers across most sampled frames" signal — could spuriously
        # continue across an unrelated clip boundary. persist=True for
        # every later frame in *this* call preserves the intended
        # continuity across this one clip's own sampled frames.
        persist = False
        for idx, frame in enumerate(frames):
            arr = np.frombuffer(frame, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            detections.extend(self._detect_in_frame(img, idx, persist))
            persist = True
        return detections

    async def detect(self, frames: list[bytes]) -> list[DetectedObject] | None:
        """Detect+track relevant objects across *frames*. None if unavailable."""
        if not frames:
            return None
        async with runtime._cv_slot():
            if not await self.ensure_ready():
                return None
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self._detect_sync, frames)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Object detection failed: %s", exc)
                return None


# Re-exported under this module's historical private name: the geometry
# itself now lives in security/geometry.py, shared with every security rule
# that measures the same distances.
_box_gap = box_gap


def _proximity_label(gap: float, vehicle_width: float) -> str:
    """Categorize a box gap (see :func:`_box_gap`) relative to vehicle width.

    Converts the pixel gap to an approximate feet estimate using the
    detected vehicle's own pixel width as scale, with tier language aligned
    to _car_protection_segment's own 1 ft / 3 ft behavioral thresholds so
    the two reinforce each other. Falls back to a qualitative-only label
    when vehicle_width isn't usable (a malformed/degenerate box — real YOLO
    detections always have positive width, so this is defensive only).
    """
    if gap <= 0:
        return "overlapping the detected vehicle's outline"
    feet = pixel_gap_to_feet(gap, vehicle_width)
    if feet is None:
        return "at an indeterminate distance from the detected vehicle"
    if feet < 1.0:
        return "well under 1 ft from the detected vehicle"
    if feet < 3.0:
        return f"approximately {feet:.0f} ft from the detected vehicle"
    return f"well away from the detected vehicle (roughly {feet:.0f} ft or more)"


@dataclass(frozen=True)
class _ZoneReference:
    """A camera's configured car zone, resolved against a real frame size.

    Carries the zone itself rather than only its pixel bounds so the two
    questions it gets asked stay separate: *is this vehicle in the zone*
    is answered against the traced outline (:meth:`covers`), while the
    coarse "how far from roughly there" reference the distance hint falls
    back to keeps using :attr:`box`, where bounding-box precision is all
    the estimate claims anyway.
    """

    zone: Zone
    size: tuple[float, float]
    box: Box

    def covers(self, box: Box) -> bool:
        """True when *box* (pixel space) genuinely overlaps the drawn zone."""
        return self.zone.overlap_with_box(box, *self.size) > 0.0


def _car_zone_reference(zone: dict[str, Any], frame: bytes) -> _ZoneReference | None:
    """Resolve a normalized (0-1) ``car_zone`` dict — a rectangle or a
    freeform polygon, see ``media_server.py``'s ``_normalize_car_zone`` —
    against *frame*'s actual resolution, for disambiguating which YOLO
    vehicle detection is the protected car (see
    :func:`_best_subject_vehicle_pair`). None if *frame* fails to decode or
    the zone has no usable points.
    """
    parsed = Zone.from_config(zone)
    if parsed is None:
        return None
    size = imaging._frame_dimensions(frame)
    if size is None:
        return None
    return _ZoneReference(zone=parsed, size=size, box=parsed.to_pixel_box(*size))


def _zone_preferred_vehicles(
    vehicles: list[DetectedObject], zone_ref: _ZoneReference | None
) -> list[DetectedObject]:
    """Narrow one frame's vehicles to the one the drawn zone points at.

    Vehicles actually inside the zone win outright; only when none is does
    proximity to its bounds decide, which is all there was to go on before
    the outline was honoured exactly. For a rectangle zone the two rules
    pick the same vehicle in every case — an overlapping box always has a
    smaller (negative) gap than a separated one. A frame with no zone, or
    with nothing to disambiguate, is handed back untouched.
    """
    if zone_ref is None or len(vehicles) <= 1:
        return vehicles
    inside = [v for v in vehicles if zone_ref.covers(v.box)]
    pool = inside or vehicles
    return [min(pool, key=lambda v: _box_gap(v.box, zone_ref.box))]


def _best_subject_vehicle_pair(
    detections: list[DetectedObject],
    zone_ref: _ZoneReference | None = None,
) -> tuple[DetectedObject, DetectedObject, int] | None:
    """Return the (subject, vehicle, frame_index) pair with the smallest gap.

    "Subject" is a person *or* an animal (see :data:`_SUBJECT_CLASSES`) — a
    dog jumping on a parked car or a cat pawing at one is exactly the kind
    of vehicle contact this pipeline needs to catch, not just a person's.
    Used to pick which single frame/box-pair the heavier depth and contact
    stages spend their budget refining. None if no sampled frame contains
    both a subject and a vehicle detection.

    *zone_ref* (see :func:`_car_zone_reference`), when given, disambiguates
    which detected vehicle is actually "the protected car" whenever a frame
    has more than one vehicle-class detection (a driveway camera that also
    sees the street, a second household vehicle, a neighbor's parked car,
    ...): only the one nearest the configured zone is considered as a
    candidate for that frame. Without this, a person standing next to an
    unrelated car could "win" the smallest-gap pairing below just because
    they're closer to it than anyone is to the actual protected vehicle,
    producing a depth/contact/proximity hint about the wrong car entirely.
    A single vehicle detection in a frame is left alone either way — there
    is nothing to disambiguate.
    """
    best: tuple[DetectedObject, DetectedObject, int] | None = None
    best_gap: float | None = None
    by_frame: dict[int, list[DetectedObject]] = {}
    for d in detections:
        by_frame.setdefault(d.frame_index, []).append(d)
    for frame_idx, items in by_frame.items():
        subjects = [d for d in items if d.label in _SUBJECT_CLASSES]
        vehicles = _zone_preferred_vehicles(
            [d for d in items if d.label in _VEHICLE_CLASSES], zone_ref
        )
        for s in subjects:
            for v in vehicles:
                gap = _box_gap(s.box, v.box)
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best = (s, v, frame_idx)
    return best


def _detection_distance_pair(
    detections: list[DetectedObject],
    zone_ref: _ZoneReference | None,
    asset_box: Box | None,
    car_description: str,
) -> tuple[DetectedObject, Box] | None:
    """Pick the subject and vehicle box the distance estimate should use."""
    if not car_description:
        return None
    if asset_box is not None:
        subjects = [d for d in detections if d.label in _SUBJECT_CLASSES]
        if not subjects:
            return None
        return (min(subjects, key=lambda d: _box_gap(d.box, asset_box)), asset_box)
    legacy = _best_subject_vehicle_pair(detections, zone_ref)
    return (legacy[0], legacy[1].box) if legacy is not None else None


def _no_subject_sentence(detections: list[DetectedObject]) -> str:
    """State plainly that no person or animal was found, when none was.

    The detector sweeps every sampled frame, so "not one person in any of
    them" is the strongest single fact this pipeline produces about a clip
    — and it used to be the one fact the prompt never carried. Listing the
    classes that *were* found says nothing about the class that wasn't, and
    a small model handed several thousand words of person-centric rules
    plus a couple of frames would narrate a person into an empty driveway.
    The security layer already records this same observation as an evidence
    note, but that note rides along with the SECURITY EVIDENCE section,
    which is suppressed precisely when there are no events to report — so
    in the no-subject case it reached nothing.

    Deliberately evidence, not a verdict, and deliberately overridable: a
    person who is distant, partly hidden, or small in frame is exactly what
    a nano-scale detector misses, and a clip where that happens must still
    be describable as what it is. It never speaks to ``suspicious`` either
    — a vehicle can damage another vehicle with nobody present at all.
    Returns "" when a subject *was* detected, so the caller can drop it.
    """
    if any(d.label in _SUBJECT_CLASSES for d in detections):
        return ""
    return (
        "No person and no animal was detected in any sampled frame of this "
        "clip. Do not describe a person, or anyone's actions, unless you can "
        "plainly see one in these frames yourself — if you can, describe them "
        "and disregard this line, since a distant, small or partly hidden "
        "person can be missed."
    )


def _build_detection_hint(
    detections: list[DetectedObject],
    car_description: str,
    zone_ref: _ZoneReference | None = None,
    asset_box: Box | None = None,
) -> str | None:
    """Render detections into an OBJECT DETECTION prompt hint, or None if empty.

    *car_description* being empty means this camera isn't under
    protected-vehicle rules (see :meth:`VisionPipeline.process_clip`) — the
    detected-classes line is still useful generically, but the
    vehicle-distance estimate is skipped since there's no protected vehicle
    for it to be relevant to on this camera.

    *asset_box*, when the security layer has identified the protected
    vehicle, is the box this hint measures against — so the distance it
    states is a distance to *your* car, not to whichever vehicle a subject
    happened to stand nearest. *zone_ref* is the fallback used when no
    identification was made, matching the behaviour before that existed.

    An empty *detections* still produces a hint, rather than None: "the
    detector swept this clip and found nothing" is evidence, and saying
    nothing at all is what let a model narrate a subject into an empty
    driveway (see :func:`_no_subject_sentence`).
    """
    labels = sorted({d.label for d in detections})
    lines = [
        f"Detected object classes across sampled frames: {', '.join(labels)}."
        if labels
        else "No objects of any tracked class were detected in any sampled frame."
    ]
    lines.append(_no_subject_sentence(detections))
    lines = [line for line in lines if line]

    pair = _detection_distance_pair(detections, zone_ref, asset_box, car_description)
    if pair is not None:
        subject, vehicle_box = pair
        vehicle_width = vehicle_box[2] - vehicle_box[0]
        gap = _box_gap(subject.box, vehicle_box)
        proximity = _proximity_label(gap, vehicle_width)
        lines.append(
            f"Object-detection distance estimate: the detected {subject.label}'s "
            f"bounding box is {proximity} (pixel-based estimate from the "
            "object detector, not a physical measurement)."
        )

    return (
        "\n\nOBJECT DETECTION: "
        + " ".join(lines)
        + " Treat this as a precise, code-computed hint about what was "
        "actually detected and roughly where — cross-check it against what "
        "you can see in the frames yourself, since detector misses or "
        "false positives are possible."
    )


# Fraction of sampled frames a tracked person must appear in to count as
# "lingering" vs. "briefly passing through" — see _build_tracking_hint.
# Deliberately asymmetric (lingering needs a clear majority; passing-through
# only needs a small minority) so an ambiguous middle ground emits no hint
# at all rather than a low-confidence guess either way.
_TRACKING_LINGER_FRACTION = 0.6


_TRACKING_BRIEF_FRACTION = 0.3


# Below this many sampled frames, "how many frames did this track appear
# in" is too noisy a sample to characterize lingering vs. passing through.
_TRACKING_MIN_FRAMES = 3


def _build_tracking_hint(
    detections: list[DetectedObject], total_frames: int
) -> str | None:
    """Render ByteTrack continuity into a TRACKING prompt hint, or None.

    A single frame saying "person detected" says nothing about behavior
    over time — the actual value ByteTrack adds (see ObjectDetector) is
    knowing whether the *same* tracked person shows up across most of the
    sampled frames (lingering/casing) or just one or two (passing through).
    Uses whichever tracked person has the highest frame-presence fraction;
    ties and untracked detections (track_id is None, e.g. tracking wasn't
    available for this call) are simply not counted.
    """
    if total_frames < _TRACKING_MIN_FRAMES:
        return None

    frames_per_track: dict[int, set[int]] = {}
    for d in detections:
        if d.label != "person" or d.track_id is None:
            continue
        frames_per_track.setdefault(d.track_id, set()).add(d.frame_index)

    if not frames_per_track:
        return None

    best_track_id = max(frames_per_track, key=lambda t: len(frames_per_track[t]))
    frame_count = len(frames_per_track[best_track_id])
    fraction = frame_count / total_frames

    if fraction >= _TRACKING_LINGER_FRACTION:
        body = (
            f"the same tracked person appears in {frame_count} of "
            f"{total_frames} sampled frames spanning this clip — consistent "
            "with lingering or casing rather than simply passing through"
        )
    elif fraction <= _TRACKING_BRIEF_FRACTION:
        body = (
            f"the same tracked person appears in only {frame_count} of "
            f"{total_frames} sampled frames — consistent with briefly "
            "passing through rather than lingering"
        )
    else:
        return None

    return (
        "\n\nTRACKING: Across the sampled frames, " + body + ". This is a "
        "best-effort signal from tracking sparse sampled frames (seconds "
        "apart), not continuous video, so treat it as a hint rather than a "
        "precise measurement of how long anyone was actually present."
    )
