"""The OpenCV (YuNet + SFace) face-recognition backend, against a stand-in cv2."""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path
from typing import Any, Self
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image

from blink_downloader.vision import opencv_faces
from blink_downloader.vision.face_models import OPENCV_SFACE
from blink_downloader.vision.opencv_faces import (
    OpenCVFaceEmbedder,
    _download,
    _ModelFile,
)

_SMALL = _ModelFile("folder", "small.onnx", 10, 100)


@pytest.fixture
def model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(opencv_faces, "MODEL_DIR", tmp_path)
    return tmp_path


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body.read(size)


def _serve(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[str]:
    """Answer every download with *body*; returns the addresses asked for."""
    asked: list[str] = []

    def urlopen(request: Any, timeout: float) -> _Response:
        asked.append(request.full_url)
        assert timeout > 0
        return _Response(body)

    monkeypatch.setattr(opencv_faces.urllib.request, "urlopen", urlopen)
    return asked


def test_the_download_address_is_pinned_to_a_commit() -> None:
    url = _SMALL.url
    assert url.startswith("https://github.com/opencv/opencv_zoo/raw/")
    assert opencv_faces._ZOO_COMMIT in url
    assert url.endswith("/models/folder/small.onnx")
    assert "/main/" not in url


def test_a_model_file_is_usable_only_when_its_size_is_plausible(
    model_dir: Path,
) -> None:
    assert not _SMALL.usable()
    (model_dir / "small.onnx").write_bytes(b"x" * 5)
    assert not _SMALL.usable()
    (model_dir / "small.onnx").write_bytes(b"x" * 50)
    assert _SMALL.usable()
    (model_dir / "small.onnx").write_bytes(b"x" * 500)
    assert not _SMALL.usable()


def test_download_saves_the_file(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    asked = _serve(monkeypatch, b"y" * 50)
    with caplog.at_level("INFO"):
        _download(_SMALL)
    assert (model_dir / "small.onnx").read_bytes() == b"y" * 50
    assert asked == [_SMALL.url]
    assert not (model_dir / "small.onnx.part").exists()
    assert "sha256" in caplog.text


def test_download_leaves_a_usable_copy_alone(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (model_dir / "small.onnx").write_bytes(b"z" * 50)
    asked = _serve(monkeypatch, b"y" * 50)
    _download(_SMALL)
    assert asked == []
    assert (model_dir / "small.onnx").read_bytes() == b"z" * 50


def test_download_creates_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "cache"
    monkeypatch.setattr(opencv_faces, "MODEL_DIR", target)
    _serve(monkeypatch, b"y" * 50)
    _download(_SMALL)
    assert (target / "small.onnx").exists()


def test_download_refuses_a_git_lfs_pointer(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub answers with a few hundred bytes of text where it cannot serve
    the file; loading that as a model would fail confusingly, later."""
    _serve(monkeypatch, b"lfs")
    with pytest.raises(OSError, match="smaller than any real copy"):
        _download(_SMALL)
    assert list(model_dir.iterdir()) == []


def test_download_refuses_a_file_bigger_than_any_real_copy(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(monkeypatch, b"y" * 500)
    with pytest.raises(OSError, match="larger than any real copy"):
        _download(_SMALL)
    assert list(model_dir.iterdir()) == []


def test_the_real_files_are_named_and_sized_plausibly() -> None:
    detector, recognizer = opencv_faces._DETECTOR_FILE, opencv_faces._RECOGNIZER_FILE
    assert detector.name.startswith("face_detection_yunet")
    assert recognizer.name.startswith("face_recognition_sface")
    # YuNet is ~227 KB, SFace ~36.9 MB.
    assert detector.min_bytes < 227_000 < detector.max_bytes
    assert recognizer.min_bytes < 36_900_000 < recognizer.max_bytes


def _fake_cv2(
    monkeypatch: pytest.MonkeyPatch,
    rows: Any = None,
    *,
    with_face_classes: bool = True,
) -> types.SimpleNamespace:
    """Install a stand-in cv2 whose YuNet finds *rows* and SFace embeds them."""
    detector = MagicMock()
    detector.detect.return_value = (1, rows)
    recognizer = MagicMock()
    recognizer.alignCrop.return_value = np.zeros((112, 112, 3), dtype=np.uint8)
    recognizer.feature.return_value = np.array([[0.25, 0.5, 0.75]], dtype=np.float32)
    cv2 = types.SimpleNamespace(
        COLOR_RGB2BGR=4,
        cvtColor=lambda array, _code: array[:, :, ::-1].copy(),
    )
    if with_face_classes:
        cv2.FaceDetectorYN = MagicMock()
        cv2.FaceDetectorYN.create.return_value = detector
        cv2.FaceRecognizerSF = MagicMock()
        cv2.FaceRecognizerSF.create.return_value = recognizer
    cv2.detector = detector
    cv2.recognizer = recognizer
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    return cv2


def _row(x: float, y: float, w: float, h: float, score: float = 0.95) -> list[float]:
    """One YuNet detection: box, five landmarks (eyes, nose, mouth), score."""
    cx = x + w / 2
    landmarks = [
        x + w * 0.3, y + h * 0.35,
        x + w * 0.7, y + h * 0.35,
        cx, y + h * 0.55,
        x + w * 0.35, y + h * 0.8,
        x + w * 0.65, y + h * 0.8,
    ]  # fmt: skip
    return [x, y, w, h, *landmarks, score]


def _jpeg(size: tuple[int, int] = (160, 120)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (90, 120, 150)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def downloaded(monkeypatch: pytest.MonkeyPatch, model_dir: Path) -> list[_ModelFile]:
    """Both model files already in place, so a load needs no network."""
    files = [opencv_faces._DETECTOR_FILE, opencv_faces._RECOGNIZER_FILE]
    for model in files:
        (model_dir / model.name).write_bytes(b"x" * model.min_bytes)
    return files


def test_the_embedder_runs_the_opencv_model() -> None:
    embedder = OpenCVFaceEmbedder()
    assert embedder.model is OPENCV_SFACE
    assert not embedder._is_loaded()


def test_load_downloads_then_creates_both_models(
    monkeypatch: pytest.MonkeyPatch, model_dir: Path
) -> None:
    cv2 = _fake_cv2(monkeypatch)
    asked = _serve(monkeypatch, b"y" * 100_001)
    # Make the download sizes plausible for the stand-in body.
    monkeypatch.setattr(
        opencv_faces, "_RECOGNIZER_FILE", _ModelFile("r", "r.onnx", 1, 10**9)
    )
    embedder = OpenCVFaceEmbedder()
    embedder._load_sync()
    assert embedder._is_loaded()
    assert len(asked) == 2
    cv2.FaceDetectorYN.create.assert_called_once()
    detector_args = cv2.FaceDetectorYN.create.call_args.args
    assert detector_args[0].endswith("face_detection_yunet_2023mar.onnx")
    assert detector_args[3] == opencv_faces._DETECTION_SCORE
    cv2.FaceRecognizerSF.create.assert_called_once()
    assert cv2.FaceRecognizerSF.create.call_args.args[0].endswith("r.onnx")


def test_load_without_the_face_classes_is_an_import_error(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    """An OpenCV build too old for YuNet/SFace is reported as a missing
    package, which the stage treats as permanent and says how to fix."""
    _fake_cv2(monkeypatch, with_face_classes=False)
    embedder = OpenCVFaceEmbedder()
    with pytest.raises(ImportError, match="FaceDetectorYN"):
        embedder._load_sync()
    assert not embedder._is_loaded()


async def test_ensure_ready_loads_and_reports_healthy(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    _fake_cv2(monkeypatch)
    embedder = OpenCVFaceEmbedder()
    assert await embedder.ensure_ready()
    assert embedder.health() is not None


async def test_a_failed_download_is_a_failed_load_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, model_dir: Path
) -> None:
    _fake_cv2(monkeypatch)

    def offline(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("network unreachable")

    monkeypatch.setattr(opencv_faces.urllib.request, "urlopen", offline)
    embedder = OpenCVFaceEmbedder()
    assert not await embedder.ensure_ready()
    assert await embedder.detect(_jpeg()) is None
    health = embedder.health()
    assert health is not None
    assert health.state == "failed"


async def test_a_missing_opencv_is_reported_by_package_name(
    monkeypatch: pytest.MonkeyPatch,
    downloaded: list[_ModelFile],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setitem(sys.modules, "cv2", None)
    embedder = OpenCVFaceEmbedder()
    assert not await embedder.ensure_ready()
    health = embedder.health()
    assert health is not None
    assert health.permanent
    assert "pip install opencv-python-headless" in caplog.text


async def test_detect_describes_each_face(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    cv2 = _fake_cv2(
        monkeypatch,
        np.array([_row(40, 20, 60, 70), _row(1, 1, 20, 20, 0.8)], dtype=np.float32),
    )
    faces = await OpenCVFaceEmbedder().detect(_jpeg(), thumbnails=True)
    assert faces is not None
    assert len(faces) == 2
    first, second = faces
    assert first.embedding == [0.25, 0.5, 0.75]
    assert first.width == 60
    assert first.probability == pytest.approx(0.95, abs=1e-6)
    assert first.thumbnail.startswith(b"\xff\xd8")
    assert not first.cut_off
    assert second.probability == pytest.approx(0.8, abs=1e-6)
    assert second.width == 20
    assert second.cut_off  # touching the corner
    # YuNet is told the size of the frame it is about to read.
    cv2.detector.setInputSize.assert_called_with((160, 120))
    assert cv2.recognizer.alignCrop.call_count == 2


async def test_detect_leaves_out_thumbnails_unless_asked(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    _fake_cv2(monkeypatch, np.array([_row(40, 20, 60, 70)], dtype=np.float32))
    faces = await OpenCVFaceEmbedder().detect(_jpeg())
    assert faces is not None
    assert faces[0].thumbnail == b""


async def test_detect_finds_nobody_when_yunet_returns_nothing(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    """``[]`` ("looked, nobody there") and ``None`` ("could not look") must
    stay different: only the second may count against a clip."""
    _fake_cv2(monkeypatch, None)
    assert await OpenCVFaceEmbedder().detect(_jpeg()) == []


async def test_detect_cannot_read_a_bad_image(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    _fake_cv2(monkeypatch, None)
    assert await OpenCVFaceEmbedder().detect(b"not an image") is None


async def test_detect_shrinks_a_large_photo_first(
    monkeypatch: pytest.MonkeyPatch, downloaded: list[_ModelFile]
) -> None:
    cv2 = _fake_cv2(monkeypatch, None)
    await OpenCVFaceEmbedder().detect(_jpeg((4000, 3000)))
    width, height = cv2.detector.setInputSize.call_args.args[0]
    assert max(width, height) <= opencv_faces._MAX_DETECTION_SIDE
    assert width / height == pytest.approx(4 / 3, abs=0.01)
