"""Tests for the optional computer-vision enhancement pipeline (vision.py).

None of ultralytics/torch/transformers/facenet-pytorch/opencv are installed
in the test environment (they're a large optional extra — see pyproject.toml
"vision"), so every heavy dependency is mocked via sys.modules, mirroring the
existing MoondreamLocalAnalyzer pattern in test_analyzer.py. numpy and PIL
*are* real (numpy is already a transitive dependency here; Pillow is a hard
dependency), so array/image plumbing is exercised for real wherever a stage
doesn't itself need the mocked library.
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from blink_downloader.database import ClipDatabase
from blink_downloader.vision import (
    ContactResult,
    ContactSegmenter,
    DepthComparison,
    DepthEstimator,
    DetectedObject,
    FaceEmbedder,
    FaceRecognitionResult,
    FaceRecognizer,
    FrameEnhancer,
    ObjectDetector,
    VisionConfig,
    VisionPipeline,
    _best_subject_vehicle_pair,
    _box_gap,
    _build_contact_hint,
    _build_depth_hint,
    _build_detection_hint,
    _build_recognition_hint,
    _build_tracking_hint,
    _proximity_label,
    cosine_similarity,
    is_face_recognition_available,
)


def _real_jpeg_bytes(size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(128, 128, 128)).save(buf, format="JPEG")
    return buf.getvalue()


# ------------------------------------------------------------------
# Availability checks
# ------------------------------------------------------------------


def test_is_face_recognition_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "facenet_pytorch", MagicMock())
    assert is_face_recognition_available() is True


def test_is_face_recognition_available_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "facenet_pytorch", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        assert is_face_recognition_available() is False


# ------------------------------------------------------------------
# FrameEnhancer (OpenCV preprocessing)
# ------------------------------------------------------------------


def test_frame_enhancer_returns_unchanged_when_opencv_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        frames = [b"frame1", b"frame2"]
        assert FrameEnhancer.enhance(frames) == frames


def _install_fake_cv2(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake_img = np.zeros((10, 10, 3), dtype=np.uint8)
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.COLOR_BGR2LAB = 44
    mock_cv2.COLOR_LAB2BGR = 56
    mock_cv2.IMWRITE_JPEG_QUALITY = 1
    mock_cv2.imdecode.return_value = fake_img
    mock_cv2.cvtColor.return_value = fake_img
    mock_cv2.split.return_value = (
        fake_img[:, :, 0],
        fake_img[:, :, 1],
        fake_img[:, :, 2],
    )
    mock_cv2.merge.return_value = fake_img
    mock_clahe = MagicMock()
    mock_clahe.apply.return_value = fake_img[:, :, 0]
    mock_cv2.createCLAHE.return_value = mock_clahe
    mock_cv2.fastNlMeansDenoisingColored.return_value = fake_img
    mock_cv2.imencode.return_value = (True, np.array([1, 2, 3], dtype=np.uint8))
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)
    return mock_cv2


def test_frame_enhancer_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_cv2(monkeypatch)
    result = FrameEnhancer.enhance([b"fake-jpeg-bytes"])
    assert result == [bytes(np.array([1, 2, 3], dtype=np.uint8))]


def test_frame_enhancer_falls_back_on_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = _install_fake_cv2(monkeypatch)
    mock_cv2.imdecode.return_value = None
    frame = b"undecodable"
    assert FrameEnhancer.enhance([frame]) == [frame]


def test_frame_enhancer_falls_back_on_encode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = _install_fake_cv2(monkeypatch)
    mock_cv2.imencode.return_value = (False, None)
    frame = b"fake-jpeg-bytes"
    assert FrameEnhancer.enhance([frame]) == [frame]


def test_frame_enhancer_falls_back_on_per_frame_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression test: a per-frame failure must fall back to the raw frame
    AND log at debug level, matching every other CV stage's failure path in
    this module — it previously swallowed the exception with no logging at
    all."""
    mock_cv2 = _install_fake_cv2(monkeypatch)
    mock_cv2.cvtColor.side_effect = RuntimeError("boom")
    frame = b"fake-jpeg-bytes"
    with caplog.at_level("DEBUG", logger="blink_downloader.vision"):
        assert FrameEnhancer.enhance([frame]) == [frame]
    assert "Frame enhancement failed" in caplog.text


# ------------------------------------------------------------------
# Pure helpers: _box_gap / _proximity_label / _best_subject_vehicle_pair /
# _build_detection_hint
# ------------------------------------------------------------------


def test_box_gap_overlapping_returns_negative() -> None:
    a = (0.0, 0.0, 10.0, 10.0)
    b = (5.0, 5.0, 15.0, 15.0)
    assert _box_gap(a, b) < 0


def test_box_gap_non_overlapping_returns_positive_distance() -> None:
    a = (0.0, 0.0, 10.0, 10.0)
    b = (20.0, 0.0, 30.0, 10.0)
    assert _box_gap(a, b) == pytest.approx(10.0)


def test_proximity_label_touching() -> None:
    assert _proximity_label(-1.0, 100.0) == "overlapping the detected vehicle's outline"


def test_proximity_label_immediately_adjacent() -> None:
    assert "immediately adjacent" in _proximity_label(5.0, 100.0)


def test_proximity_label_close() -> None:
    assert _proximity_label(20.0, 100.0) == "close to the detected vehicle"


