"""Optional computer-vision enhancement pipeline for clip analysis.

Five independently-toggleable stages, layered on top of the existing
AI-provider prompt pipeline (see ``analyzer.py``) rather than replacing it —
each stage produces a bounded, code-computed hint that gets appended to the
same prompt the configured AI provider (ollama/anthropic/openai/moondream)
already reasons over, exactly like the existing scene-baseline and
zone-motion hints. The AI model still makes the final suspicious/not call;
this module only gives it better evidence to work with.

- :class:`FrameEnhancer` — OpenCV CLAHE contrast enhancement + denoising.
- :class:`ObjectDetector` — YOLO object detection + ByteTrack tracking
  (Ultralytics), for precise person/vehicle/animal boxes instead of raw
  pixel-diff motion.
- :class:`DepthEstimator` — monocular depth estimation (Depth Anything V2
  via transformers), to tell "overlapping in the 2D frame" apart from
  "actually at the same distance from the camera".
- :class:`ContactSegmenter` — pixel-level segmentation (SAM2 via
  transformers) to refine a detected bounding-box overlap into an actual
  touching-or-not judgment.
- :class:`FaceEmbedder` / :class:`FaceRecognizer` — local-only face
  recognition (facenet-pytorch) to suppress alerts for enrolled household
  members. Enrollment data (photos, embeddings) never leaves this add-on.

Every stage lazily imports its own heavy dependency (torch, ultralytics,
opencv, transformers, facenet-pytorch — none of which are required to run
the add-on's core features) and reports itself unavailable rather than
raising if that dependency isn't installed or fails to load. Nothing in
this module is imported by ``analyzer.py`` at call time unless the
corresponding config option is enabled, and even then the heavy import
itself is deferred to first use — see each class's ``ensure_ready()``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import platform
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .security import (
    SUBJECT_LABELS,
    VEHICLE_LABELS,
    Box,
    ObjectTrack,
    ProtectedAsset,
    Zone,
    box_gap,
    box_iou,
    build_tracks,
    resolve_vehicle_asset,
)
from .security.geometry import pixel_gap_to_feet
from .security.vehicles import VehicleSignature

if TYPE_CHECKING:
    from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)

# Each stage below guards its own *first load* with an asyncio.Lock (see each
# ensure_ready()), but that only serializes concurrent calls to that same
# stage - it does nothing to stop two *different* stages (e.g. ObjectDetector
# and FaceEmbedder) from each hitting their first-ever `import cv2`/`import
# numpy`/`import torch` at the same moment on different threads (each
# _load_sync() runs in its own executor thread; FrameEnhancer.enhance() runs
# directly on the event loop thread). Concurrent clip analysis
# (concurrent_downloads > 1, or a startup backlog) makes this a real race,
# not just a theoretical one: CPython's import machinery isn't safe against
# two threads both performing the *first* import of the same native
# extension module at once, and can leave it permanently broken for the
# rest of the process's lifetime ("ImportError: cannot load module more
# than once per process") rather than raising a transient, retryable error.
# A single lock shared by every stage's import statement (not the model
# loading/download that follows, which is safe once the import itself has
# completed) closes this for good - found via a real crash on a fresh
# Home Assistant OS install with several clips analyzed concurrently at
# startup.
_native_import_lock = threading.Lock()

# Ceiling on how many of this module's heavy stages may run at once, across
# every clip being analyzed concurrently. Each stage below runs its torch
# inference in a thread executor, so without a limit a startup backlog of
# clips can have YOLO, Depth Anything, SAM2 and facenet all resident and
# computing simultaneously — several gigabytes of working set and a wedged
# CPU on the Raspberry Pi 5 this add-on is expected to run on. The limiter
# covers model *loading* as well as inference, since two first-time model
# downloads racing each other is the same problem in a worse form.
#
# One is the right default: these stages are already sequential within a
# clip, so a limit of one costs a multi-clip backlog only its own
# serialization, which it was going to pay in CPU contention anyway.
_cv_limit = 1
_cv_semaphore: asyncio.Semaphore | None = None


def configure_cv_concurrency(limit: int) -> None:
    """Set how many heavy CV stages may run at once (see ``ai_cv_concurrency``).

    Takes effect for work started after this call; anything already running
    keeps the limit it acquired under. Values below one are treated as one —
    a limit of zero would deadlock every stage rather than disabling them,
    and disabling is what the feature toggles are for.
    """
    global _cv_limit, _cv_semaphore
    limit = max(1, limit)
    if limit != _cv_limit:
        _cv_limit = limit
        _cv_semaphore = None


def _cv_slot() -> asyncio.Semaphore:
    """Return the shared concurrency semaphore, creating it on first use."""
    global _cv_semaphore
    if _cv_semaphore is None:
        _cv_semaphore = asyncio.Semaphore(_cv_limit)
    return _cv_semaphore


# Persistent cache dir for Ultralytics YOLO weights (see ObjectDetector
# below) — mirrors TORCH_HOME/HF_HOME (set in the Dockerfile) for the same
# reason: YOLO downloads to whatever path it's given rather than consulting
# either of those env vars itself, so a bare model filename would otherwise
# download into the process's cwd (an ephemeral s6-overlay runtime path,
# not the /data volume) and be re-fetched after every container recreation.
_YOLO_MODEL_CACHE_DIR = "/data/model_cache/yolo"

# COCO class names (Ultralytics' default label set) this pipeline surfaces
# in prompts. Anything else YOLO detects (furniture, traffic lights, sports
# balls, ...) isn't relevant to a home-security judgment and is filtered
# out to keep the OBJECT DETECTION hint short and focused.
# The label vocabulary itself lives in security/tracks.py, which is where
# every rule that reasons about people, animals and vehicles reads it from —
# one definition, not two that can drift apart. This module adds only the
# carryable classes and the bicycle, which the detector surfaces for the
# prompt but which no security rule treats as a subject or a vehicle.
_VEHICLE_CLASSES = VEHICLE_LABELS
_SUBJECT_CLASSES = SUBJECT_LABELS
_RELEVANT_CLASSES = (
    SUBJECT_LABELS
    | VEHICLE_LABELS
    | frozenset({"bicycle", "backpack", "handbag", "suitcase"})
)

# Cosine-similarity floor (0.0-1.0, InceptionResnetV1 512-d embeddings) for
# treating a detected face as matching an enrolled household member. Chosen
# conservatively — a missed match just means no RECOGNIZED RESIDENT hint
# (analysis proceeds exactly as it would with face recognition off), while a
# false match would wrongly reassure the model, so this errs toward "no
# match" when uncertain rather than the reverse.
_FACE_MATCH_THRESHOLD = 0.75

# Depth Anything V2 output is a *relative* (not metric) depth/disparity map,
# so "similar depth" is judged as a fraction of this clip's own depth range
# rather than an absolute unit. Below this fraction, two regions are treated
# as being at roughly the same distance from the camera.
_DEPTH_SIMILARITY_FRACTION = 0.15


# ----------------------------------------------------------------------
# Availability checks
# ----------------------------------------------------------------------


class CPUIncompatibleError(RuntimeError):
    """Raised by a stage's _load_sync() when torch_cpu_compatible() is
    False, instead of attempting the import that would otherwise crash the
    process. A distinct type from plain RuntimeError so each stage's
    ensure_ready() can catch this specific case (clean "unavailable on this
    CPU" warning) without also swallowing an unrelated RuntimeError from
    the model/import machinery itself (which should keep getting the
    existing generic-failure handling, traceback and all)."""


_CPU_INCOMPATIBLE_MESSAGE = (
    "this device's CPU is missing instructions PyTorch needs "
    "(common on Raspberry Pi 4 and older ARM boards; Raspberry "
    "Pi 5 is not affected)"
)

_HF_AUTH_FAILURE_MESSAGE = (
    "Hugging Face authentication failed while loading the %s model. "
    "The configured Hugging Face Token (HF_TOKEN) may be invalid, expired, "
    "or missing permission; update or clear it in the add-on Configuration tab."
)


def _is_huggingface_auth_error(exc: BaseException) -> bool:
    """Return whether an exception indicates rejected Hugging Face access."""
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if status_code in (401, 403):
        return True

    if type(exc).__name__.lower() in {"invalidtokenerror", "gatedrepoerror"}:
        return True

    message = str(exc).lower()
    return (
        "huggingface.co" in message
        or "huggingface hub" in message
        or "hf_hub" in message
    ) and (
        "401" in message
        or "403" in message
        or "unauthorized" in message
        or "invalid token" in message
        or "expired" in message
    )


def torch_cpu_compatible() -> bool:
    """Return True if this CPU can safely run PyTorch's official builds.

    PyTorch's official aarch64 wheels assume the CPU supports the ARMv8.1
    LSE atomic instructions (exposed as "atomics" in /proc/cpuinfo's
    Features line). Without them, importing torch — or any package that
    imports it, which is every stage below except FrameEnhancer — can
    crash the whole process with an illegal-instruction signal (SIGILL)
    rather than a catchable Python exception. This is a well-documented,
    still-unresolved upstream issue specific to Raspberry Pi 4 and older
    boards (Cortex-A72 and earlier predate LSE); Raspberry Pi 5's
    Cortex-A76 is unaffected, and x86_64 has no such requirement at all.
    Every torch-dependent stage's ``_load_sync`` below calls this *before*
    attempting its import, since a SIGILL can't be caught after the fact —
    prevention is the only option. An unreadable/unparseable /proc/cpuinfo
    is conservatively treated as unsupported, since a false "unavailable"
    just costs a feature, while a false "available" risks the crash this
    check exists to prevent.
    """
    if platform.machine() not in ("aarch64", "arm64"):
        return True
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
    except OSError:
        return False
    for line in cpuinfo.splitlines():
        if line.lower().startswith("features"):
            _, _, features = line.partition(":")
            return "atomics" in features.split()
    return False


def is_face_recognition_available() -> bool:
    """Return True if facenet_pytorch is importable and the CPU supports it.

    See :func:`torch_cpu_compatible` — facenet_pytorch depends on torch, so
    this must gate on CPU compatibility too, not just package presence.
    """
    if not torch_cpu_compatible():
        return False
    try:
        __import__("facenet_pytorch")
        return True
    except ImportError:
        return False


# ----------------------------------------------------------------------
# Stage 1: OpenCV preprocessing
# ----------------------------------------------------------------------


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
            with _native_import_lock:
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


# ----------------------------------------------------------------------
# Stage 2: object detection + tracking (Ultralytics YOLO + ByteTrack)
# ----------------------------------------------------------------------


@dataclass
class DetectedObject:
    """One detected object in one analyzed frame."""

    label: str
    confidence: float
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixel coords
    track_id: int | None
    frame_index: int


class ObjectDetector:
    """YOLO object detection + ByteTrack tracking (Ultralytics).

    The model is loaded once (and downloaded on first use, cached under
    /data — see ``_YOLO_MODEL_CACHE_DIR`` above) and reused across clips —
    reloading it per clip would make every analysis pay a multi-second
    cold-start cost. Inference runs in a thread executor so the asyncio
    event loop is never blocked, mirroring ``MoondreamLocalAnalyzer``'s
    pattern in ``analyzer.py`` for the same reason.

    Note on tracking: Ultralytics' ``.track(persist=True)`` is designed for
    continuous video frames. This pipeline only ever hands it the handful
    of frames already sampled for AI analysis (seconds apart, not
    consecutive video frames), so track IDs here are best-effort — useful
    for noticing "the same object across two sampled frames" but not a
    substitute for true continuous tracking.
    """

    def __init__(self, model_name: str = "yolo26n.pt") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Must be set before the ultralytics import below: it probes
        # ~/.config/Ultralytics for a writable settings dir as a side effect
        # of import (not lazily), and this container's $HOME isn't writable
        # by the add-on's runtime user. Finding it unwritable, ultralytics
        # falls back to /tmp on its own — but does so by printing raw,
        # unformatted lines straight to stdout (bypassing our logging setup
        # entirely, unlabeled and timestamp-free among otherwise-structured
        # log output) and using an ephemeral directory that doesn't survive
        # a container recreation. Pointing it at the same persistent,
        # known-writable directory the model weights already cache under
        # avoids both problems in one step: no fallback needed, so no
        # notice, and the settings file persists like the weights do.
        os.environ.setdefault("YOLO_CONFIG_DIR", _YOLO_MODEL_CACHE_DIR)
        # The whole body below (not just the import) runs under the shared
        # lock: this method only ever executes once per process (ensure_ready
        # already guards repeat calls), so the only cost is a one-time wait
        # if another stage's first load is in flight at the same moment -
        # see _native_import_lock's own comment for why that's necessary.
        with _native_import_lock:
            from ultralytics import YOLO  # type: ignore[import-not-found]

            # A bare filename (the default "yolo11n.pt", or any custom
            # ai_object_detection_model naming a standard pretrained
            # checkpoint) downloads into YOLO()'s cwd if given as-is;
            # resolving it against a persistent absolute directory first
            # makes that download (and every reload after a container
            # recreation) reuse the same file instead of silently
            # re-fetching it. A caller-supplied path that already has a
            # directory component is left untouched.
            model_path = self._model_name
            if os.path.basename(model_path) == model_path:
                os.makedirs(_YOLO_MODEL_CACHE_DIR, exist_ok=True)
                model_path = os.path.join(_YOLO_MODEL_CACHE_DIR, model_path)
            _LOGGER.info("Loading YOLO object-detection model '%s'", self._model_name)
            self._model = YOLO(model_path)
            _LOGGER.info("YOLO model '%s' ready", self._model_name)

    async def ensure_ready(self) -> bool:
        """Ensure the model is loaded. Returns True when ready."""
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
                    "ultralytics package is not installed, object detection "
                    "unavailable: %s. Install it with: pip install ultralytics",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Object detection unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load YOLO model")
                return False

    def _detect_in_frame(
        self, img: Any, idx: int, persist: bool
    ) -> list[DetectedObject]:
        results = self._model.track(
            img, persist=persist, tracker="bytetrack.yaml", verbose=False
        )
        if not results:
            return []
        result = results[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        names = result.names
        ids = boxes.id
        detections: list[DetectedObject] = []
        for i in range(len(boxes)):
            label = names.get(int(boxes.cls[i]), "")
            if label not in _RELEVANT_CLASSES:
                continue
            x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i])
            track_id = int(ids[i]) if ids is not None else None
            detections.append(
                DetectedObject(
                    label=label,
                    confidence=float(boxes.conf[i]),
                    box=(x1, y1, x2, y2),
                    track_id=track_id,
                    frame_index=idx,
                )
            )
        return detections

    def _detect_sync(self, frames: list[bytes]) -> list[DetectedObject]:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

        detections: list[DetectedObject] = []
        # persist=False on the first successfully-decoded frame of *this*
        # call forces Ultralytics to build fresh ByteTrack state
        # (ultralytics.trackers.track.on_predict_start: `if
        # hasattr(predictor, "trackers") and persist: return` — a call with
        # persist=False falls through and rebuilds the tracker even when
        # one already exists) instead of silently continuing whatever
        # tracker state this shared, long-lived model instance was left in
        # by the previous clip it analyzed (see this class's own
        # docstring: the model is reused across clips, potentially from a
        # completely different camera or hours/days apart). Without this,
        # track IDs — and therefore _build_tracking_hint's "same person
        # lingers across most sampled frames" signal — could spuriously
        # continue across an unrelated clip boundary. persist=True for
        # every later frame in *this* call preserves the intended
        # continuity across this one clip's own sampled frames.
        persist = False
        for idx, frame in enumerate(frames):
            arr = np.frombuffer(frame, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue
            detections.extend(self._detect_in_frame(img, idx, persist))
            persist = True
        return detections

    async def detect(self, frames: list[bytes]) -> list[DetectedObject] | None:
        """Detect+track relevant objects across *frames*. None if unavailable."""
        if not frames:
            return None
        async with _cv_slot():
            if not await self.ensure_ready():
                return None
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self._detect_sync, frames)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Object detection failed: %s", exc)
                return None


# Re-exported under this module's historical private name: the geometry
# itself now lives in security/geometry.py, shared with every security rule
# that measures the same distances.
_box_gap = box_gap


def _proximity_label(gap: float, vehicle_width: float) -> str:
    """Categorize a box gap (see :func:`_box_gap`) relative to vehicle width.

    Converts the pixel gap to an approximate feet estimate using the
    detected vehicle's own pixel width as scale, with tier language aligned
    to _car_protection_segment's own 1 ft / 3 ft behavioral thresholds so
    the two reinforce each other. Falls back to a qualitative-only label
    when vehicle_width isn't usable (a malformed/degenerate box — real YOLO
    detections always have positive width, so this is defensive only).
    """
    if gap <= 0:
        return "overlapping the detected vehicle's outline"
    feet = pixel_gap_to_feet(gap, vehicle_width)
    if feet is None:
        return "at an indeterminate distance from the detected vehicle"
    if feet < 1.0:
        return "well under 1 ft from the detected vehicle"
    if feet < 3.0:
        return f"approximately {feet:.0f} ft from the detected vehicle"
    return f"well away from the detected vehicle (roughly {feet:.0f} ft or more)"


def _frame_dimensions(frame: bytes) -> tuple[int, int] | None:
    """Decode *frame* far enough to read its ``(width, height)``.

    Everything the security layer computes — zone membership, ground
    distance, how much of the frame a subject fills — needs the frame's
    real resolution, which the detector's pixel boxes alone don't carry.
    ``None`` when the frame can't be decoded, which every caller treats as
    "no geometry available for this clip".
    """
    with _native_import_lock:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

    img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    height, width = img.shape[:2]
    return (width, height)


def _crop_region(img: Any, box: Box) -> Any:
    """Clamp *box* to *img*'s bounds and return that region of it.

    Always at least one pixel wide and tall. A detector box can sit partly
    (or, after the tracker extrapolates one, entirely) outside the frame,
    and an empty crop would fail further down in ways that read as a decode
    error rather than as the bad box it actually is.
    """
    height, width = img.shape[:2]
    x1 = max(0, min(width - 1, int(box[0])))
    y1 = max(0, min(height - 1, int(box[1])))
    x2 = max(x1 + 1, min(width, int(box[2])))
    y2 = max(y1 + 1, min(height, int(box[3])))
    return img[y1:y2, x1:x2]


def _vehicle_histogram(frame: bytes, box: Box) -> tuple[float, ...]:
    """Return a coarse colour fingerprint of *box*'s contents in *frame*.

    A 16×4 hue/saturation histogram of the cropped region, area-normalized.
    Hue and saturation rather than raw RGB so the same car scores similarly
    in morning sun and under a porch light; coarse bins because the job is
    "is this the silver hatchback or the red pickup", not fine-grained
    recognition. Empty when the crop can't be produced, which callers treat
    as "no appearance evidence" rather than as a mismatch.
    """
    with _native_import_lock:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

    img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return ()
    hsv = cv2.cvtColor(_crop_region(img, box), cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 4], [0, 180, 0, 256])
    return tuple(float(v) for v in (hist.flatten() / float(hist.sum())))


#: Side length the asset's region is resampled to before before/after
#: comparison. Small enough that the comparison is about structure rather
#: than sensor noise or a pixel of camera shake, large enough to notice a
#: dent-sized change in a vehicle-sized crop.
_CHANGE_PATCH = 64


def _region_appearance_change(before: bytes, after: bytes, box: Box) -> float | None:
    """How much *box*'s contents changed between two frames (0.0-1.0).

    The cheap, model-free half of "did the protected vehicle itself change
    during this clip" — a new dent, a thrown object left on the bonnet, a
    door left open. Both crops are contrast-normalized before comparison so
    that a cloud passing, a floodlight switching on, or the camera's own
    auto-exposure does not register as damage; what survives is structural
    difference in that one region.

    This is evidence, never a verdict: it says the region looks different,
    not that anything was damaged. ``None`` when either crop can't be
    produced.
    """
    with _native_import_lock:
        import cv2  # type: ignore[import-not-found]
        import numpy as np

    crops: list[Any] = []
    for data in (before, after):
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(_crop_region(img, box), cv2.COLOR_BGR2GRAY)
        try:
            resized = np.asarray(
                cv2.resize(gray, (_CHANGE_PATCH, _CHANGE_PATCH)), dtype="float32"
            )
        except Exception:  # noqa: BLE001
            # Every other stage in this module treats a failure as missing
            # evidence rather than an error, and this one is the least
            # important of them: no before/after comparison simply means the
            # impact rule has one fewer input.
            return None
        if resized.shape != (_CHANGE_PATCH, _CHANGE_PATCH):
            return None
        std = float(resized.std())
        # A region with no variance at all — a blown-out or fully black
        # crop — would divide by zero here and reach the impact rule as
        # NaN, where every threshold comparison silently evaluates False.
        # Dividing by one instead leaves it flat, which reads correctly as
        # "nothing structural changed".
        crops.append((resized - float(resized.mean())) / (std if std > 0 else 1.0))

    difference = float(np.abs(crops[0] - crops[1]).mean())
    # Two contrast-normalized, structurally unrelated crops differ by about
    # 1.1 on average, so halving maps "completely different" onto roughly
    # 0.55 and leaves headroom above it rather than saturating at 1.0.
    return min(1.0, difference / 2.0)


def _select_scan_frames(
    frames: list[bytes], cap: int, interval: float
) -> tuple[list[bytes], float]:
    """Pick an evenly-spaced subset of *frames* for the temporal scan.

    Object detection runs over this set rather than over the handful of
    frames chosen for the AI prompt, because those are picked by motion
    (entry, peak, exit) and are deliberately *not* evenly spaced — which
    makes "frame index × interval" the wrong clip time for them, and every
    duration, speed and trajectory derived from it wrong too. An even
    subset keeps the arithmetic honest at a bounded cost: *cap* frames of
    detection, whatever the clip's length.

    A *cap* of zero means the caller has already narrowed the pool to the
    prompt's own frames (``ai_temporal_scan_frames: 0``, documented as
    disabling the wider scan) — there is nothing left to subsample, so the
    list is returned as given.

    Returns the frames alongside the real seconds between them.
    """
    if cap <= 0 or len(frames) <= cap:
        return frames, interval
    step = math.ceil(len(frames) / cap)
    return frames[::step], interval * step


@dataclass(frozen=True)
class _ZoneReference:
    """A camera's configured car zone, resolved against a real frame size.

    Carries the zone itself rather than only its pixel bounds so the two
    questions it gets asked stay separate: *is this vehicle in the zone*
    is answered against the traced outline (:meth:`covers`), while the
    coarse "how far from roughly there" reference the distance hint falls
    back to keeps using :attr:`box`, where bounding-box precision is all
    the estimate claims anyway.
    """

    zone: Zone
    size: tuple[float, float]
    box: Box

    def covers(self, box: Box) -> bool:
        """True when *box* (pixel space) genuinely overlaps the drawn zone."""
        return self.zone.overlap_with_box(box, *self.size) > 0.0


def _car_zone_reference(zone: dict[str, Any], frame: bytes) -> _ZoneReference | None:
    """Resolve a normalized (0-1) ``car_zone`` dict — a rectangle or a
    freeform polygon, see ``media_server.py``'s ``_normalize_car_zone`` —
    against *frame*'s actual resolution, for disambiguating which YOLO
    vehicle detection is the protected car (see
    :func:`_best_subject_vehicle_pair`). None if *frame* fails to decode or
    the zone has no usable points.
    """
    parsed = Zone.from_config(zone)
    if parsed is None:
        return None
    size = _frame_dimensions(frame)
    if size is None:
        return None
    return _ZoneReference(zone=parsed, size=size, box=parsed.to_pixel_box(*size))


def _zone_preferred_vehicles(
    vehicles: list[DetectedObject], zone_ref: _ZoneReference | None
) -> list[DetectedObject]:
    """Narrow one frame's vehicles to the one the drawn zone points at.

    Vehicles actually inside the zone win outright; only when none is does
    proximity to its bounds decide, which is all there was to go on before
    the outline was honoured exactly. For a rectangle zone the two rules
    pick the same vehicle in every case — an overlapping box always has a
    smaller (negative) gap than a separated one. A frame with no zone, or
    with nothing to disambiguate, is handed back untouched.
    """
    if zone_ref is None or len(vehicles) <= 1:
        return vehicles
    inside = [v for v in vehicles if zone_ref.covers(v.box)]
    pool = inside or vehicles
    return [min(pool, key=lambda v: _box_gap(v.box, zone_ref.box))]


def _best_subject_vehicle_pair(
    detections: list[DetectedObject],
    zone_ref: _ZoneReference | None = None,
) -> tuple[DetectedObject, DetectedObject, int] | None:
    """Return the (subject, vehicle, frame_index) pair with the smallest gap.

    "Subject" is a person *or* an animal (see :data:`_SUBJECT_CLASSES`) — a
    dog jumping on a parked car or a cat pawing at one is exactly the kind
    of vehicle contact this pipeline needs to catch, not just a person's.
    Used to pick which single frame/box-pair the heavier depth and contact
    stages spend their budget refining. None if no sampled frame contains
    both a subject and a vehicle detection.

    *zone_ref* (see :func:`_car_zone_reference`), when given, disambiguates
    which detected vehicle is actually "the protected car" whenever a frame
    has more than one vehicle-class detection (a driveway camera that also
    sees the street, a second household vehicle, a neighbor's parked car,
    ...): only the one nearest the configured zone is considered as a
    candidate for that frame. Without this, a person standing next to an
    unrelated car could "win" the smallest-gap pairing below just because
    they're closer to it than anyone is to the actual protected vehicle,
    producing a depth/contact/proximity hint about the wrong car entirely.
    A single vehicle detection in a frame is left alone either way — there
    is nothing to disambiguate.
    """
    best: tuple[DetectedObject, DetectedObject, int] | None = None
    best_gap: float | None = None
    by_frame: dict[int, list[DetectedObject]] = {}
    for d in detections:
        by_frame.setdefault(d.frame_index, []).append(d)
    for frame_idx, items in by_frame.items():
        subjects = [d for d in items if d.label in _SUBJECT_CLASSES]
        vehicles = _zone_preferred_vehicles(
            [d for d in items if d.label in _VEHICLE_CLASSES], zone_ref
        )
        for s in subjects:
            for v in vehicles:
                gap = _box_gap(s.box, v.box)
                if best_gap is None or gap < best_gap:
                    best_gap = gap
                    best = (s, v, frame_idx)
    return best


def _detection_distance_pair(
    detections: list[DetectedObject],
    zone_ref: _ZoneReference | None,
    asset_box: Box | None,
    car_description: str,
) -> tuple[DetectedObject, Box] | None:
    """Pick the subject and vehicle box the distance estimate should use."""
    if not car_description:
        return None
    if asset_box is not None:
        subjects = [d for d in detections if d.label in _SUBJECT_CLASSES]
        if not subjects:
            return None
        return (min(subjects, key=lambda d: _box_gap(d.box, asset_box)), asset_box)
    legacy = _best_subject_vehicle_pair(detections, zone_ref)
    return (legacy[0], legacy[1].box) if legacy is not None else None


def _no_subject_sentence(detections: list[DetectedObject]) -> str:
    """State plainly that no person or animal was found, when none was.

    The detector sweeps every sampled frame, so "not one person in any of
    them" is the strongest single fact this pipeline produces about a clip
    — and it used to be the one fact the prompt never carried. Listing the
    classes that *were* found says nothing about the class that wasn't, and
    a small model handed several thousand words of person-centric rules
    plus a couple of frames would narrate a person into an empty driveway.
    The security layer already records this same observation as an evidence
    note, but that note rides along with the SECURITY EVIDENCE section,
    which is suppressed precisely when there are no events to report — so
    in the no-subject case it reached nothing.

    Deliberately evidence, not a verdict, and deliberately overridable: a
    person who is distant, partly hidden, or small in frame is exactly what
    a nano-scale detector misses, and a clip where that happens must still
    be describable as what it is. It never speaks to ``suspicious`` either
    — a vehicle can damage another vehicle with nobody present at all.
    Returns "" when a subject *was* detected, so the caller can drop it.
    """
    if any(d.label in _SUBJECT_CLASSES for d in detections):
        return ""
    return (
        "No person and no animal was detected in any sampled frame of this "
        "clip. Do not describe a person, or anyone's actions, unless you can "
        "plainly see one in these frames yourself — if you can, describe them "
        "and disregard this line, since a distant, small or partly hidden "
        "person can be missed."
    )


def _build_detection_hint(
    detections: list[DetectedObject],
    car_description: str,
    zone_ref: _ZoneReference | None = None,
    asset_box: Box | None = None,
) -> str | None:
    """Render detections into an OBJECT DETECTION prompt hint, or None if empty.

    *car_description* being empty means this camera isn't under
    protected-vehicle rules (see :meth:`VisionPipeline.process_clip`) — the
    detected-classes line is still useful generically, but the
    vehicle-distance estimate is skipped since there's no protected vehicle
    for it to be relevant to on this camera.

    *asset_box*, when the security layer has identified the protected
    vehicle, is the box this hint measures against — so the distance it
    states is a distance to *your* car, not to whichever vehicle a subject
    happened to stand nearest. *zone_ref* is the fallback used when no
    identification was made, matching the behaviour before that existed.

    An empty *detections* still produces a hint, rather than None: "the
    detector swept this clip and found nothing" is evidence, and saying
    nothing at all is what let a model narrate a subject into an empty
    driveway (see :func:`_no_subject_sentence`).
    """
    labels = sorted({d.label for d in detections})
    lines = [
        f"Detected object classes across sampled frames: {', '.join(labels)}."
        if labels
        else "No objects of any tracked class were detected in any sampled frame."
    ]
    lines.append(_no_subject_sentence(detections))
    lines = [line for line in lines if line]

    pair = _detection_distance_pair(detections, zone_ref, asset_box, car_description)
    if pair is not None:
        subject, vehicle_box = pair
        vehicle_width = vehicle_box[2] - vehicle_box[0]
        gap = _box_gap(subject.box, vehicle_box)
        proximity = _proximity_label(gap, vehicle_width)
        lines.append(
            f"Object-detection distance estimate: the detected {subject.label}'s "
            f"bounding box is {proximity} (pixel-based estimate from the "
            "object detector, not a physical measurement)."
        )

    return (
        "\n\nOBJECT DETECTION: "
        + " ".join(lines)
        + " Treat this as a precise, code-computed hint about what was "
        "actually detected and roughly where — cross-check it against what "
        "you can see in the frames yourself, since detector misses or "
        "false positives are possible."
    )


# Fraction of sampled frames a tracked person must appear in to count as
# "lingering" vs. "briefly passing through" — see _build_tracking_hint.
# Deliberately asymmetric (lingering needs a clear majority; passing-through
# only needs a small minority) so an ambiguous middle ground emits no hint
# at all rather than a low-confidence guess either way.
_TRACKING_LINGER_FRACTION = 0.6
_TRACKING_BRIEF_FRACTION = 0.3
# Below this many sampled frames, "how many frames did this track appear
# in" is too noisy a sample to characterize lingering vs. passing through.
_TRACKING_MIN_FRAMES = 3


def _build_tracking_hint(
    detections: list[DetectedObject], total_frames: int
) -> str | None:
    """Render ByteTrack continuity into a TRACKING prompt hint, or None.

    A single frame saying "person detected" says nothing about behavior
    over time — the actual value ByteTrack adds (see ObjectDetector) is
    knowing whether the *same* tracked person shows up across most of the
    sampled frames (lingering/casing) or just one or two (passing through).
    Uses whichever tracked person has the highest frame-presence fraction;
    ties and untracked detections (track_id is None, e.g. tracking wasn't
    available for this call) are simply not counted.
    """
    if total_frames < _TRACKING_MIN_FRAMES:
        return None

    frames_per_track: dict[int, set[int]] = {}
    for d in detections:
        if d.label != "person" or d.track_id is None:
            continue
        frames_per_track.setdefault(d.track_id, set()).add(d.frame_index)

    if not frames_per_track:
        return None

    best_track_id = max(frames_per_track, key=lambda t: len(frames_per_track[t]))
    frame_count = len(frames_per_track[best_track_id])
    fraction = frame_count / total_frames

    if fraction >= _TRACKING_LINGER_FRACTION:
        body = (
            f"the same tracked person appears in {frame_count} of "
            f"{total_frames} sampled frames spanning this clip — consistent "
            "with lingering or casing rather than simply passing through"
        )
    elif fraction <= _TRACKING_BRIEF_FRACTION:
        body = (
            f"the same tracked person appears in only {frame_count} of "
            f"{total_frames} sampled frames — consistent with briefly "
            "passing through rather than lingering"
        )
    else:
        return None

    return (
        "\n\nTRACKING: Across the sampled frames, " + body + ". This is a "
        "best-effort signal from tracking sparse sampled frames (seconds "
        "apart), not continuous video, so treat it as a hint rather than a "
        "precise measurement of how long anyone was actually present."
    )


# ----------------------------------------------------------------------
# Stage 3: depth estimation (Depth Anything V2 via transformers)
# ----------------------------------------------------------------------


@dataclass
class DepthComparison:
    """Relative-depth comparison between two detected regions in one frame."""

    similar_depth: bool
    subject_depth: float
    vehicle_depth: float


class DepthEstimator:
    """Monocular depth estimation (Depth Anything V2) via transformers.

    Requires object detection to also be enabled — depth is only computed
    at already-detected person/vehicle locations, to tell "overlapping in
    the 2D frame" apart from "actually at the same distance from the
    camera". Output is a *relative* depth map (no camera calibration), so
    comparisons here are qualitative ("about the same distance" vs
    "noticeably different distances"), never a physical measurement.
    """

    # Default/fallback checkpoint — see ai_depth_estimation_model in
    # config.yaml for the other selectable sizes (and their licensing:
    # this default is Apache-2.0, the larger Base/Large options are
    # CC-BY-NC-4.0/non-commercial — see that option's own comment).
    _DEFAULT_MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"

    # Empty default means no token configured; it is not a credential.
    def __init__(
        self,
        hf_token: str = "",
        model_id: str = _DEFAULT_MODEL_ID,  # nosec B107
    ) -> None:
        self._pipe: Any = None
        self._hf_token = hf_token
        # Named apart from the class-level default above rather than
        # shadowing it in a different case: one is the fallback checkpoint,
        # the other is whichever checkpoint this instance was actually told
        # to load, and a reader glancing at `_MODEL_ID` vs `_model_id` has
        # no way to tell which is which.
        self._model_id = model_id
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # _native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with _native_import_lock:
            from transformers import pipeline  # type: ignore[import-not-found]

            _LOGGER.info("Loading depth-estimation model '%s'", self._model_id)
            self._pipe = pipeline(
                task="depth-estimation",
                model=self._model_id,
                device="cpu",
                token=self._hf_token or None,
            )
            _LOGGER.info("Depth-estimation model ready")

    async def ensure_ready(self) -> bool:
        if self._pipe is not None:
            return True
        async with self._get_lock():
            if self._pipe is not None:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_sync)
                return True
            except ImportError as exc:
                _LOGGER.warning(
                    "transformers package is not installed, depth estimation "
                    "unavailable: %s. Install it with: pip install transformers",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Depth estimation unavailable: %s", exc)
                return False
            except Exception as exc:
                if _is_huggingface_auth_error(exc):
                    _LOGGER.error(_HF_AUTH_FAILURE_MESSAGE, "depth-estimation")
                else:
                    _LOGGER.exception("Failed to load depth-estimation model")
                return False

    def _compare_sync(
        self,
        frame: bytes,
        subject_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> DepthComparison | None:
        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(frame)).convert("RGB")
        result = self._pipe(image)
        depth_arr = np.array(result["depth"], dtype=np.float32)
        height, width = depth_arr.shape[:2]

        def region_mean(box: tuple[float, float, float, float]) -> float | None:
            x1, y1, x2, y2 = box
            xi1, yi1 = max(0, int(x1)), max(0, int(y1))
            xi2, yi2 = min(width, int(x2)), min(height, int(y2))
            if xi2 <= xi1 or yi2 <= yi1:
                return None
            region = depth_arr[yi1:yi2, xi1:xi2]
            return float(region.mean()) if region.size else None

        subject_depth = region_mean(subject_box)
        vehicle_depth = region_mean(vehicle_box)
        if subject_depth is None or vehicle_depth is None:
            return None

        depth_range = max(float(depth_arr.max() - depth_arr.min()), 1e-6)
        normalized_diff = abs(subject_depth - vehicle_depth) / depth_range
        return DepthComparison(
            similar_depth=normalized_diff < _DEPTH_SIMILARITY_FRACTION,
            subject_depth=subject_depth,
            vehicle_depth=vehicle_depth,
        )

    async def compare(
        self,
        frame: bytes,
        subject_box: tuple[float, float, float, float],
        vehicle_box: tuple[float, float, float, float],
    ) -> DepthComparison | None:
        async with _cv_slot():
            if not await self.ensure_ready():
                return None
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._compare_sync, frame, subject_box, vehicle_box
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Depth estimation failed: %s", exc)
                return None


def _build_depth_hint(result: DepthComparison, subject_label: str) -> str:
    """Render a depth comparison into a DEPTH ESTIMATE prompt hint.

    *subject_label* is the actual detected class ("person", "dog", "cat",
    ...) from :class:`DetectedObject` — named explicitly rather than the
    generic "person/animal" this used to say unconditionally, since the
    depth/contact pairing already treats animals as valid subjects (see
    :data:`_SUBJECT_CLASSES`) and the hint text should match.
    """
    if result.similar_depth:
        body = (
            f"the detected {subject_label} and detected vehicle appear to be "
            "at roughly the same distance from the camera — consistent "
            "with them actually being near the vehicle in 3D space, not "
            "just overlapping it in the 2D frame"
        )
    else:
        # Depth Anything's output is inverse depth/disparity (verified
        # against the installed transformers pipeline source, whose
        # postprocess() min-max-normalizes predicted_depth without
        # inverting it, plus the model's own documented convention): a
        # LARGER region value means CLOSER to the camera. subject_depth >
        # vehicle_depth therefore means the subject is nearer the camera
        # than the vehicle (the near/same side, in plain view); the
        # reverse means the subject is farther away than the vehicle from
        # the camera's viewpoint — consistent with standing on the
        # vehicle's far side, partly hidden behind it from this camera's
        # angle, which is worth flagging as extra-scrutiny-worthy on its
        # own, distinct from simple overlap-in-2D ambiguity.
        if result.subject_depth > result.vehicle_depth:
            side = (
                f"the {subject_label} appears nearer to the camera than the "
                "vehicle — the near/same side, in plain view"
            )
        else:
            side = (
                f"the {subject_label} appears farther from the camera than "
                "the vehicle — consistent with being on the vehicle's far "
                "side, partly out of this camera's clear view, which "
                "deserves extra scrutiny"
            )
        body = (
            f"the detected {subject_label} and detected vehicle appear to be "
            f"at noticeably different distances from the camera: {side}. "
            "They may only appear close together because one is in front "
            "of the other from this camera's angle, not because they're "
            "actually near the vehicle"
        )
    return (
        "\n\nDEPTH ESTIMATE: A monocular depth model estimates that "
        + body
        + ". This is a relative, best-effort estimate (not a precise "
        "measurement) — weigh it alongside what you can see in the frames."
    )


# ----------------------------------------------------------------------
# Stage 4: contact segmentation (SAM2 via transformers)
# ----------------------------------------------------------------------


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
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # _native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with _native_import_lock:
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
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Contact segmentation unavailable: %s", exc)
                return False
            except Exception as exc:
                if _is_huggingface_auth_error(exc):
                    _LOGGER.error(_HF_AUTH_FAILURE_MESSAGE, "SAM2")
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
        async with _cv_slot():
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


# ----------------------------------------------------------------------
# Stage 5: pose estimation (Ultralytics YOLO-pose)
# ----------------------------------------------------------------------

# COCO 17-keypoint indices, as emitted by every YOLO pose model.
_KP_LEFT_SHOULDER = 5
_KP_RIGHT_SHOULDER = 6
_KP_LEFT_WRIST = 9
_KP_RIGHT_WRIST = 10
_KP_LEFT_HIP = 11
_KP_RIGHT_HIP = 12
_KP_LEFT_ANKLE = 15
_KP_RIGHT_ANKLE = 16

# Minimum per-keypoint confidence before a joint is used at all. A pose
# model will happily place a wrist it cannot actually see, and a
# hallucinated wrist is exactly what would turn someone standing with their
# arms down into "reaching toward the vehicle".
_KEYPOINT_CONFIDENCE = 0.5

# Overlap a pose model's own person box must have with the tracked subject's
# before its skeleton is attributed to them.
_POSE_MATCH_IOU = 0.3

# How far past the shoulder, as a fraction of shoulder width, a wrist must
# extend toward the asset before it counts as reaching for it rather than
# simply hanging at the subject's side.
_REACH_SHOULDER_FRACTION = 0.6

# Height a wrist must clear its own shoulder by, as a fraction of the
# subject's box height, to read as a raised arm.
_ARM_RAISED_FRACTION = 0.06

# Share of the subject's box height the hips must sit within of the ankles
# before the pose reads as crouched rather than standing.
_CROUCH_FRACTION = 0.33


@dataclass
class PostureResult:
    """What a subject's body was doing at the moment that mattered.

    Three narrow, scale-free facts rather than an action label. Naming an
    action ("kicking", "prying") from a single sparse frame is a claim this
    pipeline cannot support; "a wrist is extended toward the vehicle" is one
    it can, and it is the part that actually changes how concerning the
    moment is.
    """

    reaching: bool = False
    arm_raised: bool = False
    crouching: bool = False
    confidence: float = 0.0

    @property
    def any_posture(self) -> bool:
        """True when at least one posture was established."""
        return self.reaching or self.arm_raised or self.crouching

    def describe(self) -> str:
        """A short phrase naming whatever was established, for prompts."""
        parts = []
        if self.reaching:
            parts.append("an arm extended toward it")
        if self.arm_raised:
            parts.append("an arm raised above shoulder height")
        if self.crouching:
            parts.append("a crouched or bent-over posture")
        return " and ".join(parts)


class PoseEstimator:
    """Body-keypoint estimation for the subject nearest a protected asset.

    Runs on exactly one frame per clip — the moment the depth and contact
    stages already examine — because that is the moment whose meaning
    changes: standing two feet from a car with your arms at your sides and
    standing two feet from it with an arm extended into the window are
    indistinguishable from a bounding box and obvious from a skeleton.

    A separate, smaller model from the detector's: pose weights are their
    own checkpoint, and making this its own toggle keeps anyone who just
    wants object detection from paying for a download they will not use.
    """

    def __init__(self, model_name: str = "yolo26n-pose.pt") -> None:
        self._model_name = model_name
        self._model: Any = None
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load_sync(self) -> None:
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        os.environ.setdefault("YOLO_CONFIG_DIR", _YOLO_MODEL_CACHE_DIR)
        # Whole body under the shared lock, not just the import — see
        # _native_import_lock's comment and ObjectDetector._load_sync.
        with _native_import_lock:
            from ultralytics import YOLO  # type: ignore[import-not-found]

            model_path = self._model_name
            if os.path.basename(model_path) == model_path:
                os.makedirs(_YOLO_MODEL_CACHE_DIR, exist_ok=True)
                model_path = os.path.join(_YOLO_MODEL_CACHE_DIR, model_path)
            _LOGGER.info("Loading YOLO pose model '%s'", self._model_name)
            self._model = YOLO(model_path)
            _LOGGER.info("YOLO pose model '%s' ready", self._model_name)

    async def ensure_ready(self) -> bool:
        """Ensure the model is loaded. Returns True when ready."""
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
                    "ultralytics package is not installed, pose estimation "
                    "unavailable: %s. Install it with: pip install ultralytics",
                    exc,
                )
                return False
            except CPUIncompatibleError as exc:
                _LOGGER.warning("Pose estimation unavailable: %s", exc)
                return False
            except Exception:
                _LOGGER.exception("Failed to load YOLO pose model")
                return False

    def _analyze_sync(
        self, frame: bytes, subject_box: Box, asset_box: Box
    ) -> PostureResult | None:
        with _native_import_lock:
            import cv2  # type: ignore[import-not-found]
            import numpy as np

        img = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        results = self._model(img, verbose=False)
        if not results:
            return None
        keypoints = _best_pose_keypoints(results[0], subject_box)
        if keypoints is None:
            return None
        return _posture_from_keypoints(keypoints, subject_box, asset_box)

    async def analyze(
        self, frame: bytes, subject_box: Box, asset_box: Box
    ) -> PostureResult | None:
        """Return what *subject_box*'s occupant was doing, or None if unavailable."""
        async with _cv_slot():
            if not await self.ensure_ready():
                return None
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._analyze_sync, frame, subject_box, asset_box
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Pose estimation failed: %s", exc)
                return None


