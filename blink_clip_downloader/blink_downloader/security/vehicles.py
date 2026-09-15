"""Deciding which car in frame is *your* car.

The single most damaging failure mode of a protected-vehicle feature is
protecting the wrong vehicle. On a shared driveway, a street-facing camera,
or an apartment car park, a neighbour returning to their own car sits a few
feet from yours and produces exactly the geometry an intruder would — so
every proximity, contact, and impact rule downstream fires on activity that
has nothing to do with the protected vehicle.

Picking "whichever vehicle is nearest the drawn zone" is not enough: two
cars parked side by side are both near the zone, and the nearest-edge test
happily picks the neighbour's when it is even slightly closer to the zone's
boundary. This module replaces that with three independent pieces of
evidence, any of which can be missing:

1. **Zone overlap** — how much the vehicle actually *occupies* the region
   the user drew, not how close it is to it.
2. **Learned parking position** — where the protected vehicle has sat in
   this camera's view across previous clips, accumulated over time (see
   :class:`VehicleSignature`).
3. **Learned appearance** — a coarse colour fingerprint of the protected
   vehicle, which separates a silver hatchback from the red pickup beside
   it even when they overlap the same region.

And, critically, it is allowed to answer "none of these vehicles is the
protected one". A camera whose owner has driven to work sees only the
neighbour's car; designating that car as protected — which the previous
nearest-vehicle logic had no way to avoid — is what turns an empty driveway
into a stream of false alerts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .geometry import Box, Zone, box_area, box_iou
from .tracks import VEHICLE_LABELS, ObjectTrack, TrackPoint

#: Weight each evidence source carries when all are available. Zone overlap
#: dominates because it is the user's own explicit statement of where their
#: vehicle is; the learned signals exist to break ties the zone cannot and
#: to work at all when no zone was ever drawn.
_WEIGHT_ZONE = 0.5
_WEIGHT_POSITION = 0.3
_WEIGHT_APPEARANCE = 0.2

#: Combined score a candidate must reach before it is accepted as the
#: protected vehicle when there is real evidence to judge against. Below
#: this, the honest answer is that the protected vehicle is not in frame.
MIN_MATCH_SCORE = 0.35

#: Confidence at or above which downstream rules treat the identification as
#: settled. Below it the protected-vehicle analysis still runs, but says out
#: loud that it may be looking at the wrong car — see
#: ``_car_protection_segment``.
CONFIDENT_THRESHOLD = 0.6

#: Share of the confidence a match keeps purely on its own merits, before
#: the margin over the runner-up is taken into account. Deliberately low:
#: two cars that both sit largely inside a loosely-drawn zone is precisely
#: the ambiguous case this module exists to stop treating as settled, and a
#: high floor would let the winner's absolute score carry it over the
#: confidence bar regardless of how close the other car came.
_MARGIN_FLOOR = 0.35

#: Samples of consistent sightings before the learned signature is trusted
#: on its own. One clip is a snapshot; a dozen is a parking habit.
SIGNATURE_MIN_SAMPLES = 4

#: Cap on how fast the learned signature moves toward a new observation, so
#: one bad frame (a delivery van stopped in the space) cannot redefine where
#: the protected vehicle lives.
_MAX_BLEND_WEIGHT = 0.25


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity between two equal-length histograms (0.0-1.0)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return max(0.0, min(1.0, dot / norm)) if norm > 0 else 0.0


@dataclass(frozen=True)
class VehicleSignature:
    """What a camera has learned about its protected vehicle over time.

    ``box`` is in normalized (0.0-1.0) coordinates so the signature stays
    valid if frame resolution ever changes. ``histogram`` is a coarse
    colour fingerprint of the vehicle's own pixels, produced by the vision
    pipeline — its exact binning is that module's business; everything here
    needs is that the same vehicle produces a similar vector.
    """

    box: Box
    histogram: tuple[float, ...] = ()
    sample_count: int = 0

    @property
    def established(self) -> bool:
        """True once enough consistent sightings back this signature."""
        return self.sample_count >= SIGNATURE_MIN_SAMPLES

    def position_similarity(self, box: Box) -> float:
        """How closely a normalized *box* matches the learned position."""
        return box_iou(self.box, box)

    def appearance_similarity(self, histogram: tuple[float, ...]) -> float:
        """How closely a colour fingerprint matches the learned one."""
        return _cosine(self.histogram, histogram)

    def blend(self, box: Box, histogram: tuple[float, ...]) -> VehicleSignature:
        """Return an updated signature folding in one new observation.

        The new observation's weight decays as ``1 / (samples + 1)``,
        capped by :data:`_MAX_BLEND_WEIGHT`, so the signature converges on
        the habit rather than chasing the latest clip.
        """
        weight = min(_MAX_BLEND_WEIGHT, 1.0 / (self.sample_count + 1))
        blended_box = tuple(
            old * (1 - weight) + new * weight for old, new in zip(self.box, box)
        )
        if histogram and len(histogram) == len(self.histogram):
            blended_hist = tuple(
                old * (1 - weight) + new * weight
                for old, new in zip(self.histogram, histogram)
            )
        else:
            blended_hist = histogram or self.histogram
        return VehicleSignature(
            box=(
                blended_box[0],
                blended_box[1],
                blended_box[2],
                blended_box[3],
            ),
            histogram=blended_hist,
            sample_count=self.sample_count + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "box": list(self.box),
            "histogram": list(self.histogram),
            "sample_count": self.sample_count,
        }

    @classmethod
    def from_observation(
        cls, box: Box, histogram: tuple[float, ...]
    ) -> VehicleSignature:
        """Start a fresh signature from a single confident sighting."""
        return cls(box=box, histogram=histogram, sample_count=1)


@dataclass
class VehicleCandidate:
    """One detected vehicle, scored against the protected-vehicle evidence."""

    track_id: int | None
    label: str
    box: Box
    normalized_box: Box
    zone_overlap: float = 0.0
    position_similarity: float = 0.0
    appearance_similarity: float = 0.0
    score: float = 0.0
    position_label: str = ""
    #: The sighting this vehicle was seen most confidently in — which
    #: frame, and its real box in that frame, so a colour fingerprint of
    #: *this* vehicle can be cropped from somewhere it actually was.
    #: Deliberately absent from :meth:`to_dict`: a pointer into one clip's
    #: frame list means nothing once persisted.
    sample: TrackPoint | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "label": self.label,
            "position": self.position_label,
            "zone_overlap": round(self.zone_overlap, 3),
            "position_similarity": round(self.position_similarity, 3),
            "appearance_similarity": round(self.appearance_similarity, 3),
            "score": round(self.score, 3),
        }


@dataclass
class VehicleIdentification:
    """Which vehicle (if any) is the protected one, and how sure we are."""

    protected: VehicleCandidate | None = None
    others: list[VehicleCandidate] = field(default_factory=list)
    basis: str = ""
    confidence: float = 0.0

    @property
    def confident(self) -> bool:
        """True when downstream rules may treat this as settled."""
        return self.protected is not None and self.confidence >= CONFIDENT_THRESHOLD

    @property
    def ambiguous(self) -> bool:
        """True when a vehicle was picked but the choice is not trustworthy."""
        return self.protected is not None and not self.confident

    def to_dict(self) -> dict[str, Any]:
        return {
            "identified": self.protected is not None,
            "confident": self.confident,
            "confidence": round(self.confidence, 3),
            "basis": self.basis,
            "protected": self.protected.to_dict() if self.protected else None,
            "others": [c.to_dict() for c in self.others],
        }


#: Thirds of the frame, with the middle band deliberately wider than a
#: strict third: a vehicle whose centre sits just off the dividing line
#: should read as "middle", not flip between "upper" and "lower" on a few
#: pixels of detector jitter between clips.
_BAND_LOW = 0.38
_BAND_HIGH = 0.62


def _band(fraction: float, low: str, middle: str, high: str) -> str:
    """Name which third of an axis *fraction* (0.0-1.0) falls in."""
    if fraction < _BAND_LOW:
        return low
    if fraction > _BAND_HIGH:
        return high
    return middle


def describe_region(box: Box, frame_size: tuple[float, float]) -> str:
    """Name the ninth of the frame a box's centre falls in.

    Used to anchor the protected vehicle spatially in the prompt. A small
    vision model is far more reliable at "the vehicle in the lower left"
    than at matching a written description like "silver 2019 hatchback"
    against what it can see.
    """
    width, height = frame_size
    if width <= 0 or height <= 0:
        return "an unknown part of the frame"
    cx = (box[0] + box[2]) / 2.0 / width
    cy = (box[1] + box[3]) / 2.0 / height
    vertical = _band(cy, "upper", "middle", "lower")
    horizontal = _band(cx, "left", "centre", "right")
    if vertical == "middle" and horizontal == "centre":
        return "the centre of the frame"
    if vertical == "middle":
        return f"the {horizontal} of the frame"
    if horizontal == "centre":
        return f"the {vertical} centre of the frame"
    return f"the {vertical} {horizontal} of the frame"


def relative_side(other: Box, reference: Box) -> str:
    """Describe where *other* sits relative to *reference*."""
    other_cx = (other[0] + other[2]) / 2.0
    ref_cx = (reference[0] + reference[2]) / 2.0
    other_cy = (other[1] + other[3]) / 2.0
    ref_cy = (reference[1] + reference[3]) / 2.0
    if abs(other_cx - ref_cx) >= abs(other_cy - ref_cy):
        return "to its right" if other_cx > ref_cx else "to its left"
    return "in front of it" if other_cy > ref_cy else "behind it"


def _median_box(track: ObjectTrack) -> Box:
    """Representative box for a parked vehicle across its sightings."""
    import statistics

    return (
        statistics.median([p.box[0] for p in track.points]),
        statistics.median([p.box[1] for p in track.points]),
        statistics.median([p.box[2] for p in track.points]),
        statistics.median([p.box[3] for p in track.points]),
    )


def _normalize(box: Box, frame_size: tuple[float, float]) -> Box:
    width, height = frame_size
    if width <= 0 or height <= 0:
        return box
    return (box[0] / width, box[1] / height, box[2] / width, box[3] / height)


def _score_candidate(
    track: ObjectTrack,
    frame_size: tuple[float, float],
    zone: Zone | None,
    signature: VehicleSignature | None,
    histograms: dict[int | None, tuple[float, ...]],
) -> VehicleCandidate:
    """Score one detected vehicle against whichever evidence exists.

    The score is the weighted mean of the sources actually available, not a
    sum with zeros for the missing ones: a user who drew a zone but has no
    learned signature yet is judged purely on the zone, rather than being
    dragged toward zero by evidence nobody has collected.
    """
    box = _median_box(track)
    normalized = _normalize(box, frame_size)
    # The frame the detector was most sure of, and that frame's real box —
    # `box` above is a median across sightings and so belongs to no single
    # frame, which makes it the wrong thing to crop a colour sample out of.
    # Every track build_tracks produces has at least one point.
    sample = max(track.points, key=lambda pt: pt.confidence)
    candidate = VehicleCandidate(
        track_id=track.track_id,
        label=track.label,
        box=box,
        normalized_box=normalized,
        position_label=describe_region(box, frame_size),
        sample=sample,
    )

    components: list[tuple[float, float]] = []
    if zone is not None:
        # Measured against the zone's real outline, so a freeform shape
        # only credits vehicles actually inside what the user drew.
        candidate.zone_overlap = zone.overlap_with_box(box, *frame_size)
        components.append((candidate.zone_overlap, _WEIGHT_ZONE))
    if signature is not None and signature.established:
        candidate.position_similarity = signature.position_similarity(normalized)
        components.append((candidate.position_similarity, _WEIGHT_POSITION))
        # Untracked pseudo-tracks all carry a track id of None, so a
        # fingerprint map keyed by id would hand every candidate the same
        # vector — noise dressed as evidence. Appearance is only usable for
        # vehicles the tracker actually told apart.
        fingerprint = (
            histograms.get(track.track_id, ()) if track.track_id is not None else ()
        )
        if fingerprint and signature.histogram:
            candidate.appearance_similarity = signature.appearance_similarity(
                fingerprint
            )
            components.append((candidate.appearance_similarity, _WEIGHT_APPEARANCE))

    total_weight = sum(weight for _, weight in components)
    if total_weight > 0:
        candidate.score = (
            sum(value * weight for value, weight in components) / total_weight
        )
    return candidate


def identify_protected_vehicle(
    tracks: list[ObjectTrack],
    frame_size: tuple[float, float],
    zone: Zone | None = None,
    signature: VehicleSignature | None = None,
    histograms: dict[int | None, tuple[float, ...]] | None = None,
) -> VehicleIdentification:
    """Decide which detected vehicle is the protected one.

    *histograms* maps a vehicle track id to its colour fingerprint, when the
    vision pipeline computed them. Every evidence source is optional; the
    score is the weighted mean of whichever are actually available, so a
    user who has drawn a zone but has no learned signature yet is judged
    purely on the zone, exactly as before, while a user who never drew one
    still gets the benefit of the learned parking position.

    Returns an identification with ``protected=None`` when real evidence
    exists and no vehicle matches it — the protected car is simply not in
    these frames — which is the case the old nearest-vehicle heuristic
    could not express.
    """
    vehicles = [t for t in tracks if t.label in VEHICLE_LABELS]
    if not vehicles:
        return VehicleIdentification(basis="no vehicle detected in these frames")

    hist_map = histograms or {}
    # A zone only means anything once there is a frame to scale it onto.
    # Without one it would convert to a zero-sized box that every candidate
    # scores zero against — reporting the protected vehicle as absent when
    # the truth is simply that we could not measure.
    has_frame = frame_size[0] > 0 and frame_size[1] > 0
    scoring_zone = zone if zone is not None and has_frame else None
    use_signature = signature is not None and signature.established

    candidates = [
        _score_candidate(track, frame_size, scoring_zone, signature, hist_map)
        for track in vehicles
    ]

    if scoring_zone is None and not use_signature:
        return _identify_without_evidence(candidates)

    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    best = ranked[0]
    if best.score < MIN_MATCH_SCORE:
        return VehicleIdentification(
            others=ranked,
            basis="no detected vehicle matches where it normally sits",
        )

    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    margin = (best.score - runner_up) / best.score if best.score > 0 else 1.0
    confidence = best.score * (
        _MARGIN_FLOOR + (1.0 - _MARGIN_FLOOR) * max(0.0, min(1.0, margin))
    )
    return VehicleIdentification(
        protected=best,
        others=ranked[1:],
        basis=_describe_basis(best, scoring_zone is not None, use_signature),
        confidence=confidence,
    )


def _identify_without_evidence(
    candidates: list[VehicleCandidate],
) -> VehicleIdentification:
    """Fall back when neither a zone nor a learned signature exists.

    A single vehicle in frame is almost certainly the one the user meant,
    so it is accepted with modest confidence. Several vehicles with nothing
    to tell them apart is the genuinely ambiguous case: the largest is
    used, since the protected car is usually the one parked closest to its
    own camera, but the low confidence propagates into the prompt and damps
    the risk score rather than pretending to a certainty nothing supports.
    """
    if len(candidates) == 1:
        return VehicleIdentification(
            protected=candidates[0],
            basis="the only vehicle visible in these frames",
            confidence=0.55,
        )
    ranked = sorted(candidates, key=lambda c: box_area(c.box), reverse=True)
    return VehicleIdentification(
        protected=ranked[0],
        others=ranked[1:],
        basis=(
            f"{len(candidates)} vehicles are visible and no protection zone has "
            "been drawn, so the largest was assumed — draw a zone on the Vehicles "
            "tab to identify the protected vehicle reliably"
        ),
        confidence=0.3,
    )


def _describe_basis(best: VehicleCandidate, had_zone: bool, had_signature: bool) -> str:
    reasons: list[str] = []
    if had_zone:
        reasons.append(
            f"it fills {best.zone_overlap * 100:.0f}% of the marked protection zone"
        )
    if had_signature:
        reasons.append(
            f"its position matches this camera's learned parking spot "
            f"({best.position_similarity * 100:.0f}%)"
        )
        if best.appearance_similarity:
            reasons.append(
                f"its colours match the learned vehicle "
                f"({best.appearance_similarity * 100:.0f}%)"
            )
    return " and ".join(reasons)


#: How much closer a subject must be to one vehicle than the other before
#: "which car were they actually at" is answerable. Inside this margin they
#: are effectively between the two, and saying otherwise would be a coin
#: flip dressed up as evidence.
_NEAREST_MARGIN = 0.75


def nearest_vehicle_for_subjects(
    subjects: list[ObjectTrack], identification: VehicleIdentification
) -> str | None:
    """Was the closest subject nearer the protected vehicle, or another one?

    Returns ``"protected"``, ``"other"``, or ``None`` when there is no
    other vehicle to compare against or the subject was between the two.

    This is the single most useful fact for the apartment-car-park case:
    a neighbour returning to the car beside yours produces proximity
    geometry indistinguishable from an intruder's until you ask *which*
    car they were actually at.
    """
    protected = identification.protected
    if protected is None or not identification.others or not subjects:
        return None

    # Judged from whichever subject got closest to any vehicle at all: in a
    # clip with a passer-by on the pavement and someone at a car, the one at
    # the car is the one the question is about.
    best: tuple[float, float] | None = None
    for track in subjects:
        gaps = (
            min(track.gaps_to(protected.box)),
            min(min(track.gaps_to(c.box)) for c in identification.others),
        )
        if best is None or min(gaps) < min(best):
            best = gaps

    assert best is not None  # subjects is non-empty, so the loop always ran
    protected_gap, other_gap = max(0.0, best[0]), max(0.0, best[1])
    if other_gap < protected_gap * _NEAREST_MARGIN:
        return "other"
    if protected_gap < other_gap * _NEAREST_MARGIN:
        return "protected"
    return None
