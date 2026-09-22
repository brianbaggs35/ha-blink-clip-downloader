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

from blink_downloader import vision as vision_module
from blink_downloader.database import ClipDatabase
from blink_downloader.security.assets import AssetLocation, AssetType, ProtectedAsset
from blink_downloader.security.geometry import Box, Zone
from blink_downloader.security.tracks import build_tracks
from blink_downloader.security.vehicles import (
    VehicleSignature,
    identify_protected_vehicle,
)
from blink_downloader.vision import (
    SOURCE_CONTACT_SEGMENTATION,
    SOURCE_DEPTH_ESTIMATION,
    SOURCE_FACE_RECOGNITION,
    SOURCE_OBJECT_DETECTION,
    SOURCE_POSE_ESTIMATION,
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
    PoseEstimator,
    PostureResult,
    VisionConfig,
    VisionPipeline,
    cosine_similarity,
    is_face_recognition_available,
    torch_cpu_compatible,
)

# Internals come from the stage module that owns them, not the package
# facade: the facade exports the pipeline's public surface, and a test that
# reaches past it should say which stage it is reaching into.
from blink_downloader.vision import imaging as imaging_module
from blink_downloader.vision import runtime as runtime_module
from blink_downloader.vision.contact import _build_contact_hint
from blink_downloader.vision.depth import _build_depth_hint
from blink_downloader.vision.detection import (
    _best_subject_vehicle_pair,
    _box_gap,
    _build_detection_hint,
    _build_tracking_hint,
    _car_zone_reference,
    _detection_distance_pair,
    _proximity_label,
    _ZoneReference,
)
from blink_downloader.vision.faces import (
    _build_recognition_hint,
    _frontality,
    _sharpness,
    match_enrollment,
)
from blink_downloader.vision.imaging import (
    _crop_region,
    _region_appearance_change,
    _select_scan_frames,
    _vehicle_histogram,
)
from blink_downloader.vision.pose import (
    _best_pose_keypoints,
    _build_posture_hint,
    _is_reaching,
    _keypoint,
    _pose_confidence,
    _posture_from_keypoints,
)
from blink_downloader.vision.runtime import _is_huggingface_auth_error


@pytest.fixture(autouse=True)
def _yolo_cache_dir_in_tmp_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """ObjectDetector._load_sync() resolves a bare model filename against
    vision._YOLO_MODEL_CACHE_DIR (a hardcoded /data path, see vision.py) and
    creates it with os.makedirs — unlike ultralytics itself, this isn't
    mocked away by the sys.modules substitution above, so without this
    fixture every ObjectDetector test would try to create a real /data
    directory on whatever machine runs the suite."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime._YOLO_MODEL_CACHE_DIR", str(tmp_path)
    )


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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is True


def test_torch_cpu_compatible_true_on_non_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """x86_64 (and any non-ARM arch) never needs the /proc/cpuinfo check —
    the LSE/illegal-instruction risk is ARM-specific."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "x86_64"
    )
    assert torch_cpu_compatible() is True


def test_torch_cpu_compatible_true_when_atomics_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
    cpuinfo = "processor\t: 0\nFeatures\t: fp asimd evtstrm aes atomics fphp\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is True


def test_torch_cpu_compatible_false_when_atomics_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the actual Raspberry Pi 4 (Cortex-A72) case — LSE/atomics
    was only added in ARMv8.1, which A72 predates."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
    cpuinfo = "processor\t: 0\nFeatures\t: fp asimd evtstrm aes fphp\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is False


def test_torch_cpu_compatible_false_when_no_features_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
    cpuinfo = "processor\t: 0\nmodel name\t: whatever\n"
    with patch("builtins.open", MagicMock(return_value=io.StringIO(cpuinfo))):
        assert torch_cpu_compatible() is False


def test_torch_cpu_compatible_false_when_cpuinfo_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conservative default: can't confirm safety, so assume unsupported
    rather than risk the crash this check exists to prevent."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.platform.machine", lambda: "aarch64"
    )
    with patch("builtins.open", side_effect=OSError("no such file")):
        assert torch_cpu_compatible() is False


def test_is_face_recognition_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    monkeypatch.setitem(sys.modules, "facenet_pytorch", MagicMock())
    assert is_face_recognition_available() is True