def test_proximity_label_far() -> None:
    assert _proximity_label(80.0, 100.0) == "well away from the detected vehicle"


def test_proximity_label_zero_width_vehicle_uses_raw_gap() -> None:
    # vehicle_width <= 0 falls back to treating the raw gap as the ratio
    assert _proximity_label(0.05, 0.0) == "immediately adjacent to the detected vehicle"


def test_best_subject_vehicle_pair_picks_smallest_gap() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 10, 10), None, 0),
        DetectedObject("car", 0.9, (50, 50, 100, 100), None, 0),
        DetectedObject("person", 0.9, (0, 0, 10, 10), None, 1),
        DetectedObject("car", 0.9, (5, 5, 20, 20), None, 1),
    ]
    pair = _best_subject_vehicle_pair(detections)
    assert pair is not None
    _person, _vehicle, frame_idx = pair
    assert frame_idx == 1


def test_best_subject_vehicle_pair_none_when_no_pairing() -> None:
    detections = [DetectedObject("person", 0.9, (0, 0, 10, 10), None, 0)]
    assert _best_subject_vehicle_pair(detections) is None


def test_best_subject_vehicle_pair_considers_animals() -> None:
    """A dog near the protected vehicle must be picked up as a subject —
    depth/contact analysis isn't just for people (e.g. a dog jumping on and
    scratching a parked car)."""
    detections = [
        DetectedObject("dog", 0.9, (0, 0, 5, 5), None, 0),
        DetectedObject("car", 0.9, (4, 4, 10, 10), None, 0),
    ]
    pair = _best_subject_vehicle_pair(detections)
    assert pair is not None
    subject, vehicle, frame_idx = pair
    assert subject.label == "dog"
    assert vehicle.label == "car"
    assert frame_idx == 0


def test_build_detection_hint_empty_detections_returns_none() -> None:
    assert _build_detection_hint([], "Silver Kia") is None


def test_build_detection_hint_lists_labels() -> None:
    detections = [DetectedObject("person", 0.9, (0, 0, 10, 10), None, 0)]
    hint = _build_detection_hint(detections, "")
    assert hint is not None
    assert "OBJECT DETECTION" in hint
    assert "person" in hint


def test_build_detection_hint_includes_distance_when_car_described() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 10, 10), None, 0),
        DetectedObject("car", 0.9, (5, 5, 20, 20), None, 0),
    ]
    hint = _build_detection_hint(detections, "Silver Kia")
    assert hint is not None
    assert "distance estimate" in hint


def test_build_detection_hint_uses_animal_label_in_distance_wording() -> None:
    detections = [
        DetectedObject("dog", 0.9, (0, 0, 5, 5), None, 0),
        DetectedObject("car", 0.9, (4, 4, 10, 10), None, 0),
    ]
    hint = _build_detection_hint(detections, "Silver Kia")
    assert hint is not None
    assert "detected dog's bounding box" in hint


def test_build_detection_hint_skips_distance_without_car_description() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 10, 10), None, 0),
        DetectedObject("car", 0.9, (5, 5, 20, 20), None, 0),
    ]
    hint = _build_detection_hint(detections, "")
    assert hint is not None
    assert "distance estimate" not in hint


# ------------------------------------------------------------------
# _build_tracking_hint — dwell/lingering signal from ByteTrack continuity
# ------------------------------------------------------------------


def test_tracking_hint_none_with_too_few_frames() -> None:
    detections = [DetectedObject("person", 0.9, (0, 0, 1, 1), 1, 0)]
    assert _build_tracking_hint(detections, total_frames=2) is None


def test_tracking_hint_none_without_any_tracked_person() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 1, 1), None, 0),
        DetectedObject("car", 0.9, (2, 2, 3, 3), 5, 0),
    ]
    assert _build_tracking_hint(detections, total_frames=5) is None


def test_tracking_hint_lingering_for_high_frame_presence() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 1, 1), 7, frame_idx)
        for frame_idx in range(4)
    ]
    hint = _build_tracking_hint(detections, total_frames=5)
    assert hint is not None
    assert "TRACKING" in hint
    assert "lingering or casing" in hint
    assert "4 of 5" in hint


def test_tracking_hint_brief_for_low_frame_presence() -> None:
    detections = [DetectedObject("person", 0.9, (0, 0, 1, 1), 7, 0)]
    hint = _build_tracking_hint(detections, total_frames=5)
    assert hint is not None
    assert "briefly passing through" in hint
    assert "1 of 5" in hint


def test_tracking_hint_none_for_ambiguous_middle_ground() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 1, 1), 7, frame_idx)
        for frame_idx in range(2)
    ]
    assert _build_tracking_hint(detections, total_frames=5) is None


def test_tracking_hint_picks_track_with_most_frame_presence() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 1, 1), 1, 0),
        DetectedObject("person", 0.9, (5, 5, 6, 6), 2, 0),
        DetectedObject("person", 0.9, (5, 5, 6, 6), 2, 1),
        DetectedObject("person", 0.9, (5, 5, 6, 6), 2, 2),
    ]
    hint = _build_tracking_hint(detections, total_frames=4)
    assert hint is not None
    assert "3 of 4" in hint