def _best_pose_keypoints(
    result: Any, subject_box: Box
) -> list[tuple[float, float, float]] | None:
    """Pick the detected skeleton belonging to *subject_box*.

    A pose model finds every person in frame; attributing a bystander's
    raised arm to the person at the car would be worse than reporting
    nothing at all, so the match is by box overlap and a non-overlapping
    best candidate is discarded.
    """
    keypoints = getattr(result, "keypoints", None)
    boxes = getattr(result, "boxes", None)
    if keypoints is None or boxes is None or len(boxes) == 0:
        return None

    best_index = -1
    best_iou = 0.0
    for i in range(len(boxes)):
        candidate = tuple(float(v) for v in boxes.xyxy[i])
        overlap = box_iou(
            (candidate[0], candidate[1], candidate[2], candidate[3]), subject_box
        )
        if overlap > best_iou:
            best_iou, best_index = overlap, i
    if best_index < 0 or best_iou < _POSE_MATCH_IOU:
        return None

    data = keypoints.data[best_index]
    return [(float(p[0]), float(p[1]), float(p[2])) for p in data]


def _keypoint(
    keypoints: list[tuple[float, float, float]], index: int
) -> tuple[float, float] | None:
    """Return a confidently-placed keypoint, or None."""
    if index >= len(keypoints):
        return None
    x, y, confidence = keypoints[index]
    return (x, y) if confidence >= _KEYPOINT_CONFIDENCE else None


