"""Stage 2, off-device: object detection on Hugging Face's servers.

For hardware that cannot run the local detector (:class:`.detection.ObjectDetector`,
which needs PyTorch): the sampled frames are posted to Hugging Face's
serverless inference and the boxes that come back are turned into the same
:class:`.detection.DetectedObject` the rest of the pipeline reads, so
nothing downstream knows or cares which detector produced them.

What it deliberately is **not**:

* It does not track. The serverless ``object-detection`` task is one image
  in, boxes out, so every detection carries ``track_id=None`` — the "tracking
  unavailable for that call" case ``security.tracks.build_tracks`` already
  handles by inferring continuity from box overlap and flagging the result
  ``tracked=False``. Rules that need a *measured* track (the TRACKING hint,
  routine door use, the "N separate people" event) therefore do not fire in
  this mode, and event confidences are lower; that under-claiming is the
  point of the flag, and this module must not paper over it by minting ids.
* It is not the only stage Hugging Face could in principle serve, but it is
  the only one it does: the serverless tier has no depth-estimation,
  box-prompted segmentation or pose model, so those stages stay local or
  unavailable (see :class:`.pipeline.VisionPipeline`). Face recognition is
  never remote, whatever the backend: enrolled photos and embeddings are
  biometric data, and nothing in here is given access to either.
* It does not return partial results. A frame that could not be examined is
  indistinguishable, downstream, from a frame with nobody in it, so one
  failed frame fails the clip's detection (``None`` — "could not look") rather
  than silently reporting an empty driveway. That is the same
  ``None``-versus-``[]`` distinction ``ObjectDetector.detect`` keeps.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..security import Box
from .detection import _RELEVANT_CLASSES, DetectedObject

_LOGGER = logging.getLogger(__name__)

#: Serverless inference, model id appended. This is the ``hf-inference``
#: provider behind the router, which is the one that runs object detection.
HUGGINGFACE_INFERENCE_URL = "https://router.huggingface.co/hf-inference/models"

#: Confidence a box must reach to be kept. Hugging Face's own default for the
#: task (what the server applies when the request names no threshold, and
#: this request does not, so that the same call also works against a
#: dedicated endpoint): a judgement call rather than a measurement. DETR's
#: scores are not on YOLO's scale, no footage of this add-on's was used to
#: calibrate it, and the security layer's thresholds were tuned on YOLO.
_MIN_CONFIDENCE = 0.5

#: Frames in flight at once. A clip is up to ``ai_temporal_scan_frames`` of
#: them (12 by default); sending them one at a time makes the stage as slow as
#: twelve round trips, while an unbounded burst is the quickest way to meet a
#: rate limit.
_CONCURRENCY = 4

#: Tries per frame, the first included.
_ATTEMPTS = 3

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

#: Longest a retry will wait on a server's own estimate (a cold model reports
#: how long it needs to load). Beyond this the clip is better off failing and
#: being tried on the next cycle than holding the analysis queue.
_MAX_WAIT_SECONDS = 20.0

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class _Failure(Exception):
    """A frame could not be examined. *permanent* says retrying cannot help."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.permanent = permanent


@dataclass
class _Run:
    """State shared by one clip's frames: whether to stop sending more."""

    failure: _Failure | None = None


def _wait_for(attempt: int, hint: float | None) -> float:
    """Seconds to wait before retry number *attempt* (1-based).

    The server's own figure when it gave one — a model that is loading says
    how long — else 1, 2, 4 ... seconds; never more than ``_MAX_WAIT_SECONDS``.
    """
    seconds = hint if hint is not None and hint > 0 else float(2 ** (attempt - 1))
    return min(seconds, _MAX_WAIT_SECONDS)


def _server_hint(status: int, headers: Any, body: Any) -> float | None:
    """How long the server asked us to wait, if it said."""
    if isinstance(body, dict) and isinstance(body.get("estimated_time"), int | float):
        return float(body["estimated_time"])
    if status == 429:
        try:
            return float(headers.get("Retry-After", ""))
        except ValueError:
            return None
    return None


