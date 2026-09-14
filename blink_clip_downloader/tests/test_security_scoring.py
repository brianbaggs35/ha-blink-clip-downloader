"""Tests for blink_downloader.security.scoring."""

from __future__ import annotations

import pytest

from blink_downloader.security.events import SecurityEvent, SecurityEventType, Severity
from blink_downloader.security.scoring import (
    DEFAULT_EVENT_POINTS,
    KNOWN_PERSON_POINTS,
    NIGHT_POINTS,
    OTHER_VEHICLE_POINTS,
    RiskAssessment,
    RiskFactor,
    RiskScorer,
    ScoringContext,
    band_for_score,
)


def _event(
    event_type: SecurityEventType,
    severity: Severity = Severity.NOTEWORTHY,
    confidence: float = 1.0,
) -> SecurityEvent:
    return SecurityEvent(
        event_type=event_type,
        severity=severity,
        confidence=confidence,
        detail=f"{event_type} happened",
    )


# ----------------------------------------------------------------------
# bands
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, Severity.ROUTINE),
        (24.9, Severity.ROUTINE),
        (25.0, Severity.NOTEWORTHY),
        (49.9, Severity.NOTEWORTHY),
        (50.0, Severity.SUSPICIOUS),
        (74.9, Severity.SUSPICIOUS),
        (75.0, Severity.CRITICAL),
        (100.0, Severity.CRITICAL),
    ],
)
def test_band_for_score(score: float, expected: Severity) -> None:
    assert band_for_score(score) is expected


def test_every_event_type_has_a_configured_weight() -> None:
    """A new event type with no entry here would silently score zero."""
    assert set(DEFAULT_EVENT_POINTS) == set(SecurityEventType)


# ----------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------


def test_no_events_scores_zero_and_routine() -> None:
    assessment = RiskScorer().score([], 1.0)
    assert assessment.score == 0.0
    assert assessment.severity is Severity.ROUTINE
    assert assessment.factors == []


def test_zero_weight_event_contributes_no_factor() -> None:
    assessment = RiskScorer().score([_event(SecurityEventType.OBJECT_ADDED)], 1.0)
    assert assessment.factors == []


def test_severity_scales_an_events_points() -> None:
    scorer = RiskScorer()
    mild = scorer.score([_event(SecurityEventType.ASSET_PROXIMITY)], 1.0)
    severe = scorer.score(
        [_event(SecurityEventType.ASSET_PROXIMITY, Severity.SUSPICIOUS)], 1.0
    )
    assert severe.score > mild.score


def test_confidence_scales_an_events_points() -> None:
    scorer = RiskScorer()
    unsure = scorer.score([_event(SecurityEventType.ZONE_ENTERED, confidence=0.0)], 1.0)
    sure = scorer.score([_event(SecurityEventType.ZONE_ENTERED, confidence=1.0)], 1.0)
    assert sure.score == pytest.approx(20.0)
    assert unsure.score == pytest.approx(7.0)


def test_confidence_outside_zero_to_one_is_clamped() -> None:
    scorer = RiskScorer()
    assert scorer.score(
        [_event(SecurityEventType.ZONE_ENTERED, confidence=5.0)], 1.0
    ).score == pytest.approx(20.0)
    assert scorer.score(
        [_event(SecurityEventType.ZONE_ENTERED, confidence=-5.0)], 1.0
    ).score == pytest.approx(7.0)


def test_weak_evidence_damps_the_score_without_erasing_it() -> None:
    events = [_event(SecurityEventType.CONTACT_CANDIDATE, Severity.SUSPICIOUS)]
    strong = RiskScorer().score(events, 1.0)
    weak = RiskScorer().score(events, 0.0)
    assert weak.score == pytest.approx(strong.score * 0.55)
    assert weak.raw_score == pytest.approx(strong.raw_score)


def test_evidence_quality_is_clamped_into_range() -> None:
    events = [_event(SecurityEventType.ZONE_ENTERED)]
    assert RiskScorer().score(events, 5.0).evidence_quality == 1.0
    assert RiskScorer().score(events, -1.0).evidence_quality == 0.0


def test_night_adds_points_and_a_named_factor() -> None:
    events = [_event(SecurityEventType.ZONE_ENTERED)]
    plain = RiskScorer().score(events, 1.0)
    at_night = RiskScorer().score(events, 1.0, ScoringContext(is_night=True))
    assert at_night.score == pytest.approx(plain.score + NIGHT_POINTS)
    assert any(f.name == "unusual_hour" for f in at_night.factors)


def test_a_known_household_member_subtracts_points_after_damping() -> None:
    events = [_event(SecurityEventType.CONTACT_CANDIDATE, Severity.SUSPICIOUS)]
    plain = RiskScorer().score(events, 0.5)
    known = RiskScorer().score(
        events, 0.5, ScoringContext(approved_person_recognized=True)
    )
    assert known.score == pytest.approx(max(0.0, plain.score + KNOWN_PERSON_POINTS))
    assert any(f.name == "known_person" for f in known.factors)


def test_being_closer_to_another_vehicle_subtracts_points() -> None:
    events = [_event(SecurityEventType.ASSET_PROXIMITY)]
    plain = RiskScorer().score(events, 1.0)
    neighbour = RiskScorer().score(
        events, 1.0, ScoringContext(subject_nearer_other_vehicle=True)
    )
    assert neighbour.score == pytest.approx(
        max(0.0, plain.score + OTHER_VEHICLE_POINTS)
    )
    assert any(f.name == "closer_to_other_vehicle" for f in neighbour.factors)


