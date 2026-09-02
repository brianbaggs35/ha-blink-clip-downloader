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
    CPUIncompatibleError,
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
    _is_huggingface_auth_error,
    _proximity_label,
    cosine_similarity,
    is_face_recognition_available,
    torch_cpu_compatible,
)


@pytest.fixture(autouse=True)
def _yolo_cache_dir_in_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """ObjectDetector._load_sync() resolves a bare model filename against
    vision._YOLO_MODEL_CACHE_DIR (a hardcoded /data path, see vision.py) and
    creates it with os.makedirs — unlike ultralytics itself, this isn't
    mocked away by the sys.modules substitution above, so without this
    fixture every ObjectDetector test would try to create a real /data
    directory on whatever machine runs the suite."""
    monkeypatch.setattr("blink_downloader.vision._YOLO_MODEL_CACHE_DIR", str(tmp_path))


def _real_jpeg_bytes(size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(128, 128, 128)).save(buf, format="JPEG")
    return buf.getvalue()


# ------------------------------------------------------------------
# Availability checks
# ------------------------------------------------------------------

# Real, full multi-core /proc/cpuinfo dumps (not synthetic one-liners) for
# actual devices this add-on runs on in the wild, used below to confirm
# torch_cpu_compatible() reads the right thing from a realistic file, not
# just a minimal fixture shaped exactly like the parser expects. Trailing
# per-core fields (CPU implementer/architecture/variant/part/revision) are
# included since a real file always has them between one core's Features
# line and the next core's "processor" line - the parser must skip over
# them correctly rather than accidentally matching on something in between.

# Raspberry Pi 5 (BCM2712, 4x Cortex-A76, ARMv8.2-A) - the add-on's own
# documented minimum-recommended board (see README.md). A76 has LSE atomics.
_CPUINFO_PI5 = """\
processor\t: 0
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd0b
CPU revision\t: 3

processor\t: 1
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd0b
CPU revision\t: 3

processor\t: 2
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd0b
CPU revision\t: 3

processor\t: 3
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd0b
CPU revision\t: 3

Hardware\t: BCM2712
Revision\t: c04170
Serial\t\t: 1000000012345678
Model\t\t: Raspberry Pi 5 Model B Rev 1.0
"""

# Raspberry Pi 4 (BCM2711, 4x Cortex-A72, ARMv8.0-A) - the documented
# unsupported case (see torch_cpu_compatible's docstring and README.md).
# A72 predates LSE atomics; note this Features line is real, not guessed -
# it genuinely has no "atomics" token, same as Pi 3 below.
_CPUINFO_PI4 = """\
processor\t: 0
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm crc32 cpuid
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd08
CPU revision\t: 3

processor\t: 1
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm crc32 cpuid
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd08
CPU revision\t: 3

processor\t: 2
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm crc32 cpuid
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd08
CPU revision\t: 3

processor\t: 3
BogoMIPS\t: 108.00
Features\t: fp asimd evtstrm crc32 cpuid
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd08
CPU revision\t: 3

Hardware\t: BCM2835
Revision\t: c03111
Serial\t\t: 1000000087654321
Model\t\t: Raspberry Pi 4 Model B Rev 1.1
"""

# Raspberry Pi 3 B+ (BCM2837B0, 4x Cortex-A53, ARMv8.0-A) - older and
# weaker than the Pi 4 case above, but the same architecture generation
# (no LSE atomics either), broadening negative-case device coverage beyond
# just the one board the docstring happens to name.
_CPUINFO_PI3 = """\
processor\t: 0
model name\t: ARMv7 Processor rev 4 (v7l)
BogoMIPS\t: 38.40
Features\t: half thumb fastmult vfp edsp neon vfpv3 tls vfpv4 idiva idivt vfpd32 lpae evtstrm crc32
CPU implementer\t: 0x41
CPU architecture: 7
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4

processor\t: 1
model name\t: ARMv7 Processor rev 4 (v7l)
BogoMIPS\t: 38.40
Features\t: half thumb fastmult vfp edsp neon vfpv3 tls vfpv4 idiva idivt vfpd32 lpae evtstrm crc32
CPU implementer\t: 0x41
CPU architecture: 7
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4

Hardware\t: BCM2835
Revision\t: a020d3
Serial\t\t: 1000000011223344
Model\t\t: Raspberry Pi 3 Model B Plus Rev 1.3
"""