def test_is_face_recognition_available_false_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: True
    )
    monkeypatch.delitem(sys.modules, "facenet_pytorch", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        assert is_face_recognition_available() is False


def test_is_face_recognition_available_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with the package present, an incompatible CPU must still report
    unavailable — this is what keeps the enrollment endpoint from ever
    trying the import that would crash the process."""
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
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


def test_proximity_label_well_under_one_foot() -> None:
    # vehicle_width=60px calibrates 10px/ft (a typical ~6ft-wide vehicle);
    # gap=5px -> 0.5ft
    assert _proximity_label(5.0, 60.0) == "well under 1 ft from the detected vehicle"


def test_proximity_label_one_foot_boundary_is_approximately() -> None:
    # gap=10px -> exactly 1.0ft: the < 1.0 branch must NOT fire at the boundary
    assert (
        _proximity_label(10.0, 60.0) == "approximately 1 ft from the detected vehicle"
    )


def test_proximity_label_between_one_and_three_feet() -> None:
    # gap=20px -> 2.0ft
    assert (
        _proximity_label(20.0, 60.0) == "approximately 2 ft from the detected vehicle"
    )


def test_proximity_label_three_foot_boundary_is_well_away() -> None:
    # gap=30px -> exactly 3.0ft: the < 3.0 branch must NOT fire at the boundary
    assert _proximity_label(30.0, 60.0) == (
        "well away from the detected vehicle (roughly 3 ft or more)"
    )


def test_proximity_label_zero_width_vehicle_is_indeterminate() -> None:
    # vehicle_width <= 0 can't calibrate a scale, so no feet estimate is given
    assert (
        _proximity_label(5.0, 0.0)
        == "at an indeterminate distance from the detected vehicle"
    )


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


def test_best_subject_vehicle_pair_zone_box_disambiguates_multiple_vehicles() -> None:
    """Two vehicle-class detections in one frame (the protected car, plus an
    unrelated car the person happens to be standing right next to) - the
    one nearest the configured zone must be treated as "the" vehicle,
    not whichever one wins on raw subject-proximity alone."""
    detections = [
        DetectedObject("person", 0.9, (0, 0, 2, 2), None, 0),
        DetectedObject(
            "car", 0.9, (2, 2, 4, 4), None, 0
        ),  # unrelated, touching the person
        DetectedObject(
            "car", 0.9, (100, 100, 110, 110), None, 0
        ),  # the actual protected car
    ]
    zone_ref = _rect_zone_ref(0.95, 0.95, 1.15, 1.15)
    pair = _best_subject_vehicle_pair(detections, zone_ref)
    assert pair is not None
    _subject, vehicle, _frame_idx = pair
    assert vehicle.box == (100, 100, 110, 110)


def test_best_subject_vehicle_pair_without_zone_box_keeps_naive_smallest_gap() -> None:
    """Same ambiguous scene as above but with no zone_box - must fall back
    to the pre-existing "closest vehicle wins" behavior unchanged."""
    detections = [
        DetectedObject("person", 0.9, (0, 0, 2, 2), None, 0),
        DetectedObject("car", 0.9, (2, 2, 4, 4), None, 0),
        DetectedObject("car", 0.9, (100, 100, 110, 110), None, 0),
    ]
    pair = _best_subject_vehicle_pair(detections)
    assert pair is not None
    _subject, vehicle, _frame_idx = pair
    assert vehicle.box == (2, 2, 4, 4)


def test_best_subject_vehicle_pair_zone_box_ignored_with_single_vehicle() -> None:
    """A single vehicle-class detection is unambiguous - zone_box only
    disambiguates *between* candidates, it must not gate whether pairing
    happens at all, even when the zone is far from that one detection."""
    detections = [
        DetectedObject("person", 0.9, (0, 0, 2, 2), None, 0),
        DetectedObject("car", 0.9, (2, 2, 4, 4), None, 0),
    ]
    zone_ref = _rect_zone_ref(5.0, 5.0, 6.0, 6.0)
    pair = _best_subject_vehicle_pair(detections, zone_ref)
    assert pair is not None
    _subject, vehicle, _frame_idx = pair
    assert vehicle.box == (2, 2, 4, 4)


# ------------------------------------------------------------------
# _car_zone_reference
# ------------------------------------------------------------------


def _rect_zone_ref(
    x_min: float, y_min: float, x_max: float, y_max: float
) -> _ZoneReference:
    """A rectangle zone already resolved against a 100x100 frame."""
    zone = Zone.from_config(
        {
            "shape": "rect",
            "x_min": x_min,
            "y_min": y_min,
            "x_max": x_max,
            "y_max": y_max,
        }
    )
    assert zone is not None
    return _ZoneReference(
        zone=zone, size=(100.0, 100.0), box=zone.to_pixel_box(100, 100)
    )


def _polygon_zone_ref(points: list[list[float]]) -> _ZoneReference:
    """A freeform zone already resolved against a 100x100 frame."""
    zone = Zone.from_config({"shape": "polygon", "points": points})
    assert zone is not None
    return _ZoneReference(
        zone=zone, size=(100.0, 100.0), box=zone.to_pixel_box(100, 100)
    )


def test_best_subject_vehicle_pair_polygon_zone_ignores_a_car_outside_the_outline() -> (
    None
):
    """The neighbour's car sits in the *bounding box* of a traced driveway
    but outside the drawn outline itself. Reducing the zone to those bounds
    (what this used to do) hands it the disambiguation win, because the
    person is standing right beside it; the traced outline must not."""
    # An L: the driveway runs down the left edge and along the bottom. Its
    # bounding box covers the whole frame, including the top-right corner
    # the drive never reaches.
    zone_ref = _polygon_zone_ref(
        [[0.0, 0.0], [0.3, 0.0], [0.3, 0.7], [1.0, 0.7], [1.0, 1.0], [0.0, 1.0]]
    )
    detections = [
        # Standing next to the neighbour's car, up in the excluded corner.
        DetectedObject("person", 0.9, (70, 10, 78, 30), None, 0),
        DetectedObject("car", 0.9, (80, 10, 95, 25), None, 0),  # neighbour's
        DetectedObject("car", 0.9, (5, 75, 25, 95), None, 0),  # in the drive
    ]
    assert zone_ref.covers((5, 75, 25, 95)) is True
    assert zone_ref.covers((80, 10, 95, 25)) is False
    # The bounding box alone cannot tell them apart at all.
    assert zone_ref.box == (0.0, 0.0, 100.0, 100.0)

    pair = _best_subject_vehicle_pair(detections, zone_ref)
    assert pair is not None
    _subject, vehicle, _frame_idx = pair
    assert vehicle.box == (5, 75, 25, 95)


def test_best_subject_vehicle_pair_polygon_zone_falls_back_when_none_are_inside() -> (
    None
):
    """No vehicle is inside the traced outline, so proximity to its bounds
    decides - exactly what happened before there was an outline test."""
    zone_ref = _polygon_zone_ref([[0.0, 0.8], [0.2, 0.8], [0.2, 1.0], [0.0, 1.0]])
    detections = [
        DetectedObject("person", 0.9, (48, 48, 52, 52), None, 0),
        DetectedObject("car", 0.9, (50, 50, 60, 60), None, 0),
        DetectedObject("car", 0.9, (25, 85, 35, 95), None, 0),  # nearer the zone
    ]
    pair = _best_subject_vehicle_pair(detections, zone_ref)
    assert pair is not None
    _subject, vehicle, _frame_idx = pair
    assert vehicle.box == (25, 85, 35, 95)


def test_car_zone_reference_converts_rect_to_pixel_coords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros(
        (20, 40, 3), dtype=np.uint8
    )  # height=20, width=40
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    zone = {"shape": "rect", "x_min": 0.25, "y_min": 0.5, "x_max": 0.75, "y_max": 1.0}
    ref = _car_zone_reference(zone, b"frame")
    assert ref is not None
    assert ref.box == (10.0, 10.0, 30.0, 20.0)
    assert ref.size == (40.0, 20.0)


def test_car_zone_reference_keeps_the_polygon_beside_its_bounding_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coarse bounds stay available for the distance hint, but the
    traced outline travels with them so membership stays exact."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    zone = {
        "shape": "polygon",
        "points": [[0.1, 0.1], [0.5, 0.0], [0.9, 0.9], [0.2, 0.8]],
    }
    ref = _car_zone_reference(zone, b"frame")
    assert ref is not None
    assert ref.box == (1.0, 0.0, 9.0, 9.0)
    assert ref.zone.is_polygon is True
    # Inside the bounds, outside the traced shape.
    assert ref.covers((8.2, 0.1, 8.9, 0.6)) is False
    assert ref.covers((3.0, 3.0, 5.0, 5.0)) is True


def test_car_zone_reference_none_on_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = None
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    zone = {"shape": "rect", "x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    assert _car_zone_reference(zone, b"frame") is None


def test_car_zone_reference_none_for_polygon_with_no_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    assert _car_zone_reference({"shape": "polygon", "points": []}, b"frame") is None


def test_build_detection_hint_reports_an_empty_sweep_rather_than_nothing() -> None:
    """A detector that ran and found nothing is evidence, not silence.

    This used to return None, so a clip the detector swept clean reached
    the model with no grounding at all — the case where grounding matters
    most.
    """
    hint = _build_detection_hint([], "Silver Kia")
    assert hint is not None
    assert "No objects of any tracked class were detected" in hint
    assert "No person and no animal was detected" in hint


def test_build_detection_hint_states_no_person_when_only_vehicles_found() -> None:
    """The bug this exists for: four parked cars, nobody in frame, and a
    prompt that never once said so — leaving a small model to narrate "a
    person is walking along the street" into an empty driveway."""
    detections = [
        DetectedObject("car", 0.9, (0, 0, 10, 10), 1, 0),
        DetectedObject("car", 0.9, (20, 0, 30, 10), 2, 0),
    ]
    hint = _build_detection_hint(detections, "Silver Kia")
    assert hint is not None
    assert "No person and no animal was detected in any sampled frame" in hint


def test_build_detection_hint_no_subject_line_stays_overridable() -> None:
    """Never a gag order: a distant or partly hidden person is exactly what
    a nano-scale detector misses, so the model must stay free to report one
    it can actually see. Nor does the line touch the verdict — a vehicle can
    damage another vehicle with nobody present."""
    hint = _build_detection_hint([DetectedObject("car", 0.9, (0, 0, 9, 9), 1, 0)], "")
    assert hint is not None
    assert "unless you can plainly see one in these frames yourself" in hint
    assert "suspicious" not in hint.lower()


def test_build_detection_hint_omits_no_subject_line_when_a_person_is_found() -> None:
    detections = [
        DetectedObject("person", 0.9, (0, 0, 10, 10), 1, 0),
        DetectedObject("car", 0.9, (20, 0, 30, 10), 2, 0),
    ]
    hint = _build_detection_hint(detections, "")
    assert hint is not None
    assert "No person and no animal" not in hint


def test_build_detection_hint_omits_no_subject_line_for_an_animal() -> None:
    """A dog is a subject in its own right (SUBJECT_LABELS), so a clip with
    one must not be described as having no animal in it."""
    hint = _build_detection_hint([DetectedObject("dog", 0.9, (0, 0, 9, 9), 1, 0)], "")
    assert hint is not None
    assert "No person and no animal" not in hint


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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
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


def _yolo_frame_env(monkeypatch: pytest.MonkeyPatch, boxes, names) -> None:
    """Point ObjectDetector at a fake ultralytics returning *boxes*."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, names)]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)


async def test_object_detector_drops_untracked_low_confidence_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One real dog must count as one dog.

    ``model.track()`` forces conf=0.1 for ByteTrack's own second-stage
    association, and on any frame where the tracker returns no activated
    track ultralytics hands back the raw predictions unfiltered and
    id-less. Storing those is what made a single subject read as two or
    three: measured on a synthetic clip of one person crossing a backdrop
    that detects nothing on its own, the junk arrived as "person" boxes at
    0.13-0.22 and a phantom "car" at 0.12.
    """
    boxes = _FakeBoxes(
        cls=[16, 16, 2],
        conf=[0.88, 0.14, 0.12],  # one real dog, one junk dog, one junk car
        xyxy=[(0.0, 0.0, 9.0, 9.0), (30.0, 30.0, 34.0, 34.0), (50.0, 50.0, 55.0, 55.0)],
        ids=None,  # the tracker returned nothing for this frame
    )
    _yolo_frame_env(monkeypatch, boxes, {16: "dog", 2: "car"})

    detections = await ObjectDetector("yolo26n.pt").detect([b"frame0"])

    assert detections is not None
    assert [(d.label, d.confidence) for d in detections] == [("dog", 0.88)]


async def test_object_detector_keeps_a_low_confidence_box_the_tracker_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor applies only to *orphan* boxes.

    A weak box carrying a track id was matched to an established track by
    ByteTrack itself — that is the recall the low threshold exists to buy,
    and dropping it would lose a real subject mid-track.
    """
    boxes = _FakeBoxes(
        cls=[0],
        conf=[0.13],
        xyxy=[(0.0, 0.0, 9.0, 9.0)],
        ids=[7],
    )
    _yolo_frame_env(monkeypatch, boxes, {0: "person"})

    detections = await ObjectDetector("yolo26n.pt").detect([b"frame0"])

    assert detections is not None
    assert [(d.label, d.track_id) for d in detections] == [("person", 7)]


async def test_object_detector_keeps_every_confident_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor must not cost a real count: three people genuinely in
    frame together stay three, tracked or not."""
    boxes = _FakeBoxes(
        cls=[0, 0, 0],
        conf=[0.88, 0.64, 0.26],
        xyxy=[(0.0, 0.0, 9.0, 9.0), (20.0, 0.0, 29.0, 9.0), (40.0, 0.0, 49.0, 9.0)],
        ids=None,
    )
    _yolo_frame_env(monkeypatch, boxes, {0: "person"})

    detections = await ObjectDetector("yolo26n.pt").detect([b"frame0"])

    assert detections is not None
    assert len(detections) == 3


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


async def test_object_detector_detect_resets_tracker_persist_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """persist=False must be passed on the first successfully-decoded frame
    of every detect() call, forcing Ultralytics to rebuild fresh ByteTrack
    state (see _detect_sync's own comment for the exact mechanism) even
    though this model instance is reused across many unrelated clips over
    the app's lifetime - persist=True for every later frame within that
    same call preserves the intended continuity across just this one
    clip's own sampled frames."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(cls=[0], conf=[0.9], xyxy=[(0.0, 0.0, 1.0, 1.0)], ids=[1])
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {0: "person"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    detector = ObjectDetector()
    await detector.detect([b"frame0", b"frame1", b"frame2"])
    persist_values = [
        call.kwargs["persist"] for call in fake_model.track.call_args_list
    ]
    assert persist_values == [False, True, True]

    # A second, unrelated detect() call (e.g. a completely different clip
    # analyzed later on this same shared, long-lived detector instance)
    # must reset again - never carry over persist=True from the previous
    # call's last frame.
    fake_model.track.reset_mock()
    await detector.detect([b"frame0"])
    assert fake_model.track.call_args.kwargs["persist"] is False


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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
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


async def test_depth_estimator_passes_custom_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ai_depth_estimation_model (Small/Base/Large) must reach the
    transformers pipeline() call, mirroring ObjectDetector's own
    model_name pass-through."""
    mock_transformers = MagicMock()
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    estimator = DepthEstimator(model_id="depth-anything/Depth-Anything-V2-Large-hf")

    assert await estimator.ensure_ready() is True

    assert (
        mock_transformers.pipeline.call_args.kwargs["model"]
        == "depth-anything/Depth-Anything-V2-Large-hf"
    )


def test_depth_estimator_defaults_to_small_model_id() -> None:
    estimator = DepthEstimator()
    assert estimator._model_id == "depth-anything/Depth-Anything-V2-Small-hf"


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
    hint = _build_depth_hint(DepthComparison(True, 10.0, 11.0), "dog")
    assert "roughly the same distance" in hint
    assert "detected dog" in hint


def test_build_depth_hint_different_subject_nearer() -> None:
    # Depth Anything: larger region value = nearer the camera (see
    # _build_depth_hint's own comment) - subject_depth > vehicle_depth
    # means the subject is on the near/same side, in plain view.
    hint = _build_depth_hint(DepthComparison(False, 200.0, 10.0), "person")
    assert "noticeably different distances" in hint
    assert "nearer to the camera than the vehicle" in hint
    assert "near/same side" in hint


def test_build_depth_hint_different_subject_farther() -> None:
    # subject_depth < vehicle_depth means the subject is farther from the
    # camera than the vehicle - the far/occluded side.
    hint = _build_depth_hint(DepthComparison(False, 10.0, 200.0), "cat")
    assert "noticeably different distances" in hint
    assert "farther from the camera than" in hint
    assert "far side" in hint
    assert "deserves extra scrutiny" in hint


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


def _stub_sam2_config(mock_transformers: MagicMock) -> None:
    """Make a mocked ``transformers`` return the legacy-shaped config dict
    the published sam2.1-hiera-tiny checkpoint still serves, so
    ``ContactSegmenter._build_config`` exercises its real rewrite path."""
    mock_transformers.Sam2VideoConfig.get_config_dict.return_value = (
        {"model_type": "sam2_video", "memory_attention_rope_theta": 10000},
        {},
    )


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
    _stub_sam2_config(mock_transformers)
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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
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
    _stub_sam2_config(mock_transformers)
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
    _stub_sam2_config(mock_transformers)
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is False


async def test_contact_segmenter_passes_huggingface_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    _stub_sam2_config(mock_transformers)
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
    _stub_sam2_config(mock_transformers)
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    segmenter = ContactSegmenter()

    assert await segmenter.ensure_ready() is False
    assert "Hugging Face authentication failed" in caplog.text


async def test_contact_segmenter_rewrites_legacy_rope_theta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The published checkpoint's config.json still carries the deprecated
    ``memory_attention_rope_theta``; loading it through transformers'
    shim logs a deprecation warning on every start and is slated to stop
    working. _build_config must move that value into ``rope_parameters``
    itself and hand the result to from_pretrained."""
    mock_transformers = MagicMock()
    _stub_sam2_config(mock_transformers)
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    segmenter = ContactSegmenter("hf_test_token")
    assert await segmenter.ensure_ready() is True

    mock_transformers.Sam2VideoConfig.get_config_dict.assert_called_once_with(
        ContactSegmenter._MODEL_ID, token="hf_test_token"
    )
    built = mock_transformers.Sam2VideoConfig.call_args.kwargs
    assert "memory_attention_rope_theta" not in built
    assert built["rope_parameters"] == {"rope_theta": 10000}
    assert (
        mock_transformers.Sam2VideoModel.from_pretrained.call_args.kwargs["config"]
        is mock_transformers.Sam2VideoConfig.return_value
    )


async def test_contact_segmenter_keeps_modern_rope_parameters_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the checkpoint is republished with a modern ``rope_parameters``
    and no legacy key, _build_config must pass it through unchanged rather
    than rewriting anything."""
    mock_transformers = MagicMock()
    mock_transformers.Sam2VideoConfig.get_config_dict.return_value = (
        {"model_type": "sam2_video", "rope_parameters": {"rope_theta": 5000}},
        {},
    )
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is True

    built = mock_transformers.Sam2VideoConfig.call_args.kwargs
    assert built["rope_parameters"] == {"rope_theta": 5000}


async def test_contact_segmenter_modern_rope_parameters_win_over_legacy_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config carrying both keys must keep the modern value — the legacy
    one is only a fallback, never an override."""
    mock_transformers = MagicMock()
    mock_transformers.Sam2VideoConfig.get_config_dict.return_value = (
        {
            "memory_attention_rope_theta": 10000,
            "rope_parameters": {"rope_theta": 5000, "rope_type": "axial"},
        },
        {},
    )
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)

    segmenter = ContactSegmenter()
    assert await segmenter.ensure_ready() is True

    built = mock_transformers.Sam2VideoConfig.call_args.kwargs
    assert built["rope_parameters"] == {"rope_theta": 5000, "rope_type": "axial"}
    assert "memory_attention_rope_theta" not in built


async def test_contact_segmenter_ensure_ready_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_transformers = MagicMock()
    _stub_sam2_config(mock_transformers)
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
    mock_processor.post_process_masks.assert_called_once_with(
        [
            mock_transformers.Sam2VideoModel.from_pretrained.return_value.return_value.pred_masks.cpu.return_value
        ],
        [(10, 10)],
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
    _stub_sam2_config(mock_transformers)
    monkeypatch.setitem(sys.modules, "transformers", mock_transformers)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())
    monkeypatch.setitem(sys.modules, "cv2", MagicMock())

    segmenter = ContactSegmenter()
    result = await segmenter.check_contact(
        _real_jpeg_bytes(), (0, 0, 1, 1), (1, 1, 2, 2)
    )
    assert result is None


def test_build_contact_hint_touching() -> None:
    hint = _build_contact_hint(ContactResult(True, 0.0), "dog")
    assert "touch or overlap" in hint
    assert "dog's" in hint


def test_build_contact_hint_not_touching() -> None:
    hint = _build_contact_hint(ContactResult(False, 21.0), "person")
    assert "21 pixels" in hint
    assert "person's" in hint


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
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
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


# Landmarks MTCNN returns per face: left eye, right eye, nose, mouth corners.
_FRONTAL_POINTS = np.array(
    [[[30.0, 40.0], [70.0, 40.0], [50.0, 55.0], [35.0, 70.0], [65.0, 70.0]]]
)


def _fake_facenet(
    monkeypatch: pytest.MonkeyPatch,
    *,
    boxes: Any = None,
    probabilities: Any = None,
    points: Any = None,
    faces: Any = None,
    embeddings: Any = None,
) -> MagicMock:
    """Install a stand-in facenet_pytorch whose MTCNN finds *boxes*.

    Returns the MTCNN instance so a test can check what it was handed.
    """
    mtcnn = MagicMock()
    mtcnn.detect.return_value = (boxes, probabilities, points)
    mtcnn.extract.return_value = faces
    mock_fp = MagicMock()
    mock_fp.MTCNN.return_value = mtcnn
    mock_fp.InceptionResnetV1.return_value.eval.return_value = MagicMock(
        return_value=embeddings
    )
    monkeypatch.setitem(sys.modules, "facenet_pytorch", mock_fp)
    monkeypatch.setitem(sys.modules, "torch", MagicMock())
    return mtcnn


def _one_face(monkeypatch: pytest.MonkeyPatch, box: list[float]) -> MagicMock:
    return _fake_facenet(
        monkeypatch,
        boxes=np.array([box]),
        probabilities=np.array([0.99]),
        points=_FRONTAL_POINTS,
        faces=_FakeFaceTensor(np.zeros((1, 3, 4, 4)), ndim=4),
        embeddings=_FakeFaceTensor(np.array([[0.1, 0.2, 0.3]])),
    )


def _checkerboard_jpeg(size: tuple[int, int]) -> bytes:
    """A detailed image — the sharpness measure has something to find.

    Squares several pixels wide, so the detail survives the measure's own
    resize to 64px instead of averaging out to flat grey.
    """
    pixels = ((np.indices(size[::-1]) // 6).sum(axis=0) % 2 * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(pixels).convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


async def test_face_embedder_embed_returns_empty_when_no_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_facenet(monkeypatch)
    embedder = FaceEmbedder()
    assert await embedder.embed(_real_jpeg_bytes()) == []


async def test_face_embedder_embed_returns_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_face(monkeypatch, [2.0, 2.0, 8.0, 8.0])
    embedder = FaceEmbedder()
    assert await embedder.embed(_real_jpeg_bytes()) == [[0.1, 0.2, 0.3]]


async def test_face_embedder_detect_matches_mtcnn_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detect() + extract() is what MTCNN.forward() does with keep_all=True;
    analysis relies on it seeing exactly the faces forward() would, so the
    boxes detect() found must be the ones extract() crops."""
    mtcnn = _one_face(monkeypatch, [2.0, 2.0, 8.0, 8.0])
    embedder = FaceEmbedder()
    await embedder.detect(_real_jpeg_bytes())
    assert mtcnn.detect.call_args.kwargs == {"landmarks": True}
    extract_args = mtcnn.extract.call_args.args
    assert np.array_equal(extract_args[1], np.array([[2.0, 2.0, 8.0, 8.0]]))
    assert extract_args[2] is None


async def test_face_embedder_embed_unsqueezes_single_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_facenet(
        monkeypatch,
        boxes=np.array([[2.0, 2.0, 8.0, 8.0]]),
        probabilities=np.array([0.99]),
        points=_FRONTAL_POINTS,
        faces=_FakeFaceTensor(np.zeros((3, 4, 4)), ndim=3),
        embeddings=_FakeFaceTensor(np.array([[0.4, 0.5]])),
    )
    embedder = FaceEmbedder()
    assert await embedder.embed(_real_jpeg_bytes()) == [[0.4, 0.5]]
    resnet_input = embedder._resnet.call_args.args[0]
    assert resnet_input.dim() == 4


async def test_face_embedder_embed_returns_none_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None, not [] — "the models could not run" must not read as "nobody
    was there" to a caller deciding whether everyone in a clip is known."""
    frame = _real_jpeg_bytes()
    monkeypatch.delitem(sys.modules, "facenet_pytorch", raising=False)
    with patch("builtins.__import__", side_effect=ImportError):
        embedder = FaceEmbedder()
        assert await embedder.embed(frame) is None


async def test_face_embedder_embed_returns_none_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mtcnn = _fake_facenet(monkeypatch)
    mtcnn.detect.side_effect = RuntimeError("boom")
    embedder = FaceEmbedder()
    assert await embedder.embed(_real_jpeg_bytes()) is None


async def test_face_embedder_detect_returns_none_for_undecodable_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_facenet(monkeypatch)
    embedder = FaceEmbedder()
    assert await embedder.detect(b"not an image") is None


async def test_face_embedder_honours_exif_orientation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone stores a portrait photo as landscape pixels plus an EXIF
    "rotate 90°" tag. Measured with real models: ignoring the tag either
    found no face, or enrolled one whose embedding matched the same person
    upright at 0.07 — a useless reference. MTCNN must see it upright."""
    mtcnn = _fake_facenet(monkeypatch)
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated; turn 90° clockwise to display
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), (128, 128, 128)).save(buf, format="JPEG", exif=exif)

    await FaceEmbedder().detect(buf.getvalue())

    assert mtcnn.detect.call_args.args[0].size == (20, 40)


async def test_face_embedder_shrinks_a_large_photo_before_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mtcnn = _fake_facenet(monkeypatch)
    await FaceEmbedder().detect(_real_jpeg_bytes((3000, 1500)))
    assert mtcnn.detect.call_args.args[0].size == (1280, 640)


async def test_face_embedder_leaves_an_analysis_frame_at_full_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mtcnn = _fake_facenet(monkeypatch)
    await FaceEmbedder().detect(_real_jpeg_bytes((640, 360)))
    assert mtcnn.detect.call_args.args[0].size == (640, 360)


async def test_face_embedder_detect_crops_a_thumbnail_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_face(monkeypatch, [50.0, 60.0, 150.0, 180.0])
    embedder = FaceEmbedder()
    frame = _real_jpeg_bytes((300, 300))

    without = await embedder.detect(frame)
    assert without is not None
    assert without[0].thumbnail == b""

    faces = await embedder.detect(frame, thumbnails=True)
    assert faces is not None
    thumbnail = Image.open(io.BytesIO(faces[0].thumbnail))
    assert thumbnail.format == "JPEG"
    assert max(thumbnail.size) <= 112
    assert faces[0].width == 100
    assert faces[0].probability == pytest.approx(0.99)


async def test_face_embedder_thumbnail_stays_inside_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A face at the edge of the frame gets its margin clipped, not padded
    with pixels from nowhere."""
    _one_face(monkeypatch, [0.0, 0.0, 40.0, 50.0])
    faces = await FaceEmbedder().detect(_real_jpeg_bytes((300, 300)), thumbnails=True)
    assert faces is not None
    width, height = Image.open(io.BytesIO(faces[0].thumbnail)).size
    assert width < height  # the crop lost its left margin, not its top one


async def test_face_quality_prefers_big_sharp_faces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _checkerboard_jpeg((300, 300))

    _one_face(monkeypatch, [20.0, 20.0, 120.0, 140.0])
    big = await FaceEmbedder().detect(frame)
    _one_face(monkeypatch, [20.0, 20.0, 30.0, 32.0])
    tiny = await FaceEmbedder().detect(frame)
    _one_face(monkeypatch, [20.0, 20.0, 120.0, 140.0])
    blurry = await FaceEmbedder().detect(_real_jpeg_bytes((300, 300)))

    assert big is not None and tiny is not None and blurry is not None
    assert big[0].quality > 0.9
    # Under 20px there is too little face to be worth enrolling at all.
    assert tiny[0].quality == 0.0
    assert blurry[0].quality < big[0].quality


def test_frontality_falls_as_the_head_turns() -> None:
    frontal = _frontality(_FRONTAL_POINTS[0])
    turned = _frontality(np.array([[30, 40], [70, 40], [65, 55], [35, 70], [65, 70]]))
    profile = _frontality(np.array([[30, 40], [70, 40], [70, 55], [35, 70], [65, 70]]))
    assert frontal == pytest.approx(1.0)
    assert 0.0 < turned < frontal
    assert profile == 0.0


def test_frontality_without_usable_landmarks_is_zero() -> None:
    assert _frontality(None) == 0.0
    assert _frontality(np.array([[50, 40], [50, 40], [50, 55]])) == 0.0


def test_sharpness_of_a_degenerate_box_is_zero() -> None:
    image = Image.new("RGB", (50, 50))
    assert _sharpness(image, (10.0, 10.0, 11.0, 30.0)) == 0.0


def test_sharpness_separates_detail_from_a_flat_patch() -> None:
    flat = Image.new("RGB", (64, 64), (100, 100, 100))
    detailed = Image.open(io.BytesIO(_checkerboard_jpeg((64, 64))))
    box = (0.0, 0.0, 64.0, 64.0)
    assert _sharpness(flat, box) == 0.0
    assert _sharpness(detailed, box) > 150.0


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


async def test_face_recognizer_frame_that_cannot_be_examined_blocks_the_bypass(
    db: ClipDatabase,
) -> None:
    """A frame detection could not run on might have held anyone. It used to
    be skipped as if empty, so an approved match in the *other* frames made
    the clip look fully accounted for — and eligible to have its suspicious
    flag cleared — when it had not been fully checked."""
    await db.add_face_enrollment("Brian", [1.0, 0.0, 0.0], approved=True)

    async def _embed(frame: bytes) -> list[list[float]] | None:
        return [[1.0, 0.0, 0.0]] if frame == b"frame-brian" else None

    embedder = MagicMock(spec=FaceEmbedder)
    embedder.embed = _embed

    result = await FaceRecognizer(embedder, db).recognize(
        [b"frame-brian", b"frame-unreadable"]
    )
    assert result.approved_names == ["Brian"]
    assert result.unrecognized_present is True


def test_match_enrollment_reports_name_and_similarity() -> None:
    enrollments = [
        {"name": "Amy", "embedding": [0.0, 1.0], "approved": True},
        {"name": "Brian", "embedding": [1.0, 0.0], "approved": False},
    ]
    match = match_enrollment([0.99, 0.05], enrollments)
    assert match is not None
    assert match[0] == "Brian"
    assert match[1] == pytest.approx(cosine_similarity([0.99, 0.05], [1.0, 0.0]))


def test_match_enrollment_below_threshold_is_none() -> None:
    enrollments = [{"name": "Brian", "embedding": [1.0, 0.0]}]
    assert match_enrollment([0.6, 0.8], enrollments) is None


def test_match_enrollment_breaks_ties_the_way_recognition_does() -> None:
    """The picker's "already recognized as" must name whoever analysis
    would: on an exact tie, that is the later enrollment."""
    enrollments = [
        {"name": "First", "embedding": [1.0, 0.0]},
        {"name": "Second", "embedding": [1.0, 0.0]},
    ]
    match = match_enrollment([1.0, 0.0], enrollments)
    assert match is not None
    assert match[0] == "Second"


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
    assert hints.detections is None


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
        assert hints.detections is None


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
    _stub_sam2_config(mock_transformers)
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
    assert hints.detections is not None
    assert sorted(d.label for d in hints.detections) == ["car", "person"]


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


async def test_vision_pipeline_car_zone_disambiguates_protected_vehicle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two vehicle-class detections in frame (the protected car, and an
    unrelated car much closer to the person) - car_zone must steer the
    hint toward the vehicle actually near the configured zone, not
    whichever one the person happens to be standing next to."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((200, 200, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[0, 2, 2],
        conf=[0.9, 0.9, 0.9],
        xyxy=[
            (0.0, 0.0, 2.0, 2.0),  # person
            (2.0, 2.0, 4.0, 4.0),  # unrelated car, touching the person
            (100.0, 100.0, 110.0, 110.0),  # the actual protected car, near the zone
        ],
        ids=None,
    )
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {0: "person", 2: "car"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    # Depth/contact are irrelevant to this hint and unavailable either way;
    # keeps this test focused on the OBJECT DETECTION hint's own wording.
    monkeypatch.setitem(sys.modules, "transformers", None)

    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        car_zone={
            "shape": "rect",
            "x_min": 0.475,
            "y_min": 0.475,
            "x_max": 0.575,
            "y_max": 0.575,
        },
    )
    assert hints.detection_hint is not None
    assert "well away from the detected vehicle" in hints.detection_hint