def test_tracking_hint_ignores_non_person_labels() -> None:
    detections = [
        DetectedObject("car", 0.9, (0, 0, 1, 1), 9, frame_idx) for frame_idx in range(4)
    ]
    assert _build_tracking_hint(detections, total_frames=5) is None


# ------------------------------------------------------------------
# ObjectDetector
# ------------------------------------------------------------------


class _FakeBoxes:
    def __init__(self, cls, conf, xyxy, ids) -> None:
        self.cls = cls
        self.conf = conf
        self.xyxy = xyxy
        self.id = ids

    def __len__(self) -> int:
        return len(self.cls)


class _FakeYoloResult:
    def __init__(self, boxes, names) -> None:
        self.boxes = boxes
        self.names = names


async def test_object_detector_ensure_ready_fails_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "ultralytics", raising=False)
    with patch("builtins.__import__", side_effect=ImportError("no ultralytics")):
        detector = ObjectDetector()
        assert await detector.ensure_ready() is False


async def test_object_detector_ensure_ready_handles_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_ultra = MagicMock()
    mock_ultra.YOLO.side_effect = RuntimeError("corrupt weights")
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    detector = ObjectDetector()
    assert await detector.ensure_ready() is False


async def test_object_detector_detect_returns_none_for_empty_frames() -> None:
    detector = ObjectDetector()
    assert await detector.detect([]) is None


async def test_object_detector_detect_filters_and_maps_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[0, 2, 9],  # person, car, an irrelevant class not in names below
        conf=[0.9, 0.8, 0.5],
        xyxy=[(0.0, 0.0, 10.0, 10.0), (20.0, 20.0, 40.0, 40.0), (1.0, 1.0, 2.0, 2.0)],
        ids=[1, 2, 3],
    )
    names = {0: "person", 2: "car", 9: "traffic light"}
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, names)]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector("yolo11n.pt")
    detections = await detector.detect([b"frame0"])
    assert detections is not None
    labels = sorted(d.label for d in detections)
    assert labels == ["car", "person"]
    person = next(d for d in detections if d.label == "person")
    assert person.track_id == 1
    assert person.frame_index == 0


async def test_object_detector_detect_skips_frame_with_no_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(None, {})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector()
    detections = await detector.detect([b"frame0"])
    assert detections == []


async def test_object_detector_detect_skips_undecodable_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = None
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    fake_model = MagicMock()
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector()
    detections = await detector.detect([b"frame0"])
    assert detections == []
    fake_model.track.assert_not_called()


async def test_object_detector_detect_returns_none_on_inference_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.side_effect = RuntimeError("boom")
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    fake_model = MagicMock()
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector()
    assert await detector.detect([b"frame0"]) is None


async def test_object_detector_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    detector = ObjectDetector()
    assert await detector.ensure_ready() is True
    assert await detector.ensure_ready() is True
    mock_ultra.YOLO.assert_called_once()