# RK3588 (e.g. Orange Pi 5 / Radxa Rock 5), a big.LITTLE design pairing 4x
# Cortex-A55 with 4x Cortex-A76 - both ARMv8.2-A, so both core types have
# atomics. This is the realistic case for the "heterogeneous cores" worry:
# the LITTLE cores (which conventionally sort first as processor 0-3) must
# not be mistaken for an incompatible board just because they're the
# lower-power cluster - they're a different core, not a different (older)
# architecture generation, and still have atomics.
_CPUINFO_RK3588 = """\
processor\t: 0
BogoMIPS\t: 48.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop asimddp
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd05
CPU revision\t: 0

processor\t: 4
BogoMIPS\t: 48.00
Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp cpuid asimdrdm lrcpc dcpop sha3 asimddp sb dcpodp flagm
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd0b
CPU revision\t: 0

Hardware\t: Rockchip RK3588
"""


@pytest.mark.parametrize(
    ("device", "cpuinfo", "expected"),
    [
        ("Raspberry Pi 5 (Cortex-A76)", _CPUINFO_PI5, True),
        ("Raspberry Pi 4 (Cortex-A72)", _CPUINFO_PI4, False),
        ("Raspberry Pi 3 B+ (Cortex-A53)", _CPUINFO_PI3, False),
        ("RK3588 (Cortex-A55 + A76 big.LITTLE)", _CPUINFO_RK3588, True),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else None,
)
def test_torch_cpu_compatible_real_device_cpuinfo(
    monkeypatch: pytest.MonkeyPatch, device: str, cpuinfo: str, expected: bool
) -> None:
    """Real, full /proc/cpuinfo content from actual boards, not a minimal
    synthetic fixture - covers multiple manufacturers/generations so a
    parsing quirk (extra fields between the Features line and the next
    core, multi-core files, big.LITTLE core-part-number variety) can't
    silently pass on a toy fixture while breaking on real hardware. See
    each _CPUINFO_* constant's own comment for why that specific device
    was chosen and what it's meant to guard against.
    """
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is expected, device


def test_torch_cpu_compatible_reads_first_core_not_a_later_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the multi-core case: the function must return
    based on the *first* "processor" block's Features line, not scan past
    it and accidentally match something in a later block or an unrelated
    line further down the file (e.g. if a later section happened to
    contain the word "atomics" in a different context)."""
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    cpuinfo = (
        "processor\t: 0\nFeatures\t: fp asimd evtstrm crc32\n\n"
        "processor\t: 1\nFeatures\t: fp asimd evtstrm crc32 atomics\n"
    )
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is False


def test_torch_cpu_compatible_atomics_matched_as_whole_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "atomics" must be matched as an exact whitespace-delimited token
    (features.split() + membership test), not a substring - a feature flag
    that merely *contains* "atomics" as part of a longer word must not
    false-positive a device into being reported as compatible."""
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    cpuinfo = "processor\t: 0\nFeatures\t: fp asimd notarealatomicsflag\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is False


@pytest.mark.parametrize(
    "cpuinfo",
    [
        # Mixed case, matching this project's case-insensitive startswith check.
        "processor\t: 0\nFEATURES\t: fp asimd atomics\n",
        "processor\t: 0\nfeatures\t: fp asimd atomics\n",
        # No leading whitespace/tab before the colon.
        "processor: 0\nFeatures: fp asimd atomics\n",
    ],
)
def test_torch_cpu_compatible_tolerates_formatting_variance(
    monkeypatch: pytest.MonkeyPatch, cpuinfo: str
) -> None:
    """Real /proc/cpuinfo formatting (capitalization, tab-vs-space before
    the colon) varies slightly across kernel versions and vendors - none of
    that should affect whether a genuinely capable device gets correctly
    detected. (The kernel always left-aligns field names with no leading
    indentation, so that's not a case worth fabricating here.)"""
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is True


def test_torch_cpu_compatible_true_on_non_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """x86_64 (and any non-ARM arch) never needs the /proc/cpuinfo check —
    the LSE/illegal-instruction risk is ARM-specific."""
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "x86_64")
    assert torch_cpu_compatible() is True


def test_torch_cpu_compatible_true_when_atomics_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    cpuinfo = "processor\t: 0\nFeatures\t: fp asimd evtstrm aes atomics fphp\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is True