def _posture_from_keypoints(
    keypoints: list[tuple[float, float, float]], subject_box: Box, asset_box: Box
) -> PostureResult:
    """Derive the three posture facts from one skeleton.

    Everything is measured against the subject's own box, so the same
    posture reads identically whether they fill the frame or a fifth of it.
    """
    height = subject_box[3] - subject_box[1]
    if height <= 0:
        return PostureResult()

    shoulders = [
        _keypoint(keypoints, _KP_LEFT_SHOULDER),
        _keypoint(keypoints, _KP_RIGHT_SHOULDER),
    ]
    wrists = [
        _keypoint(keypoints, _KP_LEFT_WRIST),
        _keypoint(keypoints, _KP_RIGHT_WRIST),
    ]
    hips = [_keypoint(keypoints, _KP_LEFT_HIP), _keypoint(keypoints, _KP_RIGHT_HIP)]
    ankles = [
        _keypoint(keypoints, _KP_LEFT_ANKLE),
        _keypoint(keypoints, _KP_RIGHT_ANKLE),
    ]

    result = PostureResult(confidence=_pose_confidence(keypoints))
    known_shoulders = [s for s in shoulders if s is not None]
    known_wrists = [w for w in wrists if w is not None]

    if known_shoulders and known_wrists:
        shoulder_y = min(s[1] for s in known_shoulders)
        result.arm_raised = any(
            w[1] < shoulder_y - _ARM_RAISED_FRACTION * height for w in known_wrists
        )

    if len(known_shoulders) == 2 and known_wrists:
        result.reaching = _is_reaching(known_shoulders, known_wrists, asset_box)

    known_hips = [h for h in hips if h is not None]
    known_ankles = [a for a in ankles if a is not None]
    if known_hips and known_ankles:
        drop = min(a[1] for a in known_ankles) - max(h[1] for h in known_hips)
        result.crouching = drop < _CROUCH_FRACTION * height

    return result


