"""One person's trip through a marked door, as the detector sees it.

``DOOR_USED`` is the one event that exists to be read as reassurance, and it
can lower a verdict, so these tests are mostly about what it refuses: every
way a visit can fail to prove itself brief, finished and alone is pinned
here, because each is a way for someone working at a lock to be taken for a
resident using one.
"""

from __future__ import annotations

import pytest

from blink_downloader.security.assets import (
    AssetLocation,
    AssetType,
    ProtectedAsset,
    build_marked_asset,
)
from blink_downloader.security.detector import (
    DetectionContext,
    DetectorThresholds,
    SecurityEventDetector,
)
from blink_downloader.security.events import (
    BYPASS_BLOCKING_EVENTS,
    SecurityEvent,
    SecurityEventType,
    Severity,
)
from blink_downloader.security.geometry import Box, Zone, box_touches_frame_edge
from blink_downloader.security.tracks import build_tracks

FRAME = (640.0, 360.0)

#: A front door on the left of a porch camera's view: 70px wide, 180px tall.
DOOR_ZONE = Zone.from_config(
    {"x_min": 0.094, "y_min": 0.333, "x_max": 0.203, "y_max": 0.833}
)
#: The right-hand fifth of the picture, for the tests about the frame's border.
RIGHT_EDGE_ZONE = Zone.from_config(
    {"x_min": 0.8, "y_min": 0.2, "x_max": 1.0, "y_max": 0.95}
)
#: The whole porch, as someone who draws round everything might mark it.
PORCH_ZONE = Zone.from_config({"x_min": 0.0, "y_min": 0.2, "x_max": 1.0, "y_max": 0.95})

Detection = tuple[str, float, Box, int | None, int]


def _person_at(x: float, *, height: float = 150.0, ground: float = 300.0) -> Box:
    return (x, ground - height, x + height * 0.4, ground)


def _walk(
    boxes: list[Box], *, label: str = "person", track_id: int | None = 1
) -> list[Detection]:
    return [(label, 0.9, box, track_id, i) for i, box in enumerate(boxes)]


def _at(*xs: float) -> list[Box]:
    return [_person_at(x) for x in xs]


def _door(zone: Zone | None = DOOR_ZONE, asset_type: str = "door") -> ProtectedAsset:
    asset = build_marked_asset("Porch", "door1", "Front door", asset_type, zone, FRAME)
    assert asset is not None
    return asset


def _events(
    detections: list[Detection],
    frame_count: int,
    *,
    interval: float = 2.0,
    asset: ProtectedAsset | None = None,
    detect_door_use: bool = True,
    thresholds: DetectorThresholds | None = None,
    **kwargs: object,
) -> list[SecurityEvent]:
    tracks = build_tracks(detections, interval, FRAME)
    return SecurityEventDetector(thresholds).detect(
        DetectionContext(
            camera="Porch",
            tracks=tracks,
            frame_interval=interval,
            frame_count=frame_count,
            marked_assets=[asset or _door()],
            detect_door_use=detect_door_use,
            **kwargs,  # type: ignore[arg-type]
        )
    )


def _door_use(events: list[SecurityEvent]) -> list[SecurityEvent]:
    return [e for e in events if e.event_type is SecurityEventType.DOOR_USED]


#: Walks up, spends two sightings at the door, and is gone: x=110 and x=90
#: are inside the zone, the rest are not.
_GOING_IN = _at(400, 300, 200, 110, 90)
#: Starts at the door (inside the zone for three sightings), then walks away.
_COMING_OUT = _at(100, 105, 110, 200, 300, 400)


# ----------------------------------------------------------------------
# What a visit looks like
# ----------------------------------------------------------------------


def test_walking_up_to_a_door_and_not_being_seen_again_is_going_in() -> None:
    (visit,) = _door_use(_events(_walk(_GOING_IN), 8))

    assert visit.severity is Severity.ROUTINE
    assert visit.asset_name == "Front door"
    assert visit.asset_type == "door"
    assert visit.subject_label == "person"
    assert visit.track_id == 1
    assert visit.confidence == pytest.approx(0.9)
    assert visit.evidence["direction"] == "in"
    # Two sightings 2s apart: 2s seen, and a sampling gap each side on top.
    assert visit.evidence["dwell_seconds"] == 2.0
    assert visit.evidence["dwell_ceiling_seconds"] == 6.0
    assert visit.evidence["sightings_at_door"] == 2
    # Last seen at frame 4 of 8: three frames, six seconds, with nobody.
    assert visit.evidence["unseen_seconds"] == 6.0
    assert (visit.start_offset, visit.end_offset) == (6.0, 8.0)
    assert '"Front door"' in visit.detail
    assert "under 6s" in visit.detail
    assert "not seen again" in visit.detail
    # A statement of consistency, never of fact.
    assert "consistent with" in visit.detail


