"""Typed security-event vocabulary shared by the whole security layer.

This module is the single place the add-on names what it thinks happened in
a clip. Everything downstream — the deterministic detector
(:mod:`.detector`), risk scoring (:mod:`.scoring`), the prompt segment the
AI provider reasons over (:mod:`.narrative`), the ``security_events``
database table, and the web UI's Security tab — keys off these enums rather
than passing bare strings around, so a typo can't silently invent a new
event type that nothing scores or renders.

Nothing in this package imports :mod:`blink_downloader.vision` or any heavy
optional dependency: the security layer reasons over plain numbers already
produced by the computer-vision stages, which keeps it importable (and
fully testable) on an install with no torch/ultralytics/opencv at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """How much attention a security event deserves.

    Ordered routine → critical. The bands themselves are fixed (they are
    what the words mean); the *risk score* that maps onto them is what
    :mod:`.scoring` computes, and the threshold at which an alert is forced
    is configurable — see ``ai_risk_alert_threshold``.
    """

    ROUTINE = "routine"
    NOTEWORTHY = "noteworthy"
    SUSPICIOUS = "suspicious"
    CRITICAL = "critical"


# Ascending order, used for "the most severe event in this clip" comparisons
# without giving StrEnum a custom __lt__ (which would make it compare
# differently from the plain strings it serializes as).
_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.ROUTINE: 0,
    Severity.NOTEWORTHY: 1,
    Severity.SUSPICIOUS: 2,
    Severity.CRITICAL: 3,
}


def severity_rank(severity: Severity | str) -> int:
    """Return the ascending rank of *severity* (routine 0 → critical 3).

    Accepts the plain string form too, since rows read back from the
    database arrive as ``str`` rather than the enum. An unrecognized value
    ranks lowest — a severity this build doesn't know about must never be
    treated as more urgent than one it does.
    """
    try:
        return _SEVERITY_ORDER[Severity(severity)]
    except ValueError:
        return 0


def max_severity(severities: list[Severity]) -> Severity:
    """Return the most severe entry, or ``ROUTINE`` for an empty list."""
    if not severities:
        return Severity.ROUTINE
    return max(severities, key=severity_rank)


class SecurityEventType(StrEnum):
    """Every event the deterministic detector can produce.

    Deliberately limited to things actually derivable from the evidence
    this add-on has — sparse sampled frames, YOLO boxes, ByteTrack IDs,
    optional depth/contact/face stages. Event types that would need pose
    estimation or temporal action recognition (striking, kicking,
    climbing, ...) are intentionally absent rather than guessed at; see
    :mod:`.detector` for what each one below requires.
    """

    #: A relevant subject (person or animal) was tracked at all. The
    #: baseline routine event, so a clip's timeline is never empty just
    #: because nothing concerning happened.
    SUBJECT_PRESENT = "subject_present"
    #: A subject was seen inside the protected asset's configured zone
    #: having previously been seen outside it.
    ZONE_ENTERED = "zone_entered"
    #: A subject's distance to the protected asset decreased materially
    #: over the clip.
    ASSET_APPROACHED = "asset_approached"
    #: A subject was within close range of the protected asset.
    ASSET_PROXIMITY = "asset_proximity"
    #: A subject stayed in view (or in the zone) well beyond a brief pass.
    LOITERING = "loitering"
    #: A subject's distance to the asset increased materially after having
    #: been close to it.
    RETREAT = "retreat"
    #: A subject close to the protected asset with an arm extended toward
    #: it — trying a handle, reaching through a window. Needs pose
    #: estimation; a bounding box cannot tell this from standing still.
    ASSET_REACH = "asset_reach"
    #: Bounding boxes overlapped at a similar depth, or pixel-level contact
    #: segmentation reported touching.
    CONTACT_CANDIDATE = "contact_candidate"
    #: Contact evidence plus an abrupt change in the subject's motion — the
    #: signature of a strike, bump, or collision rather than a touch.
    IMPACT_CANDIDATE = "impact_candidate"
    #: A retreat immediately following contact/impact evidence.
    RETREAT_AFTER_CONTACT = "retreat_after_contact"
    #: A carryable object (bag/box/case) present early in the clip and gone
    #: by the end while a subject was near it.
    OBJECT_REMOVED = "object_removed"
    #: The reverse — a carryable object absent early and present at the end.
    OBJECT_ADDED = "object_added"
    #: An animal, rather than a person, in contact with or very close to the
    #: protected asset.
    ANIMAL_ASSET_INTERACTION = "animal_asset_interaction"
    #: More than one distinct person tracked in the same clip.
    MULTIPLE_SUBJECTS = "multiple_subjects"
    #: The scene changed drastically with nothing detected in it — the
    #: signature of a covered, sprayed, or repositioned camera.
    CAMERA_OBSTRUCTION = "camera_obstruction"


#: Event types severe enough that recognizing a household member must NOT
#: clear the clip's suspicious flag (see ``BaseAnalyzer._face_bypass_applies``).
#: A recognized person denting the car is still a dented car, and that is the
#: one thing a household member's own face genuinely cannot explain away.
#:
#: Deliberately narrow, and every near-miss was excluded on purpose:
#:
#: * ``CONTACT_CANDIDATE`` — a resident touching their own car (opening a
#:   door, loading the boot) produces exactly this, several times a day.
#:   Blocking the bypass on it would make routine household car use
#:   permanently suspicious, which is precisely the false-positive problem
#:   the bypass exists to solve.
#: * ``OBJECT_REMOVED`` — most often a resident picking up their own
#:   delivered package.
#: * ``CAMERA_OBSTRUCTION`` — requires zero detections to fire, so no face
#:   can have been recognized in the first place; adding it would be dead
#:   code pretending to be a safety rule.
#:
#: ``IMPACT_CANDIDATE`` survives because it never fires on contact alone: the
#: detector requires confirmed contact *plus* one of an abrupt speed change,
#: a change in the vehicle's own appearance, or a raised arm at the moment of
#: contact (see ``detector._impact_event``). The first two no routine
#: interaction with one's own vehicle produces. The third can — closing a
#: tailgate, loading a roof rack, washing the roof — so a household member
#: doing one of those is knowingly accepted as a false positive here, on the
#: grounds that it is uncommon and that the alternative is staying quiet
#: about the one event type that would actually matter. If that trade ever
#: needs revisiting, revisit it in ``_impact_event``'s ``raised`` branch
#: rather than by widening or narrowing this set.
BYPASS_BLOCKING_EVENTS: frozenset[SecurityEventType] = frozenset(
    {SecurityEventType.IMPACT_CANDIDATE}
)


@dataclass
class SecurityEvent:
    """One structured thing the deterministic layer believes happened.

    ``confidence`` is how sure the *detector* is that this event occurred
    given the evidence it had — deliberately separate from the AI model's
    own confidence in its verdict, and separate again from
    :class:`~.evidence.EvidenceQuality`, which measures how good the
    underlying imagery was in the first place. A detector can be confident
    about a relationship it computed from boxes that were themselves tiny,
    dark, and barely tracked; keeping the three apart is what lets the UI
    say "high confidence, weak evidence" instead of implying certainty.

    ``evidence`` holds the raw numbers behind the event (gaps in feet,
    dwell seconds, overlap fractions, ...) so the UI and the prompt can
    show the working rather than only the conclusion. It is JSON-serialized
    into the ``security_events`` table verbatim.
    """

    event_type: SecurityEventType
    severity: Severity
    confidence: float
    detail: str
    subject_label: str = ""
    track_id: int | None = None
    asset_name: str = ""
    asset_type: str = ""
    start_offset: float = 0.0
    end_offset: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the plain dict shape the database and API both use."""
        return {
            "event_type": str(self.event_type),
            "severity": str(self.severity),
            "confidence": round(self.confidence, 4),
            "detail": self.detail,
            "subject_label": self.subject_label,
            "track_id": self.track_id,
            "asset_name": self.asset_name,
            "asset_type": self.asset_type,
            "start_offset": round(self.start_offset, 3),
            "end_offset": round(self.end_offset, 3),
            "evidence": dict(self.evidence),
        }
