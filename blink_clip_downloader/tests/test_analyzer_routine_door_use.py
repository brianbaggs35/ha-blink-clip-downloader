"""``ai_routine_door_use_enabled`` through a whole clip analysis.

The rule lowers a verdict without knowing who the person was, so these run
real tracks through the real detector, scorer, narrative and verdict path --
only the AI provider's answer is canned -- and pin both halves: that the
front door's ordinary visits stop alerting, and every way that must not
happen. Each ``test_*_stays_flagged`` is a way for something to be cleared
that should not be.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.analyzer import AnalysisResult, ClipAnalyzer
from blink_downloader.analyzer.base import SecurityLayerSettings
from blink_downloader.face_outcomes import FaceTally
from blink_downloader.security import (
    Box,
    SecurityOutcome,
    Zone,
    build_marked_asset,
    build_tracks,
)
from blink_downloader.vision import FaceRecognitionResult, VisionHints

FRAME = (640.0, 360.0)
INTERVAL = 2.0
DOOR_ZONE = Zone.from_config(
    {"x_min": 0.094, "y_min": 0.333, "x_max": 0.203, "y_max": 0.833}
)


def _person(x: float) -> Box:
    return (x, 150.0, x + 60.0, 300.0)


#: Walks up, is at the door for two sightings, and is not seen again.
_GOING_IN = [400.0, 300.0, 200.0, 110.0, 90.0]
#: Stands at the door for most of the clip.
_LINGERING = [400.0, 300.0] + [110.0] * 8


def _hints(
    xs: list[float],
    *,
    frame_count: int = 14,
    face: FaceRecognitionResult | None = None,
    extra: Sequence[tuple[str, float, Box, int | None, int]] = (),
) -> VisionHints:
    door = build_marked_asset("Porch", "door1", "Front door", "door", DOOR_ZONE, FRAME)
    assert door is not None
    detections = [("person", 0.9, _person(x), 1, i) for i, x in enumerate(xs)]
    hints = VisionHints(face_recognition=face)
    hints.tracks = build_tracks([*detections, *extra], INTERVAL, FRAME)
    hints.frame_size = FRAME
    hints.scan_frame_count = frame_count
    hints.scan_interval = INTERVAL
    hints.marked_assets = [door]
    return hints


def _flagged(confidence: float = 0.6) -> str:
    return json.dumps(
        {
            "suspicious": True,
            "confidence": confidence,
            "description": "A person is working at the door lock.",
        }
    )


def _analyzer(
    *, routine_door_use: bool = True, risk_alert_threshold: int = 75
) -> ClipAnalyzer:
    return ClipAnalyzer(
        ollama_url="http://localhost:11434",
        model="llava",
        prompt="p",
        security_settings=SecurityLayerSettings(
            risk_alert_threshold=risk_alert_threshold,
            routine_door_use=routine_door_use,
        ),
    )


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=(100, 100, 100)).save(buf, format="JPEG")
    return buf.getvalue()


async def _analyze(
    hints: VisionHints,
    response: str,
    *,
    analyzer: ClipAnalyzer | None = None,
    anomaly_score: float = 0.0,
) -> tuple[AnalysisResult, AsyncMock]:
    a = analyzer or _analyzer()
    pipeline = MagicMock()
    pipeline.process_clip = AsyncMock(return_value=hints)
    a.attach_vision_pipeline(pipeline)
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(_jpeg() * 3, b""))
    proc.returncode = 0
    model = AsyncMock(return_value=response)
    a._call_model = model  # type: ignore[method-assign]
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await a.analyze_clip(
            "/clips/test.mp4", "c1", "Porch", anomaly_score=anomaly_score
        )
    return result, model


def _kinds(result: AnalysisResult) -> set[str]:
    return {str(e.event_type) for e in result.security_events}


# ----------------------------------------------------------------------
# The case this exists for
# ----------------------------------------------------------------------


async def test_an_ordinary_trip_through_the_front_door_is_not_an_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reported problem: unlocking the door with their back to the
    camera, a flag the model raised while unsure of itself."""
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer"):
        result, model = await _analyze(_hints(_GOING_IN), _flagged(0.6))

    assert result.is_suspicious is False
    assert result.routine_visit_applied is True
    assert result.to_dict()["routine_visit_applied"] is True
    # Nothing but the verdict moved: what the model said, and how sure, stand.
    assert result.summary == "A person is working at the door lock."
    assert result.confidence == pytest.approx(0.6)
    assert result.face_bypass_applied is False
    assert result.risk_override_applied is False
    # ...and the visit that justified it is on record for whoever audits it.
    assert "door_used" in _kinds(result)
    assert "Routine door use cleared suspicious flag for clip='c1'" in caplog.text
    assert '"Front door"' in caplog.text
    # The model was told what it was looking at.
    assert "went through the door they were at" in model.call_args.args[1]


