"""Stage 6: local-only face recognition (facenet-pytorch).

Embeddings are computed and compared entirely inside this add-on; nothing
about an enrolled household member — photo, embedding or name — is ever
sent to an AI provider. ``_build_recognition_hint`` is strictly name-free
by design: it reports a count and a fact, never who. The name is used
afterwards, locally, only to personalize the summary a human reads.

``_FACE_MATCH_THRESHOLD`` errs toward "no match": a missed match just
means analysis proceeds as it would with recognition off, while a false
match would wrongly reassure the model about a stranger.

The same detector backs the Biometrics tab's enrollment picker
(:meth:`FaceEmbedder.detect`), which is why a detected face carries a
quality score and, on request, a small crop alongside its embedding.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..ffmpeg_output import ANALYSIS_FRAME_WIDTH
from . import runtime

if TYPE_CHECKING:
    from ..database import ClipDatabase

_LOGGER = logging.getLogger(__name__)


# Cosine-similarity floor (0.0-1.0, InceptionResnetV1 512-d embeddings) for
# treating a detected face as matching an enrolled household member. Chosen
# conservatively — a missed match just means no RECOGNIZED RESIDENT hint
# (analysis proceeds exactly as it would with face recognition off), while a
# false match would wrongly reassure the model, so this errs toward "no
# match" when uncertain rather than the reverse. Measured on camera-quality
# crops of five different people, the closest pair of *different* people
# scored 0.71 (both faces ~30px wide), which is how little room there is
# below this.
_FACE_MATCH_THRESHOLD = 0.75

#: ``ai_face_recognition_resolution`` -> the width, in pixels, of the frames
#: faces are found and matched in. Face size is the biggest lever on
#: recognition there is: measured on a face ~60px wide in a 1080p frame,
#: standard recognized 11 of 30, high 27 and very high 30, at 33, 40 and
#: 55ms a frame, with no wrong-person match at any of them. Enrollment scans
#: use the same width, because a face enrolled at one size and matched at
#: another can fail outright (0 of 30 when one side was ~20px wide).
FACE_RESOLUTION_WIDTHS = {
    "standard": ANALYSIS_FRAME_WIDTH,
    "high": 960,
    "very_high": 1280,
}

# Longest side an image is shrunk to before detection — the widest
# FACE_RESOLUTION_WIDTHS entry, so a clip frame is never touched and only an
# uploaded phone photo is, where it spares MTCNN a 12-megapixel pyramid for a
# face that fills half the frame anyway.
_MAX_DETECTION_SIDE = max(FACE_RESOLUTION_WIDTHS.values())

# The enrollment picker's crop around a face: the margin added on each side
# as a fraction of the face box, and the square size it is stored at.
_THUMBNAIL_MARGIN = 0.25
_THUMBNAIL_SIZE = 112

# Quality-score ramps, each mapping a measurement onto 0-1. Measured with
# real faces shrunk to camera size: a 40px face with a slight blur (Laplacian
# variance ~30) still matched itself at 0.67-0.78, a heavier blur (<10) at
# 0.04-0.48 — useless as a reference, and worse than useless if enrolled.
_SHARPNESS_RAMP = (10.0, 150.0)
_WIDTH_RAMP = (20.0, 80.0)


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


@dataclass(frozen=True)
class DetectedFace:
    """One face found in one image.

    *quality* (0-1) combines sharpness, size and how squarely the face
    points at the camera — the three things that decide whether it makes a
    useful enrollment reference. *thumbnail* is a small JPEG crop, only produced
    when the caller asks for it (the enrollment picker does; clip analysis
    does not).
    """

    embedding: list[float]
    probability: float
    width: int
    quality: float
    thumbnail: bytes = b""


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
        if not runtime.torch_cpu_compatible():
            raise runtime.CPUIncompatibleError(runtime._CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # runtime._native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with runtime._native_import_lock:
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
            except runtime.CPUIncompatibleError as exc:
                _LOGGER.warning("Face recognition unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load face-recognition models")
                return False

    def _detect_sync(self, frame: bytes, thumbnails: bool) -> list[DetectedFace]:
        import torch  # type: ignore[import-not-found]
        from PIL import Image, ImageOps

        # exif_transpose: a phone stores most photos sideways with an EXIF
        # "rotate me" tag. Without honouring it MTCNN either misses the face
        # or, worse, finds one and produces an embedding with no resemblance
        # to the person (0.07 similarity to the same photo upright, measured)
        # — which enrollment then stored as a reference. Frames ffmpeg
        # extracts from a clip carry no such tag, so analysis is unaffected.
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(frame))).convert("RGB")
        image.thumbnail((_MAX_DETECTION_SIDE, _MAX_DETECTION_SIDE))
        # detect() + extract() is exactly what MTCNN's own forward() does
        # with keep_all=True; calling them separately keeps the boxes,
        # probabilities and landmarks forward() throws away.
        boxes, probabilities, landmarks = self._mtcnn.detect(image, landmarks=True)
        if boxes is None:
            return []
        faces = self._mtcnn.extract(image, boxes, None)
        if faces.dim() == 3:
            faces = faces.unsqueeze(0)
        with torch.no_grad():
            embeddings = self._resnet(faces)
        return [
            _describe_face(
                image, box, float(probability), points, embedding.tolist(), thumbnails
            )
            for box, probability, points, embedding in zip(
                boxes, probabilities, landmarks, embeddings
            )
        ]

    async def detect(
        self, frame: bytes, *, thumbnails: bool = False
    ) -> list[DetectedFace] | None:
        """Every face MTCNN finds in *frame*, or ``None`` if *frame* could not
        be examined at all (models unavailable, undecodable image, a model
        error) — which callers must not mistake for "nobody there"."""
        async with runtime._cv_slot():
            if not await self.ensure_ready():
                return None
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._detect_sync, frame, thumbnails
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Face detection failed: %s", exc)
                return None

    async def embed(self, frame: bytes) -> list[list[float]] | None:
        """One 512-dim embedding per face in *frame*; ``None`` as in :meth:`detect`."""
        faces = await self.detect(frame)
        return None if faces is None else [face.embedding for face in faces]


def _describe_face(
    image: Any,
    box: Any,
    probability: float,
    points: Any,
    embedding: list[float],
    thumbnail: bool,
) -> DetectedFace:
    """Measure one detected face and, if asked, crop its thumbnail."""
    x1, y1, x2, y2 = (float(v) for v in box)
    width = max(0.0, x2 - x1)
    # Size scales the rest rather than adding to it: a sharp, frontal face
    # 25px wide still carries too little detail to be a good reference.
    quality = math.sqrt(_ramp(width, *_WIDTH_RAMP)) * (
        0.6 * _ramp(_sharpness(image, (x1, y1, x2, y2)), *_SHARPNESS_RAMP)
        + 0.4 * _frontality(points)
    )
    return DetectedFace(
        embedding=embedding,
        probability=probability,
        width=round(width),
        quality=round(quality, 3),
        thumbnail=_crop_thumbnail(image, (x1, y1, x2, y2)) if thumbnail else b"",
    )


def _ramp(value: float, low: float, high: float) -> float:
    """Map *value* linearly onto 0-1 between *low* and *high*, clamped."""
    return min(1.0, max(0.0, (value - low) / (high - low)))


def _sharpness(image: Any, box: tuple[float, float, float, float]) -> float:
    """Variance of the Laplacian over the face, normalised to a 64px crop.

    Normalising the size first keeps the number comparable between a 20px
    face in a camera frame and a 600px one in a portrait.
    """
    import numpy as np
    from PIL import Image

    x1, y1, x2, y2 = box
    if x2 - x1 < 2 or y2 - y1 < 2:
        return 0.0
    crop = image.crop((round(x1), round(y1), round(x2), round(y2)))
    gray = np.asarray(
        crop.convert("L").resize((64, 64), Image.Resampling.BILINEAR),
        dtype=np.float32,
    )
    laplacian = (
        4 * gray[1:-1, 1:-1]
        - gray[:-2, 1:-1]
        - gray[2:, 1:-1]
        - gray[1:-1, :-2]
        - gray[1:-1, 2:]
    )
    return float(laplacian.var())


def _frontality(points: Any) -> float:
    """1.0 for a face looking straight at the camera, falling to 0 in profile.

    From MTCNN's landmarks (left eye, right eye, nose, ...): the further the
    nose sits from the midpoint between the eyes, relative to how far apart
    the eyes are, the more the head is turned.
    """
    if points is None:
        return 0.0
    (left_x, _), (right_x, _), (nose_x, _) = points[0], points[1], points[2]
    eye_span = abs(float(right_x) - float(left_x))
    if eye_span <= 0:
        return 0.0
    offset = abs(float(nose_x) - (float(left_x) + float(right_x)) / 2) / eye_span
    return max(0.0, 1.0 - 2.0 * offset)


def _crop_thumbnail(image: Any, box: tuple[float, float, float, float]) -> bytes:
    """A square JPEG of the face plus a margin, for showing a person the face."""
    from PIL import Image

    x1, y1, x2, y2 = box
    side = max(x2 - x1, y2 - y1) * (1 + 2 * _THUMBNAIL_MARGIN)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    left = max(0, round(cx - side / 2))
    top = max(0, round(cy - side / 2))
    right = min(image.width, round(cx + side / 2))
    bottom = min(image.height, round(cy + side / 2))
    crop = image.crop((left, top, right, bottom))
    crop.thumbnail((_THUMBNAIL_SIZE, _THUMBNAIL_SIZE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


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

        A frame that could not be examined at all counts as unrecognized
        too: it might have held anyone, so it can never help vouch that
        everyone in the clip is known. Before this, a frame whose detection
        raised was skipped as if it were empty, letting the other frames'
        approved match clear a clip that had not been fully checked.

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
                if embeddings is None:
                    result.unrecognized_present = True
                    continue
                for embedding in embeddings:
                    match = _best_enrollment(embedding, enrollments)
                    if match is None:
                        result.unrecognized_present = True
                    elif match[1]:
                        approved.add(match[0])
                    else:
                        other.add(match[0])
            result.approved_names = sorted(approved)
            result.other_names = sorted(other)
            return result
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Face recognition failed: %s", exc)
            return FaceRecognitionResult()


def _closest_enrollment(
    embedding: Any, enrollments: list[dict[str, Any]]
) -> tuple[dict[str, Any], float] | None:
    """The best-scoring enrollment for one face and its similarity, or
    ``None`` below ``_FACE_MATCH_THRESHOLD``.

    Comparison is ``>=`` so that a later enrollment wins an exact tie —
    arbitrary either way, but preserved from the loop this was lifted out
    of, because this feeds the safety-critical suspicious-flag bypass and
    "arbitrary but unchanged" is worth more here than "arbitrary and
    different".
    """
    best: dict[str, Any] | None = None
    best_similarity = _FACE_MATCH_THRESHOLD
    for enrollment in enrollments:
        similarity = cosine_similarity(embedding, enrollment["embedding"])
        if similarity >= best_similarity:
            best_similarity = similarity
            best = enrollment
    return None if best is None else (best, best_similarity)


def _best_enrollment(
    embedding: Any, enrollments: list[dict[str, Any]]
) -> tuple[str, bool] | None:
    """``(name, approved)`` of :func:`_closest_enrollment`, as recognition uses it."""
    closest = _closest_enrollment(embedding, enrollments)
    if closest is None:
        return None
    enrollment, _similarity = closest
    return (str(enrollment["name"]), bool(enrollment.get("approved", True)))


def match_enrollment(
    embedding: list[float], enrollments: list[dict[str, Any]]
) -> tuple[str, float] | None:
    """``(name, similarity)`` of the enrollment clip analysis would match
    this face to, or ``None`` — so the enrollment picker says "already
    recognized as Brian" on exactly the faces analysis would."""
    closest = _closest_enrollment(embedding, enrollments)
    if closest is None:
        return None
    enrollment, similarity = closest
    return (str(enrollment["name"]), similarity)


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
