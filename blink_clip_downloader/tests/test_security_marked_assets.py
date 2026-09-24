"""Assets marked on the Assets tab, through the deterministic security layer.

Two questions, answered separately. The unit tests pin what each rule does
for a marked asset and which of an asset's properties switches it — the
vehicle-only impact rule, the routine-use exemption from contact, where the
depth/contact/pose verdict is allowed to land. The scenarios then run whole
clips through tracks → events → score and pin the conclusions that matter
to someone who marked their front door: a courier at it is never an alert,
a stranger walking off with the bike beside it always is.

Nothing here touches the protected vehicle's own behaviour, which
``test_security_scenarios.py`` pins; a camera with no marked assets runs
exactly the code it ran before assets existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from blink_downloader.security.assets import (
    AssetLocation,
    AssetType,
    ProtectedAsset,
    build_marked_asset,
    resolve_vehicle_asset,
)
from blink_downloader.security.detector import DetectionContext, SecurityEventDetector
from blink_downloader.security.events import (
    BYPASS_BLOCKING_EVENTS,
    SecurityEvent,
    SecurityEventType,
    Severity,
    severity_rank,
)
from blink_downloader.security.evidence import assess_evidence
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.pipeline import ClipMeasurements, assess_clip
from blink_downloader.security.scoring import RiskScorer, ScoringContext
from blink_downloader.security.tracks import build_tracks

FRAME = (640.0, 360.0)

#: A front door on the left of a porch camera's view: 70px wide, 180px tall.
DOOR_ZONE = Zone.from_config(
    {"x_min": 0.094, "y_min": 0.333, "x_max": 0.203, "y_max": 0.833}
)
#: The step in front of it, where parcels are left.
PARCEL_ZONE = Zone.from_config(
    {"x_min": 0.21, "y_min": 0.7, "x_max": 0.34, "y_max": 0.83}
)
#: A bike on its stand on the right of the frame, traced with the lasso.
BIKE_ZONE = Zone.from_config(
    {
        "shape": "polygon",
        "points": [[0.72, 0.55], [0.95, 0.55], [0.95, 0.86], [0.72, 0.86]],
    }
)
#: A car parked in the middle of the same view, for the tests about which
#: asset a depth/contact verdict belongs to.
CAR: Box = (300.0, 170.0, 450.0, 280.0)

Detection = tuple[str, float, Box, int | None, int]


def _walk(track_id: int, boxes: list[Box], label: str = "person") -> list[Detection]:
    return [(label, 0.9, box, track_id, i) for i, box in enumerate(boxes)]


def _person_at(x: float, *, height: float = 150.0, ground: float = 300.0) -> Box:
    return (x, ground - height, x + height * 0.4, ground)


def _door() -> ProtectedAsset:
    asset = build_marked_asset("Porch", "door1", "Front door", "door", DOOR_ZONE, FRAME)
    assert asset is not None
    return asset


def _parcels() -> ProtectedAsset:
    asset = build_marked_asset(
        "Porch", "parcel1", "Parcel spot", "package_area", PARCEL_ZONE, FRAME
    )
    assert asset is not None
    return asset


def _bike() -> ProtectedAsset:
    asset = build_marked_asset("Porch", "bike1", "Bike", "bicycle", BIKE_ZONE, FRAME)
    assert asset is not None
    return asset


def _detect(
    detections: list[Detection], frame_count: int, **kwargs: object
) -> list[SecurityEvent]:
    tracks = build_tracks(detections, 2.0, FRAME)
    return SecurityEventDetector().detect(
        DetectionContext(
            camera="Porch",
            tracks=tracks,
            frame_interval=2.0,
            frame_count=frame_count,
            **kwargs,  # type: ignore[arg-type]
        )
    )


def _types(events: list[SecurityEvent], asset_name: str | None = None) -> set[str]:
    return {
        str(e.event_type)
        for e in events
        if asset_name is None or e.asset_name == asset_name
    }


#: Someone walking up to the bike, standing at it, and going.
_TO_THE_BIKE = [_person_at(x, ground=305) for x in (380, 440, 470, 470, 470, 380)]
#: Someone walking up to the front door, standing at it, and going.
_TO_THE_DOOR = [_person_at(x, ground=300) for x in (320, 220, 130, 110, 110, 220)]


# ----------------------------------------------------------------------
# build_marked_asset
# ----------------------------------------------------------------------


def test_marked_asset_is_located_by_its_zone() -> None:
    asset = _door()
    assert asset.marked is True
    assert asset.key == "door1"
    assert asset.location is AssetLocation.ZONE
    assert asset.present is True
    assert asset.detected is False
    assert asset.box == pytest.approx((60.16, 119.88, 129.92, 299.88))
    assert asset.reference == '"Front door"'


@pytest.mark.parametrize(
    ("zone", "frame", "asset_type"),
    [
        (None, FRAME, "door"),
        (DOOR_ZONE, (0.0, 360.0), "door"),
        (DOOR_ZONE, (640.0, 0.0), "door"),
        (DOOR_ZONE, FRAME, "spaceship"),
    ],
)
def test_marked_asset_declines_what_it_cannot_place(
    zone: Zone | None, frame: tuple[float, float], asset_type: str
) -> None:
    assert build_marked_asset("Porch", "k", "x", asset_type, zone, frame) is None


@pytest.mark.parametrize(
    ("asset_type", "routine", "fixed"),
    [
        (AssetType.DOOR, True, True),
        (AssetType.GATE, True, True),
        (AssetType.GARAGE, True, True),
        (AssetType.MAILBOX, True, True),
        (AssetType.PACKAGE_AREA, True, False),
        (AssetType.WINDOW, False, True),
        (AssetType.BICYCLE, False, False),
        (AssetType.EQUIPMENT, False, False),
        (AssetType.OTHER, False, False),
    ],
)
def test_what_each_type_is_watched_for(
    asset_type: AssetType, routine: bool, fixed: bool
) -> None:
    asset = build_marked_asset("Porch", "k", "x", asset_type, DOOR_ZONE, FRAME)
    assert asset is not None
    assert asset.handled_routinely is routine
    assert asset.fixed is fixed
    assert asset.impact_applies is False
    assert asset.located_exactly is fixed


def test_only_the_vehicle_can_be_struck() -> None:
    car = ProtectedAsset(name="car", asset_type=AssetType.VEHICLE, camera="c")
    assert car.impact_applies is True
    assert car.handled_routinely is False
    assert car.reference == "the protected asset"


# ----------------------------------------------------------------------
# The rules, per asset
# ----------------------------------------------------------------------


def test_no_marked_assets_means_no_asset_events() -> None:
    events = _detect(_walk(1, _TO_THE_DOOR), 6)
    assert _types(events) <= {"subject_present"}


def test_a_visit_to_the_door_is_recorded_but_never_a_contact() -> None:
    """Touching a door is using it — see ProtectedAsset.handled_routinely."""
    events = _detect(
        _walk(1, _TO_THE_DOOR),
        6,
        marked_assets=[_door()],
        examined_asset_key="door1",
        depth_similar=True,
        contact_touching=True,
        posture_reaching=True,
        posture_arm_raised=True,
    )
    door = _types(events, "Front door")
    assert {"zone_entered", "asset_proximity", "retreat"} <= door
    assert "contact_candidate" not in door
    assert "asset_reach" not in door
    assert "impact_candidate" not in door
    proximity = next(e for e in events if e.event_type == "asset_proximity")
    # Standing at a door is ringing its bell: on record, not a concern.
    assert proximity.severity is Severity.NOTEWORTHY
    # A door is where it was marked, so the closeness is not a guess.
    assert proximity.confidence == pytest.approx(0.9)
    assert "normally sits" not in proximity.detail
    assert '"Front door"' in proximity.detail


def test_a_touch_of_the_bike_is_a_contact_but_never_an_impact() -> None:
    events = _detect(
        _walk(1, _TO_THE_BIKE),
        6,
        marked_assets=[_bike()],
        examined_asset_key="bike1",
        contact_touching=True,
        posture_reaching=True,
        posture_arm_raised=True,
    )
    bike = _types(events, "Bike")
    assert {"contact_candidate", "asset_reach", "retreat_after_contact"} <= bike
    assert "impact_candidate" not in bike
    contact = next(e for e in events if e.event_type == "contact_candidate")
    assert contact.severity is Severity.SUSPICIOUS
    assert contact.evidence["confirmed"] is True
    proximity = next(e for e in events if e.event_type == "asset_proximity")
    # A bike can be moved, so its zone is where it usually stands.
    assert "normally sits" in proximity.detail
    assert proximity.severity is Severity.SUSPICIOUS


def test_the_vehicles_verdict_is_never_read_as_the_doors() -> None:
    """examined_asset_key=None is the vehicle, as it always was: depth
    placing the subject far from the *car* says nothing about the door."""
    vehicle = resolve_vehicle_asset(
        "Porch",
        "blue sedan",
        build_tracks(_walk(1, _TO_THE_DOOR) + _parked_car(6), 2.0, FRAME),
        FRAME,
    )
    events = _detect(
        _walk(1, _TO_THE_DOOR) + _parked_car(6),
        6,
        asset=vehicle,
        marked_assets=[_door()],
        depth_similar=False,
    )
    assert "zone_entered" in _types(events, "Front door")


def test_the_doors_verdict_is_never_read_as_the_vehicles() -> None:
    vehicle = resolve_vehicle_asset(
        "Porch",
        "blue sedan",
        build_tracks(_walk(1, _car_side()) + _parked_car(6), 2.0, FRAME),
        FRAME,
    )
    touching = _detect(
        _walk(1, _car_side()) + _parked_car(6),
        6,
        asset=vehicle,
        marked_assets=[_door()],
        examined_asset_key="door1",
        contact_touching=True,
    )
    contact = next(e for e in touching if e.event_type == "contact_candidate")
    # Only the bare overlap backs the car's contact: segmentation looked at
    # the door, not at it.
    assert contact.evidence["confirmed"] is False


def test_a_depth_veto_on_the_examined_asset_stands_down_its_zone() -> None:
    events = _detect(
        _walk(1, _TO_THE_DOOR),
        6,
        marked_assets=[_door()],
        examined_asset_key="door1",
        depth_similar=False,
    )
    assert "zone_entered" not in _types(events, "Front door")


def test_an_unknown_key_examines_nothing() -> None:
    events = _detect(
        _walk(1, _TO_THE_BIKE),
        6,
        marked_assets=[_bike()],
        examined_asset_key="someone-else",
        contact_touching=True,
    )
    contact = next(e for e in events if e.event_type == "contact_candidate")
    assert contact.evidence["confirmed"] is False


def test_disturbed_needs_a_change_and_a_visit() -> None:
    changed = {"bike1": 0.6}
    events = _detect(
        _walk(1, _TO_THE_BIKE), 6, marked_assets=[_bike()], marked_asset_changes=changed
    )
    disturbed = [e for e in events if e.event_type == "asset_disturbed"]
    assert len(disturbed) == 1
    assert disturbed[0].severity is Severity.NOTEWORTHY
    assert disturbed[0].track_id == 1
    assert disturbed[0].end_offset == pytest.approx(10.0)
    assert "taken, moved or left" in disturbed[0].detail
    assert disturbed[0].evidence == {"appearance_change": 0.6}

    # Below the bar: the region barely changed.
    quiet = _detect(
        _walk(1, _TO_THE_BIKE),
        6,
        marked_assets=[_bike()],
        marked_asset_changes={"bike1": 0.1},
    )
    assert "asset_disturbed" not in _types(quiet)

    # Nobody went near it: a change with no visitor is the weather.
    far = [_person_at(x, ground=355, height=60) for x in (0, 60, 120)]
    unvisited = _detect(
        _walk(1, far), 3, marked_assets=[_bike()], marked_asset_changes=changed
    )
    assert "asset_disturbed" not in _types(unvisited)


def test_a_disturbed_door_reads_as_opened_not_taken() -> None:
    events = _detect(
        _walk(1, _TO_THE_DOOR),
        6,
        marked_assets=[_door()],
        marked_asset_changes={"door1": 0.7},
    )
    disturbed = next(e for e in events if e.event_type == "asset_disturbed")
    assert "opened, closed or left ajar" in disturbed.detail


def test_the_vehicle_is_never_reported_disturbed() -> None:
    """The vehicle's own change feeds the impact rule, not this one."""
    vehicle = resolve_vehicle_asset(
        "Porch",
        "blue sedan",
        build_tracks(_walk(1, _car_side()) + _parked_car(6), 2.0, FRAME),
        FRAME,
    )
    events = _detect(
        _walk(1, _car_side()) + _parked_car(6),
        6,
        asset=vehicle,
        marked_asset_changes={"": 0.9},
    )
    assert "asset_disturbed" not in _types(events)