async def test_the_same_clip_stays_flagged_with_the_option_off() -> None:
    """Off is every install's behaviour today, and must stay so."""
    result, model = await _analyze(
        _hints(_GOING_IN), _flagged(0.6), analyzer=_analyzer(routine_door_use=False)
    )

    assert result.is_suspicious is True
    assert result.routine_visit_applied is False
    assert "door_used" not in _kinds(result)
    assert "went through the door" not in model.call_args.args[1]


async def test_a_clip_the_model_found_unremarkable_is_left_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet = json.dumps({"suspicious": False, "confidence": 0.2, "description": "ok"})
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer"):
        result, _ = await _analyze(_hints(_GOING_IN), quiet)

    assert result.is_suspicious is False
    assert result.routine_visit_applied is False
    assert "Routine door use" not in caplog.text


async def test_walking_out_of_the_door_and_away_is_cleared_too() -> None:
    out = [100.0, 105.0, 110.0, 200.0, 300.0, 400.0]
    result, _ = await _analyze(_hints(out, frame_count=6), _flagged(0.55))
    assert result.routine_visit_applied is True
    assert result.is_suspicious is False


# ----------------------------------------------------------------------
# What must never be cleared
# ----------------------------------------------------------------------


async def test_lingering_at_the_door_stays_flagged() -> None:
    result, _ = await _analyze(_hints(_LINGERING, frame_count=12), _flagged(0.6))
    assert result.is_suspicious is True
    assert result.routine_visit_applied is False
    assert "door_used" not in _kinds(result)


async def test_a_confident_finding_stays_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer"):
        result, _ = await _analyze(_hints(_GOING_IN), _flagged(0.85))

    assert result.is_suspicious is True
    assert result.routine_visit_applied is False
    assert "Routine door use not applied to clip='c1'" in caplog.text
    assert "too sure (confidence 0.85)" in caplog.text


async def test_a_second_person_stays_flagged() -> None:
    behind = [("person", 0.9, _person(500.0), 2, i) for i in range(5)]
    result, _ = await _analyze(_hints(_GOING_IN, extra=behind), _flagged(0.6))
    assert result.is_suspicious is True
    assert result.routine_visit_applied is False


async def test_a_second_person_nobody_tracked_stays_flagged() -> None:
    untracked = [("person", 0.9, _person(500.0), None, i) for i in range(5)]
    result, _ = await _analyze(_hints(_GOING_IN, extra=untracked), _flagged(0.6))
    assert result.is_suspicious is True
    assert result.routine_visit_applied is False