def test_torch_cpu_compatible_false_when_atomics_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the actual Raspberry Pi 4 (Cortex-A72) case — LSE/atomics
    was only added in ARMv8.1, which A72 predates."""
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    cpuinfo = "processor\t: 0\nFeatures\t: fp asimd evtstrm aes fphp\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is False


def test_torch_cpu_compatible_false_when_no_features_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    cpuinfo = "processor\t: 0\nmodel name\t: whatever\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is False


def test_torch_cpu_compatible_false_when_cpuinfo_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative default: can't confirm safety, so assume unsupported
    rather than risk the crash this check exists to prevent."""
    monkeypatch.setattr("blink_downloader.vision.platform.machine", lambda: "aarch64")
    with patch("builtins.open", side_effect=OSError("no such file")):
        assert torch_cpu_compatible() is False


def test_is_face_recognition_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: True)
    monkeypatch.setitem(sys.modules, "facenet_pytorch", MagicMock())
    assert is_face_recognition_available() is True


def test_is_face_recognition_available_false_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: True)
    monkeypatch.delitem(sys.modules, "facenet_pytorch", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        assert is_face_recognition_available() is False


def test_is_face_recognition_available_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the package present, an incompatible CPU must still report
    unavailable — this is what keeps the enrollment endpoint from ever
    trying the import that would crash the process."""
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: False)
    monkeypatch.setitem(sys.modules, "facenet_pytorch", MagicMock())
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


def test_best_subject_vehicle_pair_rejects_later_worse_pair() -> None:
    """A pair evaluated after the best one has already been found, but with
    a larger (worse) gap, must not replace it — exercises the comparison's
    False branch, not just the "no best yet" initial-assignment case."""
    detections = [
        DetectedObject("person", 0.9, (0, 0, 10, 10), None, 0),
        DetectedObject("car", 0.9, (5, 5, 20, 20), None, 0),
        DetectedObject("person", 0.9, (0, 0, 10, 10), None, 1),
        DetectedObject("car", 0.9, (200, 200, 210, 210), None, 1),
    ]
    pair = _best_subject_vehicle_pair(detections)
    assert pair is not None
    _person, _vehicle, frame_idx = pair
    assert frame_idx == 0


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


def test_object_detector_load_sync_raises_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_load_sync() must refuse before attempting the ultralytics import at
    all when the CPU can't safely run it — that import is what would
    actually crash the process, so the guard has to come first."""
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: False)
    detector = ObjectDetector()
    with pytest.raises(CPUIncompatibleError):
        detector._load_sync()


def test_object_detector_load_sync_leaves_explicit_path_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied model path that already has a directory component
    (unlike the default bare "yolo11n.pt") must be passed to YOLO() exactly
    as given — not joined with the model cache dir, which is only for
    resolving a bare filename (see _load_sync's comment)."""
    mock_ultra = MagicMock()
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    explicit_path = "/custom/models/my-yolo.pt"
    detector = ObjectDetector(explicit_path)
    detector._load_sync()

    mock_ultra.YOLO.assert_called_once_with(explicit_path)


async def test_object_detector_ensure_ready_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: False)
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


async def test_depth_estimator_ensure_ready_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: False)
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


async def test_depth_estimator_passes_huggingface_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    estimator = DepthEstimator("hf_test_token")

    assert await estimator.ensure_ready() is True

    assert mock_transformers.pipeline.call_args.kwargs["token"] == "hf_test_token"


async def test_depth_estimator_handles_invalid_huggingface_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class InvalidTokenError(RuntimeError):
        pass

    mock_transformers = MagicMock()
    mock_transformers.pipeline.side_effect = InvalidTokenError("token rejected")
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    estimator = DepthEstimator("hf_test_token")

    assert await estimator.ensure_ready() is False
    assert "Hugging Face authentication failed" in caplog.text
    assert "hf_test_token" not in caplog.text


