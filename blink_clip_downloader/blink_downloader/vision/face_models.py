"""What each face-recognition backend measures and how far it can be trusted.

Every number that decides *who a face is* (the match bar, what counts as a
likeness, what the enrollment picker takes for the same photo twice) is a
property of the model that made the embeddings, not of this add-on: a cosine
of 0.7 means something different for FaceNet's 512 numbers than for SFace's
128. So the numbers live with the model they were measured on, and a backend
whose numbers were never measured here says so (``measured``) and is held to
the strictest rules this add-on has instead of to rules written for another
model. Stdlib only, so the enrollment logic and the database can name a model
without importing the optional computer-vision packages.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BACKEND_FACENET",
    "BACKEND_OPENCV",
    "FACENET",
    "FACENET_MATCH_THRESHOLD",
    "FACENET_RESEMBLANCE_SIMILARITY",
    "FACE_BACKENDS",
    "OPENCV_SFACE",
    "FaceModel",
    "face_model_for",
]

BACKEND_FACENET = "facenet"
BACKEND_OPENCV = "opencv"
#: Every ``ai_face_recognition_backend`` value, the default first.
FACE_BACKENDS = (BACKEND_FACENET, BACKEND_OPENCV)


#: FaceNet's two figures that ``vision/faces.py`` documents and the rest of the
#: add-on reads under their old names.
FACENET_MATCH_THRESHOLD = 0.75
FACENET_RESEMBLANCE_SIMILARITY = 0.55


@dataclass(frozen=True)
class FaceModel:
    """One backend's embedding space and the thresholds read in it.

    *name* is what is stored with every enrollment (``face_enrollments.model``),
    so a face enrolled with one model is never compared with another's
    embeddings, and *measured* is whether the numbers below come from clips run
    through this add-on's own pipeline (see ``vision/faces.py``) or are only
    the model's published figure with a margin.
    """

    name: str
    #: Cosine similarity at or above which a face is a match.
    match_threshold: float
    #: Cosine similarity at or above which a face matching nobody is taken for
    #: a poor look at someone recognized nearby. ``None`` turns the whole
    #: ``ai_face_recognition_ignore_unclear_faces`` leniency off for this
    #: model: a face that matched nobody is then a stranger unless it is too
    #: small to judge, whatever the option says.
    resemblance_similarity: float | None
    #: Enrollment picker: two shots this similar are the same photo twice --
    #: within one clip, a fraction of a second apart.
    near_duplicate_similarity: float
    #: Enrollment picker: a face this similar to a group's average joins it.
    #: Deliberately above the highest different-person score measured
    #: (FaceNet: 0.71): splitting one person into two groups costs a click,
    #: merging two people would invite enrolling a stranger under a name.
    group_similarity: float
    #: Enrollment review: a photo less similar than this to the person's others
    #: is flagged as one that is not them. (FaceNet, 4-6 photos per person: a
    #: genuine photo's best match never fell under 0.71, a stranger's reached
    #: 0.62 at most and 0.55 at the 95th percentile.) A stranger filed under an
    #: approved name is the enrollment mistake that would later let the
    #: suspicious-flag bypass clear that stranger.
    outlier_similarity: float
    measured: bool


#: InceptionResnetV1 (VGGFace2) behind MTCNN, from ``facenet-pytorch``. Every
#: figure here is the one ``vision/faces.py`` and ``face_enrollment.py`` always
#: used, measured on real faces shrunk to camera size and through H.264.
FACENET = FaceModel(
    name="facenet-vggface2",
    match_threshold=FACENET_MATCH_THRESHOLD,
    resemblance_similarity=FACENET_RESEMBLANCE_SIMILARITY,
    near_duplicate_similarity=0.95,
    group_similarity=0.72,
    outlier_similarity=0.55,
    measured=True,
)

#: SFace behind YuNet, both run by OpenCV (``cv2.FaceRecognizerSF`` /
#: ``cv2.FaceDetectorYN``). OpenCV publishes a cosine of 0.363 as SFace's
#: decision threshold; the match bar here is raised well above it so that a
#: doubtful face errs toward "no match", but none of these figures was
#: measured on camera footage, which is why the backend is opt-in, has no
#: resemblance leniency and is described as experimental wherever it appears.
OPENCV_SFACE = FaceModel(
    name="opencv-sface",
    match_threshold=0.50,
    resemblance_similarity=None,
    near_duplicate_similarity=0.95,
    group_similarity=0.50,
    outlier_similarity=0.40,
    measured=False,
)

_BY_BACKEND = {BACKEND_FACENET: FACENET, BACKEND_OPENCV: OPENCV_SFACE}


def face_model_for(backend: str) -> FaceModel:
    """The model *backend* runs; an unknown name is the default, FaceNet."""
    return _BY_BACKEND.get(backend, FACENET)