async def test_vision_pipeline_without_car_zone_prefers_the_largest_vehicle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ambiguous scene as the zone-disambiguation test above, with no
    car_zone configured. The old fallback was "whichever vehicle the person
    is standing next to", which is precisely how a neighbour at their own
    car became activity at the protected one. With nothing to disambiguate
    on, the largest vehicle is assumed instead — the protected car is
    normally the one parked closest to its own camera — and the prompt says
    outright that the choice is not confident."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((200, 200, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(
        cls=[0, 2, 2],
        conf=[0.9, 0.9, 0.9],
        xyxy=[
            (0.0, 0.0, 2.0, 2.0),
            (2.0, 2.0, 4.0, 4.0),
            (100.0, 100.0, 110.0, 110.0),
        ],
        ids=None,
    )
    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, {0: "person", 2: "car"})]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    monkeypatch.setitem(sys.modules, "transformers", None)

    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
    )
    assert hints.detection_hint is not None
    assert "well away from the detected vehicle" in hints.detection_hint
    assert hints.asset is not None
    assert hints.asset.confident is False
    assert hints.asset.identification is not None
    assert "draw a zone" in hints.asset.identification.basis


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


async def test_vision_pipeline_hints_an_empty_sweep_when_detector_found_nothing() -> (
    None
):
    """A detector that ran and returned zero boxes must still say so."""
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    with patch.object(ObjectDetector, "detect", return_value=[]):
        hints = await pipeline.process_clip([_real_jpeg_bytes()])
    assert hints.detection_hint is not None
    assert "No person and no animal was detected" in hints.detection_hint


