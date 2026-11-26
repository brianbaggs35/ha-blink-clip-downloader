"""The face-recognition model profiles and the backend choice."""

from __future__ import annotations

import pytest

from blink_downloader.config import FACE_RECOGNITION_BACKENDS
from blink_downloader.database.faces import DEFAULT_FACE_MODEL
from blink_downloader.vision import FACE_BACKENDS, create_face_embedder
from blink_downloader.vision.face_models import (
    BACKEND_FACENET,
    BACKEND_OPENCV,
    FACENET,
    FACENET_MATCH_THRESHOLD,
    FACENET_RESEMBLANCE_SIMILARITY,
    OPENCV_SFACE,
    face_model_for,
)
from blink_downloader.vision.faces import FaceEmbedder
from blink_downloader.vision.opencv_faces import OpenCVFaceEmbedder


def test_the_database_default_names_the_facenet_model() -> None:
    """Every enrollment from before the column existed reads as FaceNet's."""
    assert DEFAULT_FACE_MODEL == FACENET.name


def test_the_model_names_differ() -> None:
    assert FACENET.name != OPENCV_SFACE.name


def test_facenet_keeps_the_figures_that_were_measured() -> None:
    assert FACENET.match_threshold == FACENET_MATCH_THRESHOLD == 0.75
    assert FACENET.resemblance_similarity == FACENET_RESEMBLANCE_SIMILARITY == 0.55
    assert (FACENET.near_duplicate_similarity, FACENET.group_similarity) == (
        0.95,
        0.72,
    )
    assert FACENET.outlier_similarity == 0.55
    assert FACENET.measured


def test_the_opencv_model_is_unmeasured_and_gets_no_leniency() -> None:
    """None of its figures came from camera footage, so it is held to the
    strictest rules: a face that matches nobody is never set aside."""
    assert not OPENCV_SFACE.measured
    assert OPENCV_SFACE.resemblance_similarity is None


def test_the_opencv_match_bar_is_above_the_one_opencv_publishes() -> None:
    """OpenCV's own SFace cosine decision threshold is 0.363."""
    assert OPENCV_SFACE.match_threshold > 0.363


@pytest.mark.parametrize("model", [FACENET, OPENCV_SFACE])
def test_a_group_is_never_looser_than_a_match(model) -> None:
    """The picker must not merge faces the recognizer would keep apart, and
    the review must not call a photo unlike its person above the match bar."""
    assert model.group_similarity >= model.match_threshold - 0.25
    assert model.outlier_similarity <= model.match_threshold


def test_each_backend_maps_to_its_model() -> None:
    assert face_model_for(BACKEND_FACENET) is FACENET
    assert face_model_for(BACKEND_OPENCV) is OPENCV_SFACE


def test_an_unknown_backend_is_facenet() -> None:
    assert face_model_for("nonsense") is FACENET


def test_the_backend_lists_agree() -> None:
    assert FACE_BACKENDS == FACE_RECOGNITION_BACKENDS == ("facenet", "opencv")


def test_the_factory_builds_the_embedder_for_the_backend() -> None:
    assert type(create_face_embedder("opencv")) is OpenCVFaceEmbedder
    assert type(create_face_embedder("facenet")) is FaceEmbedder
    assert type(create_face_embedder()) is FaceEmbedder
    assert type(create_face_embedder("nonsense")) is FaceEmbedder


def test_each_embedder_says_which_model_it_runs() -> None:
    assert create_face_embedder("facenet").model is FACENET
    assert create_face_embedder("opencv").model is OPENCV_SFACE