def test_disturbed_is_not_bypass_blocking() -> None:
    """A resident collecting their own parcel produces exactly this."""
    assert SecurityEventType.ASSET_DISTURBED not in BYPASS_BLOCKING_EVENTS


def test_two_neighbouring_assets_are_not_scored_twice() -> None:
    """Walking to the door past the parcel spot is one visit, not two."""
    both = _detect(_walk(1, _TO_THE_DOOR), 6, marked_assets=[_door(), _parcels()])
    door_only = _detect(_walk(1, _TO_THE_DOOR), 6, marked_assets=[_door()])
    assert _types(both, "Parcel spot")
    score_both = RiskScorer().score(both, 1.0).score
    score_door = RiskScorer().score(door_only, 1.0).score
    # The parcel spot adds its own facts to the record, but every type it
    # repeats counts once.
    assert score_both == pytest.approx(score_door, abs=1.0)


def test_assess_clip_carries_marked_assets_through() -> None:
    tracks = build_tracks(_walk(1, _TO_THE_BIKE), 2.0, FRAME)
    outcome = assess_clip(
        ClipMeasurements(
            camera="Porch",
            tracks=tracks,
            frame_interval=2.0,
            frame_count=6,
            frames_analyzed=6,
            target_frames=6,
            marked_assets=[_bike()],
            examined_asset_key="bike1",
            contact_touching=True,
            marked_asset_changes={"bike1": 0.5},
        )
    )
    kinds = {str(e.event_type) for e in outcome.events}
    assert {"contact_candidate", "asset_disturbed"} <= kinds
    assert any('"Bike"' in segment for segment in outcome.prompt_segments)


