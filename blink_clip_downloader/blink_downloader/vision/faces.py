"""Stage 6: local-only face recognition (facenet-pytorch).

Embeddings are computed and compared entirely inside this add-on; nothing
about an enrolled household member — photo, embedding or name — is ever
sent to an AI provider. ``_build_recognition_hint`` is strictly name-free
by design: it reports a count and a fact, never who. The name is used
afterwards, locally, only to personalize the summary a human reads.

``_FACE_MATCH_THRESHOLD`` errs toward "no match": a missed match just
means analysis proceeds as it would with recognition off, while a false
match would wrongly reassure the model about a stranger.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import runtime

if TYPE_CHECKING:
    from ..database import ClipDatabase

_LOGGER = logging.getLogger(__name__)


# Cosine-similarity floor (0.0-1.0, InceptionResnetV1 512-d embeddings) for
# treating a detected face as matching an enrolled household member. Chosen
# conservatively — a missed match just means no RECOGNIZED RESIDENT hint
# (analysis proceeds exactly as it would with face recognition off), while a
# false match would wrongly reassure the model, so this errs toward "no
# match" when uncertain rather than the reverse.
_FACE_MATCH_THRESHOLD = 0.75


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

    def _embed_sync(self, frame: bytes) -> list[list[float]]:
        import torch  # type: ignore[import-not-found]
        from PIL import Image

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
        async with runtime._cv_slot():
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
                for embedding in await self._embedder.embed(frame):
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


def _best_enrollment(
    embedding: Any, enrollments: list[dict[str, Any]]
) -> tuple[str, bool] | None:
    """The best-scoring enrollment for one face, or ``None`` below threshold.

    Returns ``(name, approved)``. Comparison is ``>=`` so that a later
    enrollment wins an exact tie — arbitrary either way, but preserved
    from the loop this was lifted out of, because this feeds the
    safety-critical suspicious-flag bypass and "arbitrary but unchanged"
    is worth more here than "arbitrary and different".
    """
    best: tuple[str, bool] | None = None
    best_similarity = _FACE_MATCH_THRESHOLD
    for enrollment in enrollments:
        similarity = cosine_similarity(embedding, enrollment["embedding"])
        if similarity >= best_similarity:
            best_similarity = similarity
            best = (str(enrollment["name"]), bool(enrollment.get("approved", True)))
    return best


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
