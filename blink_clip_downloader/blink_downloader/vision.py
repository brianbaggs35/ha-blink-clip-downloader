"""Optional computer-vision enhancement pipeline for clip analysis.

Five independently-toggleable stages, layered on top of the existing
AI-provider prompt pipeline (see ``analyzer.py``) rather than replacing it —
each stage produces a bounded, code-computed hint that gets appended to the
same prompt the configured AI provider (ollama/anthropic/openai/moondream)
already reasons over, exactly like the existing scene-baseline and
zone-motion hints. The AI model still makes the final suspicious/not call;
this module only gives it better evidence to work with.

- :class:`FrameEnhancer` — OpenCV CLAHE contrast enhancement + denoising.
- :class:`ObjectDetector` — YOLO object detection + ByteTrack tracking
  (Ultralytics), for precise person/vehicle/animal boxes instead of raw
  pixel-diff motion.
- :class:`DepthEstimator` — monocular depth estimation (Depth Anything V2
  via transformers), to tell "overlapping in the 2D frame" apart from
  "actually at the same distance from the camera".
- :class:`ContactSegmenter` — pixel-level segmentation (SAM2 via
  transformers) to refine a detected bounding-box overlap into an actual
  touching-or-not judgment.
- :class:`FaceEmbedder` / :class:`FaceRecognizer` — local-only face
  recognition (facenet-pytorch) to suppress alerts for enrolled household
  members. Enrollment data (photos, embeddings) never leaves this add-on.

Every stage lazily imports its own heavy dependency (torch, ultralytics,
opencv, transformers, facenet-pytorch — none of which are required to run
the add-on's core features) and reports itself unavailable rather than
raising if that dependency isn't installed or fails to load. Nothing in
this module is imported by ``analyzer.py`` at call time unless the
corresponding config option is enabled, and even then the heavy import
itself is deferred to first use — see each class's ``ensure_ready()``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import platform
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)

# Each stage below guards its own *first load* with an asyncio.Lock (see each
# ensure_ready()), but that only serializes concurrent calls to that same
# stage - it does nothing to stop two *different* stages (e.g. ObjectDetector
# and FaceEmbedder) from each hitting their first-ever `import cv2`/`import
# numpy`/`import torch` at the same moment on different threads (each
# _load_sync() runs in its own executor thread; FrameEnhancer.enhance() runs
# directly on the event loop thread). Concurrent clip analysis
# (concurrent_downloads > 1, or a startup backlog) makes this a real race,
# not just a theoretical one: CPython's import machinery isn't safe against
# two threads both performing the *first* import of the same native
# extension module at once, and can leave it permanently broken for the
# rest of the process's lifetime ("ImportError: cannot load module more
# than once per process") rather than raising a transient, retryable error.
# A single lock shared by every stage's import statement (not the model
# loading/download that follows, which is safe once the import itself has
# completed) closes this for good - found via a real crash on a fresh
# Home Assistant OS install with several clips analyzed concurrently at
# startup.
_native_import_lock = threading.Lock()

# Persistent cache dir for Ultralytics YOLO weights (see ObjectDetector
# below) — mirrors TORCH_HOME/HF_HOME (set in the Dockerfile) for the same
# reason: YOLO downloads to whatever path it's given rather than consulting
# either of those env vars itself, so a bare model filename would otherwise
# download into the process's cwd (an ephemeral s6-overlay runtime path,
# not the /data volume) and be re-fetched after every container recreation.
_YOLO_MODEL_CACHE_DIR = "/data/model_cache/yolo"

# COCO class names (Ultralytics' default label set) this pipeline surfaces
# in prompts. Anything else YOLO detects (furniture, traffic lights, sports
# balls, ...) isn't relevant to a home-security judgment and is filtered
# out to keep the OBJECT DETECTION hint short and focused.
_RELEVANT_CLASSES = frozenset(
    {
        "person",
        "car",
        "truck",
        "bus",
        "motorcycle",
        "bicycle",
        "dog",
        "cat",
        "bird",
        "horse",
        "backpack",
        "handbag",
        "suitcase",
    }
)
_VEHICLE_CLASSES = frozenset({"car", "truck", "bus", "motorcycle"})
# Classes eligible as the "subject" in vehicle-proximity/depth/contact
# analysis — not just people. A dog jumping on a parked car (scratches) or
# a cat pawing at one is exactly the kind of non-person vehicle contact the
# base prompt's own rules already flag (see analyzer.py's default prompt,
# "An animal ... jumps on, paws at, or urinates/defecates on a vehicle"), so
# the more rigorous depth/contact stages need to consider animals as
# candidate subjects too, not only people.
_SUBJECT_CLASSES = frozenset({"person", "dog", "cat", "bird", "horse"})

# Cosine-similarity floor (0.0-1.0, InceptionResnetV1 512-d embeddings) for
# treating a detected face as matching an enrolled household member. Chosen
# conservatively — a missed match just means no RECOGNIZED RESIDENT hint
# (analysis proceeds exactly as it would with face recognition off), while a
# false match would wrongly reassure the model, so this errs toward "no
# match" when uncertain rather than the reverse.
_FACE_MATCH_THRESHOLD = 0.75

# Depth Anything V2 output is a *relative* (not metric) depth/disparity map,
# so "similar depth" is judged as a fraction of this clip's own depth range
# rather than an absolute unit. Below this fraction, two regions are treated
# as being at roughly the same distance from the camera.
_DEPTH_SIMILARITY_FRACTION = 0.15


# ----------------------------------------------------------------------
# Availability checks
# ----------------------------------------------------------------------


class CPUIncompatibleError(RuntimeError):
    """Raised by a stage's _load_sync() when torch_cpu_compatible() is
    False, instead of attempting the import that would otherwise crash the
    process. A distinct type from plain RuntimeError so each stage's
    ensure_ready() can catch this specific case (clean "unavailable on this
    CPU" warning) without also swallowing an unrelated RuntimeError from
    the model/import machinery itself (which should keep getting the
    existing generic-failure handling, traceback and all)."""


_CPU_INCOMPATIBLE_MESSAGE = (
    "this device's CPU is missing instructions PyTorch needs "
    "(common on Raspberry Pi 4 and older ARM boards; Raspberry "
    "Pi 5 is not affected)"
)


def torch_cpu_compatible() -> bool:
    """Return True if this CPU can safely run PyTorch's official builds.

    PyTorch's official aarch64 wheels assume the CPU supports the ARMv8.1
    LSE atomic instructions (exposed as "atomics" in /proc/cpuinfo's
    Features line). Without them, importing torch — or any package that
    imports it, which is every stage below except FrameEnhancer — can
    crash the whole process with an illegal-instruction signal (SIGILL)
    rather than a catchable Python exception. This is a well-documented,
    still-unresolved upstream issue specific to Raspberry Pi 4 and older
    boards (Cortex-A72 and earlier predate LSE); Raspberry Pi 5's
    Cortex-A76 is unaffected, and x86_64 has no such requirement at all.
    Every torch-dependent stage's ``_load_sync`` below calls this *before*
    attempting its import, since a SIGILL can't be caught after the fact —
    prevention is the only option. An unreadable/unparseable /proc/cpuinfo
    is conservatively treated as unsupported, since a false "unavailable"
    just costs a feature, while a false "available" risks the crash this
    check exists to prevent.
    """
    if platform.machine() not in ("aarch64", "arm64"):
        return True
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
    except OSError:
        return False
    for line in cpuinfo.splitlines():
        if line.lower().startswith("features"):
            _, _, features = line.partition(":")
            return "atomics" in features.split()
    return False


def is_face_recognition_available() -> bool:
    """Return True if facenet_pytorch is importable and the CPU supports it.

    See :func:`torch_cpu_compatible` — facenet_pytorch depends on torch, so
    this must gate on CPU compatibility too, not just package presence.
    """
    if not torch_cpu_compatible():
        return False
    try:
        __import__("facenet_pytorch")
        return True
    except ImportError:
        return False


# ----------------------------------------------------------------------
# Stage 1: OpenCV preprocessing
# ----------------------------------------------------------------------


class FrameEnhancer:
    """CLAHE contrast enhancement + light denoising via OpenCV.

    The lightest stage in this pipeline — no model to load or download,
    just per-frame image processing — but still requires the opencv
    dependency, so it's gated by ``ai_enhanced_detection_enabled`` like
    every other stage here rather than always running.
    """

    @staticmethod
    def enhance(frames: list[bytes]) -> list[bytes]:
        """Return *frames* with CLAHE contrast enhancement and light denoising applied.

        Falls back to returning *frames* unchanged (per-frame, not as a
        whole batch) if opencv isn't installed or a given frame fails to
        decode — this is a quality improvement, never a hard requirement
        for analysis to proceed.
        """
        try:
            with _native_import_lock:
                import cv2  # type: ignore[import-not-found]
                import numpy as np
        except ImportError:
            return frames

        enhanced: list[bytes] = []
        for frame in frames:
            try:
                arr = np.frombuffer(frame, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    enhanced.append(frame)
                    continue
                lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
                l_channel, a_channel, b_channel = cv2.split(lab)
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                l_channel = clahe.apply(l_channel)
                lab = cv2.merge((l_channel, a_channel, b_channel))
                img = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
                img = cv2.fastNlMeansDenoisingColored(img, None, 5, 5, 7, 21)
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                enhanced.append(buf.tobytes() if ok else frame)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Frame enhancement failed, using raw frame: %s", exc)
                enhanced.append(frame)
        return enhanced


# ----------------------------------------------------------------------
# Stage 2: object detection + tracking (Ultralytics YOLO + ByteTrack)
# ----------------------------------------------------------------------


@dataclass
class DetectedObject:
    """One detected object in one analyzed frame."""

    label: str
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixel coords
    track_id: int | None
    frame_index: int


class ObjectDetector:
    """YOLO object detection + ByteTrack tracking (Ultralytics).

    The model is loaded once (and downloaded on first use, cached under
    /data — see ``_YOLO_MODEL_CACHE_DIR`` above) and reused across clips —
    reloading it per clip would make every analysis pay a multi-second
    cold-start cost. Inference runs in a thread executor so the asyncio
    event loop is never blocked, mirroring ``MoondreamLocalAnalyzer``'s
    pattern in ``analyzer.py`` for the same reason.

    Note on tracking: Ultralytics' ``.track(persist=True)`` is designed for
    continuous video frames. This pipeline only ever hands it the handful
    of frames already sampled for AI analysis (seconds apart, not
    consecutive video frames), so track IDs here are best-effort — useful
    for noticing "the same object across two sampled frames" but not a
    substitute for true continuous tracking.
    """

    def __init__(self, model_name: str = "yolo11n.pt") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Must be set before the ultralytics import below: it probes
        # ~/.config/Ultralytics for a writable settings dir as a side effect
        # of import (not lazily), and this container's $HOME isn't writable
        # by the add-on's runtime user. Finding it unwritable, ultralytics
        # falls back to /tmp on its own — but does so by printing raw,
        # unformatted lines straight to stdout (bypassing our logging setup
        # entirely, unlabeled and timestamp-free among otherwise-structured
        # log output) and using an ephemeral directory that doesn't survive
        # a container recreation. Pointing it at the same persistent,
        # known-writable directory the model weights already cache under
        # avoids both problems in one step: no fallback needed, so no
        # notice, and the settings file persists like the weights do.
        os.environ.setdefault("YOLO_CONFIG_DIR", _YOLO_MODEL_CACHE_DIR)
        # The whole body below (not just the import) runs under the shared
        # lock: this method only ever executes once per process (ensure_ready
        # already guards repeat calls), so the only cost is a one-time wait
        # if another stage's first load is in flight at the same moment -
        # see _native_import_lock's own comment for why that's necessary.
        with _native_import_lock:
            from ultralytics import YOLO  # type: ignore[import-not-found]

            # A bare filename (the default "yolo11n.pt", or any custom
            # ai_object_detection_model naming a standard pretrained
            # checkpoint) downloads into YOLO()'s cwd if given as-is;
            # resolving it against a persistent absolute directory first
            # makes that download (and every reload after a container
            # recreation) reuse the same file instead of silently
            # re-fetching it. A caller-supplied path that already has a
            # directory component is left untouched.
            model_path = self._model_name
            if os.path.basename(model_path) == model_path:
                os.makedirs(_YOLO_MODEL_CACHE_DIR, exist_ok=True)
                model_path = os.path.join(_YOLO_MODEL_CACHE_DIR, model_path)
            _LOGGER.info("Loading YOLO object-detection model '%s'", self._model_name)
            self._model = YOLO(model_path)
            _LOGGER.info("YOLO model '%s' ready", self._model_name)

    async def ensure_ready(self) -> bool:
        """Ensure the model is loaded. Returns True when ready."""
        if self._model is not None:
            return True
        async with self._get_lock():
            if self._model is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                _LOGGER.warning(
                    "ultralytics package is not installed, object detection "
                    "unavailable: %s. Install it with: pip install ultralytics",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Object detection unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load YOLO model")
                return False

    def _detect_in_frame(self, img: Any, idx: int) -> list[DetectedObject]:
        results = self._model.track(
            img, persist=True, tracker="bytetrack.yaml", verbose=False
        )
        if not results:
            return []
        result = results[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        names = result.names
        ids = boxes.id
        detections: list[DetectedObject] = []
        for i in range(len(boxes)):
            label = names.get(int(boxes.cls[i]), "")
            if label not in _RELEVANT_CLASSES:
                continue
            x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i])
            track_id = int(ids[i]) if ids is not None else None
            detections.append(
                DetectedObject(
                    label=label,
                    confidence=float(boxes.conf[i]),
                    box=(x1, y1, x2, y2),
                    track_id=track_id,
                    frame_index=idx,
                )
            )
        return detections

    def _detect_sync(self, frames: list[bytes]) -> list[DetectedObject]:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

        detections: list[DetectedObject] = []
        for idx, frame in enumerate(frames):
            arr = np.frombuffer(frame, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            detections.extend(self._detect_in_frame(img, idx))
        return detections

    async def detect(self, frames: list[bytes]) -> list[DetectedObject] | None:
        """Detect+track relevant objects across *frames*. None if unavailable."""
        if not frames or not await self.ensure_ready():
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._detect_sync, frames)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Object detection failed: %s", exc)
            return None


def _box_gap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """Return the edge-to-edge pixel gap between boxes *a* and *b*.

    Zero or positive when the boxes don't overlap (the straight-line
    distance between their nearest edges). Negative when they overlap,
    with magnitude equal to the smaller of the two axes' overlap depth —
    more negative means more deeply overlapping.
    """
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(bx1 - ax2, ax1 - bx2, 0.0)
    dy = max(by1 - ay2, ay1 - by2, 0.0)
    if not dx and not dy:
        overlap_x = min(ax2, bx2) - max(ax1, bx1)
        overlap_y = min(ay2, by2) - max(ay1, by1)
        return -min(overlap_x, overlap_y)
    return math.hypot(dx, dy)


def _proximity_label(gap: float, vehicle_width: float) -> str:
    """Categorize a box gap (see :func:`_box_gap`) relative to vehicle width."""
    if gap <= 0:
        return "overlapping the detected vehicle's outline"
    ratio = gap / vehicle_width if vehicle_width > 0 else gap
    if ratio < 0.1:
        return "immediately adjacent to the detected vehicle"
    if ratio < 0.4:
        return "close to the detected vehicle"
    return "well away from the detected vehicle"


def _best_subject_vehicle_pair(
    detections: list[DetectedObject],
) -> tuple[DetectedObject, DetectedObject, int] | None:
    """Return the (subject, vehicle, frame_index) pair with the smallest gap.

    "Subject" is a person *or* an animal (see :data:`_SUBJECT_CLASSES`) — a
    dog jumping on a parked car or a cat pawing at one is exactly the kind
    of vehicle contact this pipeline needs to catch, not just a person's.
    Used to pick which single frame/box-pair the heavier depth and contact
    stages spend their budget refining. None if no sampled frame contains
    both a subject and a vehicle detection.
    """
    best: tuple[DetectedObject, DetectedObject, int] | None = None
    best_gap: float | None = None
    by_frame: dict[int, list[DetectedObject]] = {}
    for d in detections:
        by_frame.setdefault(d.frame_index, []).append(d)
    for frame_idx, items in by_frame.items():
        subjects = [d for d in items if d.label in _SUBJECT_CLASSES]
        vehicles = [d for d in items if d.label in _VEHICLE_CLASSES]
        for s in subjects:
            for v in vehicles:
                gap = _box_gap(s.box, v.box)
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best = (s, v, frame_idx)
    return best


def _build_detection_hint(
    detections: list[DetectedObject], car_description: str
) -> str | None:
    """Render detections into an OBJECT DETECTION prompt hint, or None if empty.

    *car_description* being empty means this camera isn't under
    protected-vehicle rules (see :meth:`VisionPipeline.process_clip`) — the
    detected-classes line is still useful generically, but the
    vehicle-distance estimate is skipped since there's no protected vehicle
    for it to be relevant to on this camera.
    """
    if not detections:
        return None
    labels = sorted({d.label for d in detections})
    lines = [f"Detected object classes across sampled frames: {', '.join(labels)}."]

    pair = _best_subject_vehicle_pair(detections) if car_description else None
    if pair is not None:
        subject, vehicle, _ = pair
        vehicle_width = vehicle.box[2] - vehicle.box[0]
        gap = _box_gap(subject.box, vehicle.box)
        proximity = _proximity_label(gap, vehicle_width)
        lines.append(
            f"Object-detection distance estimate: the detected {subject.label}'s "
            f"bounding box is {proximity} (pixel-based estimate from the "
            "object detector, not a physical measurement)."
        )

    return (
        "\n\nOBJECT DETECTION: "
        + " ".join(lines)
        + " Treat this as a precise, code-computed hint about what was "
        "actually detected and roughly where — cross-check it against what "
        "you can see in the frames yourself, since detector misses or "
        "false positives are possible."
    )


# Fraction of sampled frames a tracked person must appear in to count as
# "lingering" vs. "briefly passing through" — see _build_tracking_hint.
# Deliberately asymmetric (lingering needs a clear majority; passing-through
# only needs a small minority) so an ambiguous middle ground emits no hint
# at all rather than a low-confidence guess either way.
_TRACKING_LINGER_FRACTION = 0.6
_TRACKING_BRIEF_FRACTION = 0.3
# Below this many sampled frames, "how many frames did this track appear
# in" is too noisy a sample to characterize lingering vs. passing through.
_TRACKING_MIN_FRAMES = 3


def _build_tracking_hint(
    detections: list[DetectedObject], total_frames: int
) -> str | None:
    """Render ByteTrack continuity into a TRACKING prompt hint, or None.

    A single frame saying "person detected" says nothing about behavior
    over time — the actual value ByteTrack adds (see ObjectDetector) is
    knowing whether the *same* tracked person shows up across most of the
    sampled frames (lingering/casing) or just one or two (passing through).
    Uses whichever tracked person has the highest frame-presence fraction;
    ties and untracked detections (track_id is None, e.g. tracking wasn't
    available for this call) are simply not counted.
    """
    if total_frames < _TRACKING_MIN_FRAMES:
        return None

    frames_per_track: dict[int, set[int]] = {}
    for d in detections:
        if d.label != "person" or d.track_id is None:
            continue
        frames_per_track.setdefault(d.track_id, set()).add(d.frame_index)

    if not frames_per_track:
        return None

    best_track_id = max(frames_per_track, key=lambda t: len(frames_per_track[t]))
    frame_count = len(frames_per_track[best_track_id])
    fraction = frame_count / total_frames

    if fraction >= _TRACKING_LINGER_FRACTION:
        body = (
            f"the same tracked person appears in {frame_count} of "
            f"{total_frames} sampled frames spanning this clip — consistent "
            "with lingering or casing rather than simply passing through"
        )
    elif fraction <= _TRACKING_BRIEF_FRACTION:
        body = (
            f"the same tracked person appears in only {frame_count} of "
            f"{total_frames} sampled frames — consistent with briefly "
            "passing through rather than lingering"
        )
    else:
        return None

    return (
        "\n\nTRACKING: Across the sampled frames, " + body + ". This is a "
        "best-effort signal from tracking sparse sampled frames (seconds "
        "apart), not continuous video, so treat it as a hint rather than a "
        "precise measurement of how long anyone was actually present."
    )


# ----------------------------------------------------------------------
# Stage 3: depth estimation (Depth Anything V2 via transformers)
# ----------------------------------------------------------------------


@dataclass
class DepthComparison:
    """Relative-depth comparison between two detected regions in one frame."""

    similar_depth: bool
    subject_depth: float
    vehicle_depth: float


class DepthEstimator:
    """Monocular depth estimation (Depth Anything V2) via transformers.

    Requires object detection to also be enabled — depth is only computed
    at already-detected person/vehicle locations, to tell "overlapping in
    the 2D frame" apart from "actually at the same distance from the
    camera". Output is a *relative* depth map (no camera calibration), so
    comparisons here are qualitative ("about the same distance" vs
    "noticeably different distances"), never a physical measurement.
    """

    _MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"

    def __init__(self) -> None:
        self._pipe: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # _native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with _native_import_lock:
            from transformers import pipeline  # type: ignore[import-not-found]

            _LOGGER.info("Loading depth-estimation model '%s'", self._MODEL_ID)
            self._pipe = pipeline(
                task="depth-estimation", model=self._MODEL_ID, device="cpu"
            )
            _LOGGER.info("Depth-estimation model ready")

    async def ensure_ready(self) -> bool:
        if self._pipe is not None:
            return True
        async with self._get_lock():
            if self._pipe is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                _LOGGER.warning(
                    "transformers package is not installed, depth estimation "
                    "unavailable: %s. Install it with: pip install transformers",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Depth estimation unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load depth-estimation model")
                return False

    def _compare_sync(
        self,
        frame: bytes,
        subject_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> DepthComparison | None:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(frame)).convert("RGB")
        result = self._pipe(image)
        depth_arr = np.array(result["depth"], dtype=np.float32)
        height, width = depth_arr.shape[:2]

        def region_mean(box: tuple[float, float, float, float]) -> float | None:
            x1, y1, x2, y2 = box
            xi1, yi1 = max(0, int(x1)), max(0, int(y1))
            xi2, yi2 = min(width, int(x2)), min(height, int(y2))
            if xi2 <= xi1 or yi2 <= yi1:
                return None
            region = depth_arr[yi1:yi2, xi1:xi2]
            return float(region.mean()) if region.size else None

        subject_depth = region_mean(subject_box)
        vehicle_depth = region_mean(vehicle_box)
        if subject_depth is None or vehicle_depth is None:
            return None

        depth_range = max(float(depth_arr.max() - depth_arr.min()), 1e-6)
        normalized_diff = abs(subject_depth - vehicle_depth) / depth_range
        return DepthComparison(
            similar_depth=normalized_diff < _DEPTH_SIMILARITY_FRACTION,
            subject_depth=subject_depth,
            vehicle_depth=vehicle_depth,
        )

    async def compare(
        self,
        frame: bytes,
        subject_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> DepthComparison | None:
        if not await self.ensure_ready():
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._compare_sync, frame, subject_box, vehicle_box
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Depth estimation failed: %s", exc)
            return None


def _build_depth_hint(result: DepthComparison) -> str:
    if result.similar_depth:
        body = (
            "the detected person/animal and detected vehicle appear to be "
            "at roughly the same distance from the camera — consistent "
            "with them actually being near the vehicle in 3D space, not "
            "just overlapping it in the 2D frame"
        )
    else:
        body = (
            "the detected person/animal and detected vehicle appear to be "
            "at noticeably different distances from the camera — they may "
            "only appear close together because one is in front of the "
            "other from this camera's angle, not because they're actually "
            "near the vehicle"
        )
    return (
        "\n\nDEPTH ESTIMATE: A monocular depth model estimates that "
        + body
        + ". This is a relative, best-effort estimate (not a precise "
        "measurement) — weigh it alongside what you can see in the frames."
    )


# ----------------------------------------------------------------------
# Stage 4: contact segmentation (SAM2 via transformers)
# ----------------------------------------------------------------------


@dataclass
class ContactResult:
    """Pixel-level segmentation contact judgment between two detected regions."""

    touching: bool
    mask_gap_pixels: float


class ContactSegmenter:
    """Precise pixel-level contact detection (SAM2) via transformers.

    Requires object detection to also be enabled. Given a person box and a
    vehicle box already found by :class:`ObjectDetector`, this segments
    both objects' actual visible outlines (not just their rectangular
    bounding boxes) and checks whether those outlines touch — a much
    stronger signal for genuine physical contact than a bounding-box
    overlap, which can trigger just because one object is in front of the
    other from the camera's angle. The heaviest of the five stages in this
    module.
    """

    _MODEL_ID = "facebook/sam2.1-hiera-tiny"

    def __init__(self) -> None:
        self._model: Any = None
        self._processor: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # _native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with _native_import_lock:
            from transformers import (  # type: ignore[import-not-found]
                Sam2Model,
                Sam2Processor,
            )

            _LOGGER.info("Loading SAM2 segmentation model '%s'", self._MODEL_ID)
            # _MODEL_ID is a fixed constant for an official facebook/ repo;
            # this optional pipeline trusts the HF hub the same way the rest
            # of the CV stack trusts PyPI (B615).
            self._model = Sam2Model.from_pretrained(self._MODEL_ID)  # nosec B615
            self._processor = Sam2Processor.from_pretrained(self._MODEL_ID)  # nosec B615
            _LOGGER.info("SAM2 segmentation model ready")

    async def ensure_ready(self) -> bool:
        if self._model is not None:
            return True
        async with self._get_lock():
            if self._model is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                _LOGGER.warning(
                    "transformers package is not installed, contact "
                    "segmentation unavailable: %s. Install it with: "
                    "pip install transformers",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Contact segmentation unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load SAM2 model")
                return False

    def _check_sync(
        self,
        frame: bytes,
        subject_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> ContactResult | None:
        import cv2  # type: ignore[import-not-found]
        import numpy as np
        import torch  # type: ignore[import-not-found]
        from PIL import Image

        image = Image.open(io.BytesIO(frame)).convert("RGB")
        input_boxes = [[list(subject_box), list(vehicle_box)]]
        inputs = self._processor(
            images=image, input_boxes=input_boxes, return_tensors="pt"
        )
        with torch.no_grad():
            outputs = self._model(**inputs, multimask_output=False)
        masks = self._processor.post_process_masks(
            outputs.pred_masks.cpu(), inputs["original_sizes"]
        )[0]
        if masks.shape[0] < 2:
            return None

        person_mask = masks[0, 0].numpy().astype(np.uint8)
        vehicle_mask = masks[1, 0].numpy().astype(np.uint8)

        kernel = np.ones((7, 7), np.uint8)
        # Dilate the person mask in fixed pixel steps and check for overlap
        # with the vehicle mask each time — a cheap, well-known trick for
        # "do these masks come within N pixels of each other" that avoids an
        # expensive nearest-point search across every mask pixel pair. Each
        # iteration grows the mask by the kernel's *radius* (3px for a 7x7
        # kernel), not its full width, so the gap estimate below must use
        # 3.0, not 7.0 — using the full kernel size would overstate the true
        # gap by roughly 2.3x.
        for step in range(1, 11):
            probe = cv2.dilate(person_mask, kernel, iterations=step)
            if np.any(probe & vehicle_mask):
                return ContactResult(
                    touching=step == 1, mask_gap_pixels=3.0 * (step - 1)
                )
        return ContactResult(touching=False, mask_gap_pixels=3.0 * 10)

    async def check_contact(
        self,
        frame: bytes,
        subject_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> ContactResult | None:
        if not await self.ensure_ready():
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._check_sync, frame, subject_box, vehicle_box
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Contact segmentation failed: %s", exc)
            return None


def _build_contact_hint(result: ContactResult) -> str:
    if result.touching:
        body = "the person's and vehicle's precise segmented outlines appear to touch or overlap"
    else:
        body = (
            "the person's and vehicle's precise segmented outlines are "
            f"separated by roughly {result.mask_gap_pixels:.0f} pixels — not touching"
        )
    return (
        "\n\nCONTACT ANALYSIS: A pixel-level segmentation model found that "
        + body
        + ". Unlike a bounding-box overlap, this reflects the actual "
        "visible shape of each object, so it's a stronger signal for "
        "genuine physical contact — but still verify against what the "
        "frames actually show."
    )


# ----------------------------------------------------------------------
# Stage 5: local-only face recognition (facenet-pytorch)
# ----------------------------------------------------------------------


@dataclass
class FaceRecognitionResult:
    """Every distinct enrolled-member match found across a clip's sampled
    frames, plus whether any face present could not be matched to an
    *approved* enrollment.

    The latter is what gates the suspicious-flag bypass (see
    ``BaseAnalyzer._face_bypass_applies``) — a single unmatched or
    not-yet-approved face anywhere in the clip must block it, so a stranger
    standing next to a recognized family member still gets flagged.
    """

    approved_names: list[str] = field(default_factory=list)
    other_names: list[str] = field(default_factory=list)
    unrecognized_present: bool = False


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length embedding vectors.

    A length mismatch (e.g. an embedding-model change, or a corrupted/
    malformed stored row) returns 0.0 ("can't compare, no match") rather than
    silently comparing a truncated, meaningless subset of both vectors via
    ``zip`` — consistent with this module's general "err toward no-match"
    safety posture.
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class FaceEmbedder:
    """Face detection + embedding (facenet-pytorch: MTCNN + InceptionResnetV1).

    Entirely local — no network calls beyond the one-time pretrained-weight
    download facenet-pytorch performs itself on first use, cached under
    /data via TORCH_HOME (see the Dockerfile). Embeddings and any enrolled
    reference photos are stored in this add-on's own database, never sent
    to any cloud AI provider.
    """

    def __init__(self) -> None:
        self._mtcnn: Any = None
        self._resnet: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # _native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with _native_import_lock:
            from facenet_pytorch import (  # type: ignore[import-not-found]
                MTCNN,
                InceptionResnetV1,
            )

            _LOGGER.info("Loading local face-recognition models")
            self._mtcnn = MTCNN(keep_all=True)
            self._resnet = InceptionResnetV1(pretrained="vggface2").eval()
            _LOGGER.info("Face-recognition models ready")

    async def ensure_ready(self) -> bool:
        if self._resnet is not None:
            return True
        async with self._get_lock():
            if self._resnet is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                _LOGGER.warning(
                    "facenet_pytorch package is not installed, face "
                    "recognition unavailable: %s. Install it with: "
                    "pip install facenet-pytorch",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Face recognition unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load face-recognition models")
                return False

    def _embed_sync(self, frame: bytes) -> list[list[float]]:
        import torch  # type: ignore[import-not-found]
        from PIL import Image

        image = Image.open(io.BytesIO(frame)).convert("RGB")
        faces = self._mtcnn(image)
        if faces is None:
            return []
        if faces.dim() == 3:
            faces = faces.unsqueeze(0)
        with torch.no_grad():
            embeddings = self._resnet(faces)
        return [row.tolist() for row in embeddings]

    async def embed(self, frame: bytes) -> list[list[float]]:
        """Return one 512-dim embedding per detected face in *frame*."""
        if not await self.ensure_ready():
            return []
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._embed_sync, frame)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Face embedding failed: %s", exc)
            return []


class FaceRecognizer:
    """Matches embedded faces in a clip against locally enrolled household members."""

    def __init__(self, embedder: FaceEmbedder, db: ClipDatabase) -> None:
        self._embedder = embedder
        self._db = db

    async def recognize(self, frames: list[bytes]) -> FaceRecognitionResult:
        """Match every detected face across *frames* against enrollments.

        Every sampled frame's every detected face (MTCNN detects all faces
        per frame, not just one) is independently matched against its
        single best-scoring enrollment above ``_FACE_MATCH_THRESHOLD``, and
        bucketed by that enrollment's ``approved`` flag — or counted as
        unrecognized if nothing matched. This full accounting (not just a
        single best match) is what lets the caller tell "only known people
        present" apart from "a known person AND a stranger both appear,"
        which the suspicious-flag bypass depends on for safety.

        Guards the whole lookup like every other vision stage — a DB error
        or corrupted embedding row degrades to an empty ("nothing matched,
        nothing bypasses") result rather than propagating out of
        process_clip() and failing the entire clip, and never the reverse.
        """
        result = FaceRecognitionResult()
        try:
            enrollments = await self._db.list_face_enrollments()
            if not enrollments:
                return result

            approved: set[str] = set()
            other: set[str] = set()
            for frame in frames:
                embeddings = await self._embedder.embed(frame)
                for embedding in embeddings:
                    best_name: str | None = None
                    best_approved = False
                    best_similarity = _FACE_MATCH_THRESHOLD
                    for enrollment in enrollments:
                        similarity = cosine_similarity(
                            embedding, enrollment["embedding"]
                        )
                        if similarity >= best_similarity:
                            best_similarity = similarity
                            best_name = str(enrollment["name"])
                            best_approved = bool(enrollment.get("approved", True))
                    if best_name is None:
                        result.unrecognized_present = True
                    elif best_approved:
                        approved.add(best_name)
                    else:
                        other.add(best_name)
            result.approved_names = sorted(approved)
            result.other_names = sorted(other)
            return result
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Face recognition failed: %s", exc)
            return FaceRecognitionResult()


def _build_recognition_hint(result: FaceRecognitionResult) -> str | None:
    """Build a strictly name-free advisory hint for the AI prompt.

    Deliberately never includes the enrolled person's actual name — this
    text is sent to whichever AI provider is configured, including cloud
    providers, and a name is data derived from a local biometric match. Only
    a count/fact is shared; the name itself is only ever used later, purely
    locally (see BaseAnalyzer._personalize_summary), never in any prompt or
    outbound request.
    """
    if not result.approved_names:
        return None
    count = len(result.approved_names)
    member_word = "member" if count == 1 else "members"
    hint = (
        f"\n\nRECOGNIZED RESIDENT(S): {count} locally-enrolled household "
        f"{member_word} matched on-device (name withheld from AI analysis, "
        "never sent to any AI provider). This is a strong signal that this "
        "is routine activity by someone who lives here, not a stranger — "
        "but still judge any concerning behavior (tampering, forced entry) "
        "on its own merits regardless of who is present."
    )
    if result.other_names or result.unrecognized_present:
        hint += (
            " NOTE: at least one additional face in this clip did NOT match "
            "an approved household member — do not assume everyone present "
            "is a known resident."
        )
    return hint


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


@dataclass
class VisionConfig:
    """Which optional computer-vision stages are enabled (see config.py).

    ``enhanced_detection_enabled`` gates frame preprocessing, object
    detection/tracking, depth estimation, and contact segmentation together
    — depth/segmentation have always required detection to run at all, so
    these four stages are one on/off setting rather than four independently
    toggleable ones. Face recognition remains separate since it's a
    privacy-sensitive, not just heavier-compute, feature.
    """

    enhanced_detection_enabled: bool = False
    object_detection_model: str = "yolo11n.pt"
    face_recognition_enabled: bool = False


@dataclass
class VisionHints:
    """Per-clip output of :meth:`VisionPipeline.process_clip`."""

    enhanced_frames: list[bytes] | None = None
    detection_hint: str | None = None
    tracking_hint: str | None = None
    depth_hint: str | None = None
    contact_hint: str | None = None
    recognized_resident_hint: str | None = None
    face_recognition: FaceRecognitionResult | None = None


class VisionPipeline:
    """Orchestrates the optional computer-vision stages for one clip.

    Each stage is independently toggleable via *config* and gracefully
    no-ops (leaving the corresponding hint unset) if its dependency isn't
    installed, fails to load, or simply finds nothing relevant — analysis
    always proceeds using whatever hints ended up available, exactly like
    the existing scene-baseline/zone-motion hints in ``analyzer.py``.
    """

    def __init__(self, config: VisionConfig, db: ClipDatabase | None = None) -> None:
        self._config = config
        self._db = db
        self._detector = ObjectDetector(config.object_detection_model)
        self._depth = DepthEstimator()
        self._segmenter = ContactSegmenter()
        self._face_embedder = FaceEmbedder()

    def update_config(self, config: VisionConfig) -> None:
        """Replace the active config at runtime (e.g. after an options reload)."""
        if config.object_detection_model != self._config.object_detection_model:
            self._detector = ObjectDetector(config.object_detection_model)
        self._config = config

    async def process_clip(
        self,
        frames: list[bytes],
        car_description: str = "",
        car_protection_applies: bool = False,
        face_recognition_frames: list[bytes] | None = None,
    ) -> VisionHints:
        """Run every enabled stage against *frames* and return the resulting hints.

        *car_protection_applies* must reflect whether protected-vehicle
        rules apply to *this specific camera* (see
        BaseAnalyzer._car_protection_applies) — not merely whether a
        protected vehicle is described somewhere on the property. Vehicle
        distance/depth/contact analysis is skipped entirely when False, so
        a camera that doesn't view the protected vehicle never generates
        vehicle-proximity hints just because it happened to detect an
        unrelated car and a person in frame.

        *face_recognition_frames*, when given, is used for face recognition
        instead of *frames* — see the comment where it's consumed below for
        why recognition needs a wider frame pool than the rest of this
        pipeline.
        """
        hints = VisionHints()
        if not frames:
            return hints

        # Face recognition must always match against raw (pre-enhancement)
        # frames, since enrolled reference embeddings are computed from raw
        # reference photos — matching enhanced frames against raw references
        # is an embedding-space mismatch that can cause real matches (approved
        # household members) to be missed. Captured before `frames` is
        # potentially reassigned to CLAHE-enhanced frames below.
        #
        # Prefer face_recognition_frames (the full pre-down-selection extraction
        # pool) over frames (the handful already down-selected for the AI
        # prompt) when the caller supplies it. The down-selection in
        # analyzer.py picks frames by motion (entry/peak/exit) to narrate the
        # clip for the AI model — but a person can stand still while looking
        # straight at the camera, which is exactly a *low*-motion moment, so
        # that selection is liable to drop the one frame with the clearest,
        # most front-on view of a face. Recognition doesn't need a narrative
        # sequence; it just needs the best chance of seeing every face that
        # appears anywhere in the clip.
        raw_frames = face_recognition_frames if face_recognition_frames else frames

        if self._config.enhanced_detection_enabled:
            frames = FrameEnhancer.enhance(frames)
            hints.enhanced_frames = frames

            detections = await self._detector.detect(frames)
            if detections:
                # Vehicle-distance language (and the depth/contact stages
                # below) is only relevant on a camera actually designated to
                # view the protected vehicle — car_protection_applies is
                # False for any other camera even when a protected vehicle
                # is described elsewhere on the property, so those cameras
                # stay properly isolated (see BaseAnalyzer._car_protection_applies).
                hints.detection_hint = _build_detection_hint(
                    detections, car_description if car_protection_applies else ""
                )
                hints.tracking_hint = _build_tracking_hint(detections, len(frames))

            pair = (
                _best_subject_vehicle_pair(detections)
                if detections and car_protection_applies
                else None
            )

            if pair:
                subject, vehicle, frame_idx = pair
                depth_result = await self._depth.compare(
                    frames[frame_idx], subject.box, vehicle.box
                )
                if depth_result is not None:
                    hints.depth_hint = _build_depth_hint(depth_result)

                contact_result = await self._segmenter.check_contact(
                    frames[frame_idx], subject.box, vehicle.box
                )
                if contact_result is not None:
                    hints.contact_hint = _build_contact_hint(contact_result)

        if self._config.face_recognition_enabled and self._db is not None:
            recognizer = FaceRecognizer(self._face_embedder, self._db)
            face_result = await recognizer.recognize(raw_frames)
            hints.face_recognition = face_result
            hints.recognized_resident_hint = _build_recognition_hint(face_result)

        # The only per-clip evidence any of this ran was the one-time
        # "model ready" INFO log each stage prints on its first load —
        # after that, every stage below runs (and produces real hints that
        # do reach the AI prompt) completely silently, even at debug level.
        # Names are deliberately never logged here, matching
        # _build_recognition_hint's own name-free prompt guarantee — only
        # counts, mirroring what the prompt itself is allowed to say.
        _LOGGER.debug(
            "Vision pipeline result: enhanced_detection=%s "
            "(detection=%r, tracking=%r, depth=%r, contact=%r), "
            "face_recognition=%s (approved=%d, other=%d, unrecognized_present=%s)",
            self._config.enhanced_detection_enabled,
            hints.detection_hint,
            hints.tracking_hint,
            hints.depth_hint,
            hints.contact_hint,
            self._config.face_recognition_enabled,
            len(hints.face_recognition.approved_names) if hints.face_recognition else 0,
            len(hints.face_recognition.other_names) if hints.face_recognition else 0,
            hints.face_recognition.unrecognized_present
            if hints.face_recognition
            else False,
        )

        return hints