def _parked_car(frames: int) -> list[Detection]:
    return [("car", 0.95, CAR, 9, i) for i in range(frames)]


def _car_side() -> list[Box]:
    """Someone walking up to the parked car and standing against it."""
    return [_person_at(x, ground=285) for x in (200, 250, 290, 300, 300, 250)]


# ----------------------------------------------------------------------
# Whole clips
# ----------------------------------------------------------------------


@dataclass
class AssetScenario:
    """One end-to-end expectation about a clip on a camera with marked assets."""

    name: str
    detections: list[Detection]
    frame_count: int
    assets: list[ProtectedAsset]
    expect_severity: tuple[Severity, Severity]
    expect_events: set[SecurityEventType] = field(default_factory=set)
    forbid_events: set[SecurityEventType] = field(default_factory=set)
    examined: str | None = None
    depth_similar: bool | None = None
    contact_touching: bool | None = None
    posture_reaching: bool | None = None
    posture_arm_raised: bool | None = None
    changes: dict[str, float] = field(default_factory=dict)
    is_night: bool = False
    approved_person: bool = False
    #: A forced alert (the critical band, the default ai_risk_alert_threshold
    #: of 75) must or must not happen.
    forces_alert: bool | None = None


_ROUTINE_TO_SUSPICIOUS = (Severity.ROUTINE, Severity.SUSPICIOUS)
_NOTEWORTHY_TO_SUSPICIOUS = (Severity.NOTEWORTHY, Severity.SUSPICIOUS)