async def test_a_camera_that_never_sees_this_hour_stays_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Decided before the model is asked, because a visit also reaches the
    prompt: telling it a trip through the door is ordinary at three in the
    morning on a camera that never sees anyone then would soften its own
    verdict on the clip that most needs it left alone."""
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer"):
        result, model = await _analyze(
            _hints(_GOING_IN), _flagged(0.6), anomaly_score=0.5
        )

    assert result.is_suspicious is True
    assert result.routine_visit_applied is False
    assert "door_used" not in _kinds(result)
    assert "went through the door" not in model.call_args.args[1]
    assert "Routine door use stands down for camera='Porch'" in caplog.text
    assert "rarely sees activity at this hour (anomaly 0.50)" in caplog.text


async def test_standing_down_is_only_logged_for_a_camera_with_a_door_marked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every clip with someone in it reaches this check; a camera that has no
    door marked has nothing for the rule to do and nothing to explain."""
    hints = _hints(_GOING_IN)
    hints.marked_assets = []
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer"):
        result, _ = await _analyze(hints, _flagged(0.6), anomaly_score=0.5)

    assert result.is_suspicious is True
    assert "Routine door use" not in caplog.text


async def test_a_marked_window_is_not_a_door() -> None:
    """Only a door is gone through."""
    hints = _hints(_GOING_IN)
    window = build_marked_asset("Porch", "w1", "Window", "window", DOOR_ZONE, FRAME)
    assert window is not None
    hints.marked_assets = [window]
    result, _ = await _analyze(hints, _flagged(0.6))
    assert result.is_suspicious is True
    assert "door_used" not in _kinds(result)


async def test_a_clip_with_no_security_evidence_stays_flagged() -> None:
    """Nothing tracked, nothing to rest a claim on."""
    result, _ = await _analyze(VisionHints(), _flagged(0.6))
    assert result.is_suspicious is True
    assert result.routine_visit_applied is False


async def test_a_clip_the_risk_score_forces_stays_flagged() -> None:
    """The deterministic score is the one thing that outranks this rule: a
    clip is never both cleared and force-flagged."""
    result, _ = await _analyze(
        _hints(_GOING_IN),
        _flagged(0.6),
        analyzer=_analyzer(risk_alert_threshold=1),
    )
    assert result.is_suspicious is True
    assert result.routine_visit_applied is False


# ----------------------------------------------------------------------
# What the face says
# ----------------------------------------------------------------------


def _stranger() -> FaceRecognitionResult:
    """A clear look at a face that matched nobody."""
    return FaceRecognitionResult(
        unrecognized_present=True, tally=FaceTally(seen=2, matched=0, unclear=0)
    )


async def test_a_clear_face_that_matches_nobody_stays_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer"):
        result, model = await _analyze(
            _hints(_GOING_IN, face=_stranger()), _flagged(0.5)
        )

    assert result.is_suspicious is True
    assert result.routine_visit_applied is False
    # With a stranger in the picture there is no visit to weigh at all: no
    # event on the timeline, and above all nothing in the prompt calling it
    # ordinary use of a door, which would soften the model's own verdict on
    # the clip that most needs it left alone.
    assert "door_used" not in _kinds(result)
    assert "went through the door" not in model.call_args.args[1]
    assert "does not match an approved household member" in caplog.text


async def test_a_recognized_person_who_is_not_approved_stays_flagged() -> None:
    nanny = FaceRecognitionResult(
        other_names=["Nanny"], tally=FaceTally(seen=1, matched=1)
    )
    result, _ = await _analyze(_hints(_GOING_IN, face=nanny), _flagged(0.5))
    assert result.is_suspicious is True
    assert "door_used" not in _kinds(result)


async def test_a_frame_nobody_could_examine_stays_flagged() -> None:
    blind = FaceRecognitionResult(
        unrecognized_present=True, tally=FaceTally(unexamined=2)
    )
    result, _ = await _analyze(_hints(_GOING_IN, face=blind), _flagged(0.5))
    assert result.is_suspicious is True
    assert "door_used" not in _kinds(result)


