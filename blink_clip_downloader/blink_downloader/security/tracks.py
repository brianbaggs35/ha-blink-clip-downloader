"""Temporal aggregation of per-frame detections into object tracks.

The object detector (see ``vision.ObjectDetector``) already produces a flat
list of per-frame boxes carrying a ByteTrack id. On its own that answers
"what was in frame", but every question a security system actually cares
about is temporal: *how long* was someone there, were they *getting closer*,
did they *leave* after touching something. This module turns the flat list
into :class:`ObjectTrack` objects that can answer those, with **no extra
model inference** — it is pure post-processing of data already computed.

Two honesty constraints run through everything here:

* Frames are sampled seconds apart, not continuously, so every duration is
  an *observed span* — a lower bound on real presence, never a measurement.
* A pixel gap only becomes a distance when something of known real-world
  size is in frame to scale it (see :func:`.geometry.pixel_gap_to_feet`).
  Where no such reference exists, distances stay in pixels and are labelled
  as such rather than dressed up as feet.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from .geometry import (
    Box,
    Zone,
    box_area,
    box_center,
    box_foot_point,
    box_gap,
    box_iou,
    ground_gap,
)

#: COCO label of the class everything else is calibrated around.
PERSON_LABEL = "person"

#: Classes eligible as the "subject" of a security event — not just people.
#: A dog jumping on a parked car (scratches) or a cat pawing at one is
#: exactly the kind of non-person interaction the base analysis prompt
#: already flags, so the whole security layer treats animals as candidate
#: subjects too.
SUBJECT_LABELS: frozenset[str] = frozenset(
    {PERSON_LABEL, "dog", "cat", "bird", "horse"}
)

#: Classes that can be a protected vehicle.
VEHICLE_LABELS: frozenset[str] = frozenset({"car", "truck", "bus", "motorcycle"})

#: Classes a person can pick up and walk away with — the detectable proxy
#: for package theft (see ``SecurityEventType.OBJECT_REMOVED``). COCO has no
#: "parcel"/"box" class, so a delivered package most often lands in one of
#: these; treat a match as a candidate, never a certainty.
CARRYABLE_LABELS: frozenset[str] = frozenset({"backpack", "handbag", "suitcase"})

#: Subjects that are not people.
ANIMAL_LABELS: frozenset[str] = SUBJECT_LABELS - {PERSON_LABEL}

#: Fractional change in net position (in frame widths) below which a track
#: counts as having stayed put rather than travelled across the scene.
_STATIONARY_DRIFT = 0.05

#: Minimum overlap between a detection and an open pseudo-track's last box
#: for the two to be treated as the same object when no tracker ids are
#: available. Generous, because sampled frames are seconds apart and a
#: walking subject moves a long way between them; a parked vehicle, which is
#: what this matters most for, overlaps itself almost exactly.
_ASSOCIATION_IOU = 0.3

#: How many sampled frames a pseudo-track may go unseen before a later
#: detection at the same spot counts as a *different* object. Without this,
#: one person standing at the door at the start of a clip and a different
#: one standing there at the end merge into a single track whose apparent
#: dwell spans the whole clip — which reads as loitering when nobody
#: loitered. One missed frame is tolerated; a long absence is not.
_ASSOCIATION_MAX_FRAME_GAP = 2

#: Share of a subject's own box that must fall inside a zone's bounding box
#: for them to count as being in it even when their feet are not. A zone
#: drawn tightly around a parked car sits *above* the ground a person stands
#: on, so a foot-point test alone would almost never fire for the exact case
#: those zones exist for — someone right at the vehicle. Set high enough
#: that a passer-by clipping the zone's lower edge does not qualify.
_ZONE_BOX_OVERLAP = 0.25

#: Bounding-box area ratio (last sighting ÷ first sighting) beyond which a
#: subject is treated as having moved toward or away from the camera. A
#: person walking directly at a camera roughly doubles in apparent height
#: over a few metres, so 1.35 is comfortably above frame-to-frame noise
#: without needing a large approach to trigger.
_AREA_GROWTH_RATIO = 1.35


@dataclass(frozen=True)
class TrackPoint:
    """One sighting of a tracked object in one sampled frame."""

    frame_index: int
    offset: float
    box: Box
    confidence: float


@dataclass(frozen=True)
class ApproachProfile:
    """How a track's distance to a fixed target evolved over the clip.

    Two different pixel distances live here because they answer two
    different questions. ``min_gap`` and the fractions are *ground*
    distances (:func:`.geometry.ground_gap`) — how far apart the two were
    standing — and are what proximity and approach rules use, because a
    passer-by in the foreground is not close to the car however much their
    boxes touch. ``min_box_gap`` is the raw outline gap
    (:func:`.geometry.box_gap`, negative when the boxes overlap), which is
    what contact rules need.

    ``approach_fraction`` and ``retreat_fraction`` are relative changes in
    the 0.0-1.0 range — "closed 70% of the distance it started at" — which
    stay meaningful regardless of frame resolution, unlike raw pixels.
    """

    first_gap: float
    last_gap: float
    min_gap: float
    min_gap_offset: float
    approach_fraction: float
    retreat_fraction: float
    min_box_gap: float
    min_box_gap_index: int

    @property
    def approached(self) -> bool:
        """True when the subject closed a material share of its initial distance."""
        return self.approach_fraction >= 0.25

    @property
    def retreated(self) -> bool:
        """True when the subject backed materially away from its closest point."""
        return self.retreat_fraction >= 0.25


@dataclass
class ObjectTrack:
    """One object followed across the sampled frames of a single clip.

    ``tracked`` distinguishes a genuine ByteTrack identity from the
    fallback grouping used when tracking was unavailable (every detection
    of a label lumped together). The fallback still supports useful
    "something of this class was around for a while" reasoning, but it
    cannot tell two people apart — so it is reported honestly and drags
    evidence quality down rather than silently masquerading as a real
    track.
    """

    label: str
    track_id: int | None
    points: list[TrackPoint]
    frame_size: tuple[float, float]
    tracked: bool = True
    _zone_cache: dict[Zone, list[bool]] = field(
        default_factory=dict, repr=False, compare=False
    )

    # -- basic shape ---------------------------------------------------

    @property
    def first_offset(self) -> float:
        """Clip-relative seconds of the first sighting."""
        return self.points[0].offset

    @property
    def last_offset(self) -> float:
        """Clip-relative seconds of the last sighting."""
        return self.points[-1].offset

    @property
    def dwell_seconds(self) -> float:
        """Seconds between first and last sighting.

        A *lower bound* on how long the subject was actually present: the
        clip is sampled every few seconds, so a subject can arrive just
        after one sample and leave just before the next without ever
        widening this span.
        """
        return self.last_offset - self.first_offset

    @property
    def frame_count(self) -> int:
        """How many distinct sampled frames this track appeared in."""
        return len({p.frame_index for p in self.points})

    @property
    def mean_confidence(self) -> float:
        """Mean detector confidence across this track's sightings."""
        return sum(p.confidence for p in self.points) / len(self.points)

    @property
    def peak_height_fraction(self) -> float:
        """Largest share of the frame's *height* this object ever spanned.

        Height, not area, is what governs how much usable detail a subject
        offers — it is the standard surveillance measure for the same
        reason, and unlike area it doesn't collapse when someone is turned
        sideways. A figure spanning 8% of frame height supports "someone
        walked past" and nothing more, however confident the detector is
        about the box around it.
        """
        height = self.frame_size[1]
        if height <= 0:
            return 0.0
        return min(1.0, max(p.box[3] - p.box[1] for p in self.points) / height)

    def continuity(self, frame_interval: float) -> float:
        """Share of the frames within this track's own span that it appears in.

        1.0 means the tracker held the object in every sampled frame
        between its first and last sighting; a lower value means it was
        repeatedly lost and reacquired, which makes any conclusion drawn
        from the track weaker. Always 1.0 for a single-sighting track,
        which has no span to be discontinuous across.
        """
        if frame_interval <= 0 or self.dwell_seconds <= 0:
            return 1.0
        expected = round(self.dwell_seconds / frame_interval) + 1
        return min(1.0, self.frame_count / expected) if expected > 0 else 1.0

    # -- motion --------------------------------------------------------

    @property
    def path_length(self) -> float:
        """Total centre-point travel, in frame widths."""
        width = self.frame_size[0]
        if width <= 0 or len(self.points) < 2:
            return 0.0
        total = 0.0
        centers = [box_center(p.box) for p in self.points]
        for (x1, y1), (x2, y2) in pairwise(centers):
            total += math.hypot(x2 - x1, y2 - y1)
        return total / width

    @property
    def average_speed(self) -> float:
        """Mean travel speed in frame widths per second (0.0 when static)."""
        if self.dwell_seconds <= 0:
            return 0.0
        return self.path_length / self.dwell_seconds

    @property
    def net_drift(self) -> tuple[float, float]:
        """Net ``(dx, dy)`` from first to last sighting, in frame widths."""
        width = self.frame_size[0]
        if width <= 0:
            return (0.0, 0.0)
        (x1, y1) = box_center(self.points[0].box)
        (x2, y2) = box_center(self.points[-1].box)
        return ((x2 - x1) / width, (y2 - y1) / width)

    @property
    def area_ratio(self) -> float:
        """Apparent-size ratio between the last and first sighting.

        Greater than 1 means the object grew in frame, which for a fixed
        camera means it moved closer. Returns 1.0 when the first box is
        degenerate and the ratio would be meaningless.
        """
        first = box_area(self.points[0].box)
        last = box_area(self.points[-1].box)
        return last / first if first > 0 else 1.0

    @property
    def direction(self) -> str:
        """A short phrase describing net movement, for prompts and the UI."""
        if len(self.points) < 2:
            return "seen once"
        ratio = self.area_ratio
        if ratio >= _AREA_GROWTH_RATIO:
            return "moving toward the camera"
        if ratio <= 1.0 / _AREA_GROWTH_RATIO:
            return "moving away from the camera"
        dx, dy = self.net_drift
        if abs(dx) < _STATIONARY_DRIFT and abs(dy) < _STATIONARY_DRIFT:
            return "staying in roughly one place"
        if abs(dx) >= abs(dy):
            return "moving right across the frame" if dx > 0 else "moving left"
        return "moving down the frame" if dy > 0 else "moving up the frame"

    @property
    def max_speed_increase(self) -> float:
        """Largest *acceleration* between consecutive legs, in widths/second.

        Deliberately one-directional. Slowing to a stop is what everyone
        does on reaching a car, a door, or a gate, so treating any large
        speed *change* as "something sudden happened" would flag every
        ordinary arrival. A sharp acceleration is the unusual half: rushing
        at something, or bolting away from it. Zero for tracks with too few
        sightings to have two legs to compare.
        """
        width = self.frame_size[0]
        if width <= 0 or len(self.points) < 3:
            return 0.0
        speeds: list[float] = []
        for a, b in pairwise(self.points):
            dt = b.offset - a.offset
            if dt <= 0:
                continue
            (x1, y1), (x2, y2) = box_center(a.box), box_center(b.box)
            speeds.append(math.hypot(x2 - x1, y2 - y1) / width / dt)
        if len(speeds) < 2:
            return 0.0
        return max(max(b - a, 0.0) for a, b in pairwise(speeds))

    # -- relationships to a fixed target -------------------------------

    def gaps_to(self, target: Box) -> list[float]:
        """Per-sighting outline gap between this track and a *target* box."""
        return [box_gap(p.box, target) for p in self.points]

    def ground_gaps_to(self, target: Box) -> list[float]:
        """Per-sighting ground-plane separation from a *target* box."""
        return [ground_gap(p.box, target) for p in self.points]

    def approach_to(self, target: Box) -> ApproachProfile:
        """Summarize how this track's distance to *target* changed."""
        ground = self.ground_gaps_to(target)
        first, last = ground[0], ground[-1]
        min_gap = min(ground)
        min_index = ground.index(min_gap)
        approach = (first - min_gap) / first if first > 0 else 0.0
        retreat = (last - min_gap) / last if last > 0 else 0.0
        boxes = self.gaps_to(target)
        min_box_gap = min(boxes)
        return ApproachProfile(
            first_gap=first,
            last_gap=last,
            min_gap=min_gap,
            min_gap_offset=self.points[min_index].offset,
            approach_fraction=max(0.0, min(1.0, approach)),
            retreat_fraction=max(0.0, min(1.0, retreat)),
            min_box_gap=min_box_gap,
            min_box_gap_index=boxes.index(min_box_gap),
        )

    # -- relationships to a zone ---------------------------------------

    def zone_membership(self, zone: Zone) -> list[bool]:
        """Per-sighting "was this object in *zone*".

        Satisfied either by standing in it — the ground point, since a
        person's box centre floats well above where their feet actually are
        — or by overlapping a substantial share of their own box with it,
        which is what "at the car" looks like for a zone drawn around a
        parked vehicle rather than around a patch of ground.

        Cached per zone *value* (Zone is a frozen dataclass, so it hashes by
        its coordinates): the detector asks the same question from several
        rules, and the polygon test is the one piece of geometry here that
        isn't trivially cheap.
        """
        cached = self._zone_cache.get(zone)
        if cached is not None:
            return cached
        width, height = self.frame_size
        if width <= 0 or height <= 0:
            result = [False] * len(self.points)
        else:
            zone_box = zone.to_pixel_box(width, height)
            result = []
            for point in self.points:
                fx, fy = box_foot_point(point.box)
                standing_in = zone.contains(fx / width, fy / height)
                result.append(
                    standing_in
                    or _overlap_share(point.box, zone_box) >= _ZONE_BOX_OVERLAP
                )
        self._zone_cache[zone] = result
        return result

    def entered_zone(self, zone: Zone) -> bool:
        """True if this track was seen outside *zone* and later inside it."""
        membership = self.zone_membership(zone)
        return any(not before and after for before, after in pairwise(membership))

    def zone_dwell(self, zone: Zone) -> float:
        """Observed span (seconds) between the first and last in-zone sighting."""
        membership = self.zone_membership(zone)
        offsets = [p.offset for p, inside in zip(self.points, membership) if inside]
        return offsets[-1] - offsets[0] if offsets else 0.0

    def in_zone(self, zone: Zone) -> bool:
        """True if any sighting of this track fell inside *zone*."""
        return any(self.zone_membership(zone))


