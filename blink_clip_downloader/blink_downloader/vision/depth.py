"""Stage 3: monocular depth estimation (Depth Anything V2).

Answers the question a bounding box cannot: whether two things that
overlap in the 2D frame are actually at the same distance from the camera,
or merely one behind the other. Depth Anything V2 returns *relative* depth,
so "similar depth" is judged as a fraction of the clip's own depth range
rather than in any absolute unit.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any

from . import runtime

_LOGGER = logging.getLogger(__name__)


# Depth Anything V2 output is a *relative* (not metric) depth/disparity map,
# so "similar depth" is judged as a fraction of this clip's own depth range
# rather than an absolute unit. Below this fraction, two regions are treated
# as being at roughly the same distance from the camera.
_DEPTH_SIMILARITY_FRACTION = 0.15


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

    # Default/fallback checkpoint — see ai_depth_estimation_model in
    # config.yaml for the other selectable sizes (and their licensing:
    # this default is Apache-2.0, the larger Base/Large options are
    # CC-BY-NC-4.0/non-commercial — see that option's own comment).
    _DEFAULT_MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"

    # Empty default means no token configured; it is not a credential.
    def __init__(
        self,
        hf_token: str = "",
        model_id: str = _DEFAULT_MODEL_ID,  # nosec B107
    ) -> None:
        self._pipe: Any = None
        self._hf_token = hf_token
        # Named apart from the class-level default above rather than
        # shadowing it in a different case: one is the fallback checkpoint,
        # the other is whichever checkpoint this instance was actually told
        # to load, and a reader glancing at `_MODEL_ID` vs `_model_id` has
        # no way to tell which is which.
        self._model_id = model_id
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not runtime.torch_cpu_compatible():
            raise runtime.CPUIncompatibleError(runtime._CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # runtime._native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with runtime._native_import_lock:
            from transformers import pipeline  # type: ignore[import-not-found]

            _LOGGER.info("Loading depth-estimation model '%s'", self._model_id)
            self._pipe = pipeline(
                task="depth-estimation",
                model=self._model_id,
                device="cpu",
                token=self._hf_token or None,
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
            except runtime.CPUIncompatibleError as exc:
                _LOGGER.warning("Depth estimation unavailable: %s", exc)
                return False
            except Exception as exc:
                if runtime._is_huggingface_auth_error(exc):
                    _LOGGER.error(runtime._HF_AUTH_FAILURE_MESSAGE, "depth-estimation")
                else:
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
        async with runtime._cv_slot():
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


def _build_depth_hint(result: DepthComparison, subject_label: str) -> str:
    """Render a depth comparison into a DEPTH ESTIMATE prompt hint.

    *subject_label* is the actual detected class ("person", "dog", "cat",
    ...) from :class:`DetectedObject` — named explicitly rather than the
    generic "person/animal" this used to say unconditionally, since the
    depth/contact pairing already treats animals as valid subjects (see
    :data:`_SUBJECT_CLASSES`) and the hint text should match.
    """
    if result.similar_depth:
        body = (
            f"the detected {subject_label} and detected vehicle appear to be "
            "at roughly the same distance from the camera — consistent "
            "with them actually being near the vehicle in 3D space, not "
            "just overlapping it in the 2D frame"
        )
    else:
        # Depth Anything's output is inverse depth/disparity (verified
        # against the installed transformers pipeline source, whose
        # postprocess() min-max-normalizes predicted_depth without
        # inverting it, plus the model's own documented convention): a
        # LARGER region value means CLOSER to the camera. subject_depth >
        # vehicle_depth therefore means the subject is nearer the camera
        # than the vehicle (the near/same side, in plain view); the
        # reverse means the subject is farther away than the vehicle from
        # the camera's viewpoint — consistent with standing on the
        # vehicle's far side, partly hidden behind it from this camera's
        # angle, which is worth flagging as extra-scrutiny-worthy on its
        # own, distinct from simple overlap-in-2D ambiguity.
        if result.subject_depth > result.vehicle_depth:
            side = (
                f"the {subject_label} appears nearer to the camera than the "
                "vehicle — the near/same side, in plain view"
            )
        else:
            side = (
                f"the {subject_label} appears farther from the camera than "
                "the vehicle — consistent with being on the vehicle's far "
                "side, partly out of this camera's clear view, which "
                "deserves extra scrutiny"
            )
        body = (
            f"the detected {subject_label} and detected vehicle appear to be "
            f"at noticeably different distances from the camera: {side}. "
            "They may only appear close together because one is in front "
            "of the other from this camera's angle, not because they're "
            "actually near the vehicle"
        )
    return (
        "\n\nDEPTH ESTIMATE: A monocular depth model estimates that "
        + body
        + ". This is a relative, best-effort estimate (not a precise "
        "measurement) — weigh it alongside what you can see in the frames."
    )