def test_starting_at_the_door_and_walking_away_is_coming_out() -> None:
    (visit,) = _door_use(_events(_walk(_COMING_OUT), 6))

    assert visit.evidence["direction"] == "out"
    assert visit.evidence["dwell_seconds"] == 4.0
    assert visit.evidence["dwell_ceiling_seconds"] == 8.0
    assert visit.evidence["sightings_at_door"] == 3
    assert "unseen_seconds" not in visit.evidence
    assert (visit.start_offset, visit.end_offset) == (0.0, 4.0)
    assert "when first seen" in visit.detail
    assert "walked away" in visit.detail
    assert "under 8s" in visit.detail


def test_already_at_the_door_when_the_clip_began_can_still_go_in() -> None:
    """Motion starts a clip, so someone can be at the door in its first frame
    and still go through it; how long they were there before is unseen."""
    (visit,) = _door_use(_events(_walk(_at(100, 100, 110)), 8))
    assert visit.evidence["direction"] == "in"
    assert visit.evidence["sightings_at_door"] == 3


def test_a_visit_is_only_reported_when_asked_for() -> None:
    """Off, no such event exists: every clip is assessed as it always was."""
    events = _events(_walk(_GOING_IN), 8, detect_door_use=False)
    assert _door_use(events) == []
    # ...while the facts the visit also produces are unchanged.
    assert {"zone_entered", "asset_proximity"} <= {str(e.event_type) for e in events}


def test_the_event_is_never_one_that_withholds_the_face_bypass() -> None:
    assert SecurityEventType.DOOR_USED not in BYPASS_BLOCKING_EVENTS


@pytest.mark.parametrize("asset_type", ["gate", "garage", "package_area", "mailbox"])
def test_only_a_door_is_gone_through(asset_type: str) -> None:
    """Someone who walks through a gate carries on across the picture, and a
    parcel spot or a mailbox is not entered at all."""
    assert (
        _door_use(_events(_walk(_GOING_IN), 8, asset=_door(asset_type=asset_type)))
        == []
    )


def test_only_a_marked_door_has_the_property() -> None:
    assert _door().entered_through is True
    assert (
        ProtectedAsset(
            name="x", asset_type=AssetType.VEHICLE, camera="c"
        ).entered_through
        is False
    )
    for kind in AssetType:
        expected = kind is AssetType.DOOR
        assert (
            ProtectedAsset(name="x", asset_type=kind, camera="c").entered_through
            is expected
        )


# ----------------------------------------------------------------------
# What is not a visit
# ----------------------------------------------------------------------


def test_coming_up_to_the_door_and_leaving_again_is_not_going_through_it() -> None:
    """Trying a handle and walking off: the model's to judge, and the plain
    retreat is the record of it."""
    events = _events(_walk(_at(320, 220, 130, 110, 110, 220)), 6)
    assert _door_use(events) == []
    assert "retreat" in {str(e.event_type) for e in events}


def test_standing_at_the_door_for_the_whole_clip_is_not_a_visit() -> None:
    events = _events(_walk(_at(*[110] * 10)), 10)
    assert _door_use(events) == []
    assert "loitering" in {str(e.event_type) for e in events}


def test_walking_past_the_door_is_not_a_visit() -> None:
    assert _door_use(_events(_walk(_at(400, 300, 220, 140, 100, 300)), 6)) == []


def test_still_at_the_door_when_the_clip_ends_is_not_going_through_it() -> None:
    """One frame of silence could be a dropped detection; the clip has to go
    on without them for several."""
    # Last seen at frame 4 of 6: only frame 5 follows.
    assert _door_use(_events(_walk(_GOING_IN), 6)) == []
    # ...and last seen in the clip's final frame.
    assert _door_use(_events(_walk(_GOING_IN), 5)) == []
    # Two frames after is the least that counts.
    assert len(_door_use(_events(_walk(_GOING_IN), 7))) == 1


def test_frames_close_together_need_the_seconds_as_well_as_the_frames() -> None:
    """At 0.5s spacing three frames are a second and a half, which is a
    blink; the same shape sampled every 2s is far enough."""
    fine = _events(_walk(_GOING_IN), 8, interval=0.5)
    assert _door_use(fine) == []
    # Seven frames of silence at 0.5s is 3.5s: enough.
    assert len(_door_use(_events(_walk(_GOING_IN), 12, interval=0.5))) == 1