async def test_depth_estimator_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    estimator = DepthEstimator()
    assert await estimator.ensure_ready() is True
    assert await estimator.ensure_ready() is True
    mock_transformers.pipeline.assert_called_once()
    assert mock_transformers.pipeline.call_args.kwargs["token"] is None


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
    mock_processor.post_process_masks.return_value = [_FakeMasks(masks)]
    mock_model = MagicMock()
    mock_model.return_value.pred_masks.cpu.return_value = MagicMock()

    mock_transformers = MagicMock()
    mock_processor.init_video_session.return_value.video_height = 10
    mock_processor.init_video_session.return_value.video_width = 10
    mock_transformers.Sam2VideoModel.from_pretrained.return_value = mock_model
    mock_transformers.Sam2VideoProcessor.from_pretrained.return_value = mock_processor
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


async def test_contact_segmenter_ensure_ready_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: False)
    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is False


async def test_contact_segmenter_ensure_ready_concurrent_calls_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_transformers.Sam2VideoModel.from_pretrained.side_effect = lambda *_a, **_kw: (
        time.sleep(0.05),
        MagicMock(),
    )[1]
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    segmenter = ContactSegmenter()
    results = await asyncio.gather(segmenter.ensure_ready(), segmenter.ensure_ready())
    assert results == [True, True]
    mock_transformers.Sam2VideoModel.from_pretrained.assert_called_once()


async def test_contact_segmenter_ensure_ready_handles_generic_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    mock_transformers.Sam2VideoModel.from_pretrained.side_effect = RuntimeError(
        "no weights"
    )
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is False


async def test_contact_segmenter_passes_huggingface_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter("hf_test_token")

    assert await segmenter.ensure_ready() is True

    assert (
        mock_transformers.Sam2VideoModel.from_pretrained.call_args.kwargs["token"]
        == "hf_test_token"
    )
    assert (
        mock_transformers.Sam2VideoProcessor.from_pretrained.call_args.kwargs["token"]
        == "hf_test_token"
    )


async def test_contact_segmenter_handles_huggingface_http_auth_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = RuntimeError("401 Client Error: Unauthorized for https://huggingface.co")
    mock_transformers = MagicMock()
    mock_transformers.Sam2VideoModel.from_pretrained.side_effect = error
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()

    assert await segmenter.ensure_ready() is False
    assert "Hugging Face authentication failed" in caplog.text


async def test_contact_segmenter_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is True
    assert await segmenter.ensure_ready() is True
    mock_transformers.Sam2VideoModel.from_pretrained.assert_called_once()
    assert (
        mock_transformers.Sam2VideoModel.from_pretrained.call_args.kwargs["token"]
        is None
    )
    assert (
        mock_transformers.Sam2VideoProcessor.from_pretrained.call_args.kwargs["token"]
        is None
    )


async def test_contact_segmenter_touching_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    person_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask = np.zeros((10, 10), dtype=np.uint8)
    vehicle_mask[5, 5] = 1
    mock_transformers = _install_fake_transformers_for_sam2(
        monkeypatch, [person_mask, vehicle_mask]
    )

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
    mock_processor = mock_transformers.Sam2VideoProcessor.from_pretrained.return_value
    mock_processor.init_video_session.assert_called_once()
    mock_processor.add_inputs_to_inference_session.assert_called_once_with(
        inference_session=mock_processor.init_video_session.return_value,
        frame_idx=0,
        obj_ids=[0, 1],
        input_boxes=[[[0, 0, 5, 5], [5, 5, 10, 10]]],
    )
    mock_transformers.Sam2VideoModel.from_pretrained.return_value.assert_called_once_with(
        inference_session=mock_processor.init_video_session.return_value,
        frame_idx=0,
    )


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
    mock_processor = MagicMock()
    mock_processor.init_video_session.side_effect = RuntimeError("boom")
    mock_transformers.Sam2VideoProcessor.from_pretrained.return_value = mock_processor
    mock_transformers.Sam2VideoModel.from_pretrained.return_value = MagicMock()
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

    def unsqueeze(self, _axis: int) -> _FakeFaceTensor:
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


async def test_face_embedder_ensure_ready_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("blink_downloader.vision.torch_cpu_compatible", lambda: False)
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
    mock_processor.post_process_masks.return_value = [
        _FakeMasks([person_mask, vehicle_mask])
    ]
    mock_sam_model = MagicMock()
    mock_sam_model.return_value.pred_masks.cpu.return_value = MagicMock()

    mock_transformers = MagicMock()
    mock_transformers.pipeline.return_value = mock_pipe
    mock_processor.init_video_session.return_value.video_height = 10
    mock_processor.init_video_session.return_value.video_width = 10
    mock_transformers.Sam2VideoModel.from_pretrained.return_value = mock_sam_model
    mock_transformers.Sam2VideoProcessor.from_pretrained.return_value = mock_processor
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