def _is_reaching(
    shoulders: list[tuple[float, float]],
    wrists: list[tuple[float, float]],
    asset_box: Box,
) -> bool:
    """True when a wrist extends past the torso toward *asset_box*.

    Horizontal only, and measured relative to shoulder width: an arm at rest
    hangs within the body's own outline, while one reaching for a door
    handle or a window plainly does not. Depth is unavailable here, so a
    subject reaching directly away from the camera is missed — a false
    negative, which is the right direction for this to fail in.
    """
    shoulder_width = abs(shoulders[0][0] - shoulders[1][0])
    if shoulder_width <= 0:
        return False
    torso_x = (shoulders[0][0] + shoulders[1][0]) / 2.0
    asset_x = (asset_box[0] + asset_box[2]) / 2.0
    direction = 1.0 if asset_x > torso_x else -1.0
    threshold = _REACH_SHOULDER_FRACTION * shoulder_width
    return any((w[0] - torso_x) * direction > threshold for w in wrists)


def _pose_confidence(keypoints: list[tuple[float, float, float]]) -> float:
    """Mean confidence of the keypoints that were placed at all."""
    placed = [p[2] for p in keypoints if p[2] >= _KEYPOINT_CONFIDENCE]
    return sum(placed) / len(placed) if placed else 0.0


