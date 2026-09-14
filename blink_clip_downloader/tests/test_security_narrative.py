"""Tests for blink_downloader.security.narrative."""

from __future__ import annotations

from blink_downloader.security.events import SecurityEvent, SecurityEventType, Severity
from blink_downloader.security.evidence import EvidenceQuality
from blink_downloader.security.narrative import (
    build_security_segment,
    build_vehicle_identity_segment,
    summarize_assessment,
)
from blink_downloader.security.scoring import RiskAssessment, RiskScorer
from blink_downloader.security.vehicles import VehicleCandidate, VehicleIdentification

CAR = (300.0, 180.0, 460.0, 280.0)
NEIGHBOUR = (470.0, 180.0, 630.0, 280.0)


def _event(
    event_type: SecurityEventType = SecurityEventType.ZONE_ENTERED,
    severity: Severity = Severity.NOTEWORTHY,
    start: float = 0.0,
    end: float = 0.0,
    detail: str = "something happened",
    confidence: float = 0.8,
) -> SecurityEvent:
    return SecurityEvent(
        event_type=event_type,
        severity=severity,
        confidence=confidence,
        detail=detail,
        start_offset=start,
        end_offset=end,
    )


def _candidate(box=CAR, label="the lower centre of the frame") -> VehicleCandidate:
    return VehicleCandidate(
        track_id=1,
        label="car",
        box=box,
        normalized_box=(0.0, 0.0, 1.0, 1.0),
        position_label=label,
    )


# ----------------------------------------------------------------------
# security evidence segment
# ----------------------------------------------------------------------


def test_no_events_produces_no_segment() -> None:
    """An empty section would spend prompt budget telling the model code
    found nothing, which reads as a hint to go find something."""
    assert build_security_segment(RiskAssessment(), EvidenceQuality()) is None


def test_segment_states_the_score_evidence_and_timeline() -> None:
    assessment = RiskScorer().score([_event()], 0.8)
    segment = build_security_segment(assessment, EvidenceQuality(score=0.8))
    assert segment is not None
    assert "SECURITY EVIDENCE" in segment
    assert "Evidence quality: 80% (strong)" in segment
    assert "something happened [noteworthy]" in segment


def test_an_instantaneous_event_renders_a_single_timestamp() -> None:
    assessment = RiskScorer().score([_event(start=6.0, end=6.2)], 1.0)
    segment = build_security_segment(assessment, EvidenceQuality(score=1.0))
    assert segment is not None
    assert "  6s — " in segment


def test_a_spanning_event_renders_a_range() -> None:
    assessment = RiskScorer().score([_event(start=2.0, end=10.0)], 1.0)
    segment = build_security_segment(assessment, EvidenceQuality(score=1.0))
    assert segment is not None
    assert "  2-10s — " in segment


def test_a_long_timeline_keeps_the_most_severe_entries_in_time_order() -> None:
    events = [
        _event(start=float(i), end=float(i), detail=f"routine {i}") for i in range(12)
    ]
    events.append(
        _event(
            SecurityEventType.IMPACT_CANDIDATE,
            Severity.CRITICAL,
            start=20.0,
            end=20.0,
            detail="the important one",
        )
    )
    assessment = RiskScorer().score(events, 1.0)
    segment = build_security_segment(assessment, EvidenceQuality(score=1.0))
    assert segment is not None
    assert "the important one" in segment
    timeline = [
        line for line in segment.split("\n") if line.startswith("  ") and " — " in line
    ]
    assert len(timeline) == 8
    positions = [
        segment.index(f"routine {i}") for i in range(8) if f"routine {i}" in segment
    ]
    assert positions == sorted(positions)


def test_evidence_notes_and_missing_sources_are_stated() -> None:
    assessment = RiskScorer().score([_event()], 0.3)
    segment = build_security_segment(
        assessment,
        EvidenceQuality(score=0.3, notes=["it was dark"], unavailable=["depth"]),
    )
    assert segment is not None
    assert "Limits of this evidence: it was dark." in segment
    assert "Evidence sources unavailable for this clip: depth." in segment


def test_segment_tells_the_model_it_may_contradict_the_evidence() -> None:
    segment = build_security_segment(
        RiskScorer().score([_event()], 1.0), EvidenceQuality(score=1.0)
    )
    assert segment is not None
    assert "judge by the frames" in segment
    assert "Never quote any of these numbers" in segment


# ----------------------------------------------------------------------
# vehicle identity segment
# ----------------------------------------------------------------------


def test_identity_segment_when_the_protected_vehicle_is_absent() -> None:
    identification = VehicleIdentification(basis="it is not in these frames")
    segment = build_vehicle_identity_segment(identification)
    assert "does NOT appear" in segment
    assert "somebody else's" in segment


def test_identity_segment_anchors_the_vehicle_spatially() -> None:
    identification = VehicleIdentification(
        protected=_candidate(),
        basis="it fills 100% of the marked protection zone",
        confidence=0.9,
    )
    segment = build_vehicle_identity_segment(identification)
    assert "the one in the lower centre of the frame" in segment
    assert "fills 100%" in segment
    assert "NOT protected" not in segment


def test_identity_segment_names_the_other_vehicles_and_their_side() -> None:
    identification = VehicleIdentification(
        protected=_candidate(),
        others=[_candidate(box=NEIGHBOUR)],
        basis="zone",
        confidence=0.9,
    )
    segment = build_vehicle_identity_segment(identification)
    assert "1 other vehicle is visible (one to its right)" in segment
    assert "must be reported as suspicious=false" in segment


def test_identity_segment_pluralizes_several_other_vehicles() -> None:
    identification = VehicleIdentification(
        protected=_candidate(),
        others=[_candidate(box=NEIGHBOUR), _candidate(box=(0.0, 180.0, 160.0, 280.0))],
        basis="zone",
        confidence=0.9,
    )
    assert "2 other vehicles are visible" in build_vehicle_identity_segment(
        identification
    )


def test_identity_segment_lists_at_most_three_other_vehicles() -> None:
    identification = VehicleIdentification(
        protected=_candidate(),
        others=[_candidate(box=(float(i), 0.0, float(i) + 10, 10.0)) for i in range(5)],
        basis="zone",
        confidence=0.9,
    )
    segment = build_vehicle_identity_segment(identification)
    assert "5 other vehicles are visible" in segment
    assert segment.count("one to its") == 3


def test_identity_segment_warns_when_the_choice_is_not_confident() -> None:
    identification = VehicleIdentification(
        protected=_candidate(), basis="guesswork", confidence=0.2
    )
    assert "NOT confident" in build_vehicle_identity_segment(identification)


def test_identity_segment_reports_which_vehicle_the_subject_went_to() -> None:
    identification = VehicleIdentification(
        protected=_candidate(), basis="zone", confidence=0.9
    )
    assert "closer to one of the other vehicles" in build_vehicle_identity_segment(
        identification, "other"
    )
    assert "closer to the protected vehicle" in build_vehicle_identity_segment(
        identification, "protected"
    )
    assert "closer to" not in build_vehicle_identity_segment(identification, None)


# ----------------------------------------------------------------------
# logging summary
# ----------------------------------------------------------------------


def test_summarize_assessment() -> None:
    assessment = RiskScorer().score(
        [_event(SecurityEventType.CONTACT_CANDIDATE, Severity.SUSPICIOUS)], 0.5
    )
    summary = summarize_assessment(assessment)
    assert "events=1" in summary
    assert "primary=contact_candidate" in summary
    assert "evidence_quality=0.50" in summary


def test_summarize_an_empty_assessment() -> None:
    assert "primary=none" in summarize_assessment(RiskAssessment())
