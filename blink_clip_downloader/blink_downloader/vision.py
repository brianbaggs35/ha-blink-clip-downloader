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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)

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
# Availability checks (mirrors analyzer.is_moondream_installed())
# ----------------------------------------------------------------------


def is_opencv_available() -> bool:
    """Return True if the opencv (cv2) package is importable."""
    try:
        __import__("cv2")
        return True
    except ImportError:
        return False


def is_object_detection_available() -> bool:
    """Return True if the ultralytics package is importable."""
    try:
        __import__("ultralytics")
        return True
    except ImportError:
        return False


def is_depth_estimation_available() -> bool:
    """Return True if the transformers package is importable."""
    try:
        __import__("transformers")
        return True
    except ImportError:
        return False


def is_segmentation_available() -> bool:
    """Return True if the transformers package is importable (SAM2 support)."""
    return is_depth_estimation_available()


def is_face_recognition_available() -> bool:
    """Return True if the facenet_pytorch package is importable."""
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
    dependency, so it's gated by ``ai_cv_preprocessing_enabled`` like every
    other stage here rather than always running.
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
            import cv2  # noqa: PLC0415  # type: ignore[import-not-found]
            import numpy as np  # noqa: PLC0415
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
            except Exception:  # noqa: BLE001
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

    The model is loaded once (and downloaded on first use) and reused
    across clips — reloading it per clip would make every analysis pay a
    multi-second cold-start cost. Inference runs in a thread executor so
    the asyncio event loop is never blocked, mirroring
    ``MoondreamLocalAnalyzer``'s pattern in ``analyzer.py`` for the same
    reason.

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
        from ultralytics import YOLO  # noqa: PLC0415  # type: ignore[import-not-found]

        _LOGGER.info("Loading YOLO object-detection model '%s'", self._model_name)
        self._model = YOLO(self._model_name)
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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to load YOLO model: %s", exc)
                return False

    def _detect_sync(self, frames: list[bytes]) -> list[DetectedObject]:
        import cv2  # noqa: PLC0415  # type: ignore[import-not-found]
        import numpy as np  # noqa: PLC0415

        detections: list[DetectedObject] = []
        for idx, frame in enumerate(frames):
            arr = np.frombuffer(frame, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            results = self._model.track(
                img, persist=True, tracker="bytetrack.yaml", verbose=False
            )
            if not results:
                continue
            result = results[0]
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                continue
            names = result.names
            ids = boxes.id
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
    if dx == 0.0 and dy == 0.0:
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


def _best_person_vehicle_pair(
    detections: list[DetectedObject],
) -> tuple[DetectedObject, DetectedObject, int] | None:
    """Return the (person, vehicle, frame_index) pair with the smallest gap.

    Used to pick which single frame/box-pair the heavier depth and contact
    stages spend their budget refining. None if no sampled frame contains
    both a person and a vehicle detection.
    """
    best: tuple[DetectedObject, DetectedObject, int] | None = None
    best_gap: float | None = None
    by_frame: dict[int, list[DetectedObject]] = {}
    for d in detections:
        by_frame.setdefault(d.frame_index, []).append(d)
    for frame_idx, items in by_frame.items():
        people = [d for d in items if d.label == "person"]
        vehicles = [d for d in items if d.label in _VEHICLE_CLASSES]
        for p in people:
            for v in vehicles:
                gap = _box_gap(p.box, v.box)
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best = (p, v, frame_idx)
    return best


def _build_detection_hint(
    detections: list[DetectedObject], car_description: str
) -> str | None:
    """Render detections into an OBJECT DETECTION prompt hint, or None if empty."""
    if not detections:
        return None
    labels = sorted({d.label for d in detections})
    lines = [f"Detected object classes across sampled frames: {', '.join(labels)}."]

    pair = _best_person_vehicle_pair(detections) if car_description else None
    if pair is not None:
        person, vehicle, _ = pair
        vehicle_width = vehicle.box[2] - vehicle.box[0]
        gap = _box_gap(person.box, vehicle.box)
        proximity = _proximity_label(gap, vehicle_width)
        lines.append(
            "Object-detection distance estimate: the detected person's "
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


# ----------------------------------------------------------------------
# Stage 3: depth estimation (Depth Anything V2 via transformers)
# ----------------------------------------------------------------------


@dataclass
class DepthComparison:
    """Relative-depth comparison between two detected regions in one frame."""

    similar_depth: bool
    person_depth: float
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
        from transformers import pipeline  # noqa: PLC0415  # type: ignore[import-not-found]

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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to load depth-estimation model: %s", exc)
                return False

    def _compare_sync(
        self,
        frame: bytes,
        person_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> DepthComparison | None:
        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

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

        person_depth = region_mean(person_box)
        vehicle_depth = region_mean(vehicle_box)
        if person_depth is None or vehicle_depth is None:
            return None

        depth_range = max(float(depth_arr.max() - depth_arr.min()), 1e-6)
        normalized_diff = abs(person_depth - vehicle_depth) / depth_range
        return DepthComparison(
            similar_depth=normalized_diff < _DEPTH_SIMILARITY_FRACTION,
            person_depth=person_depth,
            vehicle_depth=vehicle_depth,
        )

    async def compare(
        self,
        frame: bytes,
        person_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> DepthComparison | None:
        if not await self.ensure_ready():
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._compare_sync, frame, person_box, vehicle_box
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Depth estimation failed: %s", exc)
            return None


def _build_depth_hint(result: DepthComparison) -> str:
    if result.similar_depth:
        body = (
            "the detected person and detected vehicle appear to be at "
            "roughly the same distance from the camera — consistent with "
            "the person actually being near the vehicle in 3D space, not "
            "just overlapping it in the 2D frame"
        )
    else:
        body = (
            "the detected person and detected vehicle appear to be at "
            "noticeably different distances from the camera — they may "
            "only appear close together because one is in front of the "
            "other from this camera's angle, not because the person is "
            "actually near the vehicle"
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
        from transformers import (  # noqa: PLC0415  # type: ignore[import-not-found]
            Sam2Model,
            Sam2Processor,
        )

        _LOGGER.info("Loading SAM2 segmentation model '%s'", self._MODEL_ID)
        self._model = Sam2Model.from_pretrained(self._MODEL_ID)
        self._processor = Sam2Processor.from_pretrained(self._MODEL_ID)
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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to load SAM2 model: %s", exc)
                return False

    def _check_sync(
        self,
        frame: bytes,
        person_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> ContactResult | None:
        import cv2  # noqa: PLC0415  # type: ignore[import-not-found]
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        image = Image.open(io.BytesIO(frame)).convert("RGB")
        input_boxes = [[list(person_box), list(vehicle_box)]]
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
        # expensive nearest-point search across every mask pixel pair.
        for step in range(1, 11):
            probe = cv2.dilate(person_mask, kernel, iterations=step)
            if np.any(probe & vehicle_mask):
                return ContactResult(
                    touching=step == 1, mask_gap_pixels=7.0 * (step - 1)
                )
        return ContactResult(touching=False, mask_gap_pixels=7.0 * 10)

    async def check_contact(
        self,
        frame: bytes,
        person_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> ContactResult | None:
        if not await self.ensure_ready():
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._check_sync, frame, person_box, vehicle_box
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
class RecognizedFace:
    """A detected face matched against a locally enrolled household member."""

    name: str
    similarity: float


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length embedding vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
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
        from facenet_pytorch import (  # noqa: PLC0415  # type: ignore[import-not-found]
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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Failed to load face-recognition models: %s", exc)
                return False

    def _embed_sync(self, frame: bytes) -> list[list[float]]:
        import torch  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

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

    async def recognize(self, frames: list[bytes]) -> RecognizedFace | None:
        """Return the best enrolled-member match across *frames*, if any."""
        enrollments = await self._db.list_face_enrollments()
        if not enrollments:
            return None

        best: RecognizedFace | None = None
        for frame in frames:
            embeddings = await self._embedder.embed(frame)
            for embedding in embeddings:
                for enrollment in enrollments:
                    similarity = cosine_similarity(embedding, enrollment["embedding"])
                    if similarity >= _FACE_MATCH_THRESHOLD and (
                        best is None or similarity > best.similarity
                    ):
                        best = RecognizedFace(
                            name=str(enrollment["name"]), similarity=similarity
                        )
        return best


def _build_recognition_hint(match: RecognizedFace) -> str:
    return (
        f"\n\nRECOGNIZED RESIDENT: A face in this clip matches the enrolled "
        f'household member "{match.name}" (local on-device match, '
        f"similarity {match.similarity:.0%}). This is a strong signal that "
        "this is routine activity by someone who lives here, not a "
        "stranger — but still judge any concerning behavior (tampering, "
        "forced entry) on its own merits regardless of who is present."
    )


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


@dataclass
class VisionConfig:
    """Which optional computer-vision stages are enabled (see config.py)."""

    cv_preprocessing_enabled: bool = False
    object_detection_enabled: bool = False
    object_detection_model: str = "yolo11n.pt"
    depth_estimation_enabled: bool = False
    segmentation_enabled: bool = False
    face_recognition_enabled: bool = False


@dataclass
class VisionHints:
    """Per-clip output of :meth:`VisionPipeline.process_clip`."""

    enhanced_frames: list[bytes] | None = None
    detection_hint: str | None = None
    depth_hint: str | None = None
    contact_hint: str | None = None
    recognized_resident_hint: str | None = None


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
        self, frames: list[bytes], car_description: str = ""
    ) -> VisionHints:
        """Run every enabled stage against *frames* and return the resulting hints."""
        hints = VisionHints()
        if not frames:
            return hints

        if self._config.cv_preprocessing_enabled:
            frames = FrameEnhancer.enhance(frames)
            hints.enhanced_frames = frames

        detections: list[DetectedObject] | None = None
        if self._config.object_detection_enabled:
            detections = await self._detector.detect(frames)
            if detections:
                hints.detection_hint = _build_detection_hint(
                    detections, car_description
                )

        pair = _best_person_vehicle_pair(detections) if detections else None

        if pair and self._config.depth_estimation_enabled:
            person, vehicle, frame_idx = pair
            depth_result = await self._depth.compare(
                frames[frame_idx], person.box, vehicle.box
            )
            if depth_result is not None:
                hints.depth_hint = _build_depth_hint(depth_result)

        if pair and self._config.segmentation_enabled:
            person, vehicle, frame_idx = pair
            contact_result = await self._segmenter.check_contact(
                frames[frame_idx], person.box, vehicle.box
            )
            if contact_result is not None:
                hints.contact_hint = _build_contact_hint(contact_result)

        if self._config.face_recognition_enabled and self._db is not None:
            recognizer = FaceRecognizer(self._face_embedder, self._db)
            match = await recognizer.recognize(frames)
            if match is not None:
                hints.recognized_resident_hint = _build_recognition_hint(match)

        return hints