async def test_object_detector_ensure_ready_concurrent_calls_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent ensure_ready() calls must only load the model once —
    exercises the double-checked-lock race branch where the second caller
    finds the model already loaded by the time it acquires the lock."""
    mock_ultra = MagicMock()
    mock_ultra.YOLO.side_effect = lambda *_a, **_kw: (time.sleep(0.05), MagicMock())[1]
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector()
    results = await asyncio.gather(detector.ensure_ready(), detector.ensure_ready())
    assert results == [True, True]
    mock_ultra.YOLO.assert_called_once()


async def test_object_detector_detect_skips_frame_with_empty_track_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.track()` returning an empty list for a frame (no results at all,
    distinct from a result with no boxes) must be skipped, not crash."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    fake_model = MagicMock()
    fake_model.track.return_value = []
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector()
    detections = await detector.detect([b"frame0"])
    assert detections == []


# ------------------------------------------------------------------
# DepthEstimator
# ------------------------------------------------------------------


async def test_depth_estimator_ensure_ready_fails_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    with patch("builtins.__import__", side_effect=ImportError("no transformers")):
        estimator = DepthEstimator()
        assert await estimator.ensure_ready() is False


async def test_depth_estimator_ensure_ready_concurrent_calls_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_transformers.pipeline.side_effect = lambda **_kw: (
        time.sleep(0.05),
        MagicMock(),
    )[1]
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    estimator = DepthEstimator()
    results = await asyncio.gather(estimator.ensure_ready(), estimator.ensure_ready())
    assert results == [True, True]
    mock_transformers.pipeline.assert_called_once()


async def test_depth_estimator_ensure_ready_handles_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_transformers.pipeline.side_effect = RuntimeError("no weights")
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    estimator = DepthEstimator()
    assert await estimator.ensure_ready() is False


async def test_depth_estimator_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    estimator = DepthEstimator()
    assert await estimator.ensure_ready() is True
    assert await estimator.ensure_ready() is True
    mock_transformers.pipeline.assert_called_once()


async def test_depth_estimator_compare_similar_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depth_map = np.zeros((100, 100), dtype=np.float32)
    depth_map[0:10, 0:10] = 50.0  # person region
    depth_map[20:30, 20:30] = 52.0  # vehicle region — close depth
    depth_map[90:100, 90:100] = 255.0  # some far background to set the range

    mock_pipe = MagicMock(return_value={"depth": depth_map})
    mock_transformers = MagicMock()
    mock_transformers.pipeline.return_value = mock_pipe
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    estimator = DepthEstimator()
    result = await estimator.compare(
        _real_jpeg_bytes((100, 100)), (0, 0, 10, 10), (20, 20, 30, 30)
    )
    assert result is not None
    assert result.similar_depth is True


async def test_depth_estimator_compare_different_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depth_map = np.zeros((100, 100), dtype=np.float32)
    depth_map[0:10, 0:10] = 10.0  # person region — near
    depth_map[20:30, 20:30] = 240.0  # vehicle region — far

    mock_pipe = MagicMock(return_value={"depth": depth_map})
    mock_transformers = MagicMock()
    mock_transformers.pipeline.return_value = mock_pipe
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    estimator = DepthEstimator()
    result = await estimator.compare(
        _real_jpeg_bytes((100, 100)), (0, 0, 10, 10), (20, 20, 30, 30)
    )
    assert result is not None
    assert result.similar_depth is False


async def test_depth_estimator_compare_returns_none_for_out_of_bounds_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depth_map = np.zeros((10, 10), dtype=np.float32)
    mock_pipe = MagicMock(return_value={"depth": depth_map})
    mock_transformers = MagicMock()
    mock_transformers.pipeline.return_value = mock_pipe
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    estimator = DepthEstimator()
    result = await estimator.compare(
        _real_jpeg_bytes((10, 10)), (0, 0, 0, 0), (1, 1, 5, 5)
    )
    assert result is None


async def test_depth_estimator_compare_returns_none_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _real_jpeg_bytes()
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        estimator = DepthEstimator()
        result = await estimator.compare(frame, (0, 0, 1, 1), (1, 1, 2, 2))
        assert result is None


async def test_depth_estimator_compare_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_pipe = MagicMock(side_effect=RuntimeError("inference failed"))
    mock_transformers = MagicMock()
    mock_transformers.pipeline.return_value = mock_pipe
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    estimator = DepthEstimator()
    result = await estimator.compare(_real_jpeg_bytes(), (0, 0, 1, 1), (1, 1, 2, 2))
    assert result is None


def test_build_depth_hint_similar() -> None:
    hint = _build_depth_hint(DepthComparison(True, 10.0, 11.0))
    assert "roughly the same distance" in hint


def test_build_depth_hint_different() -> None:
    hint = _build_depth_hint(DepthComparison(False, 10.0, 200.0))
    assert "noticeably different distances" in hint


# ------------------------------------------------------------------
# ContactSegmenter
# ------------------------------------------------------------------


class _FakeTensor:
    def __init__(self, arr: np.ndarray) -> None:
        self._arr = arr

    def numpy(self) -> np.ndarray:
        return self._arr


class _FakeMasks:
    def __init__(self, masks: list[np.ndarray]) -> None:
        self._masks = masks
        self.shape = (len(masks), 1) + (masks[0].shape if masks else (0, 0))

    def __getitem__(self, idx):
        obj_idx, _ = idx
        return _FakeTensor(self._masks[obj_idx])


def _install_fake_transformers_for_sam2(
    monkeypatch: pytest.MonkeyPatch, masks: list[np.ndarray]
) -> MagicMock:
    mock_processor = MagicMock()
    mock_processor.return_value = {
        "pixel_values": "x",
        "original_sizes": [[10, 10]],
    }
    mock_processor.post_process_masks.return_value = [_FakeMasks(masks)]
    mock_model = MagicMock()
    mock_model.return_value.pred_masks.cpu.return_value = MagicMock()

    mock_transformers = MagicMock()
    mock_transformers.Sam2Model.from_pretrained.return_value = mock_model
    mock_transformers.Sam2Processor.from_pretrained.return_value = mock_processor
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())
    return mock_transformers


async def test_contact_segmenter_ensure_ready_fails_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    with patch("builtins.__import__", side_effect=ImportError("no transformers")):
        segmenter = ContactSegmenter()
        assert await segmenter.ensure_ready() is False


async def test_contact_segmenter_ensure_ready_concurrent_calls_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_transformers.Sam2Model.from_pretrained.side_effect = lambda *_a, **_kw: (
        time.sleep(0.05),
        MagicMock(),
    )[1]
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    segmenter = ContactSegmenter()
    results = await asyncio.gather(segmenter.ensure_ready(), segmenter.ensure_ready())
    assert results == [True, True]
    mock_transformers.Sam2Model.from_pretrained.assert_called_once()


async def test_contact_segmenter_ensure_ready_handles_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_transformers.Sam2Model.from_pretrained.side_effect = RuntimeError("no weights")
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is False


async def test_contact_segmenter_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is True
    assert await segmenter.ensure_ready() is True
    mock_transformers.Sam2Model.from_pretrained.assert_called_once()


async def test_contact_segmenter_touching_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask[5, 5] = 1
    _install_fake_transformers_for_sam2(monkeypatch, [person_mask, vehicle_mask])

    mock_cv2 = MagicMock()
    mock_cv2.dilate.side_effect = lambda mask, kernel, iterations: vehicle_mask
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    segmenter = ContactSegmenter()
    result = await segmenter.check_contact(
        _real_jpeg_bytes(), (0, 0, 5, 5), (5, 5, 10, 10)
    )
    assert result is not None
    assert result.touching is True
    assert result.mask_gap_pixels == 0.0


async def test_contact_segmenter_touching_after_a_few_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask[5, 5] = 1
    _install_fake_transformers_for_sam2(monkeypatch, [person_mask, vehicle_mask])

    mock_cv2 = MagicMock()
    mock_cv2.dilate.side_effect = lambda mask, kernel, iterations: (
        vehicle_mask if iterations >= 3 else np.zeros_like(vehicle_mask)
    )
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    segmenter = ContactSegmenter()
    result = await segmenter.check_contact(
        _real_jpeg_bytes(), (0, 0, 5, 5), (5, 5, 10, 10)
    )
    assert result is not None
    assert result.touching is False
    # 3.0 (kernel radius, not the full 7x7 kernel size) * (step 3 - 1) = 6.0.
    assert result.mask_gap_pixels == pytest.approx(6.0)


async def test_contact_segmenter_never_touching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask = np.zeros((10, 10), dtype=np.uint8)
    _install_fake_transformers_for_sam2(monkeypatch, [person_mask, vehicle_mask])

    mock_cv2 = MagicMock()
    mock_cv2.dilate.side_effect = lambda mask, kernel, iterations: np.zeros_like(mask)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    segmenter = ContactSegmenter()
    result = await segmenter.check_contact(
        _real_jpeg_bytes(), (0, 0, 5, 5), (5, 5, 10, 10)
    )
    assert result is not None
    assert result.touching is False
    # 3.0 (kernel radius, not the full 7x7 kernel size) * 10 max steps = 30.0.
    assert result.mask_gap_pixels == pytest.approx(30.0)


async def test_contact_segmenter_returns_none_for_fewer_than_two_masks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_mask = np.zeros((10, 10), dtype=np.uint8)
    _install_fake_transformers_for_sam2(monkeypatch, [person_mask])
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())

    segmenter = ContactSegmenter()
    result = await segmenter.check_contact(
        _real_jpeg_bytes(), (0, 0, 5, 5), (5, 5, 10, 10)
    )
    assert result is None


async def test_contact_segmenter_returns_none_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _real_jpeg_bytes()
    monkeypatch.delitem(sys.modules, "transformers", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        segmenter = ContactSegmenter()
        result = await segmenter.check_contact(frame, (0, 0, 1, 1), (1, 1, 2, 2))
        assert result is None


async def test_contact_segmenter_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_processor = MagicMock(side_effect=RuntimeError("boom"))
    mock_transformers.Sam2Processor.from_pretrained.return_value = mock_processor
    mock_transformers.Sam2Model.from_pretrained.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())

    segmenter = ContactSegmenter()
    result = await segmenter.check_contact(
        _real_jpeg_bytes(), (0, 0, 1, 1), (1, 1, 2, 2)
    )
    assert result is None


def test_build_contact_hint_touching() -> None:
    hint = _build_contact_hint(ContactResult(True, 0.0))
    assert "touch or overlap" in hint


def test_build_contact_hint_not_touching() -> None:
    hint = _build_contact_hint(ContactResult(False, 21.0))
    assert "21 pixels" in hint


# ------------------------------------------------------------------
# FaceEmbedder / FaceRecognizer
# ------------------------------------------------------------------


class _FakeFaceTensor:
    """Minimal stand-in for a torch tensor, just enough for _embed_sync."""

    def __init__(self, arr: np.ndarray, ndim: int | None = None) -> None:
        self._arr = arr
        self._ndim = ndim if ndim is not None else arr.ndim

    def dim(self) -> int:
        return self._ndim

    def unsqueeze(self, _axis: int) -> "_FakeFaceTensor":
        return _FakeFaceTensor(self._arr, ndim=self._ndim + 1)

    def __iter__(self):
        return iter(_FakeFaceTensor(row) for row in self._arr)

    def tolist(self) -> list:
        return self._arr.tolist()


async def test_face_embedder_ensure_ready_fails_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "facenet_pytorch", raising=False)
    with patch("builtins.__import__", side_effect=ImportError("no facenet_pytorch")):
        embedder = FaceEmbedder()
        assert await embedder.ensure_ready() is False


async def test_face_embedder_ensure_ready_concurrent_calls_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_fp = MagicMock()
    mock_fp.MTCNN.side_effect = lambda **_kw: (time.sleep(0.05), MagicMock())[1]
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)

    embedder = FaceEmbedder()
    results = await asyncio.gather(embedder.ensure_ready(), embedder.ensure_ready())
    assert results == [True, True]
    mock_fp.MTCNN.assert_called_once()


async def test_face_embedder_ensure_ready_handles_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_fp = MagicMock()
    mock_fp.MTCNN.side_effect = RuntimeError("boom")
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    embedder = FaceEmbedder()
    assert await embedder.ensure_ready() is False


async def test_face_embedder_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_fp = MagicMock()
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    embedder = FaceEmbedder()
    assert await embedder.ensure_ready() is True
    assert await embedder.ensure_ready() is True
    mock_fp.MTCNN.assert_called_once()


async def test_face_embedder_embed_returns_empty_when_no_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_fp = MagicMock()
    mock_mtcnn_instance = MagicMock(return_value=None)
    mock_fp.MTCNN.return_value = mock_mtcnn_instance
    mock_fp.InceptionResnetV1.return_value.eval.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())

    embedder = FaceEmbedder()
    assert await embedder.embed(_real_jpeg_bytes()) == []


async def test_face_embedder_embed_returns_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    faces_tensor = _FakeFaceTensor(np.zeros((3, 4, 4)), ndim=4)  # already batched
    embeddings_arr = np.array([[0.1, 0.2, 0.3]])

    mock_fp = MagicMock()
    mock_fp.MTCNN.return_value = MagicMock(return_value=faces_tensor)
    mock_resnet_instance = MagicMock(return_value=_FakeFaceTensor(embeddings_arr))
    mock_fp.InceptionResnetV1.return_value.eval.return_value = mock_resnet_instance
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())

    embedder = FaceEmbedder()
    embeddings = await embedder.embed(_real_jpeg_bytes())
    assert embeddings == [[0.1, 0.2, 0.3]]


async def test_face_embedder_embed_unsqueezes_single_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    single_face = _FakeFaceTensor(np.zeros((3, 4, 4)), ndim=3)
    embeddings_arr = np.array([[0.4, 0.5]])

    mock_fp = MagicMock()
    mock_fp.MTCNN.return_value = MagicMock(return_value=single_face)
    mock_resnet_instance = MagicMock(return_value=_FakeFaceTensor(embeddings_arr))
    mock_fp.InceptionResnetV1.return_value.eval.return_value = mock_resnet_instance
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())

    embedder = FaceEmbedder()
    embeddings = await embedder.embed(_real_jpeg_bytes())
    assert embeddings == [[0.4, 0.5]]


async def test_face_embedder_embed_returns_empty_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _real_jpeg_bytes()
    monkeypatch.delitem(sys.modules, "facenet_pytorch", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        embedder = FaceEmbedder()
        assert await embedder.embed(frame) == []


async def test_face_embedder_embed_returns_empty_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_fp = MagicMock()
    mock_fp.MTCNN.return_value = MagicMock(side_effect=RuntimeError("boom"))
    mock_fp.InceptionResnetV1.return_value.eval.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())

    embedder = FaceEmbedder()
    assert await embedder.embed(_real_jpeg_bytes()) == []


def test_cosine_similarity_identical_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_zero_vector_is_zero() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_mismatched_length_is_zero() -> None:
    """Regression test: a stored embedding with a different dimensionality
    (e.g. after an embedding-model change, or a corrupted DB row) must be
    treated as "can't compare, no match" rather than silently comparing a
    zip()-truncated subset of both vectors, which could coincidentally
    produce a similarity score above the match threshold."""
    assert cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0, 0.0]) == 0.0


async def test_face_recognizer_no_enrollments_returns_empty(db: ClipDatabase) -> None:
    embedder = MagicMock(spec=FaceEmbedder)
    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame"])
    assert result == FaceRecognitionResult()
    embedder.embed.assert_not_called()


async def test_face_recognizer_matches_approved_member(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Brian", [1.0, 0.0, 0.0], approved=True)
    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = MagicMock(return_value=_async_result([[1.0, 0.0, 0.0]]))

    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame"])
    assert result.approved_names == ["Brian"]
    assert result.other_names == []
    assert result.unrecognized_present is False


async def test_face_recognizer_matches_unapproved_member(db: ClipDatabase) -> None:
    """A recognized-but-not-approved enrollment must NOT count as an approved
    match — it lands in other_names, which blocks the bypass exactly like a
    stranger would (see _face_bypass_applies)."""
    await db.add_face_enrollment("Nanny", [1.0, 0.0, 0.0], approved=False)
    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = MagicMock(return_value=_async_result([[1.0, 0.0, 0.0]]))

    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame"])
    assert result.approved_names == []
    assert result.other_names == ["Nanny"]
    assert result.unrecognized_present is False


async def test_face_recognizer_no_match_below_threshold_is_unrecognized(
    db: ClipDatabase,
) -> None:
    await db.add_face_enrollment("Brian", [1.0, 0.0, 0.0])
    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = MagicMock(return_value=_async_result([[0.0, 1.0, 0.0]]))

    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame"])
    assert result.approved_names == []
    assert result.other_names == []
    assert result.unrecognized_present is True


async def test_face_recognizer_approved_plus_stranger_blocks_bypass_signal(
    db: ClipDatabase,
) -> None:
    """The critical multi-person case: an approved household member AND an
    unrecognized stranger both appear across the clip's sampled frames — the
    result must report both facts, since this is exactly what must prevent
    the suspicious-flag bypass from firing."""
    await db.add_face_enrollment("Brian", [1.0, 0.0, 0.0], approved=True)

    async def _embed(frame: bytes) -> list[list[float]]:
        if frame == b"frame-brian":
            return [[1.0, 0.0, 0.0]]
        return [[0.0, 1.0, 0.0]]  # stranger, no enrollment matches

    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = _embed

    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame-brian", b"frame-stranger"])
    assert result.approved_names == ["Brian"]
    assert result.unrecognized_present is True


async def test_face_recognizer_picks_best_match_across_frames(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Brian", [1.0, 0.0, 0.0])
    await db.add_face_enrollment("Amy", [0.0, 1.0, 0.0])

    async def _embed(frame: bytes) -> list[list[float]]:
        if frame == b"frame-amy":
            return [[0.0, 0.9, 0.1]]
        return [[0.99, 0.05, 0.0]]

    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = _embed

    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame-amy", b"frame-brian"])
    assert result.approved_names == ["Amy", "Brian"]


async def test_face_recognizer_multiple_faces_same_frame(db: ClipDatabase) -> None:
    """MTCNN (keep_all=True) can return multiple faces from a single frame —
    each must be matched independently, not collapsed to one."""
    await db.add_face_enrollment("Brian", [1.0, 0.0, 0.0], approved=True)
    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = MagicMock(
        return_value=_async_result([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    )

    recognizer = FaceRecognizer(embedder, db)
    result = await recognizer.recognize([b"frame"])
    assert result.approved_names == ["Brian"]
    assert result.unrecognized_present is True


async def test_face_recognizer_db_error_returns_empty(db: ClipDatabase) -> None:
    embedder = MagicMock(spec=FaceEmbedder)
    broken_db = MagicMock(spec=ClipDatabase)
    broken_db.list_face_enrollments = MagicMock(
        side_effect=RuntimeError("db unavailable")
    )

    recognizer = FaceRecognizer(embedder, broken_db)
    result = await recognizer.recognize([b"frame"])
    assert result == FaceRecognitionResult()
    embedder.embed.assert_not_called()


def _async_result(value: Any):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner()


def test_build_recognition_hint_approved_only() -> None:
    hint = _build_recognition_hint(FaceRecognitionResult(approved_names=["Brian"]))
    assert hint is not None
    assert "Brian" not in hint  # name must never reach the AI prompt
    assert "1 locally-enrolled household member" in hint
    assert "NOTE" not in hint


def test_build_recognition_hint_multiple_approved() -> None:
    hint = _build_recognition_hint(
        FaceRecognitionResult(approved_names=["Brian", "Amy"])
    )
    assert hint is not None
    assert "2 locally-enrolled household members" in hint


def test_build_recognition_hint_notes_stranger_present() -> None:
    hint = _build_recognition_hint(
        FaceRecognitionResult(approved_names=["Brian"], unrecognized_present=True)
    )
    assert hint is not None
    assert "NOTE" in hint


def test_build_recognition_hint_none_when_no_approved_match() -> None:
    assert _build_recognition_hint(FaceRecognitionResult()) is None
    assert (
        _build_recognition_hint(FaceRecognitionResult(unrecognized_present=True))
        is None
    )
    assert _build_recognition_hint(FaceRecognitionResult(other_names=["Nanny"])) is None


# ------------------------------------------------------------------
# VisionPipeline orchestrator
# ------------------------------------------------------------------


async def test_vision_pipeline_all_disabled_returns_empty_hints() -> None:
    pipeline = VisionPipeline(VisionConfig())
    hints = await pipeline.process_clip([b"frame"], car_description="Silver Kia")
    assert hints.enhanced_frames is None
    assert hints.detection_hint is None
    assert hints.depth_hint is None
    assert hints.contact_hint is None
    assert hints.recognized_resident_hint is None


async def test_vision_pipeline_empty_frames_short_circuits() -> None:
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip([])
    assert hints.enhanced_frames is None


async def test_vision_pipeline_enhanced_detection_all_deps_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enhanced detection covers preprocessing + detection + depth +
    segmentation under one toggle — with every dependency unavailable, each
    stage degrades gracefully rather than raising, leaving only the
    unchanged frames behind."""
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.delitem(sys.modules, "ultralytics", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
        hints = await pipeline.process_clip([b"frame"], car_description="Silver Kia")
        # opencv unavailable -> enhance() returns frames unchanged, but the
        # pipeline still records that preprocessing ran.
        assert hints.enhanced_frames == [b"frame"]
        assert hints.detection_hint is None
        assert hints.depth_hint is None


async def test_vision_pipeline_full_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[0, 2],
        conf=[0.9, 0.9],
        xyxy=[(0.0, 0.0, 5.0, 5.0), (5.0, 5.0, 10.0, 10.0)],
        ids=None,
    )
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {0: "person", 2: "car"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    depth_map = np.zeros((10, 10), dtype=np.float32)
    depth_map[8:10, 8:10] = 100.0
    mock_pipe = MagicMock(return_value={"depth": depth_map})
    person_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask[5, 5] = 1
    mock_processor = MagicMock()
    mock_processor.return_value = {"pixel_values": "x", "original_sizes": [[10, 10]]}
    mock_processor.post_process_masks.return_value = [
        _FakeMasks([person_mask, vehicle_mask])
    ]
    mock_sam_model = MagicMock()
    mock_sam_model.return_value.pred_masks.cpu.return_value = MagicMock()

    mock_transformers = MagicMock()
    mock_transformers.pipeline.return_value = mock_pipe
    mock_transformers.Sam2Model.from_pretrained.return_value = mock_sam_model
    mock_transformers.Sam2Processor.from_pretrained.return_value = mock_processor
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())
    mock_cv2.dilate.side_effect = lambda mask, kernel, iterations: vehicle_mask

    config = VisionConfig(enhanced_detection_enabled=True)
    pipeline = VisionPipeline(config)
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes()],
        car_description="Silver Kia",
        car_protection_applies=True,
    )
    assert hints.detection_hint is not None
    assert "distance estimate" in hints.detection_hint
    assert hints.depth_hint is not None
    assert hints.contact_hint is not None