def _build_posture_hint(result: PostureResult, subject_label: str) -> str | None:
    """Render a posture result into a POSTURE prompt hint, or None."""
    if not result.any_posture:
        return None
    return (
        f"\n\nPOSTURE: Body-keypoint estimation places the detected {subject_label} "
        f"with {result.describe()} at the closest moment. This is derived from "
        "estimated joint positions in a single frame, not from watching the "
        "movement, so treat it as a hint about what the body was doing and "
        "confirm it against the frames."
    )


# ----------------------------------------------------------------------
# Stage 6: local-only face recognition (facenet-pytorch)
# ----------------------------------------------------------------------


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
        if not torch_cpu_compatible():
            raise CPUIncompatibleError(_CPU_INCOMPATIBLE_MESSAGE)
        # Whole body under the shared lock, not just the import - see
        # _native_import_lock's comment and ObjectDetector._load_sync above
        # for why (this method also only ever runs once per process).
        with _native_import_lock:
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
            except CPUIncompatibleError as exc:
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
        async with _cv_slot():
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
                embeddings = await self._embedder.embed(frame)
                for embedding in embeddings:
                    best_name: str | None = None
                    best_approved = False
                    best_similarity = _FACE_MATCH_THRESHOLD
                    for enrollment in enrollments:
                        similarity = cosine_similarity(
                            embedding, enrollment["embedding"]
                        )
                        if similarity >= best_similarity:
                            best_similarity = similarity
                            best_name = str(enrollment["name"])
                            best_approved = bool(enrollment.get("approved", True))
                    if best_name is None:
                        result.unrecognized_present = True
                    elif best_approved:
                        approved.add(best_name)
                    else:
                        other.add(best_name)
            result.approved_names = sorted(approved)
            result.other_names = sorted(other)
            return result
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Face recognition failed: %s", exc)
            return FaceRecognitionResult()


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


