"""Tests for the audio half of the security layer (security/sounds.py).

The whole point of this module is that a camera cannot see round a corner
or in the dark, so most of what is asserted here is behaviour on clips
with *nothing* tracked — the case the visual layer is blind to by
construction.

Nothing here imports the vision package or needs audio: the classified
labels are plain ``(name, score)`` pairs, which is exactly the boundary
that keeps the security layer testable on an install with no torch.
"""

from __future__ import annotations

import pytest

from blink_downloader.security import (
    AUDIO_EVENTS,
    BYPASS_BLOCKING_EVENTS,
    ClipMeasurements,
    SecurityEventType,
    Severity,
    assess_clip,
)
from blink_downloader.security.sounds import _EVENT_CONFIDENCE, detect_audio_events


def _assess(labels, **kwargs):
    fields = {
        "camera": "Front Door",
        "tracks": [],
        "frame_interval": 1.0,
        "frame_count": 8,
        "frames_analyzed": 8,
        "target_frames": 8,
        "audio_labels": labels,
    }
    return assess_clip(ClipMeasurements(**{**fields, **kwargs}))


# ----------------------------------------------------------------------
# Which sounds become events
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Glass", SecurityEventType.GLASS_BREAK_HEARD),
        ("Breaking glass, shatter", SecurityEventType.GLASS_BREAK_HEARD),
        ("Smash, crash", SecurityEventType.GLASS_BREAK_HEARD),
        ("Gunshot, gunfire", SecurityEventType.GUNSHOT_HEARD),
        ("Explosion", SecurityEventType.GUNSHOT_HEARD),
        ("Car alarm", SecurityEventType.ALARM_HEARD),
        ("Smoke detector, smoke alarm", SecurityEventType.ALARM_HEARD),
        ("Siren", SecurityEventType.ALARM_HEARD),
    ],
)
def test_an_alarming_sound_raises_its_event(
    label: str, expected: SecurityEventType
) -> None:
    events = detect_audio_events([(label, 0.9)])
    assert [e.event_type for e in events] == [expected]


@pytest.mark.parametrize(
    "label",
    ["Speech", "Walk, footsteps", "Dog", "Vehicle", "Door", "Power tool", "Knock"],
)
def test_an_everyday_sound_raises_nothing(label: str) -> None:
    """The bar is a near-zero routine-household rate, the same bar
    BYPASS_BLOCKING_EVENTS applies. Each of these is something an ordinary
    house produces daily, and an event that fires daily is noise wearing a
    security label — they reach the AI as a hint and stop there."""
    assert detect_audio_events([(label, 0.99)]) == []


def test_matching_respects_word_boundaries() -> None:
    """ "Glasses" is eyewear and "Cartoon" is not a car."""
    assert detect_audio_events([("Glasses clinking", 0.9)]) == []


def test_a_sound_below_the_event_bar_is_a_hint_and_nothing_more() -> None:
    """Far above the 15% at which a sound is worth mentioning: a hint is
    weighed by the model, an event moves a score and can raise an alert."""
    assert detect_audio_events([("Glass", _EVENT_CONFIDENCE - 0.01)]) == []
    assert len(detect_audio_events([("Glass", _EVENT_CONFIDENCE)])) == 1


def test_nothing_heard_produces_nothing() -> None:
    assert detect_audio_events(None) == []
    assert detect_audio_events([]) == []


def test_one_sound_raises_one_event_not_one_per_matching_rule() -> None:
    events = detect_audio_events([("Glass shatter smash", 0.9)])
    assert len(events) == 1


def test_the_event_says_it_was_heard_and_may_be_out_of_frame() -> None:
    (event,) = detect_audio_events([("Breaking glass", 0.77)])
    assert "heard" in event.detail
    assert "outside the camera's view" in event.detail
    assert event.evidence == {"sound": "Breaking glass", "sound_confidence": 0.77}
    # No offsets invented: the classifier says what, never when.
    assert event.start_offset == 0.0
    assert event.end_offset == 0.0


def test_every_audio_event_type_is_declared_as_one() -> None:
    """scoring and narrative both branch on AUDIO_EVENTS; a new heard
    event left out of it would be damped by visual evidence quality and
    described to the model as measured from bounding boxes."""
    produced = {
        e.event_type
        for label in ("Glass", "Gunshot", "Car alarm")
        for e in detect_audio_events([(label, 0.9)])
    }
    assert produced == set(AUDIO_EVENTS)


# ----------------------------------------------------------------------
# What they do to the clip's risk
# ----------------------------------------------------------------------


def test_breaking_glass_alerts_with_nothing_in_frame() -> None:
    """The case this exists for. No tracks at all — because it is dark, or
    round the side of the house, or because object detection is off — and
    the clip must still raise an alert."""
    outcome = _assess([("Breaking glass, shatter", 0.9)])
    assert outcome.severity is Severity.CRITICAL
    # Clears the default ai_risk_alert_threshold of 75 on its own.
    assert outcome.risk_score >= 75


