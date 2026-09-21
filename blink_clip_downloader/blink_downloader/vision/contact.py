"""Stage 4: contact segmentation (SAM2).

Refines "these boxes overlap" into "these pixels touch". A box overlap is
the cheapest possible proxy for contact and a poor one — a person walking
in front of a parked car overlaps it completely — so this stage segments
both regions and asks whether the masks themselves meet.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Any

from . import runtime

_LOGGER = logging.getLogger(__name__)


@dataclass
class ContactResult:
    """Pixel-level segmentation contact judgment between two detected regions."""

    touching: bool
    mask_gap_pixels: float


class ContactSegmenter:
    """Precise pixel-level contact detection (SAM2) via transformers.

    Requires object detection to also be enabled. Given a subject (person
    or animal) box and a vehicle box already found by
    :class:`ObjectDetector`, this segments
    both objects' actual visible outlines (not just their rectangular
    bounding boxes) and checks whether those outlines touch — a much
    stronger signal for genuine physical contact than a bounding-box
    overlap, which can trigger just because one object is in front of the
    other from the camera's angle. The heaviest of the five stages in this
    module.
    """

    _MODEL_ID = "facebook/sam2.1-hiera-tiny"

    # Empty default means no token configured; it is not a credential.
    def __init__(self, hf_token: str = "") -> None:  # nosec B107
        self._model: Any = None
        self._processor: Any = None
        self._hf_token = hf_token
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _build_config(self, config_class: Any) -> Any:
        """Build the SAM2 model config from the checkpoint, with the legacy
        RoPE key rewritten into its modern form.

        ``facebook/sam2.1-hiera-tiny``'s published ``config.json`` still
        carries ``memory_attention_rope_theta``. transformers 5.x only still
        accepts it through a deprecation shim that logs a warning on every
        load and is documented as going away, at which point the value would
        be silently dropped instead. Reading the raw config dict and moving
        that value into ``rope_parameters["rope_theta"]`` here — where
        transformers reads it from now — produces a config identical to the
        one ``from_pretrained`` builds itself (verified by comparing
        ``to_dict()`` output both ways) without ever touching the deprecated
        attribute.

        Delete this once the checkpoint's own ``config.json`` is republished
        with ``rope_parameters``; it is a no-op for a config that already has
        one.
        """
        config_dict, _ = config_class.get_config_dict(
            self._MODEL_ID, token=self._hf_token or None
        )
        theta = config_dict.pop("memory_attention_rope_theta", None)
        if theta is not None:
            rope_parameters = dict(config_dict.get("rope_parameters") or {})
            rope_parameters.setdefault("rope_theta", theta)
            config_dict["rope_parameters"] = rope_parameters
        return config_class(**config_dict)

    def _load_sync(self) -> None:
        if not runtime.torch_cpu_compatible():
            raise runtime.CPUIncompatibleError(runtime._CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # runtime._native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with runtime._native_import_lock:
            from transformers import (  # type: ignore[import-not-found]
                Sam2VideoConfig,
                Sam2VideoModel,
                Sam2VideoProcessor,
            )

            _LOGGER.info("Loading SAM2 segmentation model '%s'", self._MODEL_ID)
            # _MODEL_ID is a fixed constant for an official facebook/ repo;
            # this optional pipeline trusts the HF hub the same way the rest
            # of the CV stack trusts PyPI (B615).
            self._model = Sam2VideoModel.from_pretrained(  # nosec B615
                self._MODEL_ID,
                config=self._build_config(Sam2VideoConfig),
                token=self._hf_token or None,
            )
            self._processor = Sam2VideoProcessor.from_pretrained(  # nosec B615
                self._MODEL_ID, token=self._hf_token or None
            )
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
            except runtime.CPUIncompatibleError as exc:
                _LOGGER.warning("Contact segmentation unavailable: %s", exc)
                return False
            except Exception as exc:
                if runtime._is_huggingface_auth_error(exc):
                    _LOGGER.error(runtime._HF_AUTH_FAILURE_MESSAGE, "SAM2")
                else:
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
        inference_session = self._processor.init_video_session(
            video=[image], inference_device="cpu"
        )
        self._processor.add_inputs_to_inference_session(
            inference_session=inference_session,
            frame_idx=0,
            obj_ids=[0, 1],
            input_boxes=[[list(subject_box), list(vehicle_box)]],
        )
        with torch.no_grad():
            outputs = self._model(inference_session=inference_session, frame_idx=0)
        masks = self._processor.post_process_masks(
            [outputs.pred_masks.cpu()],
            [(image.height, image.width)],
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
        async with runtime._cv_slot():
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


def _build_contact_hint(result: ContactResult, subject_label: str) -> str:
    """Render a contact-segmentation result into a CONTACT ANALYSIS prompt
    hint. *subject_label* is the actual detected class ("person", "dog",
    "cat", ...) — see :func:`_build_depth_hint`'s docstring for why."""
    if result.touching:
        body = f"the {subject_label}'s and vehicle's precise segmented outlines appear to touch or overlap"
    else:
        body = (
            f"the {subject_label}'s and vehicle's precise segmented outlines are "
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
