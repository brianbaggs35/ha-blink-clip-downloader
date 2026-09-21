"""Rendering structured security evidence into prompt text.

The AI provider receives the deterministic layer's findings as *facts to
verify*, never as a verdict to agree with. That framing is the whole point:
the model is far better than any code at telling a person from a shadow, or
a car parked behind someone from a car being touched, and far worse at
arithmetic on bounding boxes. So the prompt hands it the arithmetic and
asks for the judgement — including explicit permission to contradict what
the code computed.

Kept in this package rather than in the analyzer's own pile of prompt
segments for the same reason ``vision.py`` renders its own hints: the text
and the data it describes should change together.
"""

from __future__ import annotations

from .evidence import EvidenceQuality
from .scoring import RiskAssessment
from .vehicles import VehicleIdentification, relative_side

#: Longest timeline the segment will render. A busy clip can produce a lot
#: of routine presence events, and the point of this section is evidence,
#: not an exhaustive log — the most severe entries are the ones kept.
_MAX_TIMELINE_ENTRIES = 8


def _timeline_line(start: float, end: float, detail: str, severity: str) -> str:
    when = f"{start:.0f}s" if end - start < 0.5 else f"{start:.0f}-{end:.0f}s"
    return f"  {when} — {detail} [{severity}]"


def build_security_segment(
    assessment: RiskAssessment, evidence: EvidenceQuality
) -> str | None:
    """Render the SECURITY EVIDENCE prompt section, or ``None`` if empty.

    Returns ``None`` when nothing was detected at all — an empty section
    would spend prompt budget telling the model that code found nothing,
    which is not evidence of anything and reads as a hint that it should
    find something.
    """
    if not assessment.events:
        return None

    ordered = sorted(
        assessment.events,
        key=lambda e: (e.start_offset, e.end_offset),
    )
    if len(ordered) > _MAX_TIMELINE_ENTRIES:
        kept = sorted(
            assessment.events,
            key=lambda e: (-_severity_weight(str(e.severity)), -e.confidence),
        )[:_MAX_TIMELINE_ENTRIES]
        ordered = sorted(kept, key=lambda e: (e.start_offset, e.end_offset))

    lines = [
        _timeline_line(e.start_offset, e.end_offset, e.detail, str(e.severity))
        for e in ordered
    ]

    parts = [
        (
            "\n\nSECURITY EVIDENCE: The following was computed by object detection "
            "and tracking across this clip — measured from bounding boxes, not from "
            "reading the frames.\n"
        ),
        (
            f"Assessed risk: {assessment.score:.0f}/100 ({assessment.severity}). "
            f"Evidence quality: {evidence.score * 100:.0f}% ({evidence.label}).\n"
        ),
        "What was observed:\n",
        "\n".join(lines),
    ]

    if evidence.notes:
        parts.append("\nLimits of this evidence: " + "; ".join(evidence.notes) + ".")
    if evidence.unavailable:
        parts.append(
            "\nEvidence sources unavailable for this clip: "
            + ", ".join(evidence.unavailable)
            + "."
        )

    parts.append(
        "\nYour job with this section: check it against what you can actually see "
        "in these frames. Where the frames support it, describe what happened. "
        "Where they contradict it — the tracked 'person' is a shadow or a bush, "
        "the 'possible contact' is a vehicle parked behind someone, the subject is "
        "plainly a resident going about normal business — say so and judge by the "
        "frames, not by this section: it is computed from boxes and can be wrong. "
        "Weak evidence quality means exactly that; do not treat a high assessed "
        "risk built on weak evidence as established fact. "
        "Never quote any of these numbers, times, scores, or the phrase 'risk "
        "score' in your description — write only what a homeowner would see."
    )
    return "".join(parts)


_SEVERITY_WEIGHTS = {
    "routine": 0,
    "noteworthy": 1,
    "suspicious": 2,
    "critical": 3,
}


def _severity_weight(severity: str) -> int:
    return _SEVERITY_WEIGHTS.get(severity, 0)


def summarize_assessment(assessment: RiskAssessment) -> str:
    """One-line summary of an assessment, for structured logging."""
    primary = assessment.primary_event
    return (
        f"risk={assessment.score:.0f}/100 severity={assessment.severity} "
        f"events={len(assessment.events)} "
        f"primary={primary.event_type if primary else 'none'} "
        f"evidence_quality={assessment.evidence_quality:.2f}"
    )


def build_vehicle_identity_segment(
    identification: VehicleIdentification, nearest: str | None = None
) -> str:
    """Tell the model *which* vehicle in frame is the protected one.

    A written description ("silver 2019 hatchback") is close to useless to a
    small vision model asked to pick one car out of two parked side by side.
    A spatial anchor — "the vehicle in the lower left; the other one, to its
    right, is not protected" — is something every model can act on, and it
    is the difference between a neighbour getting into their own car being
    routine and it being a critical alert.

    *nearest* is :func:`~.vehicles.nearest_vehicle_for_subjects`'s verdict on
    which vehicle the subject actually went to, when that is answerable.
    """
    protected = identification.protected
    if protected is None:
        return (
            "\n\nWHICH VEHICLE IS PROTECTED: The protected vehicle does NOT appear "
            "to be in these frames — "
            + (identification.basis or "it was not found")
            + ". Do not apply protected-vehicle rules to any vehicle visible here: "
            "another car parked in or near the same space is somebody else's, and "
            "a person approaching, entering, or touching it is routine."
        )

    parts = [
        (
            "\n\nWHICH VEHICLE IS PROTECTED: The protected vehicle is the one in "
            f"{protected.position_label} — {identification.basis}."
        )
    ]
    if identification.others:
        count = len(identification.others)
        sides = ", ".join(
            f"one {relative_side(other.box, protected.box)}"
            for other in identification.others[:3]
        )
        noun = "vehicle is" if count == 1 else "vehicles are"
        parts.append(
            f" {count} other {noun} visible ({sides}) and NOT protected — a person "
            "approaching, entering, loading, or touching one of those is routine "
            "and must be reported as suspicious=false unless they also interact "
            "with the protected vehicle."
        )
    if not identification.confident:
        parts.append(
            " This identification is NOT confident, so do not report vehicle "
            "proximity or contact unless you can see it clearly in the frames "
            "yourself."
        )
    if nearest == "other":
        parts.append(
            " Tracking shows the person stayed closer to one of the other "
            "vehicles than to the protected one throughout this clip."
        )
    elif nearest == "protected":
        parts.append(
            " Tracking shows the person stayed closer to the protected vehicle "
            "than to any other vehicle in frame."
        )
    return "".join(parts)
