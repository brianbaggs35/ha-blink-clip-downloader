"""Assets marked on the Assets tab, through the analyzer.

The promise the Assets tab makes is two-sided, and these tests hold the
analyzer to both halves: a camera with nothing marked is analyzed exactly as
it was before assets existed — same prompt, byte for byte, same frames, same
escalation — and a camera with assets marked gets more attention where they
are: named in the prompt, favoured in frame selection, double-checked by the
escalation tier, and evaluated by the security layer.
"""

from __future__ import annotations

import io
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from blink_downloader import frame_motion
from blink_downloader.analyzer import AnthropicAnalyzer, BaseAnalyzer, ClipAnalyzer
from blink_downloader.security import build_tracks
from blink_downloader.vision.pipeline import VisionHints

DOOR = {
    "id": "door00000001",
    "camera": "Porch",
    "name": "Front door",
    "asset_type": "door",
    "description": "red door with a brass knocker",
    "zone": {"shape": "rect", "x_min": 0.05, "y_min": 0.3, "x_max": 0.2, "y_max": 0.8},
    "enabled": True,
}
BIKE = {
    "id": "bike00000001",
    "camera": "Porch",
    "name": "Bike",
    "asset_type": "bicycle",
    "description": "",
    "zone": {
        "shape": "polygon",
        "points": [[0.6, 0.5], [0.9, 0.5], [0.9, 0.9], [0.6, 0.9]],
    },
    "enabled": True,
}
OFF = {**BIKE, "id": "bike00000002", "name": "Old bike", "enabled": False}
GARAGE = {**DOOR, "id": "garage000001", "camera": "Driveway", "name": "Garage"}


def _analyzer(**kwargs: Any) -> ClipAnalyzer:
    return ClipAnalyzer(
        ollama_url="http://localhost:11434", model="llava", prompt="Watch.", **kwargs
    )


def _jpeg(bar_x: int) -> bytes:
    img = Image.new("L", (64, 64), 20)
    for x in range(bar_x, min(64, bar_x + 10)):
        for y in range(64):
            img.putpixel((x, y), 220)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG")
    return buf.getvalue()


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------


def test_only_enabled_assets_apply_grouped_by_camera() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR, BIKE, OFF, GARAGE])
    assert [x["name"] for x in a._marked_assets_for("Porch")] == ["Front door", "Bike"]
    assert [x["name"] for x in a._marked_assets_for("Driveway")] == ["Garage"]
    assert a._marked_assets_for("Backyard") == []


def test_update_is_a_full_replace() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR])
    a.update_protected_assets([])
    assert a._marked_assets_for("Porch") == []


def test_asset_protection_covers_marked_cameras_and_the_car() -> None:
    a = _analyzer(car_description="Silver Kia", car_cameras=["Driveway"])
    a.update_protected_assets([DOOR])
    assert a._asset_protection_applies("Porch") is True
    assert a._asset_protection_applies("Driveway") is True
    assert a._asset_protection_applies("Backyard") is False


def test_rename_carries_marked_assets() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR, BIKE, GARAGE])
    a.rename_camera("Porch", "Front Porch")
    assert a._marked_assets_for("Porch") == []
    moved = a._marked_assets_for("Front Porch")
    assert [x["name"] for x in moved] == ["Front door", "Bike"]
    assert {x["camera"] for x in moved} == {"Front Porch"}
    # Merged into whatever the new name already had, not overwritten.
    a.rename_camera("Driveway", "Front Porch")
    assert len(a._marked_assets_for("Front Porch")) == 3


# ----------------------------------------------------------------------
# The prompt
# ----------------------------------------------------------------------


def test_a_camera_with_nothing_marked_gets_the_same_prompt() -> None:
    before = _analyzer()._build_prompt(
        "Backyard", clip_duration=5.0, scene_deviation=0.01
    )
    a = _analyzer()
    a.update_protected_assets([DOOR, BIKE])
    after = a._build_prompt(
        "Backyard",
        clip_duration=5.0,
        scene_deviation=0.01,
        asset_motion_shares=[],
    )
    assert after == before


def test_marked_assets_are_named_with_where_they_are() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR, BIKE])
    prompt = a._build_prompt("Porch")
    assert "PROTECTED ASSETS" in prompt
    assert '"Front door" (a door) — in the left of the frame' in prompt
    assert "looks like: red door with a brass knocker" in prompt
    assert '"Bike" (a bike or scooter) — in the lower right of the frame' in prompt
    assert "Doors, gates and garage doors" in prompt
    assert "Bikes, equipment and other marked items" in prompt
    # Only the rules for what this camera actually has.
    assert "Mailboxes" not in prompt
    assert "Delivery spots" not in prompt


