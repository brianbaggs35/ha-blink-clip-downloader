"""Tests for blink_downloader.security.detector."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from blink_downloader.security.assets import AssetLocation, AssetType, ProtectedAsset
from blink_downloader.security.detector import (
    DetectionContext,
    DetectorThresholds,
    SecurityEventDetector,
    _article,
    _feet_phrase,
    _round_evidence,
)
from blink_downloader.security.events import SecurityEventType, Severity
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.tracks import ObjectTrack, TrackPoint
from blink_downloader.security.vehicles import VehicleCandidate, VehicleIdentification

FRAME = (640.0, 360.0)
#: 160px wide, so one foot is 160/6 ≈ 26.7px of gap.
CAR: Box = (300.0, 180.0, 460.0, 280.0)
ZONE = Zone.from_config({"x_min": 0.45, "y_min": 0.48, "x_max": 0.74, "y_max": 0.8})


def _asset(
    *,
    location: AssetLocation = AssetLocation.DETECTED,
    box: Box | None = CAR,
    zone: Zone | None = None,
    confident: bool = True,
) -> ProtectedAsset:
    identification = VehicleIdentification(
        protected=VehicleCandidate(
            track_id=99, label="car", box=CAR, normalized_box=(0, 0, 1, 1)
        ),
        confidence=0.9 if confident else 0.2,
    )
    return ProtectedAsset(
        name="blue sedan",
        asset_type=AssetType.VEHICLE,
        camera="Driveway",
        description="blue sedan",
        zone=zone,
        box=box,
        location=location,
        identification=identification,
    )


def _track(
    boxes: Sequence[Box],
    *,
    label: str = "person",
    track_id: int | None = 1,
    interval: float = 2.0,
    confidence: float = 0.9,
    frames: list[int] | None = None,
    tracked: bool = True,
) -> ObjectTrack:
    indices = frames if frames is not None else list(range(len(boxes)))
    return ObjectTrack(
        label=label,
        track_id=track_id,
        points=[
            TrackPoint(i, i * interval, b, confidence) for i, b in zip(indices, boxes)
        ],
        frame_size=FRAME,
        tracked=tracked,
    )


def _ctx(tracks: list[ObjectTrack], **kwargs) -> DetectionContext:
    kwargs.setdefault(
        "frame_count", max((t.points[-1].frame_index for t in tracks), default=0) + 1
    )
    return DetectionContext(
        camera="Driveway", tracks=tracks, frame_interval=2.0, **kwargs
    )


def _types(events) -> set[SecurityEventType]:
    return {e.event_type for e in events}


def _of(events, event_type: SecurityEventType):
    return next(e for e in events if e.event_type is event_type)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def test_article() -> None:
    assert _article("person") == "A"
    assert _article("otter") == "An"
    assert _article("") == "A"


def test_feet_phrase() -> None:
    assert _feet_phrase(0.4) == "under a foot"
    assert _feet_phrase(2.4) == "about 2 ft"


def test_round_evidence_leaves_non_floats_alone() -> None:
    assert _round_evidence({"a": 1.23456, "b": "x", "c": 3, "d": True}) == {
        "a": 1.235,
        "b": "x",
        "c": 3,
        "d": True,
    }


def test_timeline_end_of_an_empty_clip() -> None:
    assert (
        DetectionContext(
            camera="c", tracks=[], frame_interval=2.0, frame_count=0
        ).timeline_end
        == 0.0
    )


# ----------------------------------------------------------------------
# subject events
# ----------------------------------------------------------------------


def test_presence_event_is_emitted_per_subject_and_is_routine() -> None:
    events = SecurityEventDetector().detect(
        _ctx(
            [
                _track([(0, 0, 20, 60)] * 3),
                _track([(0, 0, 20, 60)] * 3, label="dog", track_id=2),
            ]
        )
    )
    presence = [e for e in events if e.event_type is SecurityEventType.SUBJECT_PRESENT]
    assert len(presence) == 2
    assert all(e.severity is Severity.ROUTINE for e in presence)
    assert _of(events, SecurityEventType.SUBJECT_PRESENT).evidence["frames_seen"] == 3


def test_vehicles_do_not_produce_presence_events() -> None:
    events = SecurityEventDetector().detect(
        _ctx([_track([CAR] * 3, label="car", track_id=5)])
    )
    assert events == []


def test_multiple_tracked_people_produce_a_multiple_subjects_event() -> None:
    events = SecurityEventDetector().detect(
        _ctx(
            [
                _track([(0, 0, 20, 60)] * 3, track_id=1),
                _track([(100, 0, 120, 60)] * 3, track_id=2),
            ]
        )
    )
    assert SecurityEventType.MULTIPLE_SUBJECTS in _types(events)
    assert (
        _of(events, SecurityEventType.MULTIPLE_SUBJECTS).evidence["person_count"] == 2
    )


def test_untracked_people_cannot_be_counted_as_several() -> None:
    """Without tracking ids there is no way to know whether two sightings
    are two people or one person twice."""
    events = SecurityEventDetector().detect(
        _ctx([_track([(0, 0, 20, 60)] * 3, track_id=None, tracked=False)])
    )
    assert SecurityEventType.MULTIPLE_SUBJECTS not in _types(events)


def test_open_ground_loitering_needs_long_presence_and_little_travel() -> None:
    still = _track([(0, 0, 20, 60)] * 10)
    events = SecurityEventDetector().detect(_ctx([still]))
    loiter = _of(events, SecurityEventType.LOITERING)
    assert loiter.severity is Severity.NOTEWORTHY
    assert loiter.evidence["dwell_seconds"] == pytest.approx(18.0)


def test_a_long_walk_across_the_frame_is_not_loitering() -> None:
    walking = _track([(x, 0, x + 20, 60) for x in range(0, 600, 60)])
    assert SecurityEventType.LOITERING not in _types(
        SecurityEventDetector().detect(_ctx([walking]))
    )


def test_a_brief_visit_is_not_loitering() -> None:
    assert SecurityEventType.LOITERING not in _types(
        SecurityEventDetector().detect(_ctx([_track([(0, 0, 20, 60)] * 3)]))
    )


def test_events_are_ordered_earliest_then_most_severe() -> None:
    events = SecurityEventDetector().detect(_ctx([_track([(0, 0, 20, 60)] * 10)]))
    offsets = [e.start_offset for e in events]
    assert offsets == sorted(offsets)


# ----------------------------------------------------------------------
# asset gating
# ----------------------------------------------------------------------


def test_no_asset_means_no_asset_events() -> None:
    events = SecurityEventDetector().detect(
        _ctx([_track([CAR] + [(300, 180, 460, 280)])])
    )
    assert not (_types(events) & {SecurityEventType.ASSET_PROXIMITY})


def test_an_unlocated_asset_produces_no_asset_events() -> None:
    events = SecurityEventDetector().detect(
        _ctx(
            [_track([(310, 190, 340, 270)] * 3)],
            asset=_asset(box=None, location=AssetLocation.UNKNOWN),
        )
    )
    assert SecurityEventType.ASSET_PROXIMITY not in _types(events)


def test_an_asset_with_no_subjects_produces_no_asset_events() -> None:
    events = SecurityEventDetector().detect(
        _ctx([_track([CAR] * 3, label="car", track_id=7)], asset=_asset())
    )
    assert events == []


# ----------------------------------------------------------------------
# proximity / approach / retreat
# ----------------------------------------------------------------------


def test_close_proximity_is_suspicious_and_reports_feet() -> None:
    near_car = _track([(470, 200, 490, 280)] * 3)  # 10px ≈ 0.4ft from the car
    events = SecurityEventDetector().detect(_ctx([near_car], asset=_asset()))
    proximity = _of(events, SecurityEventType.ASSET_PROXIMITY)
    assert proximity.severity is Severity.SUSPICIOUS
    assert proximity.evidence["min_gap_feet"] == pytest.approx(0.75)
    assert "under a foot" in proximity.detail


def test_moderate_proximity_is_only_noteworthy() -> None:
    two_feet = _track([(515, 200, 535, 280)] * 3)  # 55px ≈ 2ft
    proximity = _of(
        SecurityEventDetector().detect(_ctx([two_feet], asset=_asset())),
        SecurityEventType.ASSET_PROXIMITY,
    )
    assert proximity.severity is Severity.NOTEWORTHY


def test_distant_subjects_produce_no_proximity_event() -> None:
    far = _track([(0, 200, 20, 280)] * 3)
    assert SecurityEventType.ASSET_PROXIMITY not in _types(
        SecurityEventDetector().detect(_ctx([far], asset=_asset()))
    )


def test_proximity_against_a_zone_fallback_says_it_is_a_usual_position() -> None:
    near_car = _track([(470, 200, 490, 280)] * 3)
    events = SecurityEventDetector().detect(
        _ctx([near_car], asset=_asset(location=AssetLocation.ZONE))
    )
    proximity = _of(events, SecurityEventType.ASSET_PROXIMITY)
    assert "where it normally sits" in proximity.detail
    assert proximity.evidence["asset_detected"] is False


def test_approach_requires_ending_up_near_the_asset() -> None:
    approaching = _track(
        [(0, 200, 20, 280), (200, 200, 220, 280), (470, 200, 490, 280)]
    )
    assert SecurityEventType.ASSET_APPROACHED in _types(
        SecurityEventDetector().detect(_ctx([approaching], asset=_asset()))
    )


def test_walking_nearer_but_still_far_away_is_not_an_approach() -> None:
    """Crossing most of the frame closes a lot of distance without ever
    arriving anywhere — that is a passer-by, not an approach."""
    small_car: Box = (300.0, 180.0, 340.0, 280.0)  # 40px wide, so 8ft ≈ 53px
    pedestrian = _track([(0, 0, 20, 20), (100, 150, 120, 170), (200, 300, 220, 340)])
    assert SecurityEventType.ASSET_APPROACHED not in _types(
        SecurityEventDetector().detect(_ctx([pedestrian], asset=_asset(box=small_car)))
    )


def test_standing_still_is_not_an_approach() -> None:
    assert SecurityEventType.ASSET_APPROACHED not in _types(
        SecurityEventDetector().detect(
            _ctx([_track([(470, 200, 490, 280)] * 3)], asset=_asset())
        )
    )


def test_untracked_approach_has_lower_confidence() -> None:
    boxes = [(0, 200, 20, 280), (200, 200, 220, 280), (470, 200, 490, 280)]
    tracked = _of(
        SecurityEventDetector().detect(_ctx([_track(boxes)], asset=_asset())),
        SecurityEventType.ASSET_APPROACHED,
    )
    untracked = _of(
        SecurityEventDetector().detect(
            _ctx([_track(boxes, track_id=None, tracked=False)], asset=_asset())
        ),
        SecurityEventType.ASSET_APPROACHED,
    )
    assert untracked.confidence < tracked.confidence


def test_coming_close_and_leaving_is_a_retreat() -> None:
    visitor = _track([(470, 200, 490, 280), (475, 200, 495, 280), (10, 200, 30, 280)])
    events = SecurityEventDetector().detect(_ctx([visitor], asset=_asset()))
    retreat = _of(events, SecurityEventType.RETREAT)
    assert retreat.severity is Severity.NOTEWORTHY
    assert SecurityEventType.RETREAT_AFTER_CONTACT not in _types(events)


def test_lingering_at_the_asset_is_suspicious_sooner_than_open_ground() -> None:
    lingering = _track([(470, 200, 490, 280)] * 8)
    loiter = _of(
        SecurityEventDetector().detect(_ctx([lingering], asset=_asset())),
        SecurityEventType.LOITERING,
    )
    assert loiter.severity is Severity.SUSPICIOUS
    assert loiter.evidence["near_asset"] is True


def test_lingering_is_scored_once_not_as_both_kinds_of_loitering() -> None:
    """Standing at the vehicle for a while satisfies the open-ground rule
    too, and the scorer adds up every event it is handed — so counting both
    would score one loiterer twice at full weight."""
    lingering = _track([(470, 200, 490, 280)] * 10)
    events = SecurityEventDetector().detect(_ctx([lingering], asset=_asset()))
    loiters = [e for e in events if e.event_type is SecurityEventType.LOITERING]
    assert len(loiters) == 1
    assert loiters[0].severity is Severity.SUSPICIOUS


def test_two_untracked_loiterers_are_still_counted_separately() -> None:
    """The de-duplication above keys on the observed span as well as the
    track id, because every pseudo-track assembled without a real tracker
    carries ``None`` — keying on the id alone would merge two people."""
    first = _track([(470, 200, 490, 280)] * 10, track_id=None, tracked=False)
    second = _track(
        [(470, 200, 490, 280)] * 10,
        track_id=None,
        tracked=False,
        frames=list(range(2, 12)),
    )
    events = SecurityEventDetector().detect(_ctx([first, second], asset=_asset()))
    assert len([e for e in events if e.event_type is SecurityEventType.LOITERING]) == 2


def test_lingering_where_the_vehicle_is_absent_says_so() -> None:
    """The pipeline has concluded the car is not in frame; saying somebody
    "remained at the blue sedan" contradicts the evidence, and that text
    reaches both the AI prompt and the Security tab."""
    zone = Zone.from_config(
        {"x_min": 0.45, "y_min": 0.48, "x_max": 0.90, "y_max": 0.85}
    )
    lingering = _track([(470, 200, 490, 280)] * 8)
    loiter = _of(
        SecurityEventDetector().detect(
            _ctx(
                [lingering],
                asset=_asset(location=AssetLocation.ZONE_ABSENT, box=CAR, zone=zone),
            )
        ),
        SecurityEventType.LOITERING,
    )
    assert "the space where blue sedan normally sits" in loiter.detail


def test_asset_loitering_is_skipped_when_neither_near_nor_in_zone() -> None:
    far = _track([(0, 200, 20, 280)] * 6)
    assert SecurityEventType.LOITERING not in _types(
        SecurityEventDetector().detect(_ctx([far], asset=_asset()))
    )


def test_asset_loitering_is_skipped_below_the_dwell_threshold() -> None:
    brief = _track([(470, 200, 490, 280)] * 3)
    assert SecurityEventType.LOITERING not in _types(
        SecurityEventDetector().detect(_ctx([brief], asset=_asset()))
    )


# ----------------------------------------------------------------------
# zones
# ----------------------------------------------------------------------


def test_zone_entry_is_detected_from_the_subjects_feet() -> None:
    entering = _track([(0, 200, 20, 280), (330, 200, 350, 270)])
    events = SecurityEventDetector().detect(_ctx([entering], asset=_asset(zone=ZONE)))
    zone_event = _of(events, SecurityEventType.ZONE_ENTERED)
    assert zone_event.severity is Severity.NOTEWORTHY
    assert zone_event.evidence["frames_in_zone"] == 1
    assert "and stayed" not in zone_event.detail


def test_a_longer_stay_in_the_zone_is_reported_with_its_duration() -> None:
    staying = _track([(0, 200, 20, 280)] + [(330, 200, 350, 270)] * 3)
    zone_event = _of(
        SecurityEventDetector().detect(_ctx([staying], asset=_asset(zone=ZONE))),
        SecurityEventType.ZONE_ENTERED,
    )
    assert "and stayed at least 4s" in zone_event.detail


def test_someone_already_inside_the_zone_is_still_reported() -> None:
    """A clip starts when motion is detected, so on a camera pointed at a
    parking space the subject is routinely already at the vehicle in the
    first sampled frame. Requiring an observed outside-then-inside crossing
    produced no zone event at all for exactly those clips — the wording,
    not the event, is what has to stay honest about what was seen."""
    inside = _track([(330, 200, 350, 270)] * 3)
    zone_event = _of(
        SecurityEventDetector().detect(_ctx([inside], asset=_asset(zone=ZONE))),
        SecurityEventType.ZONE_ENTERED,
    )
    assert "already inside the area" in zone_event.detail
    assert "crossed into" not in zone_event.detail
    assert zone_event.evidence["crossed_in"] is False


def test_an_observed_crossing_says_so() -> None:
    entering = _track([(0, 200, 20, 280), (330, 200, 350, 270)])
    zone_event = _of(
        SecurityEventDetector().detect(_ctx([entering], asset=_asset(zone=ZONE))),
        SecurityEventType.ZONE_ENTERED,
    )
    assert "crossed into the area" in zone_event.detail
    assert zone_event.evidence["crossed_in"] is True


def test_no_zone_configured_means_no_zone_event() -> None:
    entering = _track([(0, 200, 20, 280), (330, 200, 350, 270)])
    assert SecurityEventType.ZONE_ENTERED not in _types(
        SecurityEventDetector().detect(_ctx([entering], asset=_asset()))
    )


def test_untracked_zone_entry_has_lower_confidence() -> None:
    boxes = [(0, 200, 20, 280), (330, 200, 350, 270)]
    tracked = _of(
        SecurityEventDetector().detect(_ctx([_track(boxes)], asset=_asset(zone=ZONE))),
        SecurityEventType.ZONE_ENTERED,
    )
    untracked = _of(
        SecurityEventDetector().detect(
            _ctx([_track(boxes, track_id=None, tracked=False)], asset=_asset(zone=ZONE))
        ),
        SecurityEventType.ZONE_ENTERED,
    )
    assert untracked.confidence < tracked.confidence


def test_zone_dwell_is_used_for_loitering_when_the_subject_is_in_the_zone() -> None:
    in_zone = _track([(330, 200, 350, 270)] * 8)
    loiter = _of(
        SecurityEventDetector().detect(
            _ctx([in_zone], asset=_asset(zone=ZONE, location=AssetLocation.ZONE_ABSENT))
        ),
        SecurityEventType.LOITERING,
    )
    assert loiter.evidence["in_zone"] is True


# ----------------------------------------------------------------------
# contact / impact
# ----------------------------------------------------------------------


def _overlapping() -> ObjectTrack:
    return _track([(0, 200, 20, 280), (350, 200, 390, 280), (352, 200, 392, 280)])


def test_segmentation_contact_outranks_everything_else() -> None:
    apart = _track([(500, 200, 520, 280)] * 3)
    contact = _of(
        SecurityEventDetector().detect(
            _ctx([apart], asset=_asset(), contact_touching=True)
        ),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    assert contact.confidence == pytest.approx(0.8)
    assert "segmentation" in contact.detail


def test_depth_overrules_segmentation_when_the_two_disagree() -> None:
    """A person walking *in front of* the car: their silhouettes genuinely
    abut in the image, so segmentation reports a touch, while depth places
    them metres apart. Asserting contact on evidence this layer's own
    stages contradict is the false positive that makes people switch a
    security system off."""
    passing_by = _track([(340, 200, 380, 280)] * 3)
    events = SecurityEventDetector().detect(
        _ctx(
            [passing_by],
            asset=_asset(),
            contact_touching=True,
            depth_similar=False,
        )
    )
    assert SecurityEventType.CONTACT_CANDIDATE not in _types(events)
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(events)


def test_segmentation_still_wins_when_depth_has_nothing_to_say() -> None:
    """Depth unavailable (no torch, or the stage failed) must not weaken
    segmentation — that is the pre-existing behaviour and the reason the
    veto is written against an explicit False rather than a falsy value."""
    apart = _track([(500, 200, 520, 280)] * 3)
    contact = _of(
        SecurityEventDetector().detect(
            _ctx([apart], asset=_asset(), contact_touching=True, depth_similar=None)
        ),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    assert contact.confidence == pytest.approx(0.8)


def test_segmentation_and_depth_agreeing_is_the_strongest_contact() -> None:
    apart = _track([(500, 200, 520, 280)] * 3)
    contact = _of(
        SecurityEventDetector().detect(
            _ctx([apart], asset=_asset(), contact_touching=True, depth_similar=True)
        ),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    assert contact.confidence == pytest.approx(0.8)
    assert "segmentation" in contact.detail


def test_overlap_with_matching_depth_is_stronger_than_overlap_alone() -> None:
    with_depth = _of(
        SecurityEventDetector().detect(
            _ctx([_overlapping()], asset=_asset(), depth_similar=True)
        ),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    bare = _of(
        SecurityEventDetector().detect(_ctx([_overlapping()], asset=_asset())),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    assert with_depth.confidence > bare.confidence
    assert "no depth or segmentation evidence" in bare.detail


def test_different_depths_rule_out_contact_entirely() -> None:
    assert SecurityEventType.CONTACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(
            _ctx([_overlapping()], asset=_asset(), depth_similar=False)
        )
    )


def test_segmentation_saying_not_touching_rules_out_contact() -> None:
    assert SecurityEventType.CONTACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(
            _ctx([_overlapping()], asset=_asset(), contact_touching=False)
        )
    )


def test_no_overlap_and_no_segmentation_means_no_contact() -> None:
    apart = _track([(500, 200, 520, 280)] * 3)
    assert SecurityEventType.CONTACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(_ctx([apart], asset=_asset()))
    )


def test_depth_and_contact_verdicts_only_apply_to_the_track_they_examined() -> None:
    """Those stages look at one subject/asset pair; applying their verdict
    to a different person in the same clip would attribute evidence to the
    wrong subject."""
    examined = _overlapping()
    other = _track([(0, 200, 20, 280)] * 3, track_id=2)
    events = SecurityEventDetector().detect(
        _ctx(
            [examined, other],
            asset=_asset(),
            contact_touching=True,
            contact_track_id=1,
        )
    )
    contacts = [
        e for e in events if e.event_type is SecurityEventType.CONTACT_CANDIDATE
    ]
    assert len(contacts) == 1
    assert contacts[0].track_id == 1


def test_without_a_named_track_the_closest_subject_gets_the_verdict() -> None:
    close = _track([(470, 200, 490, 280)] * 3, track_id=1)
    far = _track([(0, 200, 20, 280)] * 3, track_id=2)
    events = SecurityEventDetector().detect(
        _ctx([close, far], asset=_asset(), contact_touching=True)
    )
    contacts = [
        e for e in events if e.event_type is SecurityEventType.CONTACT_CANDIDATE
    ]
    assert [c.track_id for c in contacts] == [1]


def test_an_animal_touching_the_car_is_reported_as_an_animal_interaction() -> None:
    dog = _track(
        [(0, 200, 20, 280), (350, 240, 390, 280), (352, 240, 392, 280)], label="dog"
    )
    events = SecurityEventDetector().detect(
        _ctx([dog], asset=_asset(), depth_similar=True)
    )
    assert SecurityEventType.ANIMAL_ASSET_INTERACTION in _types(events)
    assert SecurityEventType.CONTACT_CANDIDATE not in _types(events)
    assert (
        _of(events, SecurityEventType.ANIMAL_ASSET_INTERACTION).severity
        is Severity.NOTEWORTHY
    )


def test_an_animal_never_produces_an_impact_candidate() -> None:
    dog = _track(
        [(0, 200, 20, 280), (350, 240, 390, 280), (10, 240, 50, 280)], label="dog"
    )
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(
            _ctx([dog], asset=_asset(), depth_similar=True, appearance_change=0.9)
        )
    )


def test_rushing_at_the_asset_during_contact_is_an_impact_candidate() -> None:
    lurching = _track([(0, 200, 20, 280), (20, 200, 40, 280), (350, 200, 390, 280)])
    impact = _of(
        SecurityEventDetector().detect(
            _ctx([lurching], asset=_asset(), depth_similar=True)
        ),
        SecurityEventType.IMPACT_CANDIDATE,
    )
    assert impact.severity is Severity.CRITICAL
    assert "accelerated sharply" in impact.detail


def test_walking_up_to_a_car_and_stopping_is_never_an_impact() -> None:
    """The single most common thing anyone does near a vehicle — it must
    not produce the one critical event type this detector can emit."""
    arriving = _track([(0, 200, 20, 280), (350, 200, 390, 280), (352, 200, 392, 280)])
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(
            _ctx([arriving], asset=_asset(), depth_similar=True)
        )
    )


def test_a_movement_spike_over_bare_overlap_alone_is_not_an_impact() -> None:
    """Without depth or segmentation backing, an overlap is just a person
    somewhere behind the car."""
    lurching = _track([(0, 200, 20, 280), (20, 200, 40, 280), (350, 200, 390, 280)])
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(_ctx([lurching], asset=_asset()))
    )


def test_contact_plus_a_changed_asset_region_is_an_impact_candidate() -> None:
    impact = _of(
        SecurityEventDetector().detect(
            _ctx(
                [_overlapping()],
                asset=_asset(),
                depth_similar=True,
                appearance_change=0.6,
            )
        ),
        SecurityEventType.IMPACT_CANDIDATE,
    )
    assert "looked different afterwards" in impact.detail
    assert impact.evidence["appearance_change"] == pytest.approx(0.6)


def test_a_changed_asset_region_alone_cannot_manufacture_an_impact() -> None:
    """The one CRITICAL event this detector emits forces an alert past the AI
    model's own verdict and withholds the face-recognition bypass, so it has
    to rest on a contact something actually confirmed. A box overlap plus "the
    car's region looks different" is a passer-by crossing in front of a car
    whose door was opened, or on which snow settled — and on a device where
    depth and segmentation are unavailable that is all the evidence there is.
    """
    events = SecurityEventDetector().detect(
        _ctx([_overlapping()], asset=_asset(), appearance_change=0.9)
    )
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(events)
    # The contact itself is still reported, just not as a confirmed strike.
    contact = _of(events, SecurityEventType.CONTACT_CANDIDATE)
    assert contact.severity is Severity.NOTEWORTHY


def test_an_unconfirmed_overlap_is_noteworthy_not_suspicious() -> None:
    contact = _of(
        SecurityEventDetector().detect(_ctx([_overlapping()], asset=_asset())),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    assert contact.severity is Severity.NOTEWORTHY
    assert "no depth or segmentation evidence" in contact.detail


def test_depth_confirmed_contact_is_suspicious() -> None:
    """What the advanced stages buy: the same geometry, believed."""
    contact = _of(
        SecurityEventDetector().detect(
            _ctx([_overlapping()], asset=_asset(), depth_similar=True)
        ),
        SecurityEventType.CONTACT_CANDIDATE,
    )
    assert contact.severity is Severity.SUSPICIOUS


def test_retreat_after_an_unconfirmed_contact_stays_noteworthy() -> None:
    touch_and_go = _track(
        [(340, 200, 380, 280), (350, 200, 390, 280), (10, 200, 50, 280)]
    )
    retreat = _of(
        SecurityEventDetector().detect(_ctx([touch_and_go], asset=_asset())),
        SecurityEventType.RETREAT_AFTER_CONTACT,
    )
    assert retreat.severity is Severity.NOTEWORTHY


def test_steady_contact_with_no_change_is_not_an_impact() -> None:
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(
            _ctx(
                [_overlapping()],
                asset=_asset(),
                depth_similar=True,
                appearance_change=0.01,
            )
        )
    )


def test_leaving_after_contact_is_reported_separately() -> None:
    touch_and_go = _track(
        [(340, 200, 380, 280), (350, 200, 390, 280), (10, 200, 50, 280)]
    )
    events = SecurityEventDetector().detect(
        _ctx([touch_and_go], asset=_asset(), depth_similar=True)
    )
    assert SecurityEventType.RETREAT_AFTER_CONTACT in _types(events)
    assert SecurityEventType.RETREAT not in _types(events)


def test_staying_after_contact_is_not_a_retreat() -> None:
    assert SecurityEventType.RETREAT_AFTER_CONTACT not in _types(
        SecurityEventDetector().detect(
            _ctx([_overlapping()], asset=_asset(), depth_similar=True)
        )
    )


# ----------------------------------------------------------------------
# an absent protected vehicle
# ----------------------------------------------------------------------


def test_physical_rules_are_suppressed_when_the_asset_has_been_driven_away() -> None:
    """The apartment-car-park case: someone at a neighbour's car parked in
    the vacated space must not read as touching the protected vehicle."""
    in_the_space = _track(
        [(0, 200, 20, 280), (330, 200, 350, 270), (335, 200, 355, 270)]
    )
    events = SecurityEventDetector().detect(
        _ctx(
            [in_the_space],
            asset=_asset(location=AssetLocation.ZONE_ABSENT, zone=ZONE),
            depth_similar=True,
        )
    )
    assert _types(events) == {
        SecurityEventType.SUBJECT_PRESENT,
        SecurityEventType.ZONE_ENTERED,
    }


def test_an_unconfident_identification_damps_every_asset_event() -> None:
    near = _track([(470, 200, 490, 280)] * 3)
    confident = _of(
        SecurityEventDetector().detect(_ctx([near], asset=_asset())),
        SecurityEventType.ASSET_PROXIMITY,
    )
    unsure = _of(
        SecurityEventDetector().detect(_ctx([near], asset=_asset(confident=False))),
        SecurityEventType.ASSET_PROXIMITY,
    )
    assert unsure.confidence < confident.confidence
    # Non-asset events keep their own confidence.
    presence = _of(
        SecurityEventDetector().detect(_ctx([near], asset=_asset(confident=False))),
        SecurityEventType.SUBJECT_PRESENT,
    )
    assert presence.confidence == pytest.approx(0.9)


# ----------------------------------------------------------------------
# carryable objects
# ----------------------------------------------------------------------


def _person_all_clip() -> ObjectTrack:
    return _track([(0, 200, 20, 280)] * 6, track_id=1)


def test_a_bag_vanishing_while_a_person_is_present_is_a_possible_removal() -> None:
    bag = _track([(100, 300, 130, 330)] * 2, label="backpack", track_id=2)
    events = SecurityEventDetector().detect(_ctx([_person_all_clip(), bag]))
    removed = _of(events, SecurityEventType.OBJECT_REMOVED)
    assert removed.severity is Severity.SUSPICIOUS
    assert "may simply have lost sight of it" in removed.detail


def test_a_bag_appearing_late_is_a_routine_drop_off() -> None:
    bag = _track(
        [(100, 300, 130, 330)] * 2, label="suitcase", track_id=2, frames=[4, 5]
    )
    added = _of(
        SecurityEventDetector().detect(_ctx([_person_all_clip(), bag])),
        SecurityEventType.OBJECT_ADDED,
    )
    assert added.severity is Severity.ROUTINE


def test_a_bag_present_throughout_is_neither_added_nor_removed() -> None:
    bag = _track([(100, 300, 130, 330)] * 6, label="handbag", track_id=2)
    assert not (
        _types(SecurityEventDetector().detect(_ctx([_person_all_clip(), bag])))
        & {SecurityEventType.OBJECT_ADDED, SecurityEventType.OBJECT_REMOVED}
    )


def test_an_object_vanishing_with_nobody_around_is_not_reported() -> None:
    bag = _track([(100, 300, 130, 330)] * 2, label="backpack", track_id=2)
    assert SecurityEventType.OBJECT_REMOVED not in _types(
        SecurityEventDetector().detect(_ctx([bag], frame_count=6))
    )


def test_carryable_rules_need_enough_frames_to_have_a_before_and_after() -> None:
    bag = _track([(100, 300, 130, 330)], label="backpack", track_id=2)
    assert SecurityEventType.OBJECT_REMOVED not in _types(
        SecurityEventDetector().detect(
            _ctx([_track([(0, 200, 20, 280)]), bag], frame_count=2)
        )
    )


# ----------------------------------------------------------------------
# camera obstruction
# ----------------------------------------------------------------------


def test_a_drastically_changed_empty_scene_reads_as_possible_obstruction() -> None:
    events = SecurityEventDetector().detect(
        _ctx([], scene_deviation=0.8, frame_count=5)
    )
    obstruction = _of(events, SecurityEventType.CAMERA_OBSTRUCTION)
    assert obstruction.severity is Severity.SUSPICIOUS
    assert obstruction.confidence < 0.5
    assert "lighting or weather change produces the same signal" in obstruction.detail
    assert obstruction.end_offset == pytest.approx(8.0)


def test_a_changed_scene_with_something_in_it_is_not_obstruction() -> None:
    assert SecurityEventType.CAMERA_OBSTRUCTION not in _types(
        SecurityEventDetector().detect(
            _ctx([_track([(0, 200, 20, 280)] * 3)], scene_deviation=0.8)
        )
    )


def test_an_ordinary_scene_deviation_is_not_obstruction() -> None:
    assert SecurityEventType.CAMERA_OBSTRUCTION not in _types(
        SecurityEventDetector().detect(_ctx([], scene_deviation=0.2, frame_count=5))
    )


def test_no_scene_baseline_yet_means_no_obstruction_check() -> None:
    assert SecurityEventDetector().detect(_ctx([], frame_count=5)) == []


# ----------------------------------------------------------------------
# thresholds
# ----------------------------------------------------------------------


def test_custom_thresholds_are_honoured() -> None:
    lingering = _track([(470, 200, 490, 280)] * 3)
    strict = SecurityEventDetector(DetectorThresholds(loiter_seconds=1.0))
    assert SecurityEventType.LOITERING in _types(
        strict.detect(_ctx([lingering], asset=_asset()))
    )


def test_thresholds_default_to_the_prompts_own_distance_language() -> None:
    thresholds = DetectorThresholds()
    assert thresholds.close_feet == 1.0
    assert thresholds.near_feet == 3.0


# ----------------------------------------------------------------------
# depth-aware proximity
# ----------------------------------------------------------------------


def test_depth_confirms_a_subject_really_is_at_the_vehicle() -> None:
    near = _track([(470, 200, 490, 280)] * 3)
    plain = _of(
        SecurityEventDetector().detect(_ctx([near], asset=_asset())),
        SecurityEventType.ASSET_PROXIMITY,
    )
    confirmed = _of(
        SecurityEventDetector().detect(
            _ctx([near], asset=_asset(), depth_similar=True)
        ),
        SecurityEventType.ASSET_PROXIMITY,
    )
    assert confirmed.confidence > plain.confidence
    assert confirmed.evidence["similar_depth"] is True


def test_depth_rules_out_proximity_for_someone_at_a_different_distance() -> None:
    """The walks-past-the-car case: close in the projection, several feet
    away on the ground, and depth is the only thing that can tell."""
    near_in_frame = _track([(470, 200, 490, 280)] * 3)
    events = SecurityEventDetector().detect(
        _ctx([near_in_frame], asset=_asset(), depth_similar=False)
    )
    assert SecurityEventType.ASSET_PROXIMITY not in _types(events)
    assert SecurityEventType.RETREAT not in _types(events)


def test_depth_rules_out_zone_entry_for_a_detected_vehicle() -> None:
    entering = _track([(0, 200, 20, 280), (330, 200, 350, 270)])
    assert SecurityEventType.ZONE_ENTERED not in _types(
        SecurityEventDetector().detect(
            _ctx([entering], asset=_asset(zone=ZONE), depth_similar=False)
        )
    )


def test_depth_cannot_rule_out_zone_entry_when_the_vehicle_was_not_detected() -> None:
    """The depth comparison is against the vehicle; with no vehicle found
    there is nothing it can legitimately veto the zone with."""
    entering = _track([(0, 200, 20, 280), (330, 200, 350, 270)])
    assert SecurityEventType.ZONE_ENTERED in _types(
        SecurityEventDetector().detect(
            _ctx(
                [entering],
                asset=_asset(zone=ZONE, location=AssetLocation.ZONE),
                depth_similar=False,
            )
        )
    )


def test_a_depth_verdict_for_another_track_does_not_suppress_this_one() -> None:
    examined = _track([(470, 200, 490, 280)] * 3, track_id=1)
    other = _track([(495, 200, 515, 280)] * 3, track_id=2)
    events = SecurityEventDetector().detect(
        _ctx(
            [examined, other],
            asset=_asset(),
            depth_similar=False,
            contact_track_id=1,
        )
    )
    proximity = [e for e in events if e.event_type is SecurityEventType.ASSET_PROXIMITY]
    assert [e.track_id for e in proximity] == [2]


def test_a_degenerate_subject_box_can_never_establish_contact() -> None:
    """Defensive: a zero-height detection gives nothing to measure the
    overlap against, so it is treated as no evidence rather than as
    infinitely deep contact."""
    flat = _track([(350, 240, 390, 240)] * 3)
    assert SecurityEventType.CONTACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(_ctx([flat], asset=_asset()))
    )


# ----------------------------------------------------------------------
# posture evidence (pose estimation)
# ----------------------------------------------------------------------


def test_reaching_toward_the_asset_from_close_range_is_suspicious() -> None:
    """The one thing a bounding box is completely blind to: standing two
    feet from a car with your arms down, and standing there with an arm
    through the window, are the same box."""
    near = _track([(470, 200, 490, 280)] * 3)
    events = SecurityEventDetector().detect(
        _ctx([near], asset=_asset(), posture_reaching=True)
    )
    reach = _of(events, SecurityEventType.ASSET_REACH)
    assert reach.severity is Severity.SUSPICIOUS
    assert reach.evidence["crouching"] is False


def test_a_reach_from_across_the_driveway_is_not_reported() -> None:
    far = _track([(0, 200, 20, 280)] * 3)
    assert SecurityEventType.ASSET_REACH not in _types(
        SecurityEventDetector().detect(
            _ctx([far], asset=_asset(), posture_reaching=True)
        )
    )


def test_a_reach_is_attributed_only_to_the_examined_subject() -> None:
    examined = _track([(470, 200, 490, 280)] * 3, track_id=1)
    other = _track([(495, 200, 515, 280)] * 3, track_id=2)
    events = SecurityEventDetector().detect(
        _ctx(
            [examined, other],
            asset=_asset(),
            posture_reaching=True,
            contact_track_id=1,
        )
    )
    reaches = [e for e in events if e.event_type is SecurityEventType.ASSET_REACH]
    assert [e.track_id for e in reaches] == [1]


def test_a_crouched_reach_says_so() -> None:
    near = _track([(470, 200, 490, 280)] * 3)
    reach = _of(
        SecurityEventDetector().detect(
            _ctx([near], asset=_asset(), posture_reaching=True, posture_crouching=True)
        ),
        SecurityEventType.ASSET_REACH,
    )
    assert "crouched or bent over" in reach.detail
    assert reach.evidence["crouching"] is True


def test_a_raised_arm_during_confirmed_contact_is_an_impact_candidate() -> None:
    """Unlike the speed spike, a raised arm is visible in the single frame
    the pose stage examines, so it needs no corroborating motion signal."""
    arriving = _track([(0, 200, 20, 280), (350, 200, 390, 280), (352, 200, 392, 280)])
    impact = _of(
        SecurityEventDetector().detect(
            _ctx(
                [arriving],
                asset=_asset(),
                posture_arm_raised=True,
                contact_touching=True,
            )
        ),
        SecurityEventType.IMPACT_CANDIDATE,
    )
    assert impact.severity is Severity.CRITICAL
    assert "arm was raised above shoulder height" in impact.detail
    assert impact.evidence["arm_raised"] is True


def test_a_raised_arm_over_a_bare_overlap_is_not_an_impact_candidate() -> None:
    """The low-powered-device guarantee, in the one place it is easiest to
    lose: with no depth or segmentation stage, the only thing under the
    raised arm is a 2D overlap that anyone walking in front of a parked car
    produces — and a raised wrist is an everyday gesture (a phone held to an
    ear clears the threshold). Letting that reach CRITICAL would force an
    alert past the model's verdict and withhold the face-recognition bypass
    for somebody walking to their own car."""
    arriving = _track([(0, 200, 20, 280), (350, 200, 390, 280), (352, 200, 392, 280)])
    events = SecurityEventDetector().detect(
        _ctx([arriving], asset=_asset(), posture_arm_raised=True)
    )
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(events)
    # The contact itself is still reported — only its escalation is withheld.
    assert SecurityEventType.CONTACT_CANDIDATE in _types(events)


def test_a_raised_arm_with_no_contact_is_not_an_impact() -> None:
    apart = _track([(500, 200, 520, 280)] * 3)
    assert SecurityEventType.IMPACT_CANDIDATE not in _types(
        SecurityEventDetector().detect(
            _ctx([apart], asset=_asset(), posture_arm_raised=True)
        )
    )


def test_depth_rules_out_an_approach_for_someone_at_a_different_distance() -> None:
    """A passer-by closes a lot of 2D distance to a car they are nowhere
    near; "approached the vehicle" is as wrong for them as "stood at it"."""
    approaching = _track(
        [(0, 200, 20, 280), (200, 200, 220, 280), (470, 200, 490, 280)]
    )
    assert SecurityEventType.ASSET_APPROACHED not in _types(
        SecurityEventDetector().detect(
            _ctx([approaching], asset=_asset(), depth_similar=False)
        )
    )


def test_impact_is_never_attributed_to_a_subject_the_pose_stage_did_not_examine() -> (
    None
):
    """Both impact signals belong to one subject and one moment; giving a
    second person who merely overlapped the vehicle the same CRITICAL event
    would double-count it."""
    examined = _track(
        [(0, 200, 20, 280), (20, 200, 40, 280), (350, 200, 390, 280)], track_id=1
    )
    other = _track([(352, 200, 392, 280)] * 3, track_id=2)
    events = SecurityEventDetector().detect(
        _ctx(
            [examined, other],
            asset=_asset(),
            depth_similar=True,
            contact_track_id=1,
            posture_arm_raised=True,
            appearance_change=0.9,
        )
    )
    impacts = [e for e in events if e.event_type is SecurityEventType.IMPACT_CANDIDATE]
    assert [e.track_id for e in impacts] == [1]
