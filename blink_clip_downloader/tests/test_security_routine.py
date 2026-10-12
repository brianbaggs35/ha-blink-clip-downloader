"""When one person's trip through a door explains a whole clip, and how far.

Two layers. The pure policy -- :func:`routine_door_visit` and
:func:`flag_can_be_cleared` -- is pinned with hand-built events, because every
way it can say no is a way for something to be cleared that should not be.
The pipeline tests then run real tracks through the detector, the scorer and
the narrative, which is where a rule that is right alone could still be wrong
in combination.
"""

from __future__ import annotations

import pytest

from blink_downloader.security import (
    ROUTINE_VISIT_MAX_ANOMALY,
    ROUTINE_VISIT_MAX_CONFIDENCE,
    ROUTINE_VISIT_MAX_CONFIDENCE_CONFIRMED,
    ClipMeasurements,
    EvidenceQuality,
    SecurityEvent,
    SecurityEventType,
    Severity,
    assess_clip,
    build_marked_asset,
    build_tracks,
    door_use_allowed,
    flag_can_be_cleared,
    routine_door_visit,
)
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.narrative import build_security_segment
from blink_downloader.security.scoring import RiskScorer

FRAME = (640.0, 360.0)
DOOR_ZONE = Zone.from_config(
    {"x_min": 0.094, "y_min": 0.333, "x_max": 0.203, "y_max": 0.833}
)

GOOD = EvidenceQuality(score=0.8)

#: A phrase only the routine-visit note carries: its presence and its absence
#: are both checked against the note itself, never against a phrase that could
#: vanish from the prompt for some other reason.
_NOTE = "the shape of ordinary use of a door"


def _event(
    event_type: SecurityEventType,
    severity: Severity = Severity.ROUTINE,
    *,
    asset_name: str = "Front door",
    subject_label: str = "person",
    track_id: int | None = 1,
) -> SecurityEvent:
    return SecurityEvent(
        event_type=event_type,
        severity=severity,
        confidence=0.8,
        detail="x",
        subject_label=subject_label,
        track_id=track_id,
        asset_name=asset_name,
    )


def _visit() -> SecurityEvent:
    return _event(SecurityEventType.DOOR_USED)


#: What walking up to a door and through it also produces.
_THE_REST_OF_A_VISIT = [
    _event(SecurityEventType.SUBJECT_PRESENT, asset_name=""),
    _event(SecurityEventType.ZONE_ENTERED, Severity.NOTEWORTHY),
    _event(SecurityEventType.ASSET_APPROACHED, Severity.NOTEWORTHY),
    _event(SecurityEventType.ASSET_PROXIMITY, Severity.NOTEWORTHY),
    _event(SecurityEventType.RETREAT, Severity.NOTEWORTHY),
    _event(SecurityEventType.ASSET_DISTURBED, Severity.NOTEWORTHY),
]


# ----------------------------------------------------------------------
# routine_door_visit
# ----------------------------------------------------------------------


def test_a_visit_on_its_own_explains_the_clip() -> None:
    visit = _visit()
    assert routine_door_visit([visit], GOOD) is visit


def test_everything_a_visit_itself_produces_is_explained() -> None:
    visit = _visit()
    assert routine_door_visit([*_THE_REST_OF_A_VISIT, visit], GOOD) is visit


def test_no_visit_no_explanation() -> None:
    assert routine_door_visit([], GOOD) is None
    assert routine_door_visit(_THE_REST_OF_A_VISIT, GOOD) is None


def test_two_visits_are_not_one_story() -> None:
    """Two doors, or two people: nobody can read it as a single visit."""
    assert routine_door_visit([_visit(), _visit()], GOOD) is None


def test_every_other_kind_of_event_is_something_a_visit_does_not_explain() -> None:
    """Written as 'everything not on the list', so an event type added later
    blocks until someone decides it belongs on it."""
    explained = {str(e.event_type) for e in _THE_REST_OF_A_VISIT} | {"door_used"}
    others = [t for t in SecurityEventType if str(t) not in explained]
    assert SecurityEventType.LOITERING in others
    assert SecurityEventType.MULTIPLE_SUBJECTS in others
    assert SecurityEventType.OBJECT_REMOVED in others
    assert SecurityEventType.GLASS_BREAK_HEARD in others
    for event_type in others:
        events = [_visit(), _event(event_type, Severity.ROUTINE, asset_name="")]
        assert routine_door_visit(events, GOOD) is None, event_type


