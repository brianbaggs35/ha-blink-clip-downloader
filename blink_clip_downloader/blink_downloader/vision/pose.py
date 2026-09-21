"""Stage 5: pose estimation (Ultralytics YOLO-pose).

Body posture as evidence: reaching toward something, an arm raised,
crouching. Keypoints are read straight off the pose model and turned into
a short phrase — the interpretation lives in the small pure functions
below rather than in the model wrapper, so the thresholds that decide
"reaching" from "standing" can be read and tested without a GPU.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from ..security import (
    Box,
    box_iou,
)
from . import runtime

_LOGGER = logging.getLogger(__name__)


# COCO 17-keypoint indices, as emitted by every YOLO pose model.
_KP_LEFT_SHOULDER = 5


_KP_RIGHT_SHOULDER = 6


_KP_LEFT_WRIST = 9


_KP_RIGHT_WRIST = 10


_KP_LEFT_HIP = 11


_KP_RIGHT_HIP = 12


_KP_LEFT_ANKLE = 15


_KP_RIGHT_ANKLE = 16


# Minimum per-keypoint confidence before a joint is used at all. A pose
# model will happily place a wrist it cannot actually see, and a
# hallucinated wrist is exactly what would turn someone standing with their
# arms down into "reaching toward the vehicle".
_KEYPOINT_CONFIDENCE = 0.5


# Overlap a pose model's own person box must have with the tracked subject's
# before its skeleton is attributed to them.
_POSE_MATCH_IOU = 0.3


# How far past the shoulder, as a fraction of shoulder width, a wrist must
# extend toward the asset before it counts as reaching for it rather than
# simply hanging at the subject's side.
_REACH_SHOULDER_FRACTION = 0.6


# Height a wrist must clear its own shoulder by, as a fraction of the
# subject's box height, to read as a raised arm.
_ARM_RAISED_FRACTION = 0.06


# Share of the subject's box height the hips must sit within of the ankles
# before the pose reads as crouched rather than standing.
_CROUCH_FRACTION = 0.33


@dataclass
class PostureResult:
    """What a subject's body was doing at the moment that mattered.

    Three narrow, scale-free facts rather than an action label. Naming an
    action ("kicking", "prying") from a single sparse frame is a claim this
    pipeline cannot support; "a wrist is extended toward the vehicle" is one
    it can, and it is the part that actually changes how concerning the
    moment is.
    """

    reaching: bool = False
    arm_raised: bool = False
    crouching: bool = False
    confidence: float = 0.0

    @property
    def any_posture(self) -> bool:
        """True when at least one posture was established."""
        return self.reaching or self.arm_raised or self.crouching

    def describe(self) -> str:
        """A short phrase naming whatever was established, for prompts."""
        parts = []
        if self.reaching:
            parts.append("an arm extended toward it")
        if self.arm_raised:
            parts.append("an arm raised above shoulder height")
        if self.crouching:
            parts.append("a crouched or bent-over posture")
        return " and ".join(parts)


class PoseEstimator:
    """Body-keypoint estimation for the subject nearest a protected asset.

    Runs on exactly one frame per clip — the moment the depth and contact
    stages already examine — because that is the moment whose meaning
    changes: standing two feet from a car with your arms at your sides and
    standing two feet from it with an arm extended into the window are
    indistinguishable from a bounding box and obvious from a skeleton.

    A separate, smaller model from the detector's: pose weights are their
    own checkpoint, and making this its own toggle keeps anyone who just
    wants object detection from paying for a download they will not use.
    """

    def __init__(self, model_name: str = "yolo26n-pose.pt") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not runtime.torch_cpu_compatible():
            raise runtime.CPUIncompatibleError(runtime._CPU_INCOMPATIBLE_MESSAGE)
        os.environ.setdefault("YOLO_CONFIG_DIR", runtime._YOLO_MODEL_CACHE_DIR)
        # Whole body under the shared lock, not just the import — see
        # runtime._native_import_lock's comment and ObjectDetector._load_sync.
        with runtime._native_import_lock:
            from ultralytics import YOLO  # type: ignore[import-not-found]

            model_path = self._model_name
            if os.path.basename(model_path) == model_path:
                os.makedirs(runtime._YOLO_MODEL_CACHE_DIR, exist_ok=True)
                model_path = os.path.join(runtime._YOLO_MODEL_CACHE_DIR, model_path)
            _LOGGER.info("Loading YOLO pose model '%s'", self._model_name)
            self._model = YOLO(model_path)
            _LOGGER.info("YOLO pose model '%s' ready", self._model_name)

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
                    "ultralytics package is not installed, pose estimation "
                    "unavailable: %s. Install it with: pip install ultralytics",
                    exc,
                )
                return False
            except runtime.CPUIncompatibleError as exc:
                _LOGGER.warning("Pose estimation unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load YOLO pose model")
                return False

    def _analyze_sync(
        self, frame: bytes, subject_box: Box, asset_box: Box
    ) -> PostureResult | None:
        with runtime._native_import_lock:
            import cv2  # type: ignore[import-not-found]
            import numpy as np

        img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        results = self._model(img, verbose=False)
        if not results:
            return None
        keypoints = _best_pose_keypoints(results[0], subject_box)
        if keypoints is None:
            return None
        return _posture_from_keypoints(keypoints, subject_box, asset_box)

    async def analyze(
        self, frame: bytes, subject_box: Box, asset_box: Box
    ) -> PostureResult | None:
        """Return what *subject_box*'s occupant was doing, or None if unavailable."""
        async with runtime._cv_slot():
            if not await self.ensure_ready():
                return None
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._analyze_sync, frame, subject_box, asset_box
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Pose estimation failed: %s", exc)
                return None