def test_a_visit_must_be_provably_brief_from_the_frames_it_has() -> None:
    """The same shape, sampled further and further apart, stops being
    provable: arriving just after one frame and leaving just before the
    next could be most of two intervals either side of what was seen."""
    dense = _events(_walk(_GOING_IN), 14, interval=1.0)
    sparse = _events(_walk(_GOING_IN), 14, interval=6.0)
    assert len(_door_use(dense)) == 1
    assert _door_use(sparse) == []


@pytest.mark.parametrize(
    ("interval", "qualifies"),
    [
        # Two sightings: 1 interval seen, plus an interval each side = 3.
        (4.0, True),  # 12s, which is the limit and is allowed
        (4.01, False),
        (3.9, True),
    ],
)
def test_the_limit_is_the_lingering_limit(interval: float, qualifies: bool) -> None:
    events = _events(_walk(_GOING_IN), 14, interval=interval)
    assert bool(_door_use(events)) is qualifies


def test_a_longer_stay_is_lingering_and_never_a_visit() -> None:
    events = _events(_walk(_at(400, 300, *[110] * 7)), 16)
    assert _door_use(events) == []
    assert "loitering" in {str(e.event_type) for e in events}


def test_leaving_the_door_and_coming_back_is_not_one_visit() -> None:
    """Trying it, stepping back, trying again. Also what a detection that
    flickers across the zone's edge looks like, which errs toward leaving the
    verdict alone."""
    assert _door_use(_events(_walk(_at(400, 110, 300, 110, 90)), 10)) == []


def test_a_track_of_two_sightings_proves_nothing() -> None:
    assert _door_use(_events(_walk(_at(200, 110)), 8)) == []


def test_the_least_a_visit_may_rest_on_is_tunable() -> None:
    events = _events(
        _walk(_at(200, 110)),
        8,
        thresholds=DetectorThresholds(door_use_min_sightings=2),
    )
    assert len(_door_use(events)) == 1


def test_a_dog_going_through_the_door_is_not_a_person_using_it() -> None:
    assert _door_use(_events(_walk(_GOING_IN, label="dog"), 8)) == []


def test_a_track_the_tracker_did_not_follow_is_not_enough() -> None:
    """Without tracker ids the 'track' is inferred from overlap between
    frames seconds apart, which cannot say it is one person."""
    events = _events(_walk(_GOING_IN, track_id=None), 8)
    assert _door_use(events) == []


def test_the_depth_stage_placing_them_elsewhere_stands_the_visit_down() -> None:
    """Overlapping the door in the picture while standing metres in front of
    it is not at the door; the zone events stand down for it already."""
    for key, depth, expected in (
        ("door1", False, 0),
        ("door1", True, 1),
        ("door1", None, 1),
        # A verdict about a different asset says nothing about this one.
        ("other", False, 1),
    ):
        events = _events(
            _walk(_GOING_IN), 8, examined_asset_key=key, depth_similar=depth
        )
        assert len(_door_use(events)) == expected, (key, depth)


def test_a_door_without_a_zone_cannot_be_gone_through() -> None:
    asset = ProtectedAsset(
        name="Front door",
        asset_type=AssetType.DOOR,
        camera="Porch",
        box=(60.0, 120.0, 130.0, 300.0),
        location=AssetLocation.ZONE,
        marked=True,
    )
    assert _door_use(_events(_walk(_GOING_IN), 8, asset=asset)) == []


# ----------------------------------------------------------------------
# Leaving the picture is not going indoors
# ----------------------------------------------------------------------


def test_walking_off_the_edge_of_a_wide_zone_is_not_going_in() -> None:
    """A zone drawn over the whole porch: someone who crosses it and leaves
    by the side of the picture is last seen 'at the door' and then never
    again, which is exactly what going in looks like."""
    leaving = _walk(_at(100, 200, 300, 400, 500, 580))
    assert box_touches_frame_edge(_person_at(580), FRAME) is True
    assert _door_use(_events(leaving, 12, asset=_door(PORCH_ZONE))) == []

    # The same walk ending mid-frame is the visit it was taken for.
    stopping = _walk(_at(100, 200, 300, 400))
    assert len(_door_use(_events(stopping, 12, asset=_door(PORCH_ZONE)))) == 1


def test_coming_in_from_the_edge_of_the_picture_is_not_coming_out() -> None:
    """The same test for the other direction: somebody who walks into a zone
    that reaches the border has not come out of anything."""
    from_the_edge = _walk(_at(580, 540, 400, 300))
    assert _door_use(_events(from_the_edge, 6, asset=_door(RIGHT_EDGE_ZONE))) == []

    from_the_door = _walk(_at(500, 520, 400, 300))
    (visit,) = _door_use(_events(from_the_door, 6, asset=_door(RIGHT_EDGE_ZONE)))
    assert visit.evidence["direction"] == "out"


