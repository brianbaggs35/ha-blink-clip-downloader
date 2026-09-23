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

from .events import (
    AUDIO_EVENTS,
    SecurityEvent,
    SecurityEventType,
    Severity,
    severity_rank,
)

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
    SecurityEventType.ASSET_REACH: 24.0,
    SecurityEventType.CONTACT_CANDIDATE: 32.0,
    SecurityEventType.IMPACT_CANDIDATE: 40.0,
    SecurityEventType.RETREAT_AFTER_CONTACT: 18.0,
    SecurityEventType.OBJECT_REMOVED: 25.0,
    SecurityEventType.OBJECT_ADDED: 0.0,
    # An animal against the vehicle is a damage event, not an intrusion, so
    # it stays out of the suspicious band — but it scored below plain
    # proximity, which had a dog with its paws on the bonnet counting for
    # less than a person merely standing beside the car. Scratched paintwork
    # is precisely what an owner wants told about.
    SecurityEventType.ANIMAL_ASSET_INTERACTION: 18.0,
    SecurityEventType.CAMERA_OBSTRUCTION: 40.0,
    # Heard, not seen (see .sounds). Glass and gunfire are weighted so
    # that clearing .sounds' own 50% confidence gate is on its own enough
    # to pass the default ai_risk_alert_threshold of 75 -- which is the
    # point of them: a window going at 3am must raise the alert whether or
    # not anything was visible, and whether or not the AI provider's model
    # thought the frames looked unremarkable. They are undamped (see
    # _AUDIO_FACTOR_NAMES), so these numbers are what actually lands.
    SecurityEventType.GLASS_BREAK_HEARD: 82.0,
    SecurityEventType.GUNSHOT_HEARD: 82.0,
    # An alarm is different in kind: a passing emergency siren matches it,
    # so it contributes real weight without reaching the alert band alone.
    SecurityEventType.ALARM_HEARD: 30.0,
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

#: Events that rest on a claim of contact with the asset. Unless depth or
#: segmentation backed it — the ``confirmed`` flag in the event's evidence —
#: that claim is a bare 2D overlap, which a person or dog walking in front
#: of the car produces in every frame; the "retreat after contact" built on
#: it carries the same flag.
_CONTACT_EVENTS: frozenset[SecurityEventType] = frozenset(
    {
        SecurityEventType.CONTACT_CANDIDATE,
        SecurityEventType.ANIMAL_ASSET_INTERACTION,
        SecurityEventType.RETREAT_AFTER_CONTACT,
    }
)

#: Events that describe one subject's own movement around the asset. Each
#: type counts once per clip, at its strongest: a second person — or the dog
#: on its lead — repeating the same approach adds no evidence the first did
#: not, and :data:`SecurityEventType.MULTIPLE_SUBJECTS` is what already
#: accounts for there being a group. Summed, a family walking past the car
#: at night scored 100. Every instance still reaches the prompt and the
#: Security tab; only the score counts it once.
_ONCE_PER_CLIP: frozenset[SecurityEventType] = frozenset(
    {
        SecurityEventType.SUBJECT_PRESENT,
        SecurityEventType.ZONE_ENTERED,
        SecurityEventType.ASSET_APPROACHED,
        SecurityEventType.ASSET_PROXIMITY,
        SecurityEventType.LOITERING,
        SecurityEventType.RETREAT,
        SecurityEventType.ASSET_REACH,
        SecurityEventType.CONTACT_CANDIDATE,
        SecurityEventType.ANIMAL_ASSET_INTERACTION,
        SecurityEventType.IMPACT_CANDIDATE,
        SecurityEventType.RETREAT_AFTER_CONTACT,
    }
)

#: Where the critical band — and the default ``ai_risk_alert_threshold`` —
#: begins, and the highest score a clip may hold when an unconfirmed contact
#: is the only reason it would be higher.
_CRITICAL_SCORE = next(t for t, s in SEVERITY_BANDS if s is Severity.CRITICAL)
_UNCONFIRMED_CONTACT_CEILING = _CRITICAL_SCORE - 1.0

#: Factor names produced by the heard-not-seen events. ``_event_factor``
#: names each factor after its event type, so this is the same set as
#: :data:`.events.AUDIO_EVENTS` in the form ``score`` compares against.
_AUDIO_FACTOR_NAMES: frozenset[str] = frozenset(str(e) for e in AUDIO_EVENTS)