def _best_pose_keypoints(
    result: Any, subject_box: Box
) -> list[tuple[float, float, float]] | None:
    """Pick the detected skeleton belonging to *subject_box*.

    A pose model finds every person in frame; attributing a bystander's
    raised arm to the person at the car would be worse than reporting
    nothing at all, so the match is by box overlap and a non-overlapping
    best candidate is discarded.
    """
    keypoints = getattr(result, "keypoints", None)
    boxes = getattr(result, "boxes", None)
    if keypoints is None or boxes is None or len(boxes) == 0:
        return None

    best_index = -1
    best_iou = 0.0
    for i in range(len(boxes)):
        candidate = tuple(float(v) for v in boxes.xyxy[i])
        overlap = box_iou(
            (candidate[0], candidate[1], candidate[2], candidate[3]), subject_box
        )
        if overlap > best_iou:
            best_iou, best_index = overlap, i
    if best_index < 0 or best_iou < _POSE_MATCH_IOU:
        return None

    data = keypoints.data[best_index]
    return [(float(p[0]), float(p[1]), float(p[2])) for p in data]


def _keypoint(
    keypoints: list[tuple[float, float, float]], index: int
) -> tuple[float, float] | None:
    """Return a confidently-placed keypoint, or None."""
    if index >= len(keypoints):
        return None
    x, y, confidence = keypoints[index]
    return (x, y) if confidence >= _KEYPOINT_CONFIDENCE else None


def _posture_from_keypoints(
    keypoints: list[tuple[float, float, float]], subject_box: Box, asset_box: Box
) -> PostureResult:
    """Derive the three posture facts from one skeleton.

    Everything is measured against the subject's own box, so the same
    posture reads identically whether they fill the frame or a fifth of it.
    """
    height = subject_box[3] - subject_box[1]
    if height <= 0:
        return PostureResult()

    shoulders = [
        _keypoint(keypoints, _KP_LEFT_SHOULDER),
        _keypoint(keypoints, _KP_RIGHT_SHOULDER),
    ]
    wrists = [
        _keypoint(keypoints, _KP_LEFT_WRIST),
        _keypoint(keypoints, _KP_RIGHT_WRIST),
    ]
    hips = [_keypoint(keypoints, _KP_LEFT_HIP), _keypoint(keypoints, _KP_RIGHT_HIP)]
    ankles = [
        _keypoint(keypoints, _KP_LEFT_ANKLE),
        _keypoint(keypoints, _KP_RIGHT_ANKLE),
    ]

    result = PostureResult(confidence=_pose_confidence(keypoints))
    known_shoulders = [s for s in shoulders if s is not None]
    known_wrists = [w for w in wrists if w is not None]

    if known_shoulders and known_wrists:
        shoulder_y = min(s[1] for s in known_shoulders)
        result.arm_raised = any(
            w[1] < shoulder_y - _ARM_RAISED_FRACTION * height for w in known_wrists
        )

    if len(known_shoulders) == 2 and known_wrists:
        result.reaching = _is_reaching(known_shoulders, known_wrists, asset_box)

    known_hips = [h for h in hips if h is not None]
    known_ankles = [a for a in ankles if a is not None]
    if known_hips and known_ankles:
        drop = min(a[1] for a in known_ankles) - max(h[1] for h in known_hips)
        result.crouching = drop < _CROUCH_FRACTION * height

    return result


def _is_reaching(
    shoulders: list[tuple[float, float]],
    wrists: list[tuple[float, float]],
    asset_box: Box,
) -> bool:
    """True when a wrist extends past the torso toward *asset_box*.

    Horizontal only, and measured relative to shoulder width: an arm at rest
    hangs within the body's own outline, while one reaching for a door
    handle or a window plainly does not. Depth is unavailable here, so a
    subject reaching directly away from the camera is missed — a false
    negative, which is the right direction for this to fail in.
    """
    shoulder_width = abs(shoulders[0][0] - shoulders[1][0])
    if shoulder_width <= 0:
        return False
    torso_x = (shoulders[0][0] + shoulders[1][0]) / 2.0
    asset_x = (asset_box[0] + asset_box[2]) / 2.0
    direction = 1.0 if asset_x > torso_x else -1.0
    threshold = _REACH_SHOULDER_FRACTION * shoulder_width
    return any((w[0] - torso_x) * direction > threshold for w in wrists)


def _pose_confidence(keypoints: list[tuple[float, float, float]]) -> float:
    """Mean confidence of the keypoints that were placed at all."""
    placed = [p[2] for p in keypoints if p[2] >= _KEYPOINT_CONFIDENCE]
    return sum(placed) / len(placed) if placed else 0.0


def _build_posture_hint(result: PostureResult, subject_label: str) -> str | None:
    """Render a posture result into a POSTURE prompt hint, or None."""
    if not result.any_posture:
        return None
    return (
        f"\n\nPOSTURE: Body-keypoint estimation places the detected {subject_label} "
        f"with {result.describe()} at the closest moment. This is derived from "
        "estimated joint positions in a single frame, not from watching the "
        "movement, so treat it as a hint about what the body was doing and "
        "confirm it against the frames."
    )