def test_assets_sit_with_the_camera_context_in_the_cached_prefix() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR])
    a._current_camera = "Porch"
    prefix, tail = a._split_cache_prefix(a._build_prompt("Porch", clip_duration=40))
    assert "PROTECTED ASSETS" in prefix
    assert "PROTECTED ASSETS" not in tail


def test_short_clips_are_not_waved_through_at_marked_assets() -> None:
    a = _analyzer()
    a.update_protected_assets([BIKE])
    marked = a._build_prompt("Porch", clip_duration=5.0)
    unmarked = a._build_prompt("Backyard", clip_duration=5.0)
    assert "brevity is no reason" in marked
    assert "SHORT EVENT" in unmarked and "brevity is no reason" not in unmarked


def test_a_calm_scene_baseline_is_not_offered_at_marked_assets() -> None:
    """The bike on its stand *is* the usual background: a close match says
    nothing about whether somebody is at it now."""
    a = _analyzer()
    a.update_protected_assets([BIKE])
    assert "Favor a calm" not in a._build_prompt("Porch", scene_deviation=0.01)
    assert "Favor a calm" in a._build_prompt("Backyard", scene_deviation=0.01)


def test_asset_motion_reaches_the_prompt() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR])
    prompt = a._build_prompt(
        "Porch", asset_motion_shares=[("Front door", 0.64), ("Bike", 0.05)]
    )
    assert 'ASSET MOTION: Of this clip\'s overall motion, 64% at "Front door"' in prompt
    assert '"Bike"' not in prompt.split("ASSET MOTION")[1].split("\n\n")[0]


def test_the_training_prompt_includes_marked_assets() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR])
    assert "PROTECTED ASSETS" in a.base_prompt_for_camera("Porch")


# ----------------------------------------------------------------------
# Frames, motion, vision, security
# ----------------------------------------------------------------------


async def test_frame_selection_favours_asset_zones() -> None:
    a = _analyzer(max_frames=2)
    a.update_protected_assets([DOOR])
    frames = [_jpeg(x) for x in (2, 18, 34, 50, 2, 18)]
    with patch.object(
        BaseAnalyzer, "_select_best_frames", wraps=BaseAnalyzer._select_best_frames
    ) as select:
        await a._downselect_frames(frames, clip_duration=0.0, camera="Porch")
    assert select.call_args[0][2] == (0.05, 0.3, 0.2, 0.8)


async def test_frame_selection_ranks_by_the_car_zone_and_every_asset() -> None:
    car_zone = {"x_min": 0.3, "y_min": 0.4, "x_max": 0.5, "y_max": 0.9}
    a = _analyzer(
        max_frames=2, car_description="Silver Kia", car_zones={"Porch": car_zone}
    )
    a.update_protected_assets([DOOR, BIKE])
    frames = [_jpeg(x) for x in (2, 18, 34, 50, 2, 18)]
    with patch.object(
        frame_motion, "frame_motion_diffs", wraps=frame_motion.frame_motion_diffs
    ) as diffs:
        await a._downselect_frames(frames, clip_duration=0.0, camera="Porch")
    assert diffs.call_args[0][1] == [
        (0.3, 0.4, 0.5, 0.9),
        (0.05, 0.3, 0.2, 0.8),
        (0.6, 0.5, 0.9, 0.9),
    ]


def test_several_zones_count_a_shared_pixel_once() -> None:
    frames = [_jpeg(x) for x in (2, 30)]
    one = frame_motion.frame_motion_diffs(frames, (0.0, 0.0, 0.5, 1.0))
    overlapping = frame_motion.frame_motion_diffs(
        frames, [(0.0, 0.0, 0.5, 1.0), (0.2, 0.0, 0.5, 1.0)]
    )
    assert overlapping == one


async def test_asset_motion_is_measured_only_where_assets_are_marked() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR])
    frames = [_jpeg(x) for x in (0, 4, 8)]
    thumbs = await a._maybe_compute_motion_thumbnails(frames[:2], "Porch")
    assert thumbs is not None
    shares = await a._maybe_compute_asset_motion(thumbs, "Porch")
    assert [name for name, _share in shares] == ["Front door"]
    assert shares[0][1] > 0.5
    assert await a._maybe_compute_asset_motion(thumbs, "Backyard") == []
    assert await a._maybe_compute_asset_motion(None, "Porch") == []
    # Too few frames for a trajectory, and nothing marked: no thumbnails.
    assert await a._maybe_compute_motion_thumbnails(frames[:2], "Backyard") is None