def _strongest_of_each_subject_event(
    scored: list[tuple[SecurityEvent, RiskFactor]],
) -> list[tuple[SecurityEvent, RiskFactor]]:
    """*scored* with each :data:`_ONCE_PER_CLIP` type reduced to its most
    heavily weighted instance, everything else untouched and in order."""
    strongest: dict[SecurityEventType, int] = {}
    for index, (event, factor) in enumerate(scored):
        if event.event_type not in _ONCE_PER_CLIP:
            continue
        best = strongest.get(event.event_type)
        if best is None or factor.points > scored[best][1].points:
            strongest[event.event_type] = index
    kept = set(strongest.values())
    return [
        pair
        for index, pair in enumerate(scored)
        if pair[0].event_type not in _ONCE_PER_CLIP or index in kept
    ]


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
        scored = _strongest_of_each_subject_event(
            [(e, f) for e in events if (f := self._event_factor(e)) is not None]
        )
        factors = [f for _, f in scored]
        unconfirmed = sum(
            f.points
            for e, f in scored
            if e.event_type in _CONTACT_EVENTS
            and e.evidence.get("confirmed") is not True
        )

        if ctx.is_night:
            factors.append(
                RiskFactor(
                    name="unusual_hour",
                    points=NIGHT_POINTS,
                    detail="Activity occurred during the quiet overnight hours.",
                )
            )

        # Evidence quality measures how well the *camera* saw, so it damps
        # what the camera concluded and nothing else. A sound is not
        # better or worse evidence for having been recorded in the dark,
        # and damping it that way would mute the one case audio exists to
        # catch: glass going at night with nothing in frame, which scores
        # worst on every visual sub-score there is.
        heard = sum(f.points for f in factors if f.name in _AUDIO_FACTOR_NAMES)
        seen = sum(f.points for f in factors) - heard
        quality = max(0.0, min(1.0, evidence_quality))
        damping = _EVIDENCE_FLOOR + (1.0 - _EVIDENCE_FLOOR) * quality
        damped = seen * damping + heard
        positive = seen + heard

        # Applied after damping, not before: a household member being
        # recognized is a fact about who was there, not about how well the
        # clip was seen, so evidence quality must not shrink the credit
        # they get the way it shrinks the suspicion they offset.
        # ...and for the same reason neither discount below may offset a
        # heard event. Both answer "who was in frame and where"; a face in
        # the driveway is no evidence at all about a window breaking at
        # the back of the house. Without this floor, recognizing a
        # household member would quietly cancel a glass break.
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
                        "Everyone seen in the clip was recognized as an approved "
                        "household member."
                    ),
                )
            )

        # The penalty may eat into what the camera contributed but never
        # into what was heard, and the whole thing is still a 0-100 score:
        # clamping last, not first, is what keeps both true at once.
        score = max(0.0, min(100.0, max(damped + penalty, heard)))

        # An unconfirmed contact may raise a clip, but never be the reason
        # it reaches the band that forces an alert past the model's verdict.
        # Each event on its own already carries only a noteworthy claim,
        # yet someone walking close past the front of the car at night
        # collects five of them from one overlap — zone, approach,
        # proximity, "contact", "retreat after contact" — and summed they
        # scored 79. What the same clip scores without the unconfirmed
        # claim decides: anything else strong enough (a confirmed touch, a
        # sound, lingering) still carries it into the alert band; a bare
        # overlap cannot. The model still sees the frames and the claim.
        without = max(
            0.0, min(100.0, max(damped - unconfirmed * damping + penalty, heard))
        )
        if score >= _CRITICAL_SCORE > without:
            held = _UNCONFIRMED_CONTACT_CEILING - score
            factors.append(
                RiskFactor(
                    name="unconfirmed_contact",
                    points=held,
                    detail=(
                        "The possible contact rests only on outlines overlapping "
                        "in the image, with no depth or segmentation to confirm "
                        "it, so it is held below the alert band."
                    ),
                )
            )
            score = _UNCONFIRMED_CONTACT_CEILING

        return RiskAssessment(
            score=score,
            severity=self._severity_for(score, events, quality),
            factors=factors,
            events=list(events),
            evidence_quality=quality,
            raw_score=max(0.0, min(100.0, max(positive + penalty, heard))),
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
