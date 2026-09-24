"""Marked assets through the vision pipeline.

What the pipeline owes a marked asset is small and specific: place it on
the scan frames by its zone, point the depth/contact/pose stages at it when
somebody is actually *at* it rather than at the car, say so in those stages'
hints without calling it a vehicle, and take a before/after look at its
region. Everything that decides what any of that *means* lives in the
security layer and is tested there.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from blink_downloader.security import (
    AssetLocation,
    AssetType,
    ProtectedAsset,
    Zone,
    build_marked_asset,
)
from blink_downloader.vision import imaging as imaging_module
from blink_downloader.vision.contact import ContactResult
from blink_downloader.vision.depth import DepthComparison
from blink_downloader.vision.detection import DetectedObject
from blink_downloader.vision.pipeline import VisionConfig, VisionHints, VisionPipeline


@pytest.fixture(autouse=True)
def _yolo_cache_dir_in_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The detector creates its weights cache under /data — see
    test_vision.py's fixture of the same name."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime._YOLO_MODEL_CACHE_DIR", str(tmp_path)
    )


FRAME = (640.0, 360.0)
CAR = (380.0, 170.0, 560.0, 290.0)
DOOR_ZONE = {"x_min": 0.05, "y_min": 0.3, "x_max": 0.17, "y_max": 0.83}
BIKE_ZONE = {"x_min": 0.2, "y_min": 0.55, "x_max": 0.34, "y_max": 0.83}
WINDOW_ZONE = {"x_min": 0.4, "y_min": 0.05, "x_max": 0.55, "y_max": 0.3}


def _marked(key: str, name: str, kind: str, zone: dict[str, float]) -> ProtectedAsset:
    asset = build_marked_asset("Porch", key, name, kind, Zone.from_config(zone), FRAME)
    assert asset is not None
    return asset


def _hints(
    *, car: bool = True, marked: list[ProtectedAsset] | None = None
) -> VisionHints:
    return VisionHints(
        asset=(
            ProtectedAsset(
                name="Silver Kia",
                asset_type=AssetType.VEHICLE,
                camera="Porch",
                box=CAR,
                location=AssetLocation.DETECTED,
            )
            if car
            else None
        ),
        marked_assets=marked or [],
        frame_size=FRAME,
    )


def _person(
    box: tuple[float, float, float, float], track: int, frame: int = 0
) -> DetectedObject:
    return DetectedObject("person", 0.9, box, track, frame)


#: Standing at the front door.
AT_DOOR = (40.0, 150.0, 90.0, 298.0)
#: Standing at the car.
AT_CAR = (420.0, 140.0, 470.0, 290.0)
#: Standing at the bike.
AT_BIKE = (140.0, 150.0, 190.0, 298.0)
#: Mid-lawn, nowhere near anything.
MID_LAWN = (300.0, 250.0, 330.0, 355.0)


# ----------------------------------------------------------------------
# Which pair the heavy stages examine
# ----------------------------------------------------------------------


def test_a_person_at_the_door_is_examined_when_nobody_is_at_the_car() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    pair = VisionPipeline._select_pair(
        _hints(marked=[door]), [_person(AT_DOOR, 1), _person(MID_LAWN, 2)], None
    )
    assert pair is not None
    subject, box, _frame, track, key = pair
    assert (subject.box, track, key) == (AT_DOOR, 1, "door1")
    assert box == door.box


def test_a_person_at_the_car_still_comes_first() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    pair = VisionPipeline._select_pair(
        _hints(marked=[door]), [_person(AT_DOOR, 1), _person(AT_CAR, 2)], None
    )
    assert pair is not None
    assert (pair[3], pair[4]) == (2, None)


def test_a_person_at_the_door_outranks_a_dog_at_the_car() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    dog = DetectedObject("dog", 0.8, (430.0, 250.0, 470.0, 290.0), 7, 0)
    pair = VisionPipeline._select_pair(
        _hints(marked=[door]), [dog, _person(AT_DOOR, 1)], None
    )
    assert pair is not None
    assert pair[4] == "door1"


def test_a_dog_at_the_car_outranks_a_dog_at_the_door() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    at_car = DetectedObject("dog", 0.8, (430.0, 250.0, 470.0, 290.0), 7, 0)
    at_door = DetectedObject("dog", 0.8, (50.0, 260.0, 90.0, 298.0), 8, 0)
    pair = VisionPipeline._select_pair(_hints(marked=[door]), [at_door, at_car], None)
    assert pair is not None
    assert (pair[3], pair[4]) == (7, None)


def test_an_animal_at_a_marked_asset_is_examined_when_nobody_else_is_near() -> None:
    bike = _marked("bike1", "Bike", "bicycle", BIKE_ZONE)
    dog = DetectedObject("dog", 0.8, (150.0, 250.0, 190.0, 298.0), 7, 0)
    pair = VisionPipeline._select_pair(_hints(car=False, marked=[bike]), [dog], None)
    assert pair is not None
    assert pair[4] == "bike1"


def test_a_belonging_is_examined_before_a_door() -> None:
    """Contact the stages confirm means something at a bike, not a door."""
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    bike = _marked("bike1", "Bike", "bicycle", BIKE_ZONE)
    pair = VisionPipeline._select_pair(
        _hints(car=False, marked=[door, bike]),
        [_person(AT_DOOR, 1), _person(AT_BIKE, 2)],
        None,
    )
    assert pair is not None
    assert pair[4] == "bike1"


def test_someone_at_a_window_is_at_it_though_their_feet_are_below_it() -> None:
    window = _marked("win1", "Kitchen window", "window", WINDOW_ZONE)
    peering = _person((250.0, 30.0, 320.0, 300.0), 1)
    pair = VisionPipeline._select_pair(
        _hints(car=False, marked=[window]), [peering], None
    )
    assert pair is not None
    assert pair[4] == "win1"


def test_nobody_at_anything_keeps_the_old_vehicle_pair() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    pair = VisionPipeline._select_pair(
        _hints(marked=[door]), [_person(MID_LAWN, 2)], None
    )
    assert pair is not None
    assert (pair[3], pair[4]) == (2, None)


def test_marked_only_examines_someone_approaching_but_not_far_away() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    approaching = _person((140.0, 170.0, 190.0, 320.0), 1)
    pair = VisionPipeline._select_pair(
        _hints(car=False, marked=[door]), [approaching], None
    )
    assert pair is not None
    assert pair[4] == "door1"

    far = _person((560.0, 250.0, 590.0, 355.0), 2)
    assert (
        VisionPipeline._select_pair(_hints(car=False, marked=[door]), [far], None)
        is None
    )


def test_marked_only_with_nobody_in_frame_examines_nothing() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    car = DetectedObject("car", 0.9, CAR, 9, 0)
    assert (
        VisionPipeline._select_pair(_hints(car=False, marked=[door]), [car], None)
        is None
    )


def test_the_deepest_overlapping_sighting_is_the_one_examined() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    arriving = _person((150.0, 150.0, 200.0, 298.0), 1, 0)
    at_it = _person(AT_DOOR, 1, 1)
    pair = VisionPipeline._select_pair(
        _hints(car=False, marked=[door]), [arriving, at_it], None
    )
    assert pair is not None
    assert (pair[0], pair[2]) == (at_it, 1)


def test_pair_target_names_what_was_measured() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    hints = _hints(marked=[door])
    assert VisionPipeline._pair_target(hints, "door1") == 'asset marked "Front door"'
    assert VisionPipeline._pair_target(hints, None) == "vehicle"
    assert VisionPipeline._pair_target(hints, "gone") == "vehicle"


# ----------------------------------------------------------------------
# Locating marked assets, and their before/after comparison
# ----------------------------------------------------------------------


def test_locate_marked_assets_drops_what_it_cannot_place() -> None:
    located = VisionPipeline._locate_marked_assets(
        "Porch",
        [
            {
                "id": "door1",
                "name": "Front door",
                "asset_type": "door",
                "zone": DOOR_ZONE,
            },
            {"id": "bad1", "name": "Nothing", "asset_type": "door", "zone": None},
            {"id": "bad2", "name": "Rocket", "asset_type": "rocket", "zone": DOOR_ZONE},
        ],
        FRAME,
    )
    assert [a.key for a in located] == ["door1"]
    assert located[0].marked is True


def test_marked_asset_changes_need_two_frames_assets_and_a_subject() -> None:
    pipeline = VisionPipeline(VisionConfig())
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    car = DetectedObject("car", 0.9, CAR, 9, 0)
    assert pipeline._marked_asset_changes([b"a"], [door], [_person(MID_LAWN, 1)]) == {}
    assert (
        pipeline._marked_asset_changes([b"a", b"b"], [], [_person(MID_LAWN, 1)]) == {}
    )
    assert pipeline._marked_asset_changes([b"a", b"b"], [door], [car]) == {}


def test_marked_asset_changes_compare_each_clean_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[float, ...]] = []

    def _change(_before: bytes, _after: bytes, box: tuple[float, ...]) -> float:
        compared.append(tuple(box))
        return 0.42

    monkeypatch.setattr(imaging_module, "_region_appearance_change", _change)
    pipeline = VisionPipeline(VisionConfig())
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    bike = _marked("bike1", "Bike", "bicycle", BIKE_ZONE)
    # Someone stands at the door in the first frame: its view is not clean,
    # so it is not compared. The bike's is.
    changes = pipeline._marked_asset_changes(
        [b"first", b"middle", b"last"],
        [door, bike],
        [_person(AT_DOOR, 1, 0), _person(MID_LAWN, 1, 1)],
    )
    assert changes == {"bike1": 0.42}
    assert compared == [bike.box]


def test_a_failed_comparison_costs_that_asset_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = iter([RuntimeError("decoder"), 0.3])

    def _change(*_args: Any) -> float:
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(imaging_module, "_region_appearance_change", _change)
    pipeline = VisionPipeline(VisionConfig())
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    bike = _marked("bike1", "Bike", "bicycle", BIKE_ZONE)
    changes = pipeline._marked_asset_changes(
        [b"first", b"last"], [door, bike], [_person(MID_LAWN, 1, 1)]
    )
    assert changes == {"bike1": 0.3}


def test_a_comparison_that_produces_nothing_is_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imaging_module, "_region_appearance_change", lambda *_: None)
    pipeline = VisionPipeline(VisionConfig())
    bike = _marked("bike1", "Bike", "bicycle", BIKE_ZONE)
    assert (
        pipeline._marked_asset_changes([b"a", b"b"], [bike], [_person(MID_LAWN, 1, 1)])
        == {}
    )


# ----------------------------------------------------------------------
# Through process_clip
# ----------------------------------------------------------------------


class _FakeBoxes:
    def __init__(
        self, cls: list[int], conf: list[float], xyxy: list, ids: list[int]
    ) -> None:
        self.cls = cls
        self.conf = conf
        self.xyxy = xyxy
        self.id = ids

    def __len__(self) -> int:
        return len(self.cls)


class _FakeYoloResult:
    def __init__(self, boxes: _FakeBoxes, names: dict[int, str]) -> None:
        self.boxes = boxes
        self.names = names


def _jpeg(size: tuple[int, int]) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (90, 90, 90)).save(buf, format="JPEG")
    return buf.getvalue()


def _yolo(
    monkeypatch: pytest.MonkeyPatch, boxes: _FakeBoxes, names: dict[int, str]
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((360, 640, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)
    model = MagicMock()
    model.track.return_value = [_FakeYoloResult(boxes, names)]
    ultra = MagicMock()
    ultra.YOLO.return_value = model
    monkeypatch.setitem(sys.modules, "ultralytics", ultra)
    monkeypatch.setitem(sys.modules, "transformers", None)


async def test_process_clip_examines_the_door_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo(
        monkeypatch,
        _FakeBoxes(cls=[0, 2], conf=[0.9, 0.95], xyxy=[AT_DOOR, CAR], ids=[1, 9]),
        {0: "person", 2: "car"},
    )
    monkeypatch.setattr(imaging_module, "_region_appearance_change", lambda *_: 0.5)
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))

    async def _compare(*_args: Any) -> DepthComparison:
        return DepthComparison(True, 10.0, 11.0)

    async def _contact(*_args: Any) -> ContactResult:
        return ContactResult(True, 0.0)

    monkeypatch.setattr(pipeline._depth, "compare", _compare)
    monkeypatch.setattr(pipeline._segmenter, "check_contact", _contact)
    hints = await pipeline.process_clip(
        [_jpeg((640, 360))],
        raw_frames=[_jpeg((640, 360))] * 3,
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Porch",
        marked_assets=[
            {
                "id": "door1",
                "name": "Front door",
                "asset_type": "door",
                "zone": DOOR_ZONE,
            },
            {"id": "bike1", "name": "Bike", "asset_type": "bicycle", "zone": BIKE_ZONE},
        ],
    )
    assert [a.key for a in hints.marked_assets] == ["door1", "bike1"]
    assert hints.examined_asset_key == "door1"
    assert hints.contact_track_id == 1
    assert (
        hints.depth_hint is not None and 'asset marked "Front door"' in hints.depth_hint
    )
    assert hints.contact_hint is not None and "vehicle" not in hints.contact_hint
    # The vehicle was not examined, so its own before/after is not taken.
    assert hints.asset_appearance_change is None
    # The door has someone in front of it in every frame; the bike does not.
    assert hints.marked_asset_changes == {"bike1": 0.5}


async def test_process_clip_without_marked_assets_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo(
        monkeypatch,
        _FakeBoxes(cls=[0, 2], conf=[0.9, 0.95], xyxy=[AT_CAR, CAR], ids=[1, 9]),
        {0: "person", 2: "car"},
    )
    monkeypatch.setattr(imaging_module, "_region_appearance_change", lambda *_: 0.2)
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_jpeg((640, 360))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Porch",
    )
    assert hints.marked_assets == []
    assert hints.examined_asset_key is None
    assert hints.marked_asset_changes == {}
    assert hints.contact_track_id == 1


def test_the_nearest_of_several_approaching_is_examined() -> None:
    door = _marked("door1", "Front door", "door", DOOR_ZONE)
    nearer = _person((140.0, 170.0, 190.0, 320.0), 1)
    further = _person((150.0, 190.0, 200.0, 340.0), 2)
    pair = VisionPipeline._select_pair(
        _hints(car=False, marked=[door]), [nearer, further], None
    )
    assert pair is not None
    assert pair[3] == 1