async def test_vision_pipeline_stays_silent_when_the_detector_never_ran() -> None:
    """The safety-relevant half of the distinction above: an unavailable
    detector returns None, not an empty list, and must never produce
    "nobody was here" about frames nothing ever looked at."""
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    with patch.object(ObjectDetector, "detect", return_value=None):
        hints = await pipeline.process_clip([_real_jpeg_bytes()])
    assert hints.detection_hint is None
    assert SOURCE_OBJECT_DETECTION in hints.unavailable_sources


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
    pre-down-selection extraction pool from analyzer/base.py), face recognition
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
        hints = await pipeline.process_clip(prompt_frames, raw_frames=wider_pool)

    assert seen_frames == wider_pool
    assert hints.face_recognition is not None
    assert hints.face_recognition.approved_names == ["Brian"]


async def _recognized_frames(
    db: ClipDatabase,
    width: int,
    extracted: list[bytes],
    clip_path: str = "/clips/a.mp4",
) -> tuple[list[bytes], MagicMock]:
    """Run the face stage at *width*; return the frames it matched in and
    the frame-extraction mock."""
    await db.add_face_enrollment("Brian", [1.0, 0.0])
    seen: list[bytes] = []

    async def _embed(frame: bytes) -> list[list[float]]:
        seen.append(frame)
        return []

    pipeline = VisionPipeline(
        VisionConfig(face_recognition_enabled=True, face_frame_width=width), db=db
    )
    extract = MagicMock(side_effect=lambda *a, **k: _async_result(extracted))
    with (
        patch.object(FaceEmbedder, "embed", side_effect=_embed),
        patch("blink_downloader.vision.pipeline.extract_jpeg_frames", extract),
    ):
        await pipeline.process_clip(
            [b"prompt"],
            raw_frames=[b"pool-1", b"pool-2", b"pool-3"],
            clip_path=clip_path,
            frame_interval=1.5,
        )
    return seen, extract


async def test_face_stage_at_standard_resolution_reuses_the_analysis_frames(
    db: ClipDatabase,
) -> None:
    seen, extract = await _recognized_frames(db, 640, [b"never"])
    assert seen == [b"pool-1", b"pool-2", b"pool-3"]
    extract.assert_not_called()


async def test_face_stage_at_higher_resolution_re_extracts_the_same_moments(
    db: ClipDatabase,
) -> None:
    """Same interval and count as the analysis pool, so the same fps
    sampling lands on the same timestamps — only bigger."""
    seen, extract = await _recognized_frames(db, 1280, [b"wide-1", b"wide-2"])
    assert seen == [b"wide-1", b"wide-2"]
    assert extract.call_args.args == ("/clips/a.mp4",)
    assert extract.call_args.kwargs == {
        "width": 1280,
        "interval": 1.5,
        "count": 3,
        "label": "/clips/a.mp4",
    }