def test_asset_motion_shares_skip_what_cannot_be_attributed() -> None:
    still = [_jpeg(10), _jpeg(10)]
    thumbs = frame_motion.grayscale_thumbnails(still)
    assert frame_motion.asset_motion_shares(thumbs, [DOOR]) == []


async def test_the_vision_pipeline_is_given_this_cameras_assets() -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR, GARAGE])
    pipeline = MagicMock()
    pipeline.process_clip = AsyncMock(return_value=VisionHints())
    a.attach_vision_pipeline(pipeline)
    await a._apply_vision_pipeline([b"frame"], "Porch")
    assert pipeline.process_clip.call_args.kwargs["marked_assets"] == [DOOR]


def test_the_security_layer_evaluates_marked_assets() -> None:
    from blink_downloader.security import build_marked_asset
    from blink_downloader.security.geometry import Zone

    frame = (640.0, 360.0)
    bike = build_marked_asset(
        "Porch",
        "bike00000001",
        "Bike",
        "bicycle",
        Zone.from_config(BIKE["zone"]),
        frame,
    )
    assert bike is not None
    detections = [
        ("person", 0.9, (x, 150.0, x + 60.0, 310.0), 1, i)
        for i, x in enumerate((300.0, 360.0, 400.0, 400.0, 300.0))
    ]
    hints = VisionHints(
        tracks=build_tracks(detections, 2.0, frame),
        scan_frame_count=5,
        scan_interval=2.0,
        frame_size=frame,
        marked_assets=[bike],
        examined_asset_key="bike00000001",
        contact_touching=True,
        marked_asset_changes={"bike00000001": 0.6},
    )
    a = _analyzer()
    outcome = a._assess_security(
        "Porch",
        hints,
        clip_timestamp="",
        scene_deviation=None,
        frames_analyzed=5,
        target_frames=5,
    )
    assert outcome is not None
    kinds = {str(e.event_type) for e in outcome.events}
    assert {"contact_candidate", "asset_disturbed"} <= kinds
    assert all(e.asset_name == "Bike" for e in outcome.events if e.asset_name)


# ----------------------------------------------------------------------
# Escalation and logging
# ----------------------------------------------------------------------


async def test_a_clear_verdict_on_a_marked_camera_is_double_checked() -> None:
    a = _analyzer()
    a.update_protected_assets([BIKE])
    a._current_camera = "Porch"
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.9, "description": "Quiet"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": true, "confidence": 0.8, "description": "Bike taken"}'
    )
    a.set_escalation_analyzer(tier2)
    result = await a._call_model_with_escalation([b"frame"], "prompt")
    tier2._call_model.assert_awaited_once()
    assert "Bike taken" in result


async def test_a_clear_verdict_on_an_unmarked_camera_is_trusted() -> None:
    a = _analyzer()
    a.update_protected_assets([BIKE])
    a._current_camera = "Backyard"
    a._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value='{"suspicious": false, "confidence": 0.9, "description": "Quiet"}'
    )
    tier2 = AnthropicAnalyzer(api_key="key", model="claude-opus-4-5", prompt="test")
    tier2._call_model = AsyncMock()  # type: ignore[method-assign]
    a.set_escalation_analyzer(tier2)
    await a._call_model_with_escalation([b"frame"], "prompt")
    tier2._call_model.assert_not_awaited()


def test_the_summary_line_counts_marked_assets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = _analyzer()
    a.update_protected_assets([DOOR, BIKE])
    with caplog.at_level(logging.INFO, logger="blink_downloader.analyzer.base"):
        a._log_analysis_summary("clip1", "Porch", [b"f"], None, None, False)
    assert "assets=2" in caplog.text


def test_the_example_phrase_uses_the_cameras_own_asset() -> None:
    """A camera watching a bike must not be shown an example about a door —
    and "AC unit" keeps its capitals."""
    a = _analyzer()
    a.update_protected_assets([{**BIKE, "name": "AC unit", "asset_type": "equipment"}])
    prompt = a._build_prompt("Porch")
    assert "'a person is standing at the AC unit'" in prompt
    assert "front door" not in prompt


# ----------------------------------------------------------------------
# A whole property: six cameras, the car on two, assets on two, two bare
# ----------------------------------------------------------------------