@pytest.mark.parametrize("severity", [Severity.SUSPICIOUS, Severity.CRITICAL])
def test_nothing_at_or_above_suspicious_is_explained(severity: Severity) -> None:
    for kind in (SecurityEventType.ASSET_PROXIMITY, SecurityEventType.ZONE_ENTERED):
        assert routine_door_visit([_visit(), _event(kind, severity)], GOOD) is None


def test_an_event_about_another_asset_is_not_explained() -> None:
    """Someone who came to the door past a marked bike also did something at
    the bike."""
    other = _event(SecurityEventType.ASSET_PROXIMITY, asset_name="Bike")
    assert routine_door_visit([_visit(), other], GOOD) is None


def test_a_second_person_is_not_explained_whether_or_not_they_were_tracked() -> None:
    for track_id in (2, None):
        stranger = _event(
            SecurityEventType.SUBJECT_PRESENT, asset_name="", track_id=track_id
        )
        assert routine_door_visit([_visit(), stranger], GOOD) is None, track_id


def test_a_dog_alongside_is_not_a_second_person() -> None:
    dog = _event(
        SecurityEventType.SUBJECT_PRESENT,
        asset_name="",
        subject_label="dog",
        track_id=2,
    )
    visit = _visit()
    assert routine_door_visit([visit, dog], GOOD) is visit


@pytest.mark.parametrize(
    ("score", "explained"),
    [(0.0, False), (0.39, False), (0.4, True), (0.55, True), (1.0, True)],
)
def test_weak_evidence_explains_nothing(score: float, explained: bool) -> None:
    """Every claim is that something did not happen, and a dark, tiny, barely
    tracked figure supports none of them."""
    visit = _visit()
    result = routine_door_visit([visit], EvidenceQuality(score=score))
    assert (result is visit) is explained


# ----------------------------------------------------------------------
# door_use_allowed, flag_can_be_cleared
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anomaly", "confirmed", "expected"),
    [
        # Nobody identified: only at an hour the camera knows.
        (0.0, False, True),
        (0.34, False, True),
        (ROUTINE_VISIT_MAX_ANOMALY, False, False),
        (0.5, False, False),
        (1.0, False, False),
        # A household member matched: the face bypass has never cared when
        # someone comes home, so neither does this.
        (0.0, True, True),
        (0.5, True, True),
        (1.0, True, True),
    ],
)
def test_which_hours_a_visit_may_be_weighed_at(
    anomaly: float, confirmed: bool, expected: bool
) -> None:
    assert door_use_allowed(anomaly, identity_confirmed=confirmed) is expected


@pytest.mark.parametrize(
    ("confidence", "confirmed", "expected"),
    [
        # Nobody identified: the model plainly unsure.
        (0.5, False, True),
        (0.69, False, True),
        (ROUTINE_VISIT_MAX_CONFIDENCE, False, False),
        (0.9, False, False),
        # A household member matched: a surer model is allowed...
        (0.8, True, True),
        (0.84, True, True),
        # ...but never one as sure as the prompt asks it to be of the plainest
        # cases.
        (ROUTINE_VISIT_MAX_CONFIDENCE_CONFIRMED, True, False),
        (1.0, True, False),
    ],
)
def test_how_unsure_the_model_must_have_been(
    confidence: float, confirmed: bool, expected: bool
) -> None:
    assert flag_can_be_cleared(confidence, identity_confirmed=confirmed) is expected


def test_a_confident_finding_is_never_cleared_whoever_it_was() -> None:
    """A household member being recognized widens what counts as unsure, but
    never to a near-certain finding: that does not turn a sure finding of
    forcing a lock into a hedge."""
    assert ROUTINE_VISIT_MAX_CONFIDENCE < ROUTINE_VISIT_MAX_CONFIDENCE_CONFIRMED < 1.0
    assert ROUTINE_VISIT_MAX_CONFIDENCE_CONFIRMED <= 0.9