def test_a_gunshot_alerts_with_nothing_in_frame() -> None:
    assert _assess([("Gunshot, gunfire", 0.8)]).risk_score >= 75


def test_glass_at_the_very_bar_still_alerts() -> None:
    """Selectivity lives in the confidence bar, not in a scoring curve
    that quietly lets a just-qualifying detection through unnoticed."""
    assert _assess([("Glass", _EVENT_CONFIDENCE)]).risk_score >= 75


def test_an_alarm_alone_does_not_force_an_alert() -> None:
    """A passing emergency siren matches ALARM_HEARD. It is worth points
    and worth telling the model about; it is not worth waking someone."""
    outcome = _assess([("Siren", 0.95)])
    assert outcome.events
    assert outcome.risk_score < 75


def test_a_dark_clip_does_not_mute_what_was_heard() -> None:
    """Evidence quality rates the picture. Damping a sound by it would
    silence audio precisely when it is the only witness there is."""
    dark = _assess([("Glass", 0.9)], frames_analyzed=1)
    lit = _assess([("Glass", 0.9)], frames_analyzed=8)
    assert dark.risk_score == lit.risk_score >= 75


def test_a_recognized_face_cannot_cancel_a_glass_break() -> None:
    """A known face explains what the person in frame is doing. It says
    nothing about a window going at the back of the house, so the
    known-person discount must not reach a heard event."""
    outcome = _assess([("Glass", 0.9)], approved_person_recognized=True)
    assert outcome.risk_score >= 75


def test_the_score_stays_within_its_own_range() -> None:
    """Undamped points are large by design; the score is still 0-100, and
    the UI draws a bar the width of it."""
    outcome = _assess([("Glass", 1.0), ("Gunshot", 1.0), ("Car alarm", 1.0)])
    assert 0.0 <= outcome.risk_score <= 100.0
    assert 0.0 <= outcome.assessment.raw_score <= 100.0


def test_glass_and_gunfire_survive_the_face_bypass() -> None:
    """The safety-critical set. Both pass the same test IMPACT_CANDIDATE
    passes: not something ordinary household activity produces, and not
    something a face in frame can explain."""
    assert SecurityEventType.GLASS_BREAK_HEARD in BYPASS_BLOCKING_EVENTS
    assert SecurityEventType.GUNSHOT_HEARD in BYPASS_BLOCKING_EVENTS
    # An alarm is excluded: a passing ambulance matches it.
    assert SecurityEventType.ALARM_HEARD not in BYPASS_BLOCKING_EVENTS


# ----------------------------------------------------------------------
# What the AI provider is told
# ----------------------------------------------------------------------


def test_the_prompt_does_not_claim_a_sound_came_from_a_bounding_box() -> None:
    segment = "".join(_assess([("Breaking glass", 0.9)]).prompt_segments)
    assert "measured from sound, with nothing tracked" in segment
    assert "measured from bounding boxes, not" not in segment
    # It must also say that absence of visual confirmation is not a refutation.
    assert "not evidence against it" in segment
    assert "often wrong" in segment
    assert "this rates the picture, not the audio" in segment


def test_a_purely_visual_clip_is_described_exactly_as_before() -> None:
    from blink_downloader.security.events import SecurityEvent
    from blink_downloader.security.evidence import EvidenceQuality
    from blink_downloader.security.narrative import build_security_segment
    from blink_downloader.security.scoring import RiskAssessment

    assessment = RiskAssessment(
        score=40.0,
        severity=Severity.NOTEWORTHY,
        events=[
            SecurityEvent(
                event_type=SecurityEventType.SUBJECT_PRESENT,
                severity=Severity.ROUTINE,
                confidence=0.9,
                detail="A person was tracked",
            )
        ],
    )
    segment = build_security_segment(assessment, EvidenceQuality(score=0.7))
    assert segment is not None
    assert "measured from bounding boxes" in segment
    assert "sound" not in segment
    assert "rates the picture" not in segment


def test_a_clip_with_both_says_both() -> None:
    from blink_downloader.security.events import SecurityEvent
    from blink_downloader.security.evidence import EvidenceQuality
    from blink_downloader.security.narrative import build_security_segment
    from blink_downloader.security.scoring import RiskAssessment

    assessment = RiskAssessment(
        score=90.0,
        severity=Severity.CRITICAL,
        events=[
            SecurityEvent(
                event_type=SecurityEventType.SUBJECT_PRESENT,
                severity=Severity.ROUTINE,
                confidence=0.9,
                detail="A person was tracked",
            ),
            *detect_audio_events([("Glass", 0.9)]),
        ],
    )
    segment = build_security_segment(assessment, EvidenceQuality(score=0.7))
    assert segment is not None
    assert "measured from bounding boxes and from sound" in segment