def _overlap_share(box: Box, zone_box: Box) -> float:
    """Fraction of *box*'s own area that falls inside *zone_box*."""
    ix1, iy1 = max(box[0], zone_box[0]), max(box[1], zone_box[1])
    ix2, iy2 = min(box[2], zone_box[2]), min(box[3], zone_box[3])
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    own = box_area(box)
    return intersection / own if own > 0 else 0.0


#: One raw detection: ``(label, confidence, box, track_id, frame_index)``.
#: Deliberately a plain tuple rather than ``vision.DetectedObject`` so this
#: package stays independent of the computer-vision module and its optional
#: heavy dependencies.
Detection = tuple[str, float, Box, int | None, int]


def build_tracks(
    detections: Sequence[Detection],
    frame_interval: float,
    frame_size: tuple[float, float],
) -> list[ObjectTrack]:
    """Group flat per-frame detections into :class:`ObjectTrack` objects.

    *detections* are :data:`Detection` tuples. Clip-relative timing comes
    from ``frame_index * frame_interval``, since frames are extracted at a
    fixed interval (see ``BaseAnalyzer.extract_frames``).

    Detections carrying a real track id group by ``(track_id, label)`` — a
    label flip mid-track starts a new track rather than producing an object
    that was a dog and then a person. Detections with no track id (tracking
    unavailable for that call) are associated across frames by spatial
    overlap instead — see :func:`_associate_untracked` — and the resulting
    tracks are flagged ``tracked=False`` so everything downstream knows the
    association was inferred rather than measured.

    Tracks are returned ordered by first appearance, so the caller's
    narration follows the order things actually happened.
    """
    grouped: dict[tuple[int, str], list[TrackPoint]] = {}
    tracked_keys: set[tuple[int, str]] = set()
    untracked: list[Detection] = []
    for detection in detections:
        label, confidence, box, track_id, frame_index = detection
        if track_id is None:
            untracked.append(detection)
            continue
        key = (track_id, label)
        tracked_keys.add(key)
        grouped.setdefault(key, []).append(
            TrackPoint(
                frame_index=frame_index,
                offset=frame_index * frame_interval,
                box=box,
                confidence=confidence,
            )
        )

    for synthetic_id, (label, confidence, box, _, frame_index) in _associate_untracked(
        untracked
    ):
        grouped.setdefault((synthetic_id, label), []).append(
            TrackPoint(
                frame_index=frame_index,
                offset=frame_index * frame_interval,
                box=box,
                confidence=confidence,
            )
        )

    tracks = [
        ObjectTrack(
            label=label,
            track_id=track_id if (track_id, label) in tracked_keys else None,
            points=sorted(points, key=lambda p: p.frame_index),
            frame_size=frame_size,
            tracked=(track_id, label) in tracked_keys,
        )
        for (track_id, label), points in grouped.items()
    ]
    tracks.sort(key=lambda t: (t.first_offset, t.label, t.track_id or 0))
    return tracks