async def test_face_stage_falls_back_to_the_analysis_frames(db: ClipDatabase) -> None:
    """A smaller face than the enrollments can only match less often, never
    as someone else — so a failed extraction still recognizes, safely."""
    seen, _ = await _recognized_frames(db, 1280, [])
    assert seen == [b"pool-1", b"pool-2", b"pool-3"]
    seen_without_path, extract = await _recognized_frames(
        db, 1280, [b"x"], clip_path=""
    )
    assert seen_without_path == [b"pool-1", b"pool-2", b"pool-3"]
    extract.assert_not_called()


async def test_vision_pipeline_face_recognition_without_db_is_noop() -> None:
    pipeline = VisionPipeline(VisionConfig(face_recognition_enabled=True), db=None)
    hints = await pipeline.process_clip([b"frame"])
    assert hints.recognized_resident_hint is None
    assert hints.face_recognition is None


def test_vision_pipeline_passes_depth_model_from_config() -> None:
    pipeline = VisionPipeline(
        VisionConfig(depth_estimation_model="depth-anything/Depth-Anything-V2-Base-hf")
    )
    assert pipeline._depth._model_id == "depth-anything/Depth-Anything-V2-Base-hf"


def test_huggingface_auth_error_status_code_is_detected() -> None:
    class ForbiddenError(RuntimeError):
        status_code = 403

    error = ForbiddenError("request rejected")

    assert _is_huggingface_auth_error(error) is True


# ----------------------------------------------------------------------
# CV concurrency limiting
# ----------------------------------------------------------------------


@pytest.fixture
def cv_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore both concurrency globals after a test touches either.

    ``configure_cv_concurrency`` writes ``_cv_limit`` and invalidates
    ``_cv_semaphore``, so putting only the limit back leaves the next test
    with a semaphore built at the wrong one. monkeypatch restores both,
    including when the test fails part-way.
    """
    monkeypatch.setattr(runtime_module, "_cv_limit", runtime_module._cv_limit)
    monkeypatch.setattr(runtime_module, "_cv_semaphore", None)


def test_configure_cv_concurrency_replaces_the_semaphore(cv_concurrency: None) -> None:
    runtime_module.configure_cv_concurrency(4)
    first = runtime_module._cv_slot()
    assert runtime_module._cv_limit == 4
    # Same limit again must not throw away a semaphore stages are using.
    runtime_module.configure_cv_concurrency(4)
    assert runtime_module._cv_slot() is first
    runtime_module.configure_cv_concurrency(2)
    assert runtime_module._cv_slot() is not first


def test_configure_cv_concurrency_floors_at_one(cv_concurrency: None) -> None:
    """Zero would deadlock every stage rather than disabling them, which is
    what the per-stage toggles are for."""
    runtime_module.configure_cv_concurrency(0)
    assert runtime_module._cv_limit == 1


async def test_heavy_stages_do_not_run_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    cv_concurrency: None,
) -> None:
    """Two clips analyzed at once must not have two torch models computing
    simultaneously — on a Raspberry Pi that is the difference between slow
    and wedged."""
    runtime_module.configure_cv_concurrency(1)
    in_flight = 0
    peak = 0

    def _slow_detect(_frames: list[bytes]) -> list[DetectedObject]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        time.sleep(0.02)
        in_flight -= 1
        return []

    detector = ObjectDetector()
    detector._model = MagicMock()
    monkeypatch.setattr(detector, "_detect_sync", _slow_detect)

    await asyncio.gather(*(detector.detect([b"frame"]) for _ in range(4)))
    assert peak == 1


# ----------------------------------------------------------------------
# temporal scan frame selection
# ----------------------------------------------------------------------


def test_select_scan_frames_keeps_everything_under_the_cap() -> None:
    frames = [b"a", b"b", b"c"]
    assert _select_scan_frames(frames, 12, 2.0) == (frames, 2.0)


def test_select_scan_frames_disabled_cap_keeps_everything() -> None:
    frames = [b"a", b"b", b"c"]
    assert _select_scan_frames(frames, 0, 2.0) == (frames, 2.0)


def test_select_scan_frames_thins_evenly_and_reports_the_real_interval() -> None:
    """The interval has to grow with the stride or every duration, speed and
    trajectory computed from these frames is wrong."""
    frames = [bytes([i]) for i in range(30)]
    selected, interval = _select_scan_frames(frames, 10, 2.0)
    assert len(selected) == 10
    assert selected[0] == frames[0]
    assert selected[1] == frames[3]
    assert interval == pytest.approx(6.0)


# ----------------------------------------------------------------------
# colour fingerprints and asset appearance change
# ----------------------------------------------------------------------


def _solid_jpeg(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _noisy_jpeg(seed: int, size: tuple[int, int] = (64, 64)) -> bytes:
    rng = np.random.default_rng(seed)
    array = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@pytest.mark.usefixtures("real_cv2")
def test_vehicle_histogram_separates_two_colours() -> None:
    red = _vehicle_histogram(_solid_jpeg((200, 20, 20)), (0, 0, 64, 64))
    blue = _vehicle_histogram(_solid_jpeg((20, 20, 200)), (0, 0, 64, 64))
    assert len(red) == 64
    assert sum(red) == pytest.approx(1.0)
    assert sum(r * b for r, b in zip(red, blue)) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.usefixtures("real_cv2")
def test_vehicle_histogram_clamps_a_box_outside_the_frame() -> None:
    assert _vehicle_histogram(_solid_jpeg((10, 200, 10)), (500, 500, 900, 900))


@pytest.mark.usefixtures("real_cv2")
def test_vehicle_histogram_of_an_undecodable_frame_is_empty() -> None:
    assert _vehicle_histogram(b"not a jpeg", (0, 0, 10, 10)) == ()


@pytest.mark.usefixtures("real_cv2")
def test_appearance_change_is_near_zero_for_an_unchanged_region() -> None:
    frame = _noisy_jpeg(1)
    assert _region_appearance_change(frame, frame, (0, 0, 64, 64)) == pytest.approx(
        0.0, abs=0.01
    )


@pytest.mark.usefixtures("real_cv2")
def test_appearance_change_ignores_a_uniform_lighting_shift() -> None:
    """A cloud passing or a floodlight switching on must not read as damage
    to the vehicle."""
    rng = np.random.default_rng(7)
    base = rng.integers(20, 120, (64, 64, 3), dtype=np.uint8)
    brighter = np.clip(base.astype("int16") + 90, 0, 255).astype(np.uint8)

    def _encode(array: np.ndarray) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(array).save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    change = _region_appearance_change(_encode(base), _encode(brighter), (0, 0, 64, 64))
    assert change is not None
    assert change < 0.15


@pytest.mark.usefixtures("real_cv2")
def test_appearance_change_is_large_for_a_structurally_different_region() -> None:
    change = _region_appearance_change(_noisy_jpeg(1), _noisy_jpeg(2), (0, 0, 64, 64))
    assert change is not None
    assert change > 0.25


@pytest.mark.usefixtures("real_cv2")
def test_appearance_change_of_a_featureless_region_is_zero_not_nan() -> None:
    """A crop with no variance would divide by zero when normalized, and a
    NaN reaching the impact rule would make every threshold test silently
    false. It must read as "nothing changed" instead."""
    flat = _solid_jpeg((128, 128, 128))
    assert _region_appearance_change(flat, flat, (0, 0, 64, 64)) == 0.0


@pytest.mark.usefixtures("real_cv2")
def test_appearance_change_of_an_undecodable_frame_is_unavailable() -> None:
    assert _region_appearance_change(b"junk", _noisy_jpeg(1), (0, 0, 64, 64)) is None


def test_appearance_change_rejects_an_unexpected_resize_result(
    real_cv2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        real_cv2,
        "resize",
        lambda _img, _size: np.zeros((4, 4), dtype=np.uint8),
        raising=False,
    )
    assert (
        _region_appearance_change(_noisy_jpeg(1), _noisy_jpeg(2), (0, 0, 64, 64))
        is None
    )


def test_appearance_change_survives_a_failure_in_the_resize(
    real_cv2: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every stage in this module reports a failure as missing evidence
    rather than an error, and this is the least important of them."""
    monkeypatch.setattr(
        real_cv2, "resize", MagicMock(side_effect=RuntimeError("boom")), raising=False
    )
    assert (
        _region_appearance_change(_noisy_jpeg(1), _noisy_jpeg(2), (0, 0, 64, 64))
        is None
    )


@pytest.mark.usefixtures("real_cv2")
def test_crop_region_clamps_a_box_that_runs_off_the_frame() -> None:
    img = np.zeros((20, 30, 3), dtype=np.uint8)
    crop = _crop_region(img, (-50.0, -50.0, 500.0, 500.0))
    assert crop.shape[:2] == (20, 30)


@pytest.mark.usefixtures("real_cv2")
def test_crop_region_of_a_degenerate_box_is_never_empty() -> None:
    img = np.zeros((20, 30, 3), dtype=np.uint8)
    assert _crop_region(img, (10.0, 10.0, 10.0, 10.0)).size > 0


def test_detection_distance_pair_needs_a_subject() -> None:
    detections = [DetectedObject("car", 0.9, (0.0, 0.0, 10.0, 10.0), 1, 0)]
    assert (
        _detection_distance_pair(detections, None, (0.0, 0.0, 10.0, 10.0), "Silver Kia")
        is None
    )


# ----------------------------------------------------------------------
# security-layer integration
# ----------------------------------------------------------------------


def _yolo_env(
    monkeypatch: pytest.MonkeyPatch,
    boxes: _FakeBoxes,
    names: dict[int, str],
    *,
    frame: tuple[int, int] = (200, 200),
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((frame[1], frame[0], 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    fake_model = MagicMock()
    fake_model.track.return_value = [_FakeYoloResult(boxes, names)]
    mock_ultra = MagicMock()
    mock_ultra.YOLO.return_value = fake_model
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    monkeypatch.setitem(sys.modules, "transformers", None)


async def test_pipeline_builds_tracks_over_the_temporal_scan_not_the_prompt_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan's own interval is what track timings must be based on —
    the prompt frames are motion-selected and deliberately uneven."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0],
            conf=[0.9],
            xyxy=[(10.0, 10.0, 30.0, 90.0)],
            ids=[1],
        ),
        {0: "person"},
    )
    pipeline = VisionPipeline(
        VisionConfig(enhanced_detection_enabled=True, temporal_scan_frames=3)
    )
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        raw_frames=[_real_jpeg_bytes(size=(200, 200))] * 9,
        frame_interval=2.0,
    )
    assert hints.scan_frame_count == 3
    assert hints.scan_interval == pytest.approx(6.0)
    assert hints.tracks is not None
    assert hints.tracks[0].dwell_seconds == pytest.approx(12.0)
    assert hints.frame_size == (200.0, 200.0)


