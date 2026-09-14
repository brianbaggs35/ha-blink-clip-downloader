"""Deterministic risk scoring over detected security events.

The AI model is not the only authority on how serious a clip is, and it is
not the right one for questions with arithmetic answers. This module turns
the structured events into a 0-100 score whose every contribution can be
listed back to the user — "protected-zone entry +20, possible contact +28,
recognized household member −22" — so a verdict is auditable instead of
being a number a model produced for reasons nobody can inspect.

Two properties matter more than the exact weights:

* **Evidence quality damps the score.** A pile of confident-sounding events
  derived from four dark, tiny, barely-tracked frames cannot reach the same
  score as the same events seen clearly. Without this the scorer would
  manufacture false certainty, which is worse than no score at all.
* **Recognition reduces risk; it never erases it.** A known household
  member subtracts points, but the subtraction happens after damping and
  cannot by itself turn a critical event into a routine one — that guard
  lives in :data:`.events.BYPASS_BLOCKING_EVENTS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import SecurityEvent, SecurityEventType, Severity, severity_rank

#: Base points per event type, before severity and confidence scaling. These
#: are ordinary defaults chosen so that a single ordinary interaction cannot
#: reach the alert band on its own while a stacked sequence (enter zone →
#: approach → contact → retreat) comfortably does.
DEFAULT_EVENT_POINTS: dict[SecurityEventType, float] = {
    SecurityEventType.SUBJECT_PRESENT: 2.0,
    SecurityEventType.MULTIPLE_SUBJECTS: 8.0,
    SecurityEventType.ZONE_ENTERED: 20.0,
    SecurityEventType.ASSET_APPROACHED: 12.0,
    SecurityEventType.ASSET_PROXIMITY: 14.0,
    SecurityEventType.LOITERING: 15.0,
    SecurityEventType.RETREAT: 4.0,
    SecurityEventType.CONTACT_CANDIDATE: 32.0,
    SecurityEventType.IMPACT_CANDIDATE: 40.0,
    SecurityEventType.RETREAT_AFTER_CONTACT: 18.0,
    SecurityEventType.OBJECT_REMOVED: 25.0,
    SecurityEventType.OBJECT_ADDED: 0.0,
    SecurityEventType.ANIMAL_ASSET_INTERACTION: 8.0,
    SecurityEventType.CAMERA_OBSTRUCTION: 40.0,
}

#: How much an event's own severity scales its base points.
_SEVERITY_MULTIPLIER: dict[Severity, float] = {
    Severity.ROUTINE: 0.5,
    Severity.NOTEWORTHY: 1.0,
    Severity.SUSPICIOUS: 1.25,
    Severity.CRITICAL: 1.5,
}

#: Points added for activity at an hour when the property is normally quiet.
NIGHT_POINTS = 8.0

#: Points removed when every face in the clip belongs to an approved,
#: locally-enrolled household member. Calibrated against the commonest
#: household event there is — walking to your own car, opening the door and
#: getting in, which scores around fifty on geometry alone — so that
#: recognizing the person pulls it clear of the alert band. Deliberately not
#: large enough to zero out a stacked physical-interaction sequence: see
#: BYPASS_BLOCKING_EVENTS for the case recognition must not excuse at all.
KNOWN_PERSON_POINTS = -32.0

#: Risk score at or above which each severity band begins.
SEVERITY_BANDS: tuple[tuple[float, Severity], ...] = (
    (75.0, Severity.CRITICAL),
    (50.0, Severity.SUSPICIOUS),
    (25.0, Severity.NOTEWORTHY),
    (0.0, Severity.ROUTINE),
)

#: Share of an event's points it keeps at zero detector confidence. A
#: contact inferred from a bare bounding-box overlap and one confirmed by
#: pixel-level segmentation are not the same finding, and a high floor here
#: would let the weak one carry almost the same weight as the strong one.
_CONFIDENCE_FLOOR = 0.35

#: Evidence quality below which a critical event's claim on the clip's
#: overall severity is stepped down one band rather than taken at face value.
_WEAK_EVIDENCE = 0.4

#: Points removed when the subject was demonstrably closer to a vehicle that
#: is *not* the protected one — the apartment-car-park case, where a
#: neighbour returning to their own car produces the same geometry an
#: intruder would.
OTHER_VEHICLE_POINTS = -15.0

#: Share of an event's points that survives regardless of evidence quality,
#: and the share that scales with it. Perfect evidence leaves points
#: untouched; the worst possible evidence keeps 55% of them, so weak
#: evidence discounts a conclusion without discarding it.
_EVIDENCE_FLOOR = 0.55


def band_for_score(score: float) -> Severity:
    """Return the severity band *score* falls into."""
    for threshold, severity in SEVERITY_BANDS:
        if score >= threshold:
            return severity
    return Severity.ROUTINE  # pragma: no cover - the 0.0 band always matches


@dataclass(frozen=True)
class RiskFactor:
    """One named contribution to the risk score, positive or negative."""

    name: str
    points: float
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "points": round(self.points, 2),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScoringContext:
    """Clip-level circumstances that modify risk regardless of geometry."""

    is_night: bool = False
    approved_person_recognized: bool = False
    subject_nearer_other_vehicle: bool = False


@dataclass
class RiskAssessment:
    """The scored verdict for one clip."""

    score: float = 0.0
    severity: Severity = Severity.ROUTINE
    factors: list[RiskFactor] = field(default_factory=list)
    events: list[SecurityEvent] = field(default_factory=list)
    evidence_quality: float = 0.0
    raw_score: float = 0.0

    @property
    def primary_event(self) -> SecurityEvent | None:
        """The most severe event, breaking ties by detector confidence.

        This is what the clip gets labelled with in a list view, where
        there is only room for one event type.
        """
        if not self.events:
            return None
        return max(self.events, key=lambda e: (severity_rank(e.severity), e.confidence))

    def to_dict(self) -> dict[str, object]:
        """Serialize to the plain dict shape the API returns."""
        primary = self.primary_event
        return {
            "score": round(self.score, 2),
            "severity": str(self.severity),
            "raw_score": round(self.raw_score, 2),
            "evidence_quality": round(self.evidence_quality, 4),
            "event_type": str(primary.event_type) if primary else "",
            "factors": [f.to_dict() for f in self.factors],
            "events": [e.to_dict() for e in self.events],
        }


class RiskScorer:
    """Scores a clip's events into a :class:`RiskAssessment`."""

    def __init__(self, points: dict[SecurityEventType, float] | None = None) -> None:
        self._points = dict(DEFAULT_EVENT_POINTS)
        if points:
            self._points.update(points)

    def score(
        self,
        events: list[SecurityEvent],
        evidence_quality: float,
        context: ScoringContext | None = None,
    ) -> RiskAssessment:
        """Score *events*, damping by *evidence_quality* (0.0-1.0)."""
        ctx = context or ScoringContext()
        factors = [f for f in map(self._event_factor, events) if f is not None]

        if ctx.is_night:
            factors.append(
                RiskFactor(
                    name="unusual_hour",
                    points=NIGHT_POINTS,
                    detail="Activity occurred during the quiet overnight hours.",
                )
            )

        positive = sum(f.points for f in factors)
        quality = max(0.0, min(1.0, evidence_quality))
        damped = positive * (_EVIDENCE_FLOOR + (1.0 - _EVIDENCE_FLOOR) * quality)

        # Applied after damping, not before: a household member being
        # recognized is a fact about who was there, not about how well the
        # clip was seen, so evidence quality must not shrink the credit
        # they get the way it shrinks the suspicion they offset.
        penalty = 0.0
        if ctx.subject_nearer_other_vehicle:
            penalty += OTHER_VEHICLE_POINTS
            factors.append(
                RiskFactor(
                    name="closer_to_other_vehicle",
                    points=OTHER_VEHICLE_POINTS,
                    detail=(
                        "The subject stayed closer to a different vehicle than to "
                        "the protected one."
                    ),
                )
            )
        if ctx.approved_person_recognized:
            penalty += KNOWN_PERSON_POINTS
            factors.append(
                RiskFactor(
                    name="known_person",
                    points=KNOWN_PERSON_POINTS,
                    detail=(
                        "Every face in the clip matched an approved household member."
                    ),
                )
            )

        score = max(0.0, min(100.0, damped + penalty))

        return RiskAssessment(
            score=score,
            severity=self._severity_for(score, events, quality),
            factors=factors,
            events=list(events),
            evidence_quality=quality,
            raw_score=max(0.0, min(100.0, positive + penalty)),
        )

    def _event_factor(self, event: SecurityEvent) -> RiskFactor | None:
        """Turn one event into its scored contribution, or ``None`` if it
        carries no weight (an unknown type from a newer build, or a type
        deliberately worth zero such as a delivery being dropped off)."""
        base = self._points.get(event.event_type)
        if not base:
            return None
        multiplier = _SEVERITY_MULTIPLIER[event.severity]
        confidence_scale = _CONFIDENCE_FLOOR + (1.0 - _CONFIDENCE_FLOOR) * max(
            0.0, min(1.0, event.confidence)
        )
        return RiskFactor(
            name=str(event.event_type),
            points=base * multiplier * confidence_scale,
            detail=event.detail,
        )

    @staticmethod
    def _severity_for(
        score: float, events: list[SecurityEvent], quality: float
    ) -> Severity:
        """The score's band, with a floor for critical events only.

        Deliberately *not* "the most severe event wins": a single suspicious
        event would then decide the clip's severity every time and reduce
        the score to decoration. Only a critical event — possible impact
        with a protected asset — gets a floor, because burying one of those
        under a mid-range score is the one mistake worth being wrong about
        in the other direction. When the evidence behind it is weak, even
        that floor is stepped down a band rather than taken at face value.
        """
        banded = band_for_score(score)
        if not any(e.severity is Severity.CRITICAL for e in events):
            return banded
        floor = Severity.SUSPICIOUS if quality < _WEAK_EVIDENCE else Severity.CRITICAL
        return max(banded, floor, key=severity_rank)