async def test_a_face_too_hard_to_read_neither_helps_nor_hurts() -> None:
    """Someone at a lock with their back half turned: MTCNN finds a profile
    that matches nobody. It is not a stranger, so it does not veto, and it is
    not a match, so it does not widen what is accepted either."""
    profile = FaceRecognitionResult(
        unrecognized_present=True, tally=FaceTally(seen=2, matched=0, unclear=2)
    )
    cleared, _ = await _analyze(_hints(_GOING_IN, face=profile), _flagged(0.6))
    assert cleared.routine_visit_applied is True

    kept, _ = await _analyze(_hints(_GOING_IN, face=profile), _flagged(0.8))
    assert kept.is_suspicious is True


async def test_a_household_member_matched_widens_what_is_accepted() -> None:
    """The bypass is withheld here (a blurred frame of the same person
    matched nobody), so it is the door rule that clears: a model this sure is
    only accepted because a face the add-on trusts was in the clip."""
    matched = FaceRecognitionResult(
        approved_names=["Brian"],
        unrecognized_present=True,
        tally=FaceTally(seen=3, matched=1, unclear=2),
    )
    result, _ = await _analyze(_hints(_GOING_IN, face=matched), _flagged(0.8))

    assert result.face_bypass_applied is False
    assert result.is_suspicious is False
    assert result.routine_visit_applied is True
    # A model that sure, with nobody identified, would have stayed flagged.
    unknown, _ = await _analyze(_hints(_GOING_IN), _flagged(0.8))
    assert unknown.is_suspicious is True


async def test_a_household_member_matched_makes_an_unusual_hour_acceptable() -> None:
    """The face bypass has never cared when someone comes home."""
    matched = FaceRecognitionResult(
        approved_names=["Brian"],
        unrecognized_present=True,
        tally=FaceTally(seen=3, matched=1, unclear=2),
    )
    result, _ = await _analyze(
        _hints(_GOING_IN, face=matched), _flagged(0.6), anomaly_score=0.9
    )
    assert result.routine_visit_applied is True


async def test_even_with_a_match_a_very_confident_finding_stays_flagged() -> None:
    matched = FaceRecognitionResult(
        approved_names=["Brian"],
        unrecognized_present=True,
        tally=FaceTally(seen=3, matched=1, unclear=2),
    )
    result, _ = await _analyze(_hints(_GOING_IN, face=matched), _flagged(0.9))
    assert result.is_suspicious is True


async def test_the_face_bypass_clears_first_and_is_recorded_as_itself() -> None:
    """Identity outranks behaviour: a clip a recognized household member
    cleared is not also credited to the door rule."""
    recognized = FaceRecognitionResult(
        approved_names=["Brian"], tally=FaceTally(seen=1, matched=1)
    )
    result, _ = await _analyze(_hints(_GOING_IN, face=recognized), _flagged(0.6))

    assert result.is_suspicious is False
    assert result.face_bypass_applied is True
    assert result.routine_visit_applied is False


# ----------------------------------------------------------------------
# The wiring
# ----------------------------------------------------------------------


def test_the_rule_is_off_unless_the_settings_turn_it_on() -> None:
    assert SecurityLayerSettings().routine_door_use is False
    assert (
        ClipAnalyzer(
            ollama_url="http://localhost:11434", model="m", prompt="p"
        )._routine_door_use
        is False
    )
    assert _analyzer()._routine_door_use is True


def test_it_declines_without_a_security_outcome() -> None:
    analyzer = _analyzer()
    assert analyzer._routine_visit_clears(None, None, 0.5, "c1", "Porch") is False
    assert (
        analyzer._routine_visit_clears(SecurityOutcome(), None, 0.5, "c1", "Porch")
        is False
    )


def test_an_analysis_result_is_not_a_routine_visit_by_default() -> None:
    result = AnalysisResult(
        clip_id="c1",
        camera="Porch",
        model="m",
        response_text="",
        is_suspicious=True,
        confidence=0.8,
        summary="s",
        frame_count=3,
        analysis_duration=1.0,
        analyzed_at="2026-01-05T00:00:00+00:00",
    )
    assert result.routine_visit_applied is False
    assert result.to_dict()["routine_visit_applied"] is False
