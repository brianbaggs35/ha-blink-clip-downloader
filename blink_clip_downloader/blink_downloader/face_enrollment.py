"""Turning faces found in clips and photos into enrollments.

Backs the Biometrics tab's enrollment flow: a scan finds faces
(``vision/faces.py``'s :meth:`~blink_downloader.vision.faces.FaceEmbedder.detect`),
this module decides which of them are worth showing and how to group them,
holds them server-side until the user picks some, and reviews what is
already enrolled for photos that look wrong.

Embeddings never leave the add-on — the browser only ever sees a candidate's
id and its thumbnail — so a picked face is enrolled from the embedding held
here rather than from anything the browser sends back. Pure arithmetic plus
a small in-memory store: numpy, no torch, no aiohttp.

The similarity thresholds below were measured, not guessed, on five real
people's faces shrunk to 30-90px and re-compressed the way a camera frame
is: the same person scored a median 0.79 against themselves (lowest 0.44),
two different people a median 0.13 (highest 0.71, both faces ~30px).
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .vision.faces import _FACE_MATCH_THRESHOLD

# Within one clip, a face this similar to one already kept is the same shot
# a fraction of a second later — showing both is what made the old frame
# picker look like it was full of duplicates.
_NEAR_DUPLICATE_SIMILARITY = 0.95

# Faces at least this similar to a group's average join it. Deliberately
# above the highest different-person score measured (0.71): splitting one
# person into two groups costs a click, merging two people would invite
# enrolling a stranger under someone's name.
_GROUP_SIMILARITY = 0.72

# An enrolled photo whose *best* similarity to the same person's other
# photos is below this probably shows someone else. Measured with 4-6 photos
# per person: a genuine photo's best match never fell below 0.71, a
# stranger's filed under that name reached 0.62 at most and 0.55 at the
# 95th percentile. Worth a warning, because a stranger's face filed under an
# approved name is exactly the enrollment mistake that would later let the
# suspicious-flag bypass clear that stranger.
_OUTLIER_SIMILARITY = 0.55

# MTCNN's own cut-off is 0.7, and a single-person portrait was measured
# returning two extra "faces" at 0.80-0.85 — a bag and a patch of shadow.
# Enrollment has no reason to offer those; analysis is unaffected by this.
MIN_CANDIDATE_PROBABILITY = 0.90

#: Most faces the candidate store holds at once — and so the most any one
#: request can usefully name, since an older id would already be gone.
CANDIDATE_CAPACITY = 2000


@dataclass(frozen=True)
class FaceCandidate:
    """A face found by a scan, waiting for the user to enroll it or not."""

    id: str
    embedding: list[float]
    thumbnail: bytes
    quality: float
    created: float
    #: Width of the clip frame the face came from; None for an uploaded photo.
    frame_width: int | None = None


class FaceCandidateStore:
    """Faces found by recent scans, held until enrolled or expired.

    Bounded both ways: *capacity* evicts the oldest first, so a long
    session of scanning cannot grow without limit, and *ttl* drops anything
    older than that — a candidate is meant to be picked within the same
    visit to the tab.
    """

    def __init__(
        self,
        capacity: int = CANDIDATE_CAPACITY,
        ttl: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._ttl = ttl
        self._clock = clock
        self._items: OrderedDict[str, FaceCandidate] = OrderedDict()

    def add(
        self,
        embedding: list[float],
        thumbnail: bytes,
        quality: float,
        frame_width: int | None = None,
    ) -> str:
        """Store one face and return the id the browser will refer to it by."""
        self._expire()
        candidate = FaceCandidate(
            id=secrets.token_urlsafe(12),
            embedding=embedding,
            thumbnail=thumbnail,
            quality=quality,
            created=self._clock(),
            frame_width=frame_width,
        )
        self._items[candidate.id] = candidate
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
        return candidate.id

    def get(self, candidate_id: str) -> FaceCandidate | None:
        """The candidate with this id, unless it has expired or been taken."""
        self._expire()
        return self._items.get(candidate_id)

    def take(self, candidate_id: str) -> FaceCandidate | None:
        """Remove and return a candidate — enrolling one twice is never wanted."""
        self._expire()
        return self._items.pop(candidate_id, None)

    def _expire(self) -> None:
        cutoff = self._clock() - self._ttl
        while self._items:
            oldest = next(iter(self._items.values()))
            if oldest.created >= cutoff:
                break
            self._items.popitem(last=False)


def _unit_rows(embeddings: Sequence[Sequence[float]]) -> np.ndarray:
    """Embeddings as a matrix of unit-length rows, so a dot product is a cosine."""
    matrix = np.asarray(embeddings, dtype=np.float64)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def suppress_near_duplicates(
    embeddings: Sequence[Sequence[float]], qualities: Sequence[float]
) -> list[int]:
    """Indices worth keeping, best quality first, dropping near-identical shots.

    Greedy: the best face is always kept, and each next-best is kept unless
    it is at least ``_NEAR_DUPLICATE_SIMILARITY`` similar to one already
    kept. The caller reports how many were dropped, so nothing disappears
    silently.
    """
    if not embeddings:
        return []
    unit = _unit_rows(embeddings)
    kept: list[int] = []
    for index in sorted(range(len(qualities)), key=lambda i: -qualities[i]):
        if kept and float(np.max(unit[kept] @ unit[index])) >= (
            _NEAR_DUPLICATE_SIMILARITY
        ):
            continue
        kept.append(index)
    return kept


def group_candidates(candidates: Sequence[FaceCandidate]) -> list[list[str]]:
    """Candidate ids grouped by apparent person, biggest group first.

    Each face, best quality first, joins the group whose average it is most
    similar to — if that is at least ``_GROUP_SIMILARITY`` — or starts a
    new one. Averaging is what makes this steadier than comparing faces
    pairwise: one blurry frame cannot bridge two different people.
    Presentation only; the user still sees and picks every face.

    One matrix product per face against every group's running sum, rather
    than a Python loop over the groups: at the candidate store's capacity
    the loop took 3-5 seconds, which a request handler cannot afford. An
    exact tie goes to the later group, as the loop's ``>=`` did.
    """
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda c: -c.quality)
    unit = _unit_rows([c.embedding for c in ordered])
    members: list[list[int]] = []
    sums = np.zeros_like(unit)
    norms = np.zeros(len(unit))
    for index, vector in enumerate(unit):
        count = len(members)
        best = -1
        if count:
            with np.errstate(divide="ignore", invalid="ignore"):
                similarity = (sums[:count] @ vector) / norms[:count]
            # A zero-length sum (an all-zero embedding) matches nothing.
            similarity[norms[:count] == 0] = -np.inf
            last_best = count - 1 - int(np.argmax(similarity[::-1]))
            if similarity[last_best] >= _GROUP_SIMILARITY:
                best = last_best
        if best < 0:
            best = count
            members.append([])
        members[best].append(index)
        sums[best] += vector
        norms[best] = np.linalg.norm(sums[best])
    groups = [[ordered[i].id for i in group] for group in members]
    return sorted(groups, key=len, reverse=True)


def review_enrollments(enrollments: Sequence[dict[str, Any]]) -> dict[int, dict]:
    """Per enrolled photo, the two mistakes worth pointing out.

    ``unlike_others``: the photo resembles none of the same person's other
    photos — most likely someone else filed under their name. ``also_matches``:
    the name of a *different* person this photo would be recognized as,
    which means clip analysis can confuse the two. Only photos with at
    least one warning appear in the result.
    """
    usable = [e for e in enrollments if e.get("embedding")]
    if len(usable) < 2 or len({len(e["embedding"]) for e in usable}) != 1:
        return {}
    unit = _unit_rows([e["embedding"] for e in usable])
    similarity = unit @ unit.T
    names = [str(e["name"]) for e in usable]
    warnings: dict[int, dict] = {}
    for i, enrollment in enumerate(usable):
        same = [j for j in range(len(usable)) if j != i and names[j] == names[i]]
        other = [j for j in range(len(usable)) if names[j] != names[i]]
        unlike_others = bool(same) and float(similarity[i, same].max()) < (
            _OUTLIER_SIMILARITY
        )
        also_matches = ""
        if other:
            # argmax, like max(), takes the first of any tie.
            closest = other[int(np.argmax(similarity[i, other]))]
            if float(similarity[i, closest]) >= _FACE_MATCH_THRESHOLD:
                also_matches = names[closest]
        if unlike_others or also_matches:
            warnings[int(enrollment["id"])] = {
                "unlike_others": unlike_others,
                "also_matches": also_matches,
            }
    return warnings