# ----------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------


@dataclass
class VisionConfig:
    """Which optional computer-vision stages are enabled (see config.py).

    ``enhanced_detection_enabled`` gates frame preprocessing, object
    detection/tracking, depth estimation, and contact segmentation together
    — depth/segmentation have always required detection to run at all, so
    these four stages are one on/off setting rather than four independently
    toggleable ones. Face recognition remains separate since it's a
    privacy-sensitive, not just heavier-compute, feature.
    """

    enhanced_detection_enabled: bool = False
    object_detection_model: str = "yolo26n.pt"
    depth_estimation_model: str = "depth-anything/Depth-Anything-V2-Small-hf"
    face_recognition_enabled: bool = False
    #: Body-keypoint estimation for the subject nearest a protected asset.
    #: Its own toggle, and its own (separate, small) model checkpoint — a
    #: user who wants object detection should not pay for a download they
    #: will never use.
    pose_estimation_enabled: bool = False
    pose_model: str = "yolo26n-pose.pt"
    hf_token: str = ""
    #: How many evenly-spaced frames the temporal scan runs detection over
    #: (see :func:`_select_scan_frames`). This is the single knob that trades
    #: detection cost against how much of the clip's *behaviour* — dwell,
    #: approach, retreat — the security layer can see.
    temporal_scan_frames: int = 12
    #: Whether the structured security layer runs at all. Costs no extra
    #: model inference when detection is already on, so it defaults to
    #: enabled; turning it off falls back to the plain prompt hints.
    security_events_enabled: bool = True


