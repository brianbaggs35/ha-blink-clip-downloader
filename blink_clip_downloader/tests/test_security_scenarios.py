"""Scenario-based evaluation of the deterministic security layer.

Unit tests prove each rule fires on its own inputs. This file asks the
harder question: given a whole clip's worth of geometry, does the pipeline
reach the *right conclusion* — and, just as importantly, does it stay quiet
for the ordinary things that happen on a driveway every day?

Every scenario below is synthetic track geometry rather than real footage,
which is the point: it runs anywhere, in CI, with no video fixtures, no
models, and no GPU, and it pins the end-to-end behaviour that a change to
any single threshold would quietly shift. Real labelled clips can be added
later by feeding their detections through :func:`_run` — the expectations
format needs nothing else.

The false-positive scenarios matter most. A security system that flags a
resident getting into their own car is one the user turns off.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from blink_downloader.security.assets import resolve_vehicle_asset
from blink_downloader.security.detector import DetectionContext, SecurityEventDetector
from blink_downloader.security.events import SecurityEventType, Severity, severity_rank
from blink_downloader.security.evidence import assess_evidence
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.pipeline import SecurityOutcome
from blink_downloader.security.scoring import RiskAssessment, RiskScorer, ScoringContext
from blink_downloader.security.tracks import build_tracks, subject_tracks
from blink_downloader.security.vehicles import nearest_vehicle_for_subjects

FRAME = (640.0, 360.0)
#: A sedan parked in the middle of a driveway camera's view.
MY_CAR: Box = (240.0, 170.0, 420.0, 290.0)
#: A second car alongside it — the neighbour's, in an apartment car park.
NEIGHBOUR: Box = (430.0, 170.0, 610.0, 290.0)
#: The zone a user would draw around their own car on the Vehicles tab.
ZONE = Zone.from_config({"x_min": 0.38, "y_min": 0.47, "x_max": 0.66, "y_max": 0.81})
#: A looser zone covering the whole parking area rather than just the car —
#: the other common way users draw one.
WIDE_ZONE = Zone.from_config({"x_min": 0.2, "y_min": 0.45, "x_max": 0.85, "y_max": 1.0})

Detection = tuple[str, float, Box, int | None, int]


def _walk(
    track_id: int,
    boxes: list[Box],
    label: str = "person",
    confidence: float = 0.9,
) -> list[Detection]:
    return [(label, confidence, box, track_id, i) for i, box in enumerate(boxes)]


def _parked(
    track_id: int, box: Box, frames: int, label: str = "car"
) -> list[Detection]:
    return [(label, 0.95, box, track_id, i) for i in range(frames)]


def _person_at(x: float, *, height: float = 150.0, ground: float = 300.0) -> Box:
    """A person-shaped box standing at *x* with their feet on *ground*."""
    return (x, ground - height, x + height * 0.4, ground)


@dataclass
class Scenario:
    """One end-to-end expectation about a whole clip."""

    name: str
    detections: list[Detection]
    frame_count: int
    #: Inclusive band the clip's overall severity must land in.
    expect_severity: tuple[Severity, Severity]
    expect_events: set[SecurityEventType] = field(default_factory=set)
    forbid_events: set[SecurityEventType] = field(default_factory=set)
    zone: Zone | None = ZONE
    car_description: str = "blue sedan"
    is_night: bool = False
    approved_person: bool = False
    depth_similar: bool | None = None
    contact_touching: bool | None = None
    appearance_change: float | None = None
    scene_deviation: float | None = None


def _run(scenario: Scenario) -> tuple[RiskAssessment, set[SecurityEventType]]:
    """Run one scenario through the whole deterministic pipeline."""
    tracks = build_tracks(scenario.detections, 2.0, FRAME)
    asset = resolve_vehicle_asset(
        "Driveway", scenario.car_description, tracks, FRAME, zone=scenario.zone
    )
    quality = assess_evidence(scenario.frame_count, scenario.frame_count, tracks, 2.0)
    events = SecurityEventDetector().detect(
        DetectionContext(
            camera="Driveway",
            tracks=tracks,
            frame_interval=2.0,
            frame_count=scenario.frame_count,
            asset=asset,
            depth_similar=scenario.depth_similar,
            contact_touching=scenario.contact_touching,
            appearance_change=scenario.appearance_change,
            scene_deviation=scenario.scene_deviation,
        )
    )
    nearer_other = (
        asset is not None
        and asset.identification is not None
        and nearest_vehicle_for_subjects(subject_tracks(tracks), asset.identification)
        == "other"
    )
    assessment = RiskScorer().score(
        events,
        quality.score,
        ScoringContext(
            is_night=scenario.is_night,
            approved_person_recognized=scenario.approved_person,
            subject_nearer_other_vehicle=nearer_other,
        ),
    )
    return assessment, {e.event_type for e in events}


# ----------------------------------------------------------------------
# The scenario table
# ----------------------------------------------------------------------

ROUTINE_ONLY = (Severity.ROUTINE, Severity.ROUTINE)
UP_TO_NOTEWORTHY = (Severity.ROUTINE, Severity.NOTEWORTHY)

SCENARIOS: list[Scenario] = [
    # -- ordinary activity that must NOT raise an alert ----------------
    Scenario(
        name="person walks past the driveway on the pavement",
        detections=_walk(
            1, [_person_at(x, height=70, ground=355) for x in (0, 120, 240, 360, 480)]
        )
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        expect_severity=ROUTINE_ONLY,
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ZONE_ENTERED,
        },
    ),
    Scenario(
        name="a car drives past with nobody around",
        detections=_parked(2, MY_CAR, 5)
        + [
            ("car", 0.9, (float(x), 60.0, float(x) + 120, 130.0), 3, i)
            for i, x in enumerate((0, 140, 280, 420, 520))
        ],
        frame_count=5,
        expect_severity=ROUTINE_ONLY,
        forbid_events={SecurityEventType.SUBJECT_PRESENT},
    ),
    Scenario(
        name="a dog wanders near the car",
        detections=_walk(1, [(300.0, 250.0, 360.0, 300.0)] * 4, label="dog")
        + _parked(2, MY_CAR, 4),
        frame_count=4,
        expect_severity=UP_TO_NOTEWORTHY,
        forbid_events={SecurityEventType.IMPACT_CANDIDATE},
    ),
    Scenario(
        name="a delivery leaves a parcel on the step",
        detections=_walk(1, [_person_at(x) for x in (0, 80, 140, 80, 0)])
        + [("backpack", 0.6, (120.0, 280.0, 160.0, 320.0), 4, i) for i in (3, 4)]
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        expect_severity=UP_TO_NOTEWORTHY,
        expect_events={SecurityEventType.OBJECT_ADDED},
        forbid_events={SecurityEventType.OBJECT_REMOVED},
    ),
    Scenario(
        name="a recognized resident gets into their own car",
        detections=_walk(1, [_person_at(x) for x in (0, 90, 170, 215, 220)])
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        expect_severity=UP_TO_NOTEWORTHY,
        approved_person=True,
        depth_similar=True,
    ),
    Scenario(
        name="a neighbour returns to the car parked beside ours",
        detections=_walk(1, [_person_at(x) for x in (630, 615, 600, 590, 585)])
        + _parked(2, MY_CAR, 5)
        + _parked(3, NEIGHBOUR, 5),
        frame_count=5,
        expect_severity=UP_TO_NOTEWORTHY,
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.IMPACT_CANDIDATE,
        },
    ),
    Scenario(
        name="our car is gone and a neighbour uses the empty space",
        detections=_walk(1, [_person_at(x) for x in (600, 520, 420, 330, 300)])
        + _parked(3, NEIGHBOUR, 5),
        frame_count=5,
        expect_severity=UP_TO_NOTEWORTHY,
        depth_similar=True,
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ASSET_PROXIMITY,
            SecurityEventType.IMPACT_CANDIDATE,
        },
    ),
    # -- activity that should draw attention ---------------------------
    Scenario(
        name="a stranger lingers at the driver's door",
        detections=_walk(1, [_person_at(215)] * 9) + _parked(2, MY_CAR, 9),
        frame_count=9,
        expect_severity=(Severity.NOTEWORTHY, Severity.SUSPICIOUS),
        expect_events={
            SecurityEventType.LOITERING,
            SecurityEventType.ASSET_PROXIMITY,
        },
    ),
    Scenario(
        name="someone enters the protected zone after dark",
        detections=_walk(1, [_person_at(x) for x in (0, 40, 70, 95, 100)])
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        zone=WIDE_ZONE,
        is_night=True,
        expect_severity=(Severity.NOTEWORTHY, Severity.SUSPICIOUS),
        expect_events={SecurityEventType.ZONE_ENTERED},
        forbid_events={SecurityEventType.CONTACT_CANDIDATE},
    ),
    Scenario(
        name="a parcel disappears while someone is in frame",
        detections=_walk(1, [_person_at(x) for x in (0, 60, 120, 60, 0)])
        + [("backpack", 0.6, (120.0, 280.0, 160.0, 320.0), 4, i) for i in (0, 1)]
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        expect_severity=(Severity.NOTEWORTHY, Severity.SUSPICIOUS),
        expect_events={SecurityEventType.OBJECT_REMOVED},
    ),
    # -- the cases that must reach the top of the list ------------------
    Scenario(
        name="someone touches the car, confirmed at the same depth",
        detections=_walk(1, [_person_at(x) for x in (0, 120, 230, 250, 250)])
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        depth_similar=True,
        expect_severity=(Severity.SUSPICIOUS, Severity.CRITICAL),
        expect_events={SecurityEventType.CONTACT_CANDIDATE},
    ),
    Scenario(
        name="someone rushes the car and the car looks different afterwards",
        detections=_walk(1, [_person_at(x) for x in (0, 20, 250, 240, 600)])
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        depth_similar=True,
        appearance_change=0.6,
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={
            SecurityEventType.IMPACT_CANDIDATE,
            SecurityEventType.RETREAT_AFTER_CONTACT,
        },
    ),
    Scenario(
        name="the camera view is swamped and nothing is in it",
        detections=[],
        frame_count=5,
        scene_deviation=0.9,
        expect_severity=(Severity.NOTEWORTHY, Severity.SUSPICIOUS),
        expect_events={SecurityEventType.CAMERA_OBSTRUCTION},
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario(scenario: Scenario) -> None:
    assessment, types = _run(scenario)
    low, high = scenario.expect_severity
    rank = severity_rank(assessment.severity)
    assert severity_rank(low) <= rank <= severity_rank(high), (
        f"{scenario.name}: got {assessment.severity} "
        f"(risk {assessment.score:.0f}) from {sorted(str(t) for t in types)}"
    )
    missing = scenario.expect_events - types
    assert not missing, f"{scenario.name}: missing {sorted(str(m) for m in missing)}"
    unwanted = scenario.forbid_events & types
    assert not unwanted, f"{scenario.name}: unwanted {sorted(str(u) for u in unwanted)}"


def test_the_scenario_table_covers_both_directions() -> None:
    """An evaluation set made only of true positives proves nothing about
    false positives, which are what make a security feature unusable."""
    quiet = [s for s in SCENARIOS if s.expect_severity[1] <= Severity.NOTEWORTHY]
    loud = [s for s in SCENARIOS if severity_rank(s.expect_severity[0]) >= 2]
    assert len(quiet) >= 5
    assert len(loud) >= 2


def test_scenario_names_are_unique() -> None:
    assert len({s.name for s in SCENARIOS}) == len(SCENARIOS)


def test_security_outcome_exposes_the_assessment_directly() -> None:
    """Callers read `outcome.severity`/`risk_score`/`events` rather than
    reaching through to the assessment, so those shortcuts are part of the
    contract, not conveniences."""
    scenario = SCENARIOS[0]
    assessment, _ = _run(scenario)
    outcome = SecurityOutcome(assessment=assessment)
    assert outcome.severity is assessment.severity
    assert outcome.risk_score == assessment.score
    assert outcome.events is assessment.events