def test_hovering_at_the_edge_of_the_zone_is_not_walking_away() -> None:
    """Out of the zone by a pixel is not clear of the door."""
    hovering = _walk(_at(112, 112, 112, 115, 115, 115))
    assert _door_use(_events(hovering, 6)) == []


@pytest.mark.parametrize(
    ("box", "touching"),
    [
        ((100.0, 50.0, 160.0, 200.0), False),
        ((0.0, 50.0, 60.0, 200.0), True),
        ((6.4, 50.0, 60.0, 200.0), True),
        ((7.0, 50.0, 60.0, 200.0), False),
        ((580.0, 50.0, 640.0, 200.0), True),
        ((580.0, 50.0, 633.5, 200.0), False),
        ((100.0, 50.0, 160.0, 360.0), True),
        ((100.0, 50.0, 160.0, 356.5), True),
        ((100.0, 50.0, 160.0, 356.0), False),
        ((100.0, 50.0, 160.0, 350.0), False),
        # The top is not a border someone leaves through.
        ((100.0, 0.0, 160.0, 200.0), False),
    ],
)
def test_which_borders_count_as_leaving_the_picture(box: Box, touching: bool) -> None:
    assert box_touches_frame_edge(box, FRAME) is touching


# ----------------------------------------------------------------------
# Invariants over random clips
# ----------------------------------------------------------------------


def _random_clip(
    seed: int,
) -> tuple[list[Detection], int, float]:
    """Someone wandering about a porch: a random walk with dropped
    detections, sampled anywhere from every half second to every six -- and,
    half the time, gone from the picture partway through, which is the only
    way a clip can show somebody going in."""
    import random

    rng = random.Random(seed)
    interval = rng.choice([0.5, 1.0, 2.0, 3.0, 4.0, 6.0])
    frames = rng.randint(4, 24)
    x = rng.uniform(20, 560) if rng.random() < 0.5 else rng.uniform(60, 130)
    vanishes_at = rng.randint(3, frames) if rng.random() < 0.5 else frames
    detections: list[Detection] = []
    for i in range(vanishes_at):
        x = min(580.0, max(0.0, x + rng.uniform(-90, 90)))
        if rng.random() < 0.9:
            detections.append(("person", 0.9, _person_at(x), 1, i))
    return detections, frames, interval


_RANDOM_CASES = range(600)


def test_random_clips_never_break_what_a_visit_promises() -> None:
    """Whatever the walk, a visit the detector reports has proved what the
    rule says it proves: brief even allowing for the gaps between frames, one
    stay, gone for long enough, and never both lingering and brief."""
    fired = 0
    for seed in _RANDOM_CASES:
        detections, frames, interval = _random_clip(seed)
        if not detections:
            continue
        events = _events(detections, frames, interval=interval)
        visits = _door_use(events)
        assert len(visits) <= 1, seed
        if not visits:
            continue
        fired += 1
        (visit,) = visits
        ev = visit.evidence
        assert ev["direction"] in ("in", "out"), seed
        assert ev["dwell_ceiling_seconds"] <= 12.0, seed  # type: ignore[operator]
        assert ev["dwell_seconds"] + 2 * interval == pytest.approx(  # type: ignore[operator]
            ev["dwell_ceiling_seconds"]
        ), seed
        assert len(detections) >= 3, seed
        if ev["direction"] == "in":
            assert ev["unseen_seconds"] >= 3.0, seed  # type: ignore[operator]
        if interval >= 6.0:
            assert ev["sightings_at_door"] == 1, seed
        # A stay that can be proved brief is, by construction, not one the
        # lingering rule fires on.
        assert "loitering" not in {
            str(e.event_type) for e in events if e.asset_name == "Front door"
        }, seed
    # Not vacuous: the generator makes both kinds of clip.
    assert 10 < fired < len(_RANDOM_CASES)


def test_random_clips_with_a_second_person_are_never_explained() -> None:
    from blink_downloader.security import EvidenceQuality, routine_door_visit

    explained = 0
    for seed in _RANDOM_CASES:
        detections, frames, interval = _random_clip(seed)
        if not detections:
            continue
        other = [("person", 0.9, _person_at(500.0), 2, 0)]
        events = _events([*detections, *other], frames, interval=interval)
        assert routine_door_visit(events, EvidenceQuality(score=0.9)) is None, seed
        alone = _events(detections, frames, interval=interval)
        explained += routine_door_visit(alone, EvidenceQuality(score=0.9)) is not None
    assert explained > 0


def test_random_clips_with_the_option_off_report_no_visit() -> None:
    for seed in _RANDOM_CASES:
        detections, frames, interval = _random_clip(seed)
        if detections:
            events = _events(
                detections, frames, interval=interval, detect_door_use=False
            )
            assert _door_use(events) == [], seed