async def test_vision_pipeline_skips_vehicle_analysis_on_non_car_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A camera not designated to view the protected vehicle must never
    generate vehicle-distance/depth/contact hints, even if it happens to
    detect an unrelated person and an unrelated car in frame — camera
    isolation is enforced by car_protection_applies, not by whether a
    protected vehicle description merely exists somewhere on the property."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[0, 2],
        conf=[0.9, 0.9],
        xyxy=[(0.0, 0.0, 5.0, 5.0), (5.0, 5.0, 10.0, 10.0)],
        ids=None,
    )
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {0: "person", 2: "car"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    config = VisionConfig(enhanced_detection_enabled=True)
    pipeline = VisionPipeline(config)
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes()],
        car_description="Silver Kia",
        car_protection_applies=False,
    )
    # Detected-classes line still appears (generically useful), but never
    # the vehicle-distance language, and depth/contact never run at all.
    assert hints.detection_hint is not None
    assert "distance estimate" not in hints.detection_hint
    assert hints.depth_hint is None
    assert hints.contact_hint is None


async def test_vision_pipeline_dog_vehicle_contact_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dog (not a person) near the protected vehicle must still get the
    full depth/contact treatment — this is exactly the "dog jumps on the
    car and scratches it" scenario, not just a person-proximity case."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[16, 2],  # dog, car
        conf=[0.9, 0.9],
        xyxy=[(0.0, 0.0, 5.0, 5.0), (4.0, 4.0, 10.0, 10.0)],
        ids=None,
    )
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {16: "dog", 2: "car"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes()],
        car_description="Silver Kia",
        car_protection_applies=True,
    )
    assert hints.detection_hint is not None
    assert "dog" in hints.detection_hint
    assert "distance estimate" in hints.detection_hint


