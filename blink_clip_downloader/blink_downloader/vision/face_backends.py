"""Which face-recognition backend runs, chosen by ``ai_face_recognition_backend``."""

from __future__ import annotations

from .face_models import BACKEND_OPENCV
from .faces import FaceEmbedder
from .opencv_faces import OpenCVFaceEmbedder

__all__ = ["create_face_embedder"]


def create_face_embedder(backend: str = "facenet") -> FaceEmbedder:
    """The embedder for *backend*; anything but ``opencv`` is FaceNet, the default."""
    if backend == BACKEND_OPENCV:
        return OpenCVFaceEmbedder()
    return FaceEmbedder()