ASSET_SCENARIOS: list[AssetScenario] = [
    AssetScenario(
        name="a courier rings, leaves a parcel and goes — full CV on",
        detections=_walk(1, _TO_THE_DOOR),
        frame_count=6,
        assets=[_door(), _parcels()],
        examined="door1",
        depth_similar=True,
        contact_touching=True,
        posture_reaching=True,
        posture_arm_raised=True,
        changes={"parcel1": 0.6},
        expect_severity=_ROUTINE_TO_SUSPICIOUS,
        expect_events={SecurityEventType.ZONE_ENTERED},
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.IMPACT_CANDIDATE,
            SecurityEventType.ASSET_REACH,
        },
        forces_alert=False,
    ),
    AssetScenario(
        name="the same courier at night",
        detections=_walk(1, _TO_THE_DOOR),
        frame_count=6,
        assets=[_door(), _parcels()],
        examined="door1",
        depth_similar=True,
        contact_touching=True,
        changes={"parcel1": 0.6},
        is_night=True,
        expect_severity=_ROUTINE_TO_SUSPICIOUS,
        forces_alert=False,
    ),
    AssetScenario(
        name="someone walks past on the pavement",
        detections=_walk(
            1, [_person_at(x, height=60, ground=355) for x in (0, 120, 240, 360, 480)]
        ),
        frame_count=5,
        assets=[_door(), _parcels(), _bike()],
        expect_severity=(Severity.ROUTINE, Severity.ROUTINE),
        forbid_events={
            SecurityEventType.ZONE_ENTERED,
            SecurityEventType.ASSET_PROXIMITY,
            SecurityEventType.CONTACT_CANDIDATE,
        },
        forces_alert=False,
    ),
    AssetScenario(
        name="a stranger lingers at the door at night",
        detections=_walk(1, [_person_at(110, ground=300)] * 10),
        frame_count=10,
        assets=[_door()],
        is_night=True,
        expect_severity=_NOTEWORTHY_TO_SUSPICIOUS,
        expect_events={SecurityEventType.LOITERING, SecurityEventType.ZONE_ENTERED},
        forbid_events={SecurityEventType.CONTACT_CANDIDATE},
    ),
    AssetScenario(
        name="a stranger handles the bike and it is gone afterwards",
        detections=_walk(1, _TO_THE_BIKE),
        frame_count=6,
        assets=[_door(), _bike()],
        examined="bike1",
        depth_similar=True,
        contact_touching=True,
        changes={"bike1": 0.7},
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ASSET_DISTURBED,
            SecurityEventType.RETREAT_AFTER_CONTACT,
        },
        forbid_events={SecurityEventType.IMPACT_CANDIDATE},
        forces_alert=True,
    ),
    AssetScenario(
        name="a recognized resident takes their own bike out",
        detections=_walk(1, _TO_THE_BIKE),
        frame_count=6,
        assets=[_bike()],
        examined="bike1",
        depth_similar=True,
        contact_touching=True,
        changes={"bike1": 0.7},
        approved_person=True,
        expect_severity=_ROUTINE_TO_SUSPICIOUS,
        forbid_events={SecurityEventType.IMPACT_CANDIDATE},
        forces_alert=False,
    ),
    AssetScenario(
        name="a bare overlap with the bike on a basic device stays unconfirmed",
        detections=_walk(1, _TO_THE_BIKE),
        frame_count=6,
        assets=[_bike()],
        is_night=True,
        expect_severity=_ROUTINE_TO_SUSPICIOUS,
        forces_alert=False,
    ),
    AssetScenario(
        name="a dog sniffs round the bike",
        detections=_walk(1, [(560.0, 270.0, 620.0, 310.0)] * 5, label="dog"),
        frame_count=5,
        assets=[_bike()],
        examined="bike1",
        contact_touching=True,
        expect_severity=(Severity.ROUTINE, Severity.NOTEWORTHY),
        expect_events={SecurityEventType.ANIMAL_ASSET_INTERACTION},
        forces_alert=False,
    ),
]


