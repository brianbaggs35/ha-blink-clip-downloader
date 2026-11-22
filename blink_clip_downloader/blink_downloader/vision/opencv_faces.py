"""Opt-in face recognition backend: YuNet + SFace, run by OpenCV.

``ai_face_recognition_backend: opencv`` swaps facenet-pytorch (unmaintained
since April 2024, and installed with ``--no-deps`` for want of a Pillow it
could live with) for two small ONNX models that ``opencv-python-headless``
already knows how to run -- ``cv2.FaceDetectorYN`` (YuNet) to find faces and
``cv2.FaceRecognizerSF`` (SFace) to embed them -- so recognition no longer needs
PyTorch at all. Everything stays on this device, as before.

It is a drop-in :class:`~blink_downloader.vision.faces.FaceEmbedder` whose
:attr:`model` is :data:`~blink_downloader.vision.face_models.OPENCV_SFACE`:
128-number embeddings in their own space, so nobody enrolled with FaceNet is
recognized by it until they are enrolled again (``face_enrollments.model``
keeps the two apart), and thresholds that were never measured on camera
footage, which is why it is experimental and gets none of the set-aside
leniency (see ``face_models.py``).

The two model files are downloaded once, at a pinned opencv_zoo commit, into
``/data/model_cache`` (the same cache as the YOLO weights), or taken from
there if a person put them there for an add-on that cannot reach GitHub.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import runtime
from .face_models import OPENCV_SFACE, FaceModel
from .faces import _MAX_DETECTION_SIDE, DetectedFace, FaceEmbedder, _describe_face

_LOGGER = logging.getLogger(__name__)

#: Where the two models are kept; downloaded again if missing.
MODEL_DIR = Path("/data/model_cache/opencv_faces")

#: opencv_zoo's head when these files were chosen (2026-08-12). A commit, not
#: ``main``, so the bytes behind the address cannot change.
_ZOO_COMMIT = "eb9f4f2d4b79b58e5513b5d25d5f0243a8357bf4"
_ZOO_URL = "https://github.com/opencv/opencv_zoo/raw/{commit}/models/{folder}/{name}"
_DOWNLOAD_TIMEOUT_SECONDS = 120
_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class _ModelFile:
    folder: str
    name: str
    #: What a real file's size must fall between. GitHub serves a Git LFS
    #: *pointer* (a few hundred bytes of text) where it cannot serve the file,
    #: and a pinned address that now answers with an error page is the same
    #: kind of wrong; neither must be loaded as a model.
    min_bytes: int
    max_bytes: int

    @property
    def path(self) -> Path:
        return MODEL_DIR / self.name

    @property
    def url(self) -> str:
        return _ZOO_URL.format(commit=_ZOO_COMMIT, folder=self.folder, name=self.name)

    def usable(self) -> bool:
        try:
            return self.min_bytes <= self.path.stat().st_size <= self.max_bytes
        except OSError:
            return False


#: YuNet (MIT, 227 KB) and SFace (Apache-2.0, 36.9 MB), as listed by opencv_zoo.
_DETECTOR_FILE = _ModelFile(
    "face_detection_yunet", "face_detection_yunet_2023mar.onnx", 100_000, 1_000_000
)
_RECOGNIZER_FILE = _ModelFile(
    "face_recognition_sface",
    "face_recognition_sface_2021dec.onnx",
    20_000_000,
    80_000_000,
)

#: YuNet's own settings: faces scoring under the first are not reported, boxes
#: overlapping more than the second are one face. Looser than the zoo's 0.9
#: demo default on purpose: a face that is found and matches nobody counts
#: toward "a stranger", while one that is missed is not seen at all.
_DETECTION_SCORE = 0.7
_DETECTION_NMS = 0.3
_DETECTION_TOP_K = 5000


def _download(model: _ModelFile) -> None:
    """Fetch *model* into :data:`MODEL_DIR` unless a usable copy is there."""
    if model.usable():
        return
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    part = model.path.with_name(model.path.name + ".part")
    digest = hashlib.sha256()
    size = 0
    _LOGGER.info("Downloading the face-recognition model %s", model.name)
    # The address is a constant https URL (see _ZOO_URL), never anything a
    # request or a setting supplies.
    request = urllib.request.Request(model.url)
    try:
        with (
            urllib.request.urlopen(  # nosec B310
                request, timeout=_DOWNLOAD_TIMEOUT_SECONDS
            ) as response,
            open(part, "wb") as out,
        ):
            while chunk := response.read(_CHUNK_BYTES):
                size += len(chunk)
                if size > model.max_bytes:
                    raise OSError(f"{model.name} is larger than any real copy")
                digest.update(chunk)
                out.write(chunk)
        if size < model.min_bytes:
            raise OSError(f"{model.name} is smaller than any real copy ({size} bytes)")
        os.replace(part, model.path)
    finally:
        part.unlink(missing_ok=True)
    # Logged so the file can be pinned by hash: the address is fixed to a
    # commit, but this add-on has no published hash to check it against.
    _LOGGER.info(
        "Face-recognition model %s ready (sha256 %s)", model.name, digest.hexdigest()
    )


class OpenCVFaceEmbedder(FaceEmbedder):
    """Face detection (YuNet) + embedding (SFace), both through OpenCV."""

    model: FaceModel = OPENCV_SFACE
    _label = "face-recognition (OpenCV)"
    _package = "cv2"
    _install = "opencv-python-headless"

    def __init__(self) -> None:
        super().__init__()
        self._detector: Any = None
        self._recognizer: Any = None
        # YuNet's input size is set on the detector for each frame, so one
        # detector serves one frame at a time.
        self._detect_lock = threading.Lock()

    def _is_loaded(self) -> bool:
        return self._recognizer is not None

    def _load_sync(self) -> None:
        # Outside the import lock: it can take a while, and no other stage
        # should wait on a download.
        _download(_DETECTOR_FILE)
        _download(_RECOGNIZER_FILE)
        with runtime._native_import_lock:
            import cv2  # type: ignore[import-not-found]

            if not (
                hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")
            ):
                raise ImportError(
                    "this OpenCV build has no FaceDetectorYN/FaceRecognizerSF"
                )
            _LOGGER.info("Loading local face-recognition models (OpenCV)")
            self._detector = cv2.FaceDetectorYN.create(
                str(_DETECTOR_FILE.path),
                "",
                (320, 320),
                _DETECTION_SCORE,
                _DETECTION_NMS,
                _DETECTION_TOP_K,
            )
            self._recognizer = cv2.FaceRecognizerSF.create(
                str(_RECOGNIZER_FILE.path), ""
            )
            _LOGGER.info("Face-recognition models ready (OpenCV)")

    def _detect_sync(self, frame: bytes, thumbnails: bool) -> list[DetectedFace]:
        import cv2  # type: ignore[import-not-found]
        import numpy as np
        from PIL import Image, ImageOps

        # See FaceEmbedder._detect_sync: a phone photo's EXIF rotation must be
        # honoured or the embedding resembles nobody.
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(frame))).convert("RGB")
        image.thumbnail((_MAX_DETECTION_SIDE, _MAX_DETECTION_SIDE))
        bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        height, width = bgr.shape[:2]
        with self._detect_lock:
            self._detector.setInputSize((width, height))
            _, found = self._detector.detect(bgr)
            if found is None:
                return []
            faces = []
            for row in found:
                # alignCrop warps the face onto SFace's 112x112 template from
                # YuNet's five landmarks, which are the last-but-one ten values.
                aligned = self._recognizer.alignCrop(bgr, row)
                embedding = np.asarray(self._recognizer.feature(aligned))
                x, y, box_width, box_height = (float(v) for v in row[:4])
                faces.append(
                    _describe_face(
                        image,
                        (x, y, x + box_width, y + box_height),
                        float(row[14]),
                        np.asarray(row[4:14], dtype=np.float32).reshape(5, 2),
                        embedding.reshape(-1).tolist(),
                        thumbnails,
                    )
                )
        return faces
