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
#: A drive traced with the Vehicles tab's freeform lasso: down the left of
#: the plot and then along the front of the house, an L. Its axis-aligned
#: bounding box also covers the lawn and the kerb on the right — ground the
#: user deliberately traced *around*, and where a neighbour's or visitor's
#: car routinely sits.
TRACED_DRIVE = Zone.from_config(
    {
        "shape": "polygon",
        "points": [
            [0.05, 0.35],
            [0.34, 0.35],
            [0.34, 0.72],
            [0.95, 0.72],
            [0.95, 0.95],
            [0.05, 0.95],
        ],
    }
)
#: A car on the kerb: inside TRACED_DRIVE's bounding box, nowhere near the
#: drive itself.
ON_THE_KERB: Box = (420.0, 150.0, 600.0, 240.0)
#: A car actually parked on the traced drive.
ON_THE_DRIVE: Box = (60.0, 270.0, 280.0, 340.0)

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
    #: Optional stages that produced nothing — the shape of a low-powered
    #: device running object detection but not the heavy torch stages.
    unavailable_sources: list[str] = field(default_factory=list)
    #: Highest severity any single event is allowed to claim. Used to pin
    #: that an unconfirmed claim stays unconfirmed.
    max_event_severity: Severity | None = None
    #: Seconds between sampled frames. The temporal scan's is the clip's
    #: length over ai_temporal_scan_frames — about 1s for a typical clip.
    frame_interval: float = 2.0