async def test_vision_pipeline_depth_hint_unset_when_compare_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subject/vehicle pair is found (so depth comparison actually runs),
    but the depth stage itself comes back empty (e.g. its dependency is
    unavailable) — depth_hint must stay unset rather than a stale/garbage
    value, and this must not prevent contact_hint from still being
    evaluated independently."""
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

    # DepthEstimator.compare() (and ContactSegmenter.check_contact())
    # degrade to None when their shared dependency (transformers) can't be
    # imported. A `None` entry in sys.modules makes Python re-raise
    # ImportError immediately for just that one module, unlike patching
    # builtins.__import__ globally — which would also break the (already
    # mocked, cached-in-sys.modules) ultralytics import this test still
    # needs for object detection to find the subject/vehicle pair at all.
    monkeypatch.setitem(sys.modules, "transformers", None)

    config = VisionConfig(enhanced_detection_enabled=True)
    pipeline = VisionPipeline(config)
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes()],
        car_description="Silver Kia",
        car_protection_applies=True,
    )

    assert hints.depth_hint is None
    assert hints.contact_hint is None


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


async def test_vision_pipeline_face_recognition_prefers_face_recognition_frames_param(
    db: ClipDatabase,
) -> None:
    """When the caller supplies face_recognition_frames (the wider,
    pre-down-selection extraction pool from analyzer.py), face recognition
    must scan that instead of the smaller `frames` set used for the AI
    prompt and the other CV stages — see process_clip's docstring for why a
    person can be in the raw pool but missing from the down-selected set."""
    await db.add_face_enrollment("Brian", [1.0, 0.0])
    prompt_frames = [b"prompt-frame-1"]
    wider_pool = [b"pool-frame-1", b"pool-frame-2", b"pool-frame-3"]
    seen_frames: list[bytes] = []

    async def _embed(frame: bytes) -> list[list[float]]:
        seen_frames.append(frame)
        return [[1.0, 0.0]]

    config = VisionConfig(face_recognition_enabled=True)
    pipeline = VisionPipeline(config, db=db)
    with patch.object(FaceEmbedder, "embed", side_effect=_embed):
        hints = await pipeline.process_clip(
            prompt_frames, face_recognition_frames=wider_pool
        )

    assert seen_frames == wider_pool
    assert hints.face_recognition is not None
    assert hints.face_recognition.approved_names == ["Brian"]


async def test_vision_pipeline_face_recognition_without_db_is_noop() -> None:
    pipeline = VisionPipeline(VisionConfig(face_recognition_enabled=True), db=None)
    hints = await pipeline.process_clip([b"frame"])
    assert hints.recognized_resident_hint is None
    assert hints.face_recognition is None


def test_vision_pipeline_update_config_reuses_detector_when_model_unchanged() -> None:
    pipeline = VisionPipeline(VisionConfig(object_detection_model="yolo11n.pt"))
    original_detector = pipeline._detector
    pipeline.update_config(VisionConfig(object_detection_model="yolo11n.pt"))
    assert pipeline._detector is original_detector


def test_vision_pipeline_update_config_reloads_detector_on_model_change() -> None:
    pipeline = VisionPipeline(VisionConfig(object_detection_model="yolo11n.pt"))
    original_detector = pipeline._detector
    pipeline.update_config(VisionConfig(object_detection_model="yolo11s.pt"))
    assert pipeline._detector is not original_detector


def test_vision_pipeline_update_config_reloads_huggingface_stages_on_token_change() -> (
    None
):
    pipeline = VisionPipeline(VisionConfig(hf_token="old_token"))
    original_depth = pipeline._depth
    original_segmenter = pipeline._segmenter

    pipeline.update_config(VisionConfig(hf_token="new_token"))

    assert pipeline._depth is not original_depth
    assert pipeline._segmenter is not original_segmenter
    assert pipeline._depth._hf_token == "new_token"
    assert pipeline._segmenter._hf_token == "new_token"


def test_huggingface_auth_error_status_code_is_detected() -> None:
    class ForbiddenError(RuntimeError):
        status_code = 403

    error = ForbiddenError("request rejected")

    assert _is_huggingface_auth_error(error) is True