async def test_pipeline_zero_scan_cap_detects_over_the_prompt_frames_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ai_temporal_scan_frames: 0`` is documented as disabling the wider
    scan. It used to hand the detector the *entire* raw extraction instead,
    making the one value a user picks to spend less the most expensive
    setting available."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0],
            conf=[0.9],
            xyxy=[(10.0, 10.0, 30.0, 90.0)],
            ids=[1],
        ),
        {0: "person"},
    )
    pipeline = VisionPipeline(
        VisionConfig(enhanced_detection_enabled=True, temporal_scan_frames=0)
    )
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))] * 2,
        raw_frames=[_real_jpeg_bytes(size=(200, 200))] * 30,
        frame_interval=2.0,
    )
    assert hints.scan_frame_count == 2


async def test_pipeline_reports_only_the_dependent_stages_when_nothing_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty detection list means the detector ran and saw nothing —
    unlike None, which means it could not run at all, so only that case
    reports object detection itself as unavailable. The stages that work
    from its boxes then have nothing to measure, which is reported as not
    applicable rather than as missing evidence."""
    _yolo_env(monkeypatch, _FakeBoxes(cls=[], conf=[], xyxy=[], ids=[]), {})
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip([_real_jpeg_bytes(size=(200, 200))])

    assert hints.detections == []
    assert SOURCE_OBJECT_DETECTION not in hints.unavailable_sources
    assert SOURCE_DEPTH_ESTIMATION not in hints.unavailable_sources
    assert SOURCE_DEPTH_ESTIMATION in hints.not_applicable_sources


async def test_pipeline_skips_the_security_layer_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[0], conf=[0.9], xyxy=[(10.0, 10.0, 30.0, 90.0)], ids=[1]),
        {0: "person"},
    )
    pipeline = VisionPipeline(
        VisionConfig(enhanced_detection_enabled=True, security_events_enabled=False)
    )
    hints = await pipeline.process_clip([_real_jpeg_bytes(size=(200, 200))])
    assert hints.detections
    assert hints.tracks is None
    assert hints.asset is None


async def test_pipeline_records_unavailable_sources_when_detection_is_off() -> None:
    pipeline = VisionPipeline(VisionConfig())
    hints = await pipeline.process_clip([b"frame"])
    assert hints.unavailable_sources == [
        "object detection",
        "depth estimation",
        "contact segmentation",
        "pose estimation",
        "face recognition",
    ]


async def test_pipeline_stops_cleanly_when_the_frame_cannot_be_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[0], conf=[0.9], xyxy=[(10.0, 10.0, 30.0, 90.0)], ids=[1]),
        {0: "person"},
    )
    monkeypatch.setattr(imaging_module, "_frame_dimensions", lambda _frame: None)
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip([_real_jpeg_bytes(size=(200, 200))])
    assert hints.detections
    assert hints.frame_size is None
    assert hints.tracks is None
    # The detection and tracking hints need no frame size, so losing the
    # geometry must not cost the prompt those too.
    assert hints.detection_hint is not None


async def test_pipeline_learns_a_vehicle_signature_from_a_confident_sighting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[2], conf=[0.95], xyxy=[(20.0, 20.0, 180.0, 140.0)], ids=[2]),
        {2: "car"},
    )
    monkeypatch.setattr(imaging_module, "_vehicle_histogram", lambda _f, _b: (1.0, 0.0))
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        car_zone={"x_min": 0.1, "y_min": 0.1, "x_max": 0.9, "y_max": 0.7},
        camera="Driveway",
    )
    assert hints.asset is not None
    assert hints.asset.confident is True
    assert hints.vehicle_signature_update is not None
    assert hints.vehicle_signature_update.sample_count == 1
    assert hints.vehicle_signature_update.histogram == (1.0, 0.0)


def test_resolve_asset_skips_a_fingerprint_it_has_no_frame_for() -> None:
    """A sighting index that doesn't address the frames in hand is skipped
    rather than indexed off the end -- the vehicle simply contributes no
    appearance evidence, which is how every other missing source behaves."""
    frame_size = (200.0, 200.0)
    tracks = build_tracks(
        [
            # Frame 9 is past the end of the two-frame list below.
            ("car", 0.95, (40.0, 40.0, 120.0, 100.0), 7, 9),
            ("car", 0.90, (44.0, 42.0, 124.0, 102.0), 7, 1),
        ],
        2.0,
        frame_size,
    )
    hints = vision_module.VisionHints()
    hints.tracks = tracks
    hints.frame_size = frame_size
    signature = VehicleSignature(
        box=(0.2, 0.2, 0.6, 0.5), histogram=(0.4, 0.6), sample_count=5
    )

    calls: list[bytes] = []

    def _record(frame: bytes, _box: Box) -> tuple[float, ...]:
        calls.append(frame)
        return (1.0, 0.0)

    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    with patch.object(imaging_module, "_vehicle_histogram", _record):
        asset = pipeline._resolve_asset(
            hints, [b"frame-0", b"frame-1"], "Driveway", "Silver Kia", None, signature
        )

    assert asset is not None
    # Cropping scan_frames[9] would have raised. The only crop that happens
    # is _learn_signature's own fallback to the first frame, which is the
    # same thing it does for any candidate it cannot place precisely.
    assert calls == [b"frame-0"]


def test_learn_signature_crops_the_frame_the_car_was_actually_seen_in() -> None:
    """The learned colour fingerprint used to be cropped out of frame 0 with
    the track's *median* box -- so a car that pulls in halfway through a clip
    taught the signature whatever was parked there before it, or nothing at
    all. It must use the sighting the detector was surest of, which is the
    same frame/box the fingerprints it gets compared against come from."""
    detections = [
        # Empty driveway for the first two frames; the car arrives at 2.
        ("car", 0.90, (40.0, 40.0, 120.0, 100.0), 7, 2),
        ("car", 0.97, (44.0, 42.0, 124.0, 102.0), 7, 3),
    ]
    frame_size = (200.0, 200.0)
    tracks = build_tracks(detections, 2.0, frame_size)
    zone = Zone.from_config({"x_min": 0.2, "y_min": 0.2, "x_max": 0.62, "y_max": 0.51})
    assert zone is not None
    identification = identify_protected_vehicle(tracks, frame_size, zone=zone)
    assert identification.protected is not None
    assert identification.confident

    hints = vision_module.VisionHints()
    hints.tracks = tracks
    hints.frame_size = frame_size
    asset = ProtectedAsset(
        name="Silver Kia",
        asset_type=AssetType.VEHICLE,
        camera="Driveway",
        box=identification.protected.box,
        location=AssetLocation.DETECTED,
        identification=identification,
    )

    seen: list[tuple[bytes, Box]] = []

    def _record(frame: bytes, box: Box) -> tuple[float, ...]:
        seen.append((frame, box))
        return (1.0,)

    with patch.object(imaging_module, "_vehicle_histogram", _record):
        result = VisionPipeline._learn_signature(
            asset, hints, [b"frame-0", b"frame-1", b"frame-2", b"frame-3"], None
        )

    assert result is not None
    assert seen == [(b"frame-3", (44.0, 42.0, 124.0, 102.0))]


@pytest.mark.parametrize(
    ("drop_sample", "scan_frames"),
    [
        (False, [b"frame-0"]),
        (True, [b"frame-0", b"frame-1", b"frame-2", b"frame-3"]),
    ],
    ids=["index-past-the-end", "candidate-without-a-sighting"],
)
def test_learn_signature_falls_back_to_the_median_box_when_it_cannot_do_better(
    drop_sample: bool, scan_frames: list[bytes]
) -> None:
    """Both ways the preferred sighting can be unusable end at the same
    safe place: the first frame and the track's median box, which is what
    this did before it knew any better."""
    detections = [
        ("car", 0.90, (40.0, 40.0, 120.0, 100.0), 7, 2),
        ("car", 0.97, (44.0, 42.0, 124.0, 102.0), 7, 3),
    ]
    frame_size = (200.0, 200.0)
    tracks = build_tracks(detections, 2.0, frame_size)
    zone = Zone.from_config({"x_min": 0.2, "y_min": 0.2, "x_max": 0.62, "y_max": 0.51})
    assert zone is not None
    identification = identify_protected_vehicle(tracks, frame_size, zone=zone)
    assert identification.protected is not None
    if drop_sample:
        identification.protected.sample = None
    median_box = identification.protected.box

    hints = vision_module.VisionHints()
    hints.tracks = tracks
    hints.frame_size = frame_size
    asset = ProtectedAsset(
        name="Silver Kia",
        asset_type=AssetType.VEHICLE,
        camera="Driveway",
        box=median_box,
        location=AssetLocation.DETECTED,
        identification=identification,
    )

    seen: list[tuple[bytes, Box]] = []

    def _record(frame: bytes, box: Box) -> tuple[float, ...]:
        seen.append((frame, box))
        return (1.0,)

    with patch.object(imaging_module, "_vehicle_histogram", _record):
        assert VisionPipeline._learn_signature(asset, hints, scan_frames, None)

    assert seen == [(b"frame-0", median_box)]


async def test_pipeline_blends_into_an_existing_vehicle_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[2], conf=[0.95], xyxy=[(20.0, 20.0, 180.0, 140.0)], ids=[2]),
        {2: "car"},
    )
    monkeypatch.setattr(imaging_module, "_vehicle_histogram", lambda _f, _b: (1.0, 0.0))
    existing = VehicleSignature(
        box=(0.1, 0.1, 0.9, 0.7), histogram=(0.0, 1.0), sample_count=9
    )
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        car_zone={"x_min": 0.1, "y_min": 0.1, "x_max": 0.9, "y_max": 0.7},
        camera="Driveway",
        vehicle_signature=existing,
    )
    assert hints.vehicle_signature_update is not None
    assert hints.vehicle_signature_update.sample_count == 10


async def test_pipeline_does_not_learn_from_an_unconfident_identification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Learning from a guess is how a signature drifts onto the neighbour's
    car and stays there."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[2, 2],
            conf=[0.9, 0.9],
            xyxy=[(10.0, 10.0, 90.0, 90.0), (100.0, 100.0, 190.0, 190.0)],
            ids=[2, 3],
        ),
        {2: "car"},
    )
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Driveway",
    )
    assert hints.asset is not None
    assert hints.asset.confident is False
    assert hints.vehicle_signature_update is None


async def test_pipeline_compares_candidate_colours_against_a_learned_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[2, 2],
            conf=[0.9, 0.95],
            xyxy=[(10.0, 10.0, 90.0, 90.0), (100.0, 100.0, 190.0, 190.0)],
            ids=[2, 3],
        ),
        {2: "car"},
    )
    fingerprints = {
        (10.0, 10.0, 90.0, 90.0): (1.0, 0.0),
        (100.0, 100.0, 190.0, 190.0): (0.0, 1.0),
    }
    monkeypatch.setattr(
        imaging_module,
        "_vehicle_histogram",
        lambda _frame, box: fingerprints.get(tuple(box), (0.5, 0.5)),
    )
    signature = VehicleSignature(
        box=(0.05, 0.05, 0.45, 0.45), histogram=(1.0, 0.0), sample_count=8
    )
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Driveway",
        vehicle_signature=signature,
    )
    assert hints.asset is not None
    assert hints.asset.identification is not None
    protected = hints.asset.identification.protected
    assert protected is not None
    assert protected.track_id == 2
    assert protected.appearance_similarity == pytest.approx(1.0)


async def test_pipeline_skips_colour_matching_for_non_vehicle_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only vehicles are candidates for "which car is yours"; fingerprinting
    a person would be wasted decode work at best."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0, 2],
            conf=[0.9, 0.95],
            xyxy=[(5.0, 5.0, 25.0, 95.0), (20.0, 20.0, 180.0, 140.0)],
            ids=[1, 2],
        ),
        {0: "person", 2: "car"},
    )
    fingerprinted: list[tuple[float, ...]] = []

    def _histogram(_frame: bytes, box: tuple[float, ...]) -> tuple[float, ...]:
        fingerprinted.append(tuple(box))
        return (1.0, 0.0)

    monkeypatch.setattr(imaging_module, "_vehicle_histogram", _histogram)
    signature = VehicleSignature(
        box=(0.1, 0.1, 0.9, 0.7), histogram=(1.0, 0.0), sample_count=8
    )
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Driveway",
        vehicle_signature=signature,
    )
    assert (5.0, 5.0, 25.0, 95.0) not in fingerprinted


async def test_pipeline_pair_stages_need_a_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A car with nobody near it leaves depth/contact/pose nothing to
    measure. That is not missing evidence, and must not be reported — or
    scored — as though the stages had failed: it is the ordinary shape of
    most clips, and counting it docked nearly all of them."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[2], conf=[0.95], xyxy=[(20.0, 20.0, 180.0, 140.0)], ids=[2]),
        {2: "car"},
    )
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Driveway",
    )
    assert hints.contact_track_id is None
    assert hints.unavailable_sources == [SOURCE_FACE_RECOGNITION]
    assert hints.not_applicable_sources == [
        SOURCE_DEPTH_ESTIMATION,
        SOURCE_CONTACT_SEGMENTATION,
        SOURCE_POSE_ESTIMATION,
    ]


# ----------------------------------------------------------------------
# Pose estimation
# ----------------------------------------------------------------------


class _FakeKeypoints:
    def __init__(self, data) -> None:
        self.data = data


class _FakePoseResult:
    def __init__(self, boxes, keypoints) -> None:
        self.boxes = boxes
        self.keypoints = keypoints


def _skeleton(**joints: tuple[float, float, float]) -> list[tuple[float, float, float]]:
    """A 17-keypoint COCO skeleton with everything unplaced but *joints*."""
    points = [(0.0, 0.0, 0.0)] * 17
    names = {
        "left_shoulder": 5,
        "right_shoulder": 6,
        "left_wrist": 9,
        "right_wrist": 10,
        "left_hip": 11,
        "right_hip": 12,
        "left_ankle": 15,
        "right_ankle": 16,
    }
    for name, value in joints.items():
        points[names[name]] = value
    return points


#: A person 200px tall standing to the left of a car, arms down.
_STANDING = _skeleton(
    left_shoulder=(100.0, 120.0, 0.9),
    right_shoulder=(140.0, 120.0, 0.9),
    left_wrist=(98.0, 200.0, 0.9),
    right_wrist=(142.0, 200.0, 0.9),
    left_hip=(105.0, 210.0, 0.9),
    right_hip=(135.0, 210.0, 0.9),
    left_ankle=(105.0, 300.0, 0.9),
    right_ankle=(135.0, 300.0, 0.9),
)
_SUBJECT_BOX = (90.0, 100.0, 150.0, 300.0)
_ASSET_BOX = (200.0, 140.0, 400.0, 280.0)


def test_posture_from_keypoints_standing_still_establishes_nothing() -> None:
    result = _posture_from_keypoints(_STANDING, _SUBJECT_BOX, _ASSET_BOX)
    assert result.any_posture is False
    assert result.describe() == ""


def test_posture_detects_an_arm_extended_toward_the_asset() -> None:
    reaching = _skeleton(
        left_shoulder=(100.0, 120.0, 0.9),
        right_shoulder=(140.0, 120.0, 0.9),
        right_wrist=(200.0, 130.0, 0.9),
        left_hip=(105.0, 210.0, 0.9),
        left_ankle=(105.0, 300.0, 0.9),
    )
    result = _posture_from_keypoints(reaching, _SUBJECT_BOX, _ASSET_BOX)
    assert result.reaching is True
    assert "arm extended toward it" in result.describe()


def test_posture_ignores_an_arm_extended_away_from_the_asset() -> None:
    away = _skeleton(
        left_shoulder=(100.0, 120.0, 0.9),
        right_shoulder=(140.0, 120.0, 0.9),
        left_wrist=(20.0, 130.0, 0.9),
    )
    assert _posture_from_keypoints(away, _SUBJECT_BOX, _ASSET_BOX).reaching is False


def test_posture_detects_a_raised_arm() -> None:
    raised = _skeleton(
        left_shoulder=(100.0, 120.0, 0.9),
        right_shoulder=(140.0, 120.0, 0.9),
        left_wrist=(100.0, 60.0, 0.9),
    )
    result = _posture_from_keypoints(raised, _SUBJECT_BOX, _ASSET_BOX)
    assert result.arm_raised is True
    assert "raised above shoulder height" in result.describe()


def test_posture_detects_crouching() -> None:
    crouched = _skeleton(
        left_hip=(105.0, 250.0, 0.9),
        right_hip=(135.0, 250.0, 0.9),
        left_ankle=(105.0, 300.0, 0.9),
        right_ankle=(135.0, 300.0, 0.9),
    )
    result = _posture_from_keypoints(crouched, _SUBJECT_BOX, _ASSET_BOX)
    assert result.crouching is True
    assert "crouched or bent-over" in result.describe()


def test_posture_ignores_low_confidence_joints() -> None:
    """A pose model will happily place a wrist it cannot see, and a
    hallucinated wrist is exactly what turns arms-down into 'reaching'."""
    guessed = _skeleton(
        left_shoulder=(100.0, 120.0, 0.9),
        right_shoulder=(140.0, 120.0, 0.9),
        right_wrist=(250.0, 130.0, 0.1),
    )
    assert _posture_from_keypoints(guessed, _SUBJECT_BOX, _ASSET_BOX).reaching is False


def test_posture_of_a_degenerate_subject_box_is_empty() -> None:
    assert _posture_from_keypoints(_STANDING, (10.0, 10.0, 20.0, 10.0), _ASSET_BOX) == (
        PostureResult()
    )


def test_posture_needs_both_shoulders_to_judge_reaching() -> None:
    one_shoulder = _skeleton(
        left_shoulder=(100.0, 120.0, 0.9), right_wrist=(250.0, 130.0, 0.9)
    )
    assert _posture_from_keypoints(one_shoulder, _SUBJECT_BOX, _ASSET_BOX).reaching is (
        False
    )


def test_reaching_needs_non_zero_shoulder_width() -> None:
    assert _is_reaching(
        [(100.0, 120.0), (100.0, 120.0)], [(300.0, 120.0)], _ASSET_BOX
    ) is (False)


def test_reaching_toward_an_asset_on_the_left() -> None:
    assert (
        _is_reaching(
            [(100.0, 120.0), (140.0, 120.0)], [(60.0, 120.0)], (0.0, 100.0, 60.0, 200.0)
        )
        is True
    )


def test_pose_confidence_of_an_unplaced_skeleton_is_zero() -> None:
    assert _pose_confidence([(0.0, 0.0, 0.0)] * 17) == 0.0


def test_keypoint_beyond_the_skeleton_is_absent() -> None:
    assert _keypoint([(1.0, 2.0, 0.9)], 9) is None


def test_best_pose_keypoints_matches_the_tracked_subject() -> None:
    boxes = _FakeBoxes(
        cls=[0, 0],
        conf=[0.9, 0.9],
        xyxy=[(400.0, 100.0, 460.0, 300.0), _SUBJECT_BOX],
        ids=None,
    )
    result = _FakePoseResult(boxes, _FakeKeypoints([_skeleton(), _STANDING]))
    assert _best_pose_keypoints(result, _SUBJECT_BOX) == _STANDING


def test_best_pose_keypoints_discards_a_non_overlapping_skeleton() -> None:
    """Attributing a bystander's raised arm to the person at the car would
    be worse than reporting nothing."""
    boxes = _FakeBoxes(
        cls=[0], conf=[0.9], xyxy=[(400.0, 100.0, 460.0, 300.0)], ids=None
    )
    result = _FakePoseResult(boxes, _FakeKeypoints([_STANDING]))
    assert _best_pose_keypoints(result, _SUBJECT_BOX) is None


def test_best_pose_keypoints_without_any_detections() -> None:
    empty = _FakeBoxes(cls=[], conf=[], xyxy=[], ids=None)
    assert _best_pose_keypoints(
        _FakePoseResult(empty, _FakeKeypoints([])), _SUBJECT_BOX
    ) is (None)
    assert _best_pose_keypoints(_FakePoseResult(None, None), _SUBJECT_BOX) is None


def test_build_posture_hint_is_absent_when_nothing_was_established() -> None:
    assert _build_posture_hint(PostureResult(), "person") is None


def test_build_posture_hint_hedges() -> None:
    hint = _build_posture_hint(PostureResult(reaching=True), "person")
    assert hint is not None
    assert "POSTURE:" in hint
    assert "not from watching the movement" in hint


async def test_pose_estimator_reports_unavailable_without_ultralytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "ultralytics", raising=False)
    with patch("builtins.__import__", side_effect=ImportError("no ultralytics")):
        assert await PoseEstimator().ensure_ready() is False


def test_pose_estimator_load_sync_refuses_an_incompatible_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "torch_cpu_compatible", lambda: False)
    estimator = PoseEstimator()
    with pytest.raises(CPUIncompatibleError):
        estimator._load_sync()


async def test_pose_estimator_ensure_ready_false_when_cpu_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "blink_downloader.vision.runtime.torch_cpu_compatible", lambda: False
    )
    assert await PoseEstimator().ensure_ready() is False


async def test_pose_estimator_ensure_ready_concurrent_calls_load_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the double-checked-lock branch where the second caller
    finds the model already loaded by the time it acquires the lock."""
    mock_ultra = MagicMock()
    mock_ultra.YOLO.side_effect = lambda *_a, **_kw: (time.sleep(0.05), MagicMock())[1]
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)

    estimator = PoseEstimator()
    results = await asyncio.gather(estimator.ensure_ready(), estimator.ensure_ready())
    assert results == [True, True]
    mock_ultra.YOLO.assert_called_once()


