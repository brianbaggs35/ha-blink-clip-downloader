"""Stage 1: OpenCV preprocessing (CLAHE contrast + denoising).

Off by default, and deliberately so: enhanced frames measurably *hurt* YOLO
on this content — a house read as a truck, phantom people and cats — so
this stage feeds the AI provider's own eyes, not the detector's.
"""

from __future__ import annotations

import logging

from . import runtime

_LOGGER = logging.getLogger(__name__)


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
            with runtime._native_import_lock:
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