def test_penalties_stack_and_the_score_floors_at_zero() -> None:
    assessment = RiskScorer().score(
        [_event(SecurityEventType.SUBJECT_PRESENT, Severity.ROUTINE)],
        1.0,
        ScoringContext(
            approved_person_recognized=True, subject_nearer_other_vehicle=True
        ),
    )
    assert assessment.score == 0.0


def test_score_is_capped_at_one_hundred() -> None:
    events = [
        _event(SecurityEventType.IMPACT_CANDIDATE, Severity.CRITICAL),
        _event(SecurityEventType.CONTACT_CANDIDATE, Severity.SUSPICIOUS),
        _event(SecurityEventType.ZONE_ENTERED),
        _event(SecurityEventType.LOITERING, Severity.SUSPICIOUS),
        _event(SecurityEventType.OBJECT_REMOVED, Severity.SUSPICIOUS),
    ]
    assessment = RiskScorer().score(events, 1.0)
    assert assessment.score == 100.0
    assert assessment.raw_score == 100.0


def test_custom_point_overrides_are_merged_not_replaced() -> None:
    scorer = RiskScorer({SecurityEventType.ZONE_ENTERED: 100.0})
    assessment = scorer.score([_event(SecurityEventType.ZONE_ENTERED)], 1.0)
    assert assessment.score == 100.0
    # An untouched type keeps its default.
    assert scorer.score(
        [_event(SecurityEventType.RETREAT)], 1.0
    ).score == pytest.approx(DEFAULT_EVENT_POINTS[SecurityEventType.RETREAT])


# ----------------------------------------------------------------------
# severity resolution
# ----------------------------------------------------------------------


def test_a_single_suspicious_event_does_not_override_the_band() -> None:
    """Otherwise the score would be decoration — any suspicious event would
    decide the clip's severity on its own."""
    assessment = RiskScorer().score(
        [_event(SecurityEventType.RETREAT, Severity.SUSPICIOUS)], 1.0
    )
    assert assessment.score < 25.0
    assert assessment.severity is Severity.ROUTINE


def test_a_critical_event_floors_the_clips_severity() -> None:
    assessment = RiskScorer().score(
        [_event(SecurityEventType.IMPACT_CANDIDATE, Severity.CRITICAL, confidence=0.1)],
        1.0,
    )
    assert assessment.score < 75.0
    assert assessment.severity is Severity.CRITICAL


def test_a_critical_event_on_weak_evidence_is_stepped_down_one_band() -> None:
    assessment = RiskScorer().score(
        [_event(SecurityEventType.IMPACT_CANDIDATE, Severity.CRITICAL, confidence=0.1)],
        0.1,
    )
    assert assessment.severity is Severity.SUSPICIOUS


def test_the_band_still_wins_when_it_is_higher_than_the_floor() -> None:
    events = [
        _event(SecurityEventType.IMPACT_CANDIDATE, Severity.CRITICAL),
        _event(SecurityEventType.CONTACT_CANDIDATE, Severity.SUSPICIOUS),
        _event(SecurityEventType.ZONE_ENTERED),
    ]
    assessment = RiskScorer().score(events, 0.9)
    assert assessment.score >= 75.0
    assert assessment.severity is Severity.CRITICAL


# ----------------------------------------------------------------------
# assessment shape
# ----------------------------------------------------------------------


def test_primary_event_is_the_most_severe_then_most_confident() -> None:
    events = [
        _event(SecurityEventType.SUBJECT_PRESENT, Severity.ROUTINE, 1.0),
        _event(SecurityEventType.CONTACT_CANDIDATE, Severity.SUSPICIOUS, 0.4),
        _event(SecurityEventType.LOITERING, Severity.SUSPICIOUS, 0.9),
    ]
    assessment = RiskScorer().score(events, 1.0)
    primary = assessment.primary_event
    assert primary is not None
    assert primary.event_type is SecurityEventType.LOITERING


def test_primary_event_of_an_empty_assessment_is_none() -> None:
    assert RiskAssessment().primary_event is None


def test_assessment_to_dict() -> None:
    assessment = RiskScorer().score(
        [_event(SecurityEventType.ZONE_ENTERED)], 0.5, ScoringContext(is_night=True)
    )
    payload = assessment.to_dict()
    assert payload["severity"] == "routine"
    assert payload["event_type"] == "zone_entered"
    assert payload["evidence_quality"] == 0.5
    assert payload["factors"] == [
        {"name": "zone_entered", "points": 20.0, "detail": "zone_entered happened"},
        {
            "name": "unusual_hour",
            "points": NIGHT_POINTS,
            "detail": "Activity occurred during the quiet overnight hours.",
        },
    ]
    assert payload["events"] == [
        RiskScorer()
        .score([_event(SecurityEventType.ZONE_ENTERED)], 0.5)
        .events[0]
        .to_dict()
    ]


def test_assessment_to_dict_without_events_has_no_event_type() -> None:
    assert RiskAssessment().to_dict()["event_type"] == ""


def test_risk_factor_to_dict_rounds() -> None:
    assert RiskFactor("x", 1.23456, "why").to_dict() == {
        "name": "x",
        "points": 1.23,
        "detail": "why",
    }