async def test_pose_estimator_load_failure_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_ultra = MagicMock()
    mock_ultra.YOLO.side_effect = RuntimeError("corrupt weights")
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    assert await PoseEstimator().ensure_ready() is False


async def test_pose_estimator_resolves_a_bare_model_name_to_the_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(runtime_module, "_YOLO_MODEL_CACHE_DIR", str(tmp_path))
    mock_ultra = MagicMock()
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    assert await PoseEstimator("yolo26n-pose.pt").ensure_ready() is True
    assert mock_ultra.YOLO.call_args[0][0] == str(tmp_path / "yolo26n-pose.pt")


async def test_pose_estimator_keeps_an_absolute_model_path_as_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A path (rather than a bare weights filename) is somewhere the user
    put the file — it must not be rewritten into the cache directory."""
    monkeypatch.setattr(runtime_module, "_YOLO_MODEL_CACHE_DIR", str(tmp_path))
    mock_ultra = MagicMock()
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    explicit = str(tmp_path / "custom" / "pose.pt")
    assert await PoseEstimator(explicit).ensure_ready() is True
    assert mock_ultra.YOLO.call_args[0][0] == explicit


async def test_pose_estimator_analyzes_the_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((360, 640, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    boxes = _FakeBoxes(cls=[0], conf=[0.9], xyxy=[_SUBJECT_BOX], ids=None)
    reaching = _skeleton(
        left_shoulder=(100.0, 120.0, 0.9),
        right_shoulder=(140.0, 120.0, 0.9),
        right_wrist=(200.0, 130.0, 0.9),
    )
    model = MagicMock(return_value=[_FakePoseResult(boxes, _FakeKeypoints([reaching]))])
    estimator = PoseEstimator()
    estimator._model = model

    result = await estimator.analyze(b"frame", _SUBJECT_BOX, _ASSET_BOX)
    assert result is not None
    assert result.reaching is True


async def test_pose_estimator_returns_nothing_for_an_undecodable_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = None
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)
    estimator = PoseEstimator()
    estimator._model = MagicMock(return_value=[])
    assert await estimator.analyze(b"frame", _SUBJECT_BOX, _ASSET_BOX) is None


async def test_pose_estimator_returns_nothing_when_the_model_finds_no_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((360, 640, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)
    estimator = PoseEstimator()
    estimator._model = MagicMock(return_value=[])
    assert await estimator.analyze(b"frame", _SUBJECT_BOX, _ASSET_BOX) is None


async def test_pose_estimator_survives_an_inference_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)
    estimator = PoseEstimator()
    estimator._model = MagicMock(side_effect=RuntimeError("boom"))
    assert await estimator.analyze(b"frame", _SUBJECT_BOX, _ASSET_BOX) is None


async def test_pose_estimator_unavailable_returns_nothing() -> None:
    estimator = PoseEstimator()
    with patch.object(PoseEstimator, "ensure_ready", return_value=False):
        assert await estimator.analyze(b"frame", _SUBJECT_BOX, _ASSET_BOX) is None


async def test_pipeline_records_posture_when_pose_estimation_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0, 2],
            conf=[0.9, 0.95],
            xyxy=[(5.0, 5.0, 45.0, 195.0), (60.0, 60.0, 190.0, 150.0)],
            ids=[1, 2],
        ),
        {0: "person", 2: "car"},
    )
    posture = PostureResult(reaching=True, confidence=0.9)
    pipeline = VisionPipeline(
        VisionConfig(enhanced_detection_enabled=True, pose_estimation_enabled=True)
    )
    with patch.object(PoseEstimator, "analyze", return_value=posture):
        hints = await pipeline.process_clip(
            [_real_jpeg_bytes(size=(200, 200))],
            car_description="Silver Kia",
            car_protection_applies=True,
            camera="Driveway",
        )
    assert hints.posture is posture
    assert hints.posture_hint is not None
    assert "pose estimation" not in hints.unavailable_sources


async def test_pipeline_reports_pose_unavailable_when_it_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0, 2],
            conf=[0.9, 0.95],
            xyxy=[(5.0, 5.0, 45.0, 195.0), (60.0, 60.0, 190.0, 150.0)],
            ids=[1, 2],
        ),
        {0: "person", 2: "car"},
    )
    pipeline = VisionPipeline(
        VisionConfig(enhanced_detection_enabled=True, pose_estimation_enabled=True)
    )
    with patch.object(PoseEstimator, "analyze", return_value=None):
        hints = await pipeline.process_clip(
            [_real_jpeg_bytes(size=(200, 200))],
            car_description="Silver Kia",
            car_protection_applies=True,
            camera="Driveway",
        )
    assert hints.posture is None
    assert "pose estimation" in hints.unavailable_sources


async def test_appearance_change_is_skipped_while_someone_blocks_the_vehicle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The region would be showing a person rather than the car, and "it
    looks different" would be measuring where they stood — which is not
    something the one CRITICAL event should ever rest on."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0, 2],
            conf=[0.9, 0.95],
            xyxy=[(70.0, 70.0, 110.0, 160.0), (60.0, 60.0, 190.0, 150.0)],
            ids=[1, 2],
        ),
        {0: "person", 2: "car"},
    )
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Driveway",
    )
    assert hints.asset_appearance_change is None


async def test_appearance_change_is_computed_with_a_clear_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yolo_env(
        monkeypatch,
        _FakeBoxes(
            cls=[0, 2],
            conf=[0.9, 0.95],
            xyxy=[(5.0, 5.0, 45.0, 195.0), (60.0, 60.0, 190.0, 150.0)],
            ids=[1, 2],
        ),
        {0: "person", 2: "car"},
    )
    calls: list[tuple[float, ...]] = []

    def _change(_before: bytes, _after: bytes, box: tuple[float, ...]) -> float:
        calls.append(tuple(box))
        return 0.42

    monkeypatch.setattr(imaging_module, "_region_appearance_change", _change)
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    hints = await pipeline.process_clip(
        [_real_jpeg_bytes(size=(200, 200))],
        car_description="Silver Kia",
        car_protection_applies=True,
        camera="Driveway",
    )
    assert hints.asset_appearance_change == pytest.approx(0.42)
    assert calls == [(60.0, 60.0, 190.0, 150.0)]


async def test_pose_estimator_loads_once_under_concurrent_first_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clips analyzed at once both reach ensure_ready before either has
    finished loading; the second must reuse the first's model rather than
    load a second copy."""
    mock_ultra = MagicMock()
    monkeypatch.setitem(sys.modules, "ultralytics", mock_ultra)
    estimator = PoseEstimator()
    results = await asyncio.gather(estimator.ensure_ready(), estimator.ensure_ready())
    assert results == [True, True]
    assert mock_ultra.YOLO.call_count == 1