@pytest.mark.parametrize("scenario", ASSET_SCENARIOS, ids=lambda s: s.name)
def test_asset_scenario(scenario: AssetScenario) -> None:
    tracks = build_tracks(scenario.detections, 2.0, FRAME)
    events = SecurityEventDetector().detect(
        DetectionContext(
            camera="Porch",
            tracks=tracks,
            frame_interval=2.0,
            frame_count=scenario.frame_count,
            marked_assets=scenario.assets,
            examined_asset_key=scenario.examined,
            depth_similar=scenario.depth_similar,
            contact_touching=scenario.contact_touching,
            posture_reaching=scenario.posture_reaching,
            posture_arm_raised=scenario.posture_arm_raised,
            marked_asset_changes=scenario.changes,
        )
    )
    quality = assess_evidence(
        scenario.frame_count, scenario.frame_count, tracks, 2.0, None
    )
    assessment = RiskScorer().score(
        events,
        quality.score,
        ScoringContext(
            is_night=scenario.is_night,
            approved_person_recognized=scenario.approved_person,
        ),
    )
    kinds = {e.event_type for e in events}
    low, high = scenario.expect_severity
    assert (
        severity_rank(low) <= severity_rank(assessment.severity) <= severity_rank(high)
    ), (assessment.score, assessment.severity, sorted(kinds))
    assert scenario.expect_events <= kinds, sorted(kinds)
    assert not (scenario.forbid_events & kinds), sorted(kinds)
    if scenario.forces_alert is not None:
        assert (assessment.score >= 75) is scenario.forces_alert, assessment.score