#: Names of the optional evidence sources, as reported in
#: ``VisionHints.unavailable_sources`` and rendered verbatim into the
#: prompt's "Evidence sources unavailable for this clip" line. Named
#: constants rather than repeated literals because they are compared and
#: counted downstream — ``security.evidence.assess_evidence`` scores stage
#: coverage from the length of that list, so a typo here would silently
#: change an evidence-quality score rather than fail.
SOURCE_OBJECT_DETECTION = "object detection"
SOURCE_DEPTH_ESTIMATION = "depth estimation"
SOURCE_CONTACT_SEGMENTATION = "contact segmentation"
SOURCE_POSE_ESTIMATION = "pose estimation"
SOURCE_FACE_RECOGNITION = "face recognition"


@dataclass
class VisionHints:
    """Per-clip output of :meth:`VisionPipeline.process_clip`."""

    enhanced_frames: list[bytes] | None = None
    detection_hint: str | None = None
    tracking_hint: str | None = None
    depth_hint: str | None = None
    contact_hint: str | None = None
    recognized_resident_hint: str | None = None
    posture_hint: str | None = None
    face_recognition: FaceRecognitionResult | None = None
    # Raw per-object detections from ObjectDetector, kept alongside the
    # rendered detection_hint text above so callers (analyzer.py) can
    # persist structured results — see database.py's detected_objects
    # table — rather than only ever having the flattened prompt string.
    detections: list[DetectedObject] | None = None

    # --- structured security evidence (see blink_downloader.security) ---
    # Everything below is raw material for the deterministic event
    # detector, which analyzer.py runs: this module's job ends at producing
    # measurements, and the rules that interpret them live where they can
    # be tested without torch installed.
    tracks: list[ObjectTrack] | None = None
    asset: ProtectedAsset | None = None
    frame_size: tuple[float, float] | None = None
    #: Frames the temporal scan actually covered, and the real seconds
    #: between them — the security layer's whole sense of time.
    scan_frame_count: int = 0
    scan_interval: float = 0.0
    #: Which track the depth/contact stages examined, so their verdicts are
    #: attributed to the right subject rather than to whoever else was in
    #: frame.
    contact_track_id: int | None = None
    depth_similar: bool | None = None
    contact_touching: bool | None = None
    #: How much the protected asset's own image region changed between the
    #: start and end of the clip (see :func:`_region_appearance_change`).
    asset_appearance_change: float | None = None
    #: What the subject's body was doing at the closest moment, when pose
    #: estimation is enabled and found them.
    posture: PostureResult | None = None
    #: Optional stages that produced nothing for this clip, named so the
    #: final result can say which evidence was missing instead of quietly
    #: concluding without it.
    unavailable_sources: list[str] = field(default_factory=list)
    #: An updated learned vehicle signature to persist, set only when this
    #: clip identified the protected vehicle confidently enough to learn
    #: from (see :mod:`blink_downloader.security.vehicles`).
    vehicle_signature_update: VehicleSignature | None = None