async def test_vision_pipeline_tracking_hint_across_multiple_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[0],
        conf=[0.9],
        xyxy=[(0.0, 0.0, 5.0, 5.0)],
        ids=[42],
    )
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {0: "person"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    frames = [_real_jpeg_bytes()] * 5
    hints = await pipeline.process_clip(frames)
    assert hints.tracking_hint is not None
    assert "lingering or casing" in hints.tracking_hint


async def test_vision_pipeline_face_recognition(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Brian", [1.0, 0.0])
    pipeline = VisionPipeline(VisionConfig(face_recognition_enabled=True), db=db)
    with patch.object(FaceEmbedder, "embed", return_value=[[1.0, 0.0]]):
        hints = await pipeline.process_clip([b"frame"])
    assert hints.recognized_resident_hint is not None
    assert "Brian" not in hints.recognized_resident_hint  # name never sent to AI
    assert hints.face_recognition is not None
    assert hints.face_recognition.approved_names == ["Brian"]


async def test_vision_pipeline_face_recognition_uses_raw_frames_not_enhanced(
    db: ClipDatabase,
) -> None:
    """Regression test: when enhanced_detection_enabled and
    face_recognition_enabled are both on, face recognition must run against
    the original raw frames, not the CLAHE-enhanced ones — VisionConfig's
    docstring promises these two stages are independent, and matching
    enhanced frames against embeddings computed from raw reference photos is
    an embedding-space mismatch that can cause a real household member to go
    unrecognized."""
    await db.add_face_enrollment("Brian", [1.0, 0.0])
    raw_frames = [b"raw-frame-1", b"raw-frame-2"]
    seen_frames: list[bytes] = []

    async def _embed(frame: bytes) -> list[list[float]]:
        seen_frames.append(frame)
        return [[1.0, 0.0]]

    config = VisionConfig(
        enhanced_detection_enabled=True, face_recognition_enabled=True
    )
    pipeline = VisionPipeline(config, db=db)
    with (
        patch.object(
            FrameEnhancer,
            "enhance",
            side_effect=lambda frames: [b"enhanced-" + f for f in frames],
        ),
        patch.object(FaceEmbedder, "embed", side_effect=_embed),
    ):
        hints = await pipeline.process_clip(raw_frames)

    assert seen_frames == raw_frames
    assert hints.enhanced_frames == [b"enhanced-raw-frame-1", b"enhanced-raw-frame-2"]
    assert hints.face_recognition is not None
    assert hints.face_recognition.approved_names == ["Brian"]


async def test_vision_pipeline_face_recognition_without_db_is_noop() -> None:
    pipeline = VisionPipeline(VisionConfig(face_recognition_enabled=True), db=None)
    hints = await pipeline.process_clip([b"frame"])
    assert hints.recognized_resident_hint is None
    assert hints.face_recognition is None


def test_vision_pipeline_update_config_reuses_detector_when_model_unchanged() -> None:
    pipeline = VisionPipeline(VisionConfig(object_detection_model="yolo11n.pt"))
    original_detector = pipeline._detector  # noqa: SLF001
    pipeline.update_config(VisionConfig(object_detection_model="yolo11n.pt"))
    assert pipeline._detector is original_detector  # noqa: SLF001


def test_vision_pipeline_update_config_reloads_detector_on_model_change() -> None:
    pipeline = VisionPipeline(VisionConfig(object_detection_model="yolo11n.pt"))
    original_detector = pipeline._detector  # noqa: SLF001
    pipeline.update_config(VisionConfig(object_detection_model="yolo11s.pt"))
    assert pipeline._detector is not original_detector  # noqa: SLF001
