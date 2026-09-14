"""Tests for blink_downloader.security.events."""

from __future__ import annotations

from blink_downloader.security.events import (
    BYPASS_BLOCKING_EVENTS,
    SecurityEvent,
    SecurityEventType,
    Severity,
    max_severity,
    severity_rank,
)


def test_severity_rank_is_ascending() -> None:
    assert severity_rank(Severity.ROUTINE) == 0
    assert severity_rank(Severity.NOTEWORTHY) == 1
    assert severity_rank(Severity.SUSPICIOUS) == 2
    assert severity_rank(Severity.CRITICAL) == 3


def test_severity_rank_accepts_the_plain_string_form() -> None:
    """Rows read back from the database arrive as str, not as the enum."""
    assert severity_rank("critical") == 3


def test_unknown_severity_ranks_lowest() -> None:
    """A severity a future build invented must never outrank a known one."""
    assert severity_rank("apocalyptic") == 0


def test_max_severity() -> None:
    assert (
        max_severity([Severity.ROUTINE, Severity.CRITICAL, Severity.NOTEWORTHY])
        is Severity.CRITICAL
    )


def test_max_severity_of_nothing_is_routine() -> None:
    assert max_severity([]) is Severity.ROUTINE


def test_bypass_blocking_is_deliberately_narrow() -> None:
    """Widening this set makes routine household activity permanently
    suspicious — see the set's own comment for why each near-miss is out."""
    assert BYPASS_BLOCKING_EVENTS == {SecurityEventType.IMPACT_CANDIDATE}
    assert SecurityEventType.CONTACT_CANDIDATE not in BYPASS_BLOCKING_EVENTS
    assert SecurityEventType.OBJECT_REMOVED not in BYPASS_BLOCKING_EVENTS


def test_event_to_dict_rounds_and_copies_evidence() -> None:
    evidence = {"gap": 1.23456}
    event = SecurityEvent(
        event_type=SecurityEventType.ZONE_ENTERED,
        severity=Severity.NOTEWORTHY,
        confidence=0.123456,
        detail="entered",
        subject_label="person",
        track_id=4,
        asset_name="blue sedan",
        asset_type="vehicle",
        start_offset=1.23456,
        end_offset=9.87654,
        evidence=evidence,
    )
    payload = event.to_dict()
    assert payload == {
        "event_type": "zone_entered",
        "severity": "noteworthy",
        "confidence": 0.1235,
        "detail": "entered",
        "subject_label": "person",
        "track_id": 4,
        "asset_name": "blue sedan",
        "asset_type": "vehicle",
        "start_offset": 1.235,
        "end_offset": 9.877,
        "evidence": {"gap": 1.23456},
    }
    payload["evidence"]["gap"] = 0
    assert evidence["gap"] == 1.23456


def test_event_defaults_are_empty_not_none() -> None:
    event = SecurityEvent(
        event_type=SecurityEventType.SUBJECT_PRESENT,
        severity=Severity.ROUTINE,
        confidence=0.5,
        detail="seen",
    )
    assert event.evidence == {}
    assert event.track_id is None