class VisionPipeline:
    """Orchestrates the optional computer-vision stages for one clip.

    Each stage is independently toggleable via *config* and gracefully
    no-ops (leaving the corresponding hint unset) if its dependency isn't
    installed, fails to load, or simply finds nothing relevant — analysis
    always proceeds using whatever hints ended up available, exactly like
    the existing scene-baseline/zone-motion hints in ``analyzer.py``.
    """

    def __init__(self, config: VisionConfig, db: ClipDatabase | None = None) -> None:
        self._config = config
        self._db = db
        self._detector = ObjectDetector(config.object_detection_model)
        self._depth = DepthEstimator(config.hf_token, config.depth_estimation_model)
        self._segmenter = ContactSegmenter(config.hf_token)
        self._pose = PoseEstimator(config.pose_model)
        self._face_embedder = FaceEmbedder()

    async def process_clip(
        self,
        frames: list[bytes],
        car_description: str = "",
        car_protection_applies: bool = False,
        raw_frames: list[bytes] | None = None,
        car_zone: dict[str, Any] | None = None,
        camera: str = "",
        frame_interval: float = 2.0,
        vehicle_signature: VehicleSignature | None = None,
    ) -> VisionHints:
        """Run every enabled stage and return this clip's hints and evidence.

        *frames* are the handful already down-selected for the AI prompt.
        *raw_frames*, when given, is the full pre-down-selection extraction
        pool, and is what the temporal scan and face recognition both work
        from — for different reasons that happen to point the same way.
        Face recognition needs the widest possible pool because the clearest
        view of a face is often a *low*-motion moment the down-selection
        drops; the temporal scan needs it because down-selected frames are
        deliberately unevenly spaced, which makes every duration and speed
        derived from them wrong. See :func:`_select_scan_frames`.

        *car_protection_applies* must reflect whether protected-vehicle
        rules apply to *this specific camera* (see
        ``BaseAnalyzer._car_protection_applies``) — not merely whether a
        protected vehicle is described somewhere on the property. Every
        vehicle-related stage is skipped entirely when False, so a camera
        that doesn't view the protected vehicle never generates vehicle
        evidence just because it happened to see a car.

        *car_zone*, *vehicle_signature* and the colour fingerprints computed
        below are the three pieces of evidence that decide *which* detected
        vehicle is the protected one — see
        :func:`~blink_downloader.security.vehicles.identify_protected_vehicle`.
        """
        hints = VisionHints()
        if not frames:
            return hints

        # Face recognition must always match against raw (pre-enhancement)
        # frames, since enrolled reference embeddings are computed from raw
        # reference photos — matching enhanced frames against raw references
        # is an embedding-space mismatch that can cause real matches (approved
        # household members) to be missed. Captured before `frames` is
        # potentially reassigned to CLAHE-enhanced frames below.
        raw_pool = raw_frames if raw_frames else frames

        if self._config.enhanced_detection_enabled:
            await self._run_detection_stages(
                hints,
                frames,
                raw_pool,
                camera=camera,
                car_description=car_description if car_protection_applies else "",
                car_zone=car_zone if car_protection_applies else None,
                frame_interval=frame_interval,
                vehicle_signature=vehicle_signature,
            )
        else:
            hints.unavailable_sources.append(SOURCE_OBJECT_DETECTION)
            hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
            hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
            hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)

        if self._config.face_recognition_enabled and self._db is not None:
            recognizer = FaceRecognizer(self._face_embedder, self._db)
            face_result = await recognizer.recognize(raw_pool)
            hints.face_recognition = face_result
            hints.recognized_resident_hint = _build_recognition_hint(face_result)
        else:
            hints.unavailable_sources.append(SOURCE_FACE_RECOGNITION)

        # The only per-clip evidence any of this ran was the one-time
        # "model ready" INFO log each stage prints on its first load —
        # after that, every stage below runs (and produces real hints that
        # do reach the AI prompt) completely silently, even at debug level.
        # Names are deliberately never logged here, matching
        # _build_recognition_hint's own name-free prompt guarantee — only
        # counts, mirroring what the prompt itself is allowed to say.
        _LOGGER.debug(
            "Vision pipeline result: enhanced_detection=%s "
            "(detection=%r, tracking=%r, depth=%r, contact=%r, tracks=%d, "
            "scan_frames=%d), face_recognition=%s "
            "(approved=%d, other=%d, unrecognized_present=%s)",
            self._config.enhanced_detection_enabled,
            hints.detection_hint,
            hints.tracking_hint,
            hints.depth_hint,
            hints.contact_hint,
            len(hints.tracks or []),
            hints.scan_frame_count,
            self._config.face_recognition_enabled,
            len(hints.face_recognition.approved_names) if hints.face_recognition else 0,
            len(hints.face_recognition.other_names) if hints.face_recognition else 0,
            hints.face_recognition.unrecognized_present
            if hints.face_recognition
            else False,
        )

        return hints

    async def _run_detection_stages(
        self,
        hints: VisionHints,
        frames: list[bytes],
        raw_pool: list[bytes],
        camera: str,
        car_description: str,
        car_zone: dict[str, Any] | None,
        frame_interval: float,
        vehicle_signature: VehicleSignature | None,
    ) -> None:
        """Frame preprocessing, detection, tracking and vehicle identification."""
        hints.enhanced_frames = FrameEnhancer.enhance(frames)

        # `0` means "no wider scan" (see DOCS.md's ai_temporal_scan_frames):
        # detect over the frames the prompt already uses, not over the whole
        # raw extraction, which would be the most expensive setting there is
        # rather than the cheapest one the option promises.
        cap = self._config.temporal_scan_frames
        selected, scan_interval = _select_scan_frames(
            raw_pool if cap > 0 else frames, cap, frame_interval
        )
        # Enhancement includes denoising, which is the most expensive thing
        # this module does without a model behind it. When the scan set is
        # the very same list that was just enhanced for the prompt — the
        # case whenever no wider raw pool was supplied — reuse that result
        # rather than paying for it twice on hardware where it matters.
        scan_frames = (
            hints.enhanced_frames
            if selected is frames
            else FrameEnhancer.enhance(selected)
        )
        hints.scan_frame_count = len(scan_frames)
        hints.scan_interval = scan_interval

        detections = await self._detector.detect(scan_frames)
        hints.detections = detections
        if not detections:
            if detections is None:
                hints.unavailable_sources.append(SOURCE_OBJECT_DETECTION)
            else:
                # Ran and found nothing, which is not the same as not having
                # run — only the former is evidence, and conflating the two
                # would tell the model "nobody was here" on a clip the
                # detector never actually looked at.
                hints.detection_hint = _build_detection_hint([], car_description)
            hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
            hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
            hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)
            return

        # A frame that won't decode costs the security layer its geometry,
        # but the object-detection and tracking hints below are built from
        # the detections alone and stay just as valid — returning early here
        # would drop them for no reason.
        frame_size = _frame_dimensions(scan_frames[0])
        hints.frame_size = (
            (float(frame_size[0]), float(frame_size[1])) if frame_size else None
        )

        if self._config.security_events_enabled and hints.frame_size is not None:
            hints.tracks = build_tracks(
                [
                    (d.label, d.confidence, d.box, d.track_id, d.frame_index)
                    for d in detections
                ],
                scan_interval,
                hints.frame_size,
            )
            hints.asset = self._resolve_asset(
                hints, scan_frames, camera, car_description, car_zone, vehicle_signature
            )

        zone_ref = (
            _car_zone_reference(car_zone, scan_frames[0])
            if car_zone and car_description
            else None
        )
        asset_box = hints.asset.box if hints.asset and hints.asset.present else None
        hints.detection_hint = _build_detection_hint(
            detections, car_description, zone_ref, asset_box
        )
        hints.tracking_hint = _build_tracking_hint(detections, len(scan_frames))

        await self._run_pair_stages(hints, scan_frames, detections, zone_ref)

    def _resolve_asset(
        self,
        hints: VisionHints,
        scan_frames: list[bytes],
        camera: str,
        car_description: str,
        car_zone: dict[str, Any] | None,
        vehicle_signature: VehicleSignature | None,
    ) -> ProtectedAsset | None:
        """Work out which detected vehicle, if any, is the protected one."""
        if not car_description or hints.tracks is None or hints.frame_size is None:
            return None

        # A colour fingerprint per candidate vehicle, taken from the frame
        # where the detector saw it most confidently. Only computed when
        # there is a learned signature to compare against — otherwise it is
        # decode work whose result nothing would read.
        histograms: dict[int | None, tuple[float, ...]] = {}
        if vehicle_signature is not None and vehicle_signature.histogram:
            for track in hints.tracks:
                # Untracked vehicles share a track id of None, so a
                # fingerprint stored for one would be handed to all of them
                # (and only the last decode would survive). Skipping them
                # here also saves the decode.
                if track.label not in VEHICLE_LABELS or track.track_id is None:
                    continue
                best = max(track.points, key=lambda pt: pt.confidence)
                if 0 <= best.frame_index < len(scan_frames):
                    histograms[track.track_id] = _vehicle_histogram(
                        scan_frames[best.frame_index], best.box
                    )

        asset = resolve_vehicle_asset(
            camera,
            car_description,
            hints.tracks,
            hints.frame_size,
            zone=Zone.from_config(car_zone),
            signature=vehicle_signature,
            histograms=histograms,
        )
        # resolve_vehicle_asset only declines when it is given no
        # description, and this method returned above if that were the case.
        assert asset is not None
        hints.vehicle_signature_update = self._learn_signature(
            asset, hints, scan_frames, vehicle_signature
        )
        return asset

    @staticmethod
    def _learn_signature(
        asset: ProtectedAsset,
        hints: VisionHints,
        scan_frames: list[bytes],
        current: VehicleSignature | None,
    ) -> VehicleSignature | None:
        """Fold a confident sighting into this camera's learned signature.

        Only confident identifications teach: learning from a guess is how
        a signature drifts onto the neighbour's car and stays there, which
        would make the whole mechanism worse than not having it. The blend
        itself is deliberately slow — see
        :meth:`~blink_downloader.security.vehicles.VehicleSignature.blend`.
        """
        identification = asset.identification
        if (
            identification is None
            or not identification.confident
            or identification.protected is None
            or hints.frame_size is None
            or not scan_frames
        ):
            return None
        protected = identification.protected
        normalized = protected.normalized_box
        # `protected.box` is a median across sightings and belongs to no
        # single frame; cropping it out of frame 0 samples whatever was
        # there then, which for a car that pulls in mid-clip is empty
        # driveway. Use the sighting the detector was surest of — the same
        # choice `_resolve_asset` makes for the fingerprints this one is
        # later compared against, so both are measured the same way.
        # The range check mirrors `_resolve_asset`'s own: the sighting index
        # addresses the scan frames the detector ran over, so a caller
        # holding a shorter list falls back to the median box rather than
        # indexing off the end.
        sample = protected.sample
        if sample is not None and 0 <= sample.frame_index < len(scan_frames):
            crop_frame, crop_box = scan_frames[sample.frame_index], sample.box
        else:
            crop_frame, crop_box = scan_frames[0], protected.box
        histogram = _vehicle_histogram(crop_frame, crop_box)
        if current is None:
            return VehicleSignature.from_observation(normalized, histogram)
        return current.blend(normalized, histogram)

    async def _run_pair_stages(
        self,
        hints: VisionHints,
        scan_frames: list[bytes],
        detections: list[DetectedObject],
        zone_ref: _ZoneReference | None,
    ) -> None:
        """Depth and contact analysis for the subject nearest the asset.

        These are the two stages that can tell "walked past the car" from
        "stood right at it" — a distinction a 2D frame simply does not
        contain — so they are pointed at whichever subject actually came
        closest, and their verdict is tagged with that subject's track id so
        nothing downstream misapplies it to somebody else in frame.
        """
        pair = self._select_pair(hints, detections, zone_ref)
        if pair is None:
            hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
            hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
            hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)
            return
        subject, asset_box, frame_idx, track_id = pair
        hints.contact_track_id = track_id

        depth_result = await self._depth.compare(
            scan_frames[frame_idx], subject.box, asset_box
        )
        if depth_result is None:
            hints.unavailable_sources.append(SOURCE_DEPTH_ESTIMATION)
        else:
            hints.depth_similar = depth_result.similar_depth
            hints.depth_hint = _build_depth_hint(depth_result, subject.label)

        contact_result = await self._segmenter.check_contact(
            scan_frames[frame_idx], subject.box, asset_box
        )
        if contact_result is None:
            hints.unavailable_sources.append(SOURCE_CONTACT_SEGMENTATION)
        else:
            hints.contact_touching = contact_result.touching
            hints.contact_hint = _build_contact_hint(contact_result, subject.label)

        if self._config.pose_estimation_enabled:
            posture = await self._pose.analyze(
                scan_frames[frame_idx], subject.box, asset_box
            )
            if posture is None:
                hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)
            else:
                hints.posture = posture
                hints.posture_hint = _build_posture_hint(posture, subject.label)
        else:
            hints.unavailable_sources.append(SOURCE_POSE_ESTIMATION)

        # Cheap, model-free "did the vehicle itself change" evidence, worth
        # computing exactly when somebody was close enough to change it.
        hints.asset_appearance_change = self._asset_change(
            scan_frames, asset_box, detections
        )

    @staticmethod
    def _asset_change(
        scan_frames: list[bytes],
        asset_box: Box,
        detections: list[DetectedObject],
    ) -> float | None:
        """Compare the asset's own region between the clip's ends.

        Skipped entirely when a subject overlaps the asset in either of the
        two frames being compared: the region would then be showing a person
        rather than the vehicle, and "it looks different" would be measuring
        where they happened to be standing. Since this feeds the one
        CRITICAL event the detector can emit, a clean view on both ends is
        the right precondition — and its absence means one fewer input, not
        a wrong one.
        """
        last_index = len(scan_frames) - 1
        for detection in detections:
            if (
                detection.frame_index in (0, last_index)
                and detection.label in _SUBJECT_CLASSES
                and box_gap(detection.box, asset_box) <= 0
            ):
                return None
        return _region_appearance_change(
            scan_frames[0], scan_frames[last_index], asset_box
        )

    @staticmethod
    def _select_pair(
        hints: VisionHints,
        detections: list[DetectedObject],
        zone_ref: _ZoneReference | None,
    ) -> tuple[DetectedObject, Box, int, int | None] | None:
        """Pick the subject/asset box pair the heavy stages should examine.

        Prefers the identified protected vehicle, so depth and contact are
        measured against the right car rather than against whichever one a
        subject happened to stand nearest. Falls back to the legacy
        nearest-pair search when the security layer is switched off or found
        no asset.
        """
        asset = hints.asset
        if asset is not None and asset.present and asset.box is not None:
            subjects = [d for d in detections if d.label in _SUBJECT_CLASSES]
            if not subjects:
                return None
            nearest = min(subjects, key=lambda d: box_gap(d.box, asset.box))  # type: ignore[arg-type]
            return (nearest, asset.box, nearest.frame_index, nearest.track_id)

        legacy = _best_subject_vehicle_pair(detections, zone_ref)
        if legacy is None:
            return None
        subject, vehicle, frame_idx = legacy
        return (subject, vehicle.box, frame_idx, subject.track_id)