def _run(scenario: Scenario) -> tuple[RiskAssessment, set[SecurityEventType]]:
    """Run one scenario through the whole deterministic pipeline."""
    tracks = build_tracks(scenario.detections, scenario.frame_interval, FRAME)
    asset = resolve_vehicle_asset(
        "Driveway", scenario.car_description, tracks, FRAME, zone=scenario.zone
    )
    quality = assess_evidence(
        scenario.frame_count,
        scenario.frame_count,
        tracks,
        scenario.frame_interval,
        scenario.unavailable_sources,
    )
    events = SecurityEventDetector().detect(
        DetectionContext(
            camera="Driveway",
            tracks=tracks,
            frame_interval=scenario.frame_interval,
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
#: Never the critical band, which forces an alert past the model's verdict.
UP_TO_SUSPICIOUS = (Severity.ROUTINE, Severity.SUSPICIOUS)
#: A device running detection but none of the heavy torch stages.
_BASIC = ["depth estimation", "contact segmentation", "pose estimation"]
#: A walker's left edge, frame by frame, crossing the whole view.
_PASS = (0, 90, 180, 270, 360, 450)

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
        name="a pedestrian passes in front of the parked car, overlapping it",
        # Nearer the camera than the car, so their box covers the lower half
        # of it in every frame — a deep 2D overlap — while their feet are in
        # plain view over five feet in front of its bumper. The overlap was
        # reported as a possible contact, which reached the prompt and the
        # Security tab for everyone walking along the pavement.
        detections=_walk(
            1,
            [_person_at(x, height=120, ground=355) for x in (100, 200, 300, 400, 500)],
        )
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        zone=None,
        unavailable_sources=_BASIC,
        is_night=True,
        expect_severity=ROUTINE_ONLY,
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.RETREAT_AFTER_CONTACT,
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
        name="a recognized resident steps out of the door and walks to their car",
        # Standing still, then walking at an ordinary pace, is a large
        # acceleration — but at the doorstep, not the car. Measured anywhere,
        # it read as rushing the car: a possible impact, which forces an
        # alert and withholds this resident's face-recognition bypass.
        detections=_walk(1, [_person_at(x) for x in (20, 20, 110, 200, 240, 250, 250)])
        + _parked(2, MY_CAR, 7),
        frame_count=7,
        frame_interval=1.0,
        approved_person=True,
        depth_similar=True,
        contact_touching=True,
        expect_severity=UP_TO_NOTEWORTHY,
        forbid_events={SecurityEventType.IMPACT_CANDIDATE},
    ),
    Scenario(
        name="a recognized resident gets out of their car and walks indoors",
        # Standing at the car and then walking off at an ordinary 4.5 ft/s
        # (0.9 of their own height a second) is a speed-up at the car. In
        # frame widths a second it cleared the "bolting" bar on any camera
        # close enough to the car, and with the contact confirmed that made
        # every homecoming a possible impact: forced alert, bypass withheld.
        detections=_walk(
            1, [_person_at(300, ground=296)] * 3 + [_person_at(435), _person_at(570)]
        )
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        frame_interval=1.0,
        approved_person=True,
        depth_similar=True,
        contact_touching=True,
        expect_severity=UP_TO_SUSPICIOUS,
        forbid_events={SecurityEventType.IMPACT_CANDIDATE},
    ),
    Scenario(
        name="someone touches the car and then runs from it",
        detections=_walk(1, [_person_at(300, ground=296)] * 3 + [_person_at(0)])
        + _parked(2, MY_CAR, 4),
        frame_count=4,
        frame_interval=1.0,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={SecurityEventType.IMPACT_CANDIDATE},
    ),
    Scenario(
        name="someone rushes the car from standing still",
        detections=_walk(1, [_person_at(x) for x in (20, 20, 300, 300, 300)])
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        frame_interval=1.0,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={SecurityEventType.IMPACT_CANDIDATE},
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
    # -- the four cases the add-on is judged on day to day ------------
    Scenario(
        name="a dog jumps up on the protected car",
        detections=_walk(
            1,
            [_person_at(90, height=55, ground=295)] * 2
            + [(250.0, 200.0, 330.0, 300.0)] * 6,
            label="dog",
            confidence=0.85,
        )
        + _parked(2, MY_CAR, 8),
        frame_count=8,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.NOTEWORTHY, Severity.SUSPICIOUS),
        expect_events={SecurityEventType.ANIMAL_ASSET_INTERACTION},
        # Damage, not an intruder: the owner should hear about scratched
        # paintwork without the dog being promoted to a prowler.
        forbid_events={SecurityEventType.IMPACT_CANDIDATE},
    ),
    Scenario(
        name="a stranger touches the protected car, confirmed by both stages",
        detections=_walk(1, [_person_at(560), _person_at(480)] + [_person_at(300)] * 6)
        + _parked(2, MY_CAR, 8),
        frame_count=8,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.SUSPICIOUS, Severity.CRITICAL),
        expect_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ASSET_PROXIMITY,
        },
    ),
    Scenario(
        name="a person walks past the car at a different distance",
        # Their box overlaps the car in the projection the whole way — the
        # single most common false positive a driveway camera produces.
        detections=_walk(
            1, [(200.0 + i * 45, 120.0, 300.0 + i * 45, 359.0) for i in range(6)]
        )
        + _parked(2, MY_CAR, 6),
        frame_count=6,
        depth_similar=False,
        expect_severity=(Severity.ROUTINE, Severity.ROUTINE),
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ASSET_PROXIMITY,
            SecurityEventType.ASSET_APPROACHED,
            SecurityEventType.ZONE_ENTERED,
        },
    ),
    Scenario(
        name="the same passer-by on a device with no depth or segmentation",
        detections=_walk(
            1, [(200.0 + i * 45, 120.0, 300.0 + i * 45, 359.0) for i in range(6)]
        )
        + _parked(2, MY_CAR, 6),
        frame_count=6,
        unavailable_sources=[
            "depth estimation",
            "contact segmentation",
            "pose estimation",
            "face recognition",
        ],
        # With no depth stage, the outline overlap alone once earned an
        # unconfirmed "possible contact" here. But these feet are in plain
        # view nearly six feet in front of the car's ground line — the same
        # measured distance the proximity rule and the far-side rule already
        # trust — and nobody touches a car from there. The zone entry still
        # stands, so the clip is on record without claiming a touch.
        expect_severity=(Severity.ROUTINE, Severity.NOTEWORTHY),
        max_event_severity=Severity.NOTEWORTHY,
        forbid_events={SecurityEventType.CONTACT_CANDIDATE},
        expect_events={SecurityEventType.ZONE_ENTERED},
    ),
    # -- walking close past the car's front at night, no depth stages ----
    # Five noteworthy events from one overlap summed to 79 (a forced alert)
    # for one walker and 100 for a couple or a dog walk, until an
    # unconfirmed contact was barred from being the reason a clip alerts,
    # and one subject's movement counted once per type.
    Scenario(
        name="someone walks close past the front of the car at night, no depth",
        detections=_walk(1, [_person_at(x, height=140, ground=305) for x in _PASS])
        + _parked(2, MY_CAR, 6),
        frame_count=6,
        is_night=True,
        unavailable_sources=_BASIC,
        expect_severity=UP_TO_SUSPICIOUS,
        max_event_severity=Severity.NOTEWORTHY,
    ),
    Scenario(
        name="a couple walks close past the front of the car at night",
        detections=_walk(1, [_person_at(x, height=140, ground=305) for x in _PASS])
        + _walk(3, [_person_at(x - 50, height=130, ground=300) for x in _PASS])
        + _parked(2, MY_CAR, 6),
        frame_count=6,
        is_night=True,
        unavailable_sources=_BASIC,
        expect_severity=UP_TO_SUSPICIOUS,
    ),
    Scenario(
        name="a person walks their dog close past the car at night",
        detections=_walk(1, [_person_at(x, height=140, ground=305) for x in _PASS])
        + _walk(
            3,
            [
                (x + 64.0, 283.0, x + 98.0, 305.0)  # on the lead, just ahead
                for x in _PASS
            ],
            label="dog",
        )
        + _parked(2, MY_CAR, 6),
        frame_count=6,
        is_night=True,
        unavailable_sources=_BASIC,
        expect_severity=UP_TO_SUSPICIOUS,
    ),
    # ...while the same close contact, confirmed, still alerts — by one
    # stranger or two, day or night.
    Scenario(
        name="a stranger touches the car at night, confirmed by both stages",
        detections=_walk(1, [_person_at(560), _person_at(480)] + [_person_at(300)] * 6)
        + _parked(2, MY_CAR, 8),
        frame_count=8,
        is_night=True,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={SecurityEventType.CONTACT_CANDIDATE},
    ),
    Scenario(
        name="two strangers at the car at night, contact confirmed",
        detections=_walk(1, [_person_at(560), _person_at(480)] + [_person_at(300)] * 6)
        + _walk(3, [_person_at(560), _person_at(470)] + [_person_at(330)] * 6)
        + _parked(2, MY_CAR, 8),
        frame_count=8,
        is_night=True,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.MULTIPLE_SUBJECTS,
        },
    ),
    # -- the far side of the car: it hides the subject's feet ----------
    Scenario(
        name="a stranger at the car's far side at night, confirmed by both stages",
        # Their box ends mid-car, so the foot point reads as behind it.
        detections=_walk(
            1,
            [
                _person_at(560, height=120, ground=250),
                _person_at(480, height=120, ground=250),
            ]
            + [(330.0, 150.0, 378.0, 250.0)] * 6,
        )
        + _parked(2, MY_CAR, 8),
        frame_count=8,
        is_night=True,
        depth_similar=True,
        contact_touching=True,
        expect_severity=(Severity.CRITICAL, Severity.CRITICAL),
        expect_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ASSET_PROXIMITY,
        },
    ),
    Scenario(
        name="a passer-by five feet in front of the car, depth wrongly similar",
        # What the far-side rule must not reach: their feet are in plain
        # view in front of the car's ground line, and depth calls someone
        # overlapping a car in the image "similar" more often than not.
        # Letting that override the visible gap put them "within 1 ft" of
        # the car and forced a critical alert.
        detections=_walk(1, [_person_at(x, height=155, ground=330) for x in _PASS])
        + _parked(2, MY_CAR, 6),
        frame_count=6,
        is_night=True,
        depth_similar=True,
        contact_touching=True,
        expect_severity=UP_TO_SUSPICIOUS,
        forbid_events={SecurityEventType.ASSET_PROXIMITY},
    ),
    Scenario(
        name="someone passes behind the car, depth says further away",
        detections=_walk(
            1,
            [
                (float(x), 150.0, float(x) + 48, 250.0)
                for x in (200, 260, 320, 380, 440)
            ],
        )
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        is_night=True,
        depth_similar=False,
        expect_severity=ROUTINE_ONLY,
        forbid_events={
            SecurityEventType.ASSET_PROXIMITY,
            SecurityEventType.CONTACT_CANDIDATE,
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
    Scenario(
        name="a passer-by crosses in front of the car, segmentation says touching",
        # Walking across the frame between the camera and the car. Their
        # silhouettes genuinely abut in the image, so the segmenter reports
        # a touch -- and depth is the one stage that can say they are metres
        # apart. This is the single most common thing a driveway camera
        # sees, and calling it contact is how a security system loses its
        # owner's trust.
        detections=_walk(
            1, [_person_at(x, height=160, ground=330) for x in (60, 150, 240, 330, 420)]
        )
        + _parked(2, MY_CAR, 5),
        frame_count=5,
        contact_touching=True,
        depth_similar=False,
        expect_severity=ROUTINE_ONLY,
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.IMPACT_CANDIDATE,
            SecurityEventType.ASSET_PROXIMITY,
        },
    ),
    # -- a freeform zone must be enforced as drawn, not as its bounds ---
    Scenario(
        name="a visitor parks on the kerb while our car is out",
        detections=[("car", 0.95, ON_THE_KERB, 3, i) for i in range(4)]
        + _walk(
            1,
            [
                (610.0, 140.0, 640.0, 250.0),
                (596.0, 141.0, 632.0, 251.0),
                (584.0, 140.0, 620.0, 250.0),
                (580.0, 140.0, 616.0, 250.0),
            ],
        ),
        frame_count=4,
        zone=TRACED_DRIVE,
        # The contact stage is aimed at whichever subject is nearest the
        # asset, so if the kerb car were mistaken for the protected one it
        # would return a genuine "touching" for the person getting into it
        # — a real measurement of the wrong car. The zone answering "none of
        # these is yours" is what has to stop that becoming an alert.
        contact_touching=True,
        depth_similar=True,
        expect_severity=ROUTINE_ONLY,
        forbid_events={
            SecurityEventType.ASSET_PROXIMITY,
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ZONE_ENTERED,
        },
    ),
    Scenario(
        name="a neighbour on the kerb while our car sits on the traced drive",
        detections=[("car", 0.95, ON_THE_DRIVE, 2, i) for i in range(4)]
        + [("car", 0.95, ON_THE_KERB, 3, i) for i in range(4)]
        + _walk(
            1,
            [
                (610.0, 140.0, 640.0, 250.0),
                (596.0, 141.0, 632.0, 251.0),
                (584.0, 140.0, 620.0, 250.0),
                (580.0, 140.0, 616.0, 250.0),
            ],
        ),
        frame_count=4,
        zone=TRACED_DRIVE,
        contact_touching=False,
        depth_similar=False,
        expect_severity=ROUTINE_ONLY,
        forbid_events={
            SecurityEventType.CONTACT_CANDIDATE,
            SecurityEventType.ZONE_ENTERED,
        },
    ),
    Scenario(
        name="a stranger at our car on the traced drive is still caught",
        detections=[("car", 0.95, ON_THE_DRIVE, 2, i) for i in range(5)]
        + _walk(
            1,
            [
                (400.0, 200.0, 440.0, 340.0),
                (330.0, 200.0, 370.0, 340.0),
                (270.0, 200.0, 310.0, 340.0),
                (250.0, 200.0, 290.0, 340.0),
                (250.0, 200.0, 290.0, 340.0),
            ],
        ),
        frame_count=5,
        zone=TRACED_DRIVE,
        contact_touching=True,
        depth_similar=True,
        expect_severity=(Severity.SUSPICIOUS, Severity.CRITICAL),
        expect_events={SecurityEventType.CONTACT_CANDIDATE},
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
    cap = scenario.max_event_severity
    if cap is not None:
        loud = [
            e
            for e in assessment.events
            if severity_rank(e.severity) > severity_rank(cap)
        ]
        assert not loud, (
            f"{scenario.name}: {[str(e.event_type) for e in loud]} claimed more than "
            f"{cap} on evidence that does not support it"
        )


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