_HOUSE_ASSETS = [
    {**DOOR, "id": "house0000001", "camera": "Front Door", "name": "Front door"},
    {
        **DOOR,
        "id": "house0000002",
        "camera": "Front Door",
        "name": "Mailbox",
        "asset_type": "mailbox",
        "zone": {
            "shape": "rect",
            "x_min": 0.7,
            "y_min": 0.4,
            "x_max": 0.85,
            "y_max": 0.75,
        },
    },
    {
        **BIKE,
        "id": "house0000003",
        "camera": "Garage",
        "name": "Barbecue",
        "asset_type": "equipment",
    },
]


def _house() -> ClipAnalyzer:
    a = _analyzer(
        car_description="Silver Kia sedan", car_cameras=["Driveway 1", "Driveway 2"]
    )
    a.update_protected_assets(_HOUSE_ASSETS)
    return a


@pytest.mark.parametrize(
    ("camera", "vehicle_rules", "assets", "high_recall"),
    [
        ("Driveway 1", True, [], True),
        ("Driveway 2", True, [], True),
        ("Front Door", False, ["Front door", "Mailbox"], True),
        ("Garage", False, ["Barbecue"], True),
        ("Back Door", False, [], False),
        ("Walkway", False, [], False),
    ],
)
def test_each_camera_of_a_whole_property_gets_its_own_protection(
    camera: str, vehicle_rules: bool, assets: list[str], high_recall: bool
) -> None:
    a = _house()
    prompt = a._build_prompt(camera, clip_duration=6.0, scene_deviation=0.02)
    assert ("PROTECTED VEHICLE: Silver Kia sedan" in prompt) is vehicle_rules
    assert ("PROTECTED ASSETS" in prompt) is bool(assets)
    for name in assets:
        assert f'"{name}"' in prompt
    # No camera is told about another camera's assets.
    for other in {"Front door", "Mailbox", "Barbecue"} - set(assets):
        assert f'"{other}"' not in prompt
    assert a._asset_protection_applies(camera) is high_recall
    assert [x["name"] for x in a._marked_assets_for(camera)] == assets


def test_cameras_with_nothing_marked_keep_their_basic_protection() -> None:
    """Back Door and Walkway are analyzed exactly as they would be on a
    property where nobody had ever opened the Assets tab: same prompt, and
    still told the car belongs to other cameras."""
    plain = _analyzer(
        car_description="Silver Kia sedan", car_cameras=["Driveway 1", "Driveway 2"]
    )
    house = _house()
    for camera in ("Back Door", "Walkway"):
        prompt = house._build_prompt(camera, clip_duration=6.0, scene_deviation=0.02)
        assert prompt == plain._build_prompt(
            camera, clip_duration=6.0, scene_deviation=0.02
        )
        assert "does not view the protected vehicle" in prompt
        # The calm scene framing an unmarked camera has always had.
        assert "Favor a calm" in prompt


def test_the_rules_sent_match_what_each_camera_has() -> None:
    a = _house()
    front = a._build_prompt("Front Door")
    garage = a._build_prompt("Garage")
    assert "Doors, gates and garage doors" in front and "Mailboxes" in front
    assert "Bikes, equipment and other marked items" not in front
    assert "Bikes, equipment and other marked items" in garage
    assert "Mailboxes" not in garage and "Doors, gates" not in garage


async def test_each_camera_hands_the_vision_pipeline_only_its_own_assets() -> None:
    a = _house()
    pipeline = MagicMock()
    pipeline.process_clip = AsyncMock(return_value=VisionHints())
    a.attach_vision_pipeline(pipeline)
    seen: dict[str, list[str]] = {}
    for camera in ("Driveway 1", "Front Door", "Garage", "Walkway"):
        await a._apply_vision_pipeline([b"frame"], camera)
        kwargs = pipeline.process_clip.call_args.kwargs
        seen[camera] = [x["name"] for x in kwargs["marked_assets"]]
        assert kwargs["car_protection_applies"] is (camera == "Driveway 1")
    assert seen == {
        "Driveway 1": [],
        "Front Door": ["Front door", "Mailbox"],
        "Garage": ["Barbecue"],
        "Walkway": [],
    }


@pytest.mark.parametrize(
    ("detection", "security_layer", "expected"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_object_detection_enabled_needs_detection_and_the_security_layer(
    detection: bool, security_layer: bool, expected: bool
) -> None:
    """What the Assets tab tells its user about the per-asset checks."""
    from blink_downloader.vision.pipeline import VisionConfig, VisionPipeline

    a = _analyzer()
    assert a.object_detection_enabled is False
    a.attach_vision_pipeline(
        VisionPipeline(
            VisionConfig(
                enhanced_detection_enabled=detection,
                security_events_enabled=security_layer,
            )
        )
    )
    assert a.object_detection_enabled is expected