def _associate_untracked(detections: list[Detection]) -> list[tuple[int, Detection]]:
    """Link detections that carry no tracker id into plausible tracks.

    Without this, every detection of a class would collapse into one
    pseudo-track per label — and two cars parked side by side would become a
    single phantom vehicle sitting in the gap between them, which is worse
    than useless for deciding which one is the protected car.

    The association is a simple greedy nearest-overlap match against each
    open track's most recent box, one frame at a time. Sampled frames are
    seconds apart so this cannot compete with a real tracker, and the tracks
    it produces say so (``tracked=False``); but it is exactly right for the
    stationary objects that matter most here, and better than nothing for
    moving ones.

    Returns ``(synthetic_id, detection)`` pairs. Ids are negative so they
    can never collide with a real tracker's.
    """
    if not detections:
        return []

    assigned: list[tuple[int, Detection]] = []
    # label -> list of (synthetic_id, last_box, last_frame_index)
    open_tracks: dict[str, list[tuple[int, Box, int]]] = {}
    next_id = -1

    for detection in sorted(detections, key=lambda d: d[4]):
        label, _confidence, box, _track_id, frame_index = detection
        candidates = [
            (box_iou(box, last_box), index)
            for index, (_tid, last_box, last_frame) in enumerate(
                open_tracks.get(label, [])
            )
            if 0 < frame_index - last_frame <= _ASSOCIATION_MAX_FRAME_GAP
        ]
        best = max(candidates, default=(0.0, -1))
        if best[0] >= _ASSOCIATION_IOU:
            track_id, _, _ = open_tracks[label][best[1]]
            open_tracks[label][best[1]] = (track_id, box, frame_index)
        else:
            track_id = next_id
            next_id -= 1
            open_tracks.setdefault(label, []).append((track_id, box, frame_index))
        assigned.append((track_id, detection))

    return assigned


def subject_tracks(tracks: list[ObjectTrack]) -> list[ObjectTrack]:
    """Filter *tracks* down to people and animals."""
    return [t for t in tracks if t.label in SUBJECT_LABELS]


def person_tracks(tracks: list[ObjectTrack]) -> list[ObjectTrack]:
    """Filter *tracks* down to people."""
    return [t for t in tracks if t.label == PERSON_LABEL]