def _error_text(body: Any) -> str:
    """The server's own explanation, trimmed to one short line."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if error:
            return str(error)[:200]
    return ""


def _classify(status: int, body: Any, model: str) -> _Failure:
    """Turn a non-200 answer into the failure it means."""
    detail = _error_text(body)
    suffix = f": {detail}" if detail else ""
    if status == 401:
        return _Failure(
            "Hugging Face rejected the token (HTTP 401) — check "
            "huggingface_api_key (or hf_token) in the add-on settings",
            permanent=True,
        )
    if status == 402:
        return _Failure(
            "the Hugging Face account has no credit left (HTTP 402). Inference "
            "Providers bills the account the token belongs to: a free account "
            "has no monthly credit, PRO includes a small one, and anything "
            "beyond it is pay-as-you-go. Add credit at "
            "huggingface.co/settings/billing, or set ai_vision_backend back to "
            "local",
            permanent=True,
        )
    if status == 403:
        return _Failure(
            "Hugging Face denied the request (HTTP 403) — the token needs "
            f"'Make calls to Inference Providers'{suffix}",
            permanent=True,
        )
    if status == 404:
        return _Failure(
            f"model {model!r} is not available through Hugging Face's "
            "serverless inference (HTTP 404) — check "
            f"ai_remote_detection_model; it serves only a few detectors "
            f"(facebook/detr-resnet-50, facebook/detr-resnet-101){suffix}",
            permanent=True,
        )
    return _Failure(f"Hugging Face answered HTTP {status}{suffix}")


def _corners(box: Any) -> Box | None:
    """A box's corners as floats, or ``None`` if it is not a usable box.

    Usable means all four corners present and numeric, with positive width
    and height: the security layer's geometry divides by both.
    """
    if not isinstance(box, dict):
        return None
    try:
        x1, y1, x2, y2 = (float(box[key]) for key in ("xmin", "ymin", "xmax", "ymax"))
    except (KeyError, TypeError, ValueError):
        return None
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def _detection(item: Any, frame_index: int) -> DetectedObject | None:
    """One server-reported box as the pipeline's own object, or ``None`` if it
    is malformed, a class nothing here reads, or under the confidence floor."""
    if not isinstance(item, dict):
        return None
    label = str(item.get("label", "")).strip().lower()
    score = item.get("score")
    corners = _corners(item.get("box"))
    if label not in _RELEVANT_CLASSES or corners is None:
        return None
    if not isinstance(score, int | float) or score < _MIN_CONFIDENCE:
        return None
    return DetectedObject(
        label=label,
        confidence=float(score),
        box=corners,
        track_id=None,
        frame_index=frame_index,
    )


def _parse_detections(payload: Any, frame_index: int) -> list[DetectedObject]:
    """Turn one frame's response into detections, keeping what the pipeline reads.

    Anything malformed is skipped rather than fatal — a box with a missing
    corner is not worth failing a clip over, and the model's other boxes in
    the same answer are still good. A payload that is not a list at all,
    though, is not an answer, and raises.
    """
    if not isinstance(payload, list):
        raise _Failure("Hugging Face returned something other than a list of boxes")
    found = (_detection(item, frame_index) for item in payload)
    return [detection for detection in found if detection is not None]


class RemoteObjectDetector:
    """Object detection on Hugging Face's serverless inference.

    Interchangeable with :class:`.detection.ObjectDetector`: ``detect`` takes
    the clip's scan frames and returns the detections across them, or ``None``
    when it could not look.

    The frames are uploaded. They are the same frames the configured AI
    provider is sent, and a cloud provider receives them too — but a user on
    a local provider chose that for privacy, which is why this backend is
    opt-in, announced in the log at start-up, and documented wherever the
    option is.
    """

    def __init__(
        self,
        model_id: str,
        token: str,
        base_url: str = HUGGINGFACE_INFERENCE_URL,
    ) -> None:
        self._model_id = model_id
        self._token = token
        self._base_url = base_url.rstrip("/")

    async def detect(self, frames: list[bytes]) -> list[DetectedObject] | None:
        """Detect relevant objects across *frames*. ``None`` if unavailable."""
        if not frames:
            return None
        if not self._token:
            _LOGGER.warning(
                "Remote object detection unavailable: ai_vision_backend is "
                "'huggingface' but neither huggingface_api_key nor hf_token is set"
            )
            return None
        run = _Run()
        gate = asyncio.Semaphore(_CONCURRENCY)
        async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:
            results = await asyncio.gather(
                *(
                    self._detect_frame(session, gate, run, index, frame)
                    for index, frame in enumerate(frames)
                )
            )
        if run.failure is not None:
            _LOGGER.warning("Remote object detection failed: %s", run.failure.message)
            return None
        return [detection for found in results for detection in found]

    async def _detect_frame(
        self,
        session: aiohttp.ClientSession,
        gate: asyncio.Semaphore,
        run: _Run,
        index: int,
        frame: bytes,
    ) -> list[DetectedObject]:
        async with gate:
            if run.failure is not None:
                # Another frame already failed this clip; nothing this one
                # finds can rescue it, and each request costs money.
                return []
            try:
                return _parse_detections(await self._post(session, frame), index)
            except _Failure as failure:
                # Keep the first permanent failure over a transient one: it
                # is the one the user can act on.
                if run.failure is None or (
                    failure.permanent and not run.failure.permanent
                ):
                    run.failure = failure
                return []

    async def _post(self, session: aiohttp.ClientSession, frame: bytes) -> Any:
        """One frame's JSON answer, retrying what retrying can fix."""
        url = f"{self._base_url}/{self._model_id}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "image/jpeg",
            "Accept": "application/json",
        }
        failure = _Failure("no attempt was made")
        for attempt in range(1, _ATTEMPTS + 1):
            hint: float | None = None
            try:
                async with session.post(url, data=frame, headers=headers) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 200:
                        return body
                    failure = _classify(resp.status, body, self._model_id)
                    if resp.status in _RETRYABLE_STATUSES:
                        hint = _server_hint(resp.status, resp.headers, body)
                    elif failure.permanent or resp.status < 500:
                        raise failure
            except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
                # ValueError: a body that was not JSON (a gateway's HTML page).
                failure = _Failure(f"could not reach Hugging Face: {exc}")
            if attempt < _ATTEMPTS:
                await asyncio.sleep(_wait_for(attempt, hint))
        raise failure