async def test_pose_estimator_reports_an_incompatible_cpu_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "torch_cpu_compatible", lambda: False)
    assert await PoseEstimator().ensure_ready() is False


async def test_pose_estimator_returns_nothing_when_no_skeleton_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bystander's skeleton must not be attributed to the person at the
    car — no match means no posture, not the nearest guess."""
    mock_cv2 = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    mock_cv2.imdecode.return_value = np.zeros((360, 640, 3), dtype=np.uint8)
    monkeypatch.setitem(sys.modules, "cv2", mock_cv2)

    elsewhere = _FakeBoxes(
        cls=[0], conf=[0.9], xyxy=[(500.0, 100.0, 560.0, 300.0)], ids=None
    )
    estimator = PoseEstimator()
    estimator._model = MagicMock(
        return_value=[_FakePoseResult(elsewhere, _FakeKeypoints([_STANDING]))]
    )
    assert await estimator.analyze(b"frame", _SUBJECT_BOX, _ASSET_BOX) is None


async def test_pipeline_enhances_only_the_prompt_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enhancement is for the prompt images alone, so it runs exactly once —
    denoising is the most expensive thing this module does without a model
    behind it, and that matters on the hardware this add-on targets."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[0], conf=[0.9], xyxy=[(10.0, 10.0, 30.0, 90.0)], ids=[1]),
        {0: "person"},
    )
    calls = 0
    original = FrameEnhancer.enhance

    def _counted(frames: list[bytes]) -> list[bytes]:
        nonlocal calls
        calls += 1
        return original(frames)

    monkeypatch.setattr(FrameEnhancer, "enhance", staticmethod(_counted))
    pipeline = VisionPipeline(VisionConfig(enhanced_detection_enabled=True))
    await pipeline.process_clip([_real_jpeg_bytes(size=(200, 200))])
    assert calls == 1


async def test_pipeline_never_enhances_the_frames_the_detector_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection models see raw frames — the rule process_clip already
    applies to face recognition, for the same reason.

    CLAHE plus non-local-means denoising is not what YOLO was trained on,
    and feeding it enhanced frames lost true positives while gaining false
    ones (a house read as a "truck", a second "person" beside the only one
    there). Asserted on what the detector actually decoded, not on a call
    count, so tagging the enhanced copies proves which list each consumer
    was handed."""
    _yolo_env(
        monkeypatch,
        _FakeBoxes(cls=[0], conf=[0.9], xyxy=[(10.0, 10.0, 30.0, 90.0)], ids=[1]),
        {0: "person"},
    )
    monkeypatch.setattr(
        FrameEnhancer,
        "enhance",
        staticmethod(lambda frames: [b"ENHANCED" + frame for frame in frames]),
    )
    pipeline = VisionPipeline(
        VisionConfig(enhanced_detection_enabled=True, temporal_scan_frames=4)
    )
    prompt_frames = [_real_jpeg_bytes(size=(200, 200))]
    hints = await pipeline.process_clip(
        prompt_frames,
        raw_frames=[_real_jpeg_bytes(size=(200, 200))] * 4,
    )

    # The prompt images still get the enhanced copies.
    assert hints.enhanced_frames == [b"ENHANCED" + prompt_frames[0]]
    # Nothing the detector decoded carries the marker.
    decoded = [
        call.args[0].tobytes() for call in sys.modules["cv2"].imdecode.call_args_list
    ]
    assert decoded
    assert not any(frame.startswith(b"ENHANCED") for frame in decoded)