# ----------------------------------------------------------------------
# Through the detector, the scorer and the prompt
# ----------------------------------------------------------------------


def _person(x: float) -> Box:
    return (x, 150.0, x + 60.0, 300.0)


def _measurements(
    xs: list[float], *, detect_door_use: bool = True, frame_count: int = 8
) -> ClipMeasurements:
    door = build_marked_asset("Porch", "door1", "Front door", "door", DOOR_ZONE, FRAME)
    assert door is not None
    detections = [("person", 0.9, _person(x), 1, i) for i, x in enumerate(xs)]
    return ClipMeasurements(
        camera="Porch",
        tracks=build_tracks(detections, 2.0, FRAME),
        frame_interval=2.0,
        frame_count=frame_count,
        frames_analyzed=5,
        target_frames=5,
        marked_assets=[door],
        detect_door_use=detect_door_use,
    )


_GOING_IN = [400.0, 300.0, 200.0, 110.0, 90.0]


def test_a_real_visit_is_explained_and_reaches_the_prompt() -> None:
    outcome = assess_clip(_measurements(_GOING_IN))
    assert routine_door_visit(outcome.events, outcome.evidence) is not None
    prompt = "".join(outcome.prompt_segments)
    assert _NOTE in prompt
    assert "resident with their back to the camera" in prompt
    # It claims no more than tracking found. Frames seconds apart cannot tell
    # going through the door from trying it and walking off between two of
    # them, so that is left to the model -- and trying the door and leaving is
    # named as something that still counts.
    assert "went through the door" not in prompt
    assert "cannot tell going through the door from trying it" in prompt
    assert "the door being tried and then left" in prompt
    # It says what else would still count, and asks for no guess when none of
    # it is there: a model unsure of a figure with their back to the lens
    # hedges with a low-confidence "suspicious", which is what produces the
    # false alarms this is for.
    assert "worked at repeatedly or for a long time" in prompt
    assert "rather than a low-confidence guess" in prompt


def test_with_the_option_off_nothing_changes_for_the_same_clip() -> None:
    on = assess_clip(_measurements(_GOING_IN))
    off = assess_clip(_measurements(_GOING_IN, detect_door_use=False))

    assert "door_used" in {str(e.event_type) for e in on.events}
    assert "door_used" not in {str(e.event_type) for e in off.events}
    assert routine_door_visit(off.events, off.evidence) is None
    assert _NOTE not in "".join(off.prompt_segments)
    # The event is worth nothing, so the score cannot have moved.
    assert on.risk_score == pytest.approx(off.risk_score)
    assert [e.event_type for e in on.events if e.event_type != "door_used"] == [
        e.event_type for e in off.events
    ]


def test_the_visit_event_is_worth_no_points() -> None:
    events = [_event(SecurityEventType.SUBJECT_PRESENT, asset_name="")]
    with_visit = [*events, _visit()]
    scorer = RiskScorer()
    assert scorer.score(with_visit, 0.8).score == pytest.approx(
        scorer.score(events, 0.8).score
    )
    assert [f.name for f in scorer.score(with_visit, 0.8).factors] == [
        "subject_present"
    ]


def test_a_lingering_visit_is_not_explained_and_gets_no_note() -> None:
    outcome = assess_clip(_measurements([400.0, 300.0] + [110.0] * 7, frame_count=14))
    assert routine_door_visit(outcome.events, outcome.evidence) is None
    assert _NOTE not in "".join(outcome.prompt_segments)


def test_the_note_is_added_only_for_a_visit_that_explains_the_clip() -> None:
    outcome = assess_clip(_measurements(_GOING_IN))
    # Same events, but evidence too weak to rest a "did not happen" on.
    weak = build_security_segment(outcome.assessment, EvidenceQuality(score=0.2))
    assert weak is not None
    assert _NOTE not in weak
    strong = build_security_segment(outcome.assessment, EvidenceQuality(score=0.8))
    assert strong is not None
    assert _NOTE in strong
