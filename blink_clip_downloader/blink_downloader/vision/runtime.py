"""What every stage needs before it can run, and whether it can run at all.

Three things live here because they are process-wide rather than
per-stage, and having two of any of them would be a bug:

* ``_native_import_lock`` — one lock around every first ``import cv2`` /
  ``import torch`` in this package. CPython's import machinery is not safe
  against two threads performing the *first* import of the same native
  extension at once, and the damage is permanent for the process rather
  than a retryable error.
* ``_cv_slot`` — one semaphore capping how many heavy stages run at once,
  across every clip being analyzed concurrently.
* ``torch_cpu_compatible`` / ``is_face_recognition_available`` — the
  availability checks every stage consults before attempting an import
  that would otherwise crash the process on an older ARM board.

And one thing every stage shares without sharing any state: :class:`LoadGuard`,
which remembers how a model's load went so that a failure is neither retried
(and logged with a traceback) on every single frame or clip, nor invisible to
the person who has to fix it.

Stage modules reach these through the module (``runtime.torch_cpu_``
``compatible()``) rather than importing the names, so there is exactly one
of each rather than a copied reference per stage — and exactly one place
for a test to substitute one.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

#: The optional stages' names, as they appear in :class:`StageHealth` and on
#: the tabs. ``pipeline.SOURCE_*`` are the same strings, because the evidence
#: line a clip's analysis ends up with and the stage list a person reads
#: should agree.
STAGE_OBJECT_DETECTION = "object detection"
STAGE_DEPTH_ESTIMATION = "depth estimation"
STAGE_CONTACT_SEGMENTATION = "contact segmentation"
STAGE_POSE_ESTIMATION = "pose estimation"
STAGE_FACE_RECOGNITION = "face recognition"
STAGE_AUDIO_ANALYSIS = "audio analysis"


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


#: How long a stage waits before trying to load a model that failed to load,
#: doubling with each further failure up to :data:`_RETRY_MAX_SECONDS`. A
#: judgement, not a measurement: long enough that a wrong model name or a
#: blocked download is not retried per clip (each attempt holds a heavy-stage
#: slot while it fails), short enough that a download interrupted by a network
#: blip is retried within a few clips.
_RETRY_FIRST_SECONDS = 60.0

_RETRY_MAX_SECONDS = 3600.0

# What a failure's reason may say. Exception text from a model download can
# be long and can name a path; the reason is shown on a settings tab, so it is
# cut to one bounded line and anything shaped like a Hugging Face token is
# removed (the library redacts them itself, but this is not the place to find
# out that a version stopped).
_MAX_REASON_LENGTH = 200

_HF_TOKEN_PATTERN = re.compile(r"hf_[A-Za-z0-9]{8,}")


def describe_load_failure(exc: BaseException) -> str:
    """One bounded line saying why a model failed to load, for the tabs.

    The exception's class and the first line of its message: enough to tell a
    blocked download from corrupt weights, without a traceback and without a
    token that an upstream message happened to echo.
    """
    first_line = (str(exc).strip().splitlines() or [""])[0]
    reason = f"{type(exc).__name__}: {first_line}" if first_line else type(exc).__name__
    reason = _HF_TOKEN_PATTERN.sub("hf_***", reason)
    if len(reason) > _MAX_REASON_LENGTH:
        reason = reason[: _MAX_REASON_LENGTH - 1] + "…"
    return reason


@dataclass(frozen=True)
class StageHealth:
    """Whether one optional stage's model is usable, and if not, why.

    ``state`` is ``"ready"`` once the model has loaded, ``"failed"`` after a
    load that did not. A stage that has not been asked to load yet has no
    health at all (:meth:`LoadGuard.health` returns ``None``), which is
    different from either: nothing has been tried.
    """

    stage: str
    state: str
    #: Why it failed, as :func:`describe_load_failure` words it; empty when ready.
    detail: str = ""
    #: ``True`` for a failure that waiting cannot fix (a package that is not
    #: installed, a CPU PyTorch cannot run on) — it is not retried until restart.
    permanent: bool = False
    #: Seconds until the next load attempt, for a failure that will be retried.
    retry_in: float | None = None

    def to_dict(self) -> dict[str, object]:
        """The JSON the tabs read: whole seconds, and no more than is shown."""
        return {
            "stage": self.stage,
            "state": self.state,
            "detail": self.detail,
            "permanent": self.permanent,
            "retry_in": None if self.retry_in is None else round(self.retry_in),
        }


class LoadGuard:
    """Remembers how a stage's model load went.

    Each stage's ``ensure_ready`` used to try the load again on every call
    after a failure, and log a traceback each time: a model name that does not
    exist, or a download the network blocks, then cost one failed attempt and
    one traceback per frame, with the heavy-stage slot held throughout. This
    keeps the verdict between attempts. It holds no model and does no loading;
    the stage still owns both.
    """

    def __init__(self, stage: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._stage = stage
        self._clock = clock
        self._ready = False
        self._failures = 0
        self._detail = ""
        self._permanent = False
        self._retry_at = 0.0

    def backing_off(self) -> bool:
        """Whether a load should not be attempted now because one just failed."""
        if self._failures == 0 or self._ready:
            return False
        return self._permanent or self._clock() < self._retry_at

    def retry_now(self) -> None:
        """Make a failed load due immediately — for a person who asked.

        Backing off exists so that clips do not each pay for a failing load;
        someone pressing a button after fixing the cause has asked for it
        outright. A permanent failure stays permanent: nothing they can do
        from a browser installs a package.
        """
        if not self._permanent:
            self._retry_at = self._clock()

    def ready(self) -> None:
        """Record that the model loaded."""
        self._ready = True
        self._failures = 0
        self._detail = ""
        self._permanent = False

    def failed(self, reason: str, *, permanent: bool = False) -> bool:
        """Record a failed load; return whether *reason* is news.

        ``True`` when this failure differs from the previous one (or is the
        first), which is when the caller should log it in full; a repeat of
        the same reason deserves a debug line, not another warning.
        """
        is_new = self._failures == 0 or reason != self._detail
        self._ready = False
        self._failures += 1
        self._detail = reason
        self._permanent = permanent
        delay = min(
            _RETRY_FIRST_SECONDS * 2 ** (self._failures - 1), _RETRY_MAX_SECONDS
        )
        self._retry_at = self._clock() + delay
        return is_new

    def health(self) -> StageHealth | None:
        """What is known about the model, or ``None`` if it was never asked for."""
        if self._ready:
            return StageHealth(self._stage, "ready")
        if self._failures == 0:
            return None
        retry_in = None if self._permanent else max(0.0, self._retry_at - self._clock())
        return StageHealth(
            self._stage,
            "failed",
            detail=self._detail,
            permanent=self._permanent,
            retry_in=retry_in,
        )


def record_load_failure(
    guard: LoadGuard,
    logger: logging.Logger,
    exc: BaseException,
    *,
    label: str,
    package: str,
    install: str,
    huggingface_name: str | None = None,
) -> None:
    """Record why a stage's model did not load, and log it once.

    What every stage's ``ensure_ready`` used to spell out on its own, with its
    own wording: a missing package, a CPU PyTorch cannot run on and (for the
    stages that download from the Hub) a rejected token are *permanent* —
    nothing the add-on does while running changes them — and anything else is
    treated as transient and retried on :class:`LoadGuard`'s schedule.
    *package* is the import name that was missing, *install* what pip is told,
    *huggingface_name* the model's name in the token message (``None`` for a
    stage that does not use the Hub). A failure is logged in full only when
    its reason is new; a repeat of it is a debug line.
    """
    permanent = True
    level = logging.WARNING
    exc_info: BaseException | None = None
    if isinstance(exc, ImportError):
        reason = f"the {package} package is not installed"
        message = (
            f"{package} package is not installed, {label} unavailable: {exc}. "
            f"Install it with: pip install {install}"
        )
    elif isinstance(exc, CPUIncompatibleError):
        reason = "this CPU cannot run PyTorch"
        message = f"{label.capitalize()} unavailable: {exc}"
    elif huggingface_name is not None and _is_huggingface_auth_error(exc):
        reason = "Hugging Face refused the configured token"
        message = _HF_AUTH_FAILURE_MESSAGE % huggingface_name
        level = logging.ERROR
    else:
        permanent = False
        reason = describe_load_failure(exc)
        message = f"Failed to load the {label} model: {reason}"
        exc_info = exc
    if not guard.failed(reason, permanent=permanent):
        logger.debug(message)
        return
    # A traceback only for the failure nobody anticipated.
    logger.log(level, message, exc_info=exc_info)


async def load_model(
    guard: LoadGuard,
    lock: asyncio.Lock,
    is_loaded: Callable[[], bool],
    load: Callable[[], None],
    logger: logging.Logger,
    *,
    label: str,
    package: str,
    install: str,
    huggingface_name: str | None = None,
) -> bool:
    """Load a stage's model once, remembering how it went; ``True`` when loaded.

    The body of every stage's ``ensure_ready``: *load* runs in a thread under
    *lock*, only while *guard* is not backing off from a failure, and a
    failure is recorded and logged by :func:`record_load_failure` (see it for
    *label*, *package*, *install* and *huggingface_name*). Checked again once
    the lock is held, because a caller that waited behind a load which just
    failed must not start another.
    """
    if is_loaded():
        return True
    if guard.backing_off():
        return False
    async with lock:
        if is_loaded():
            return True
        if guard.backing_off():
            return False
        try:
            await asyncio.get_running_loop().run_in_executor(None, load)
        except Exception as exc:  # noqa: BLE001 - recorded and logged below
            record_load_failure(
                guard,
                logger,
                exc,
                label=label,
                package=package,
                install=install,
                huggingface_name=huggingface_name,
            )
            return False
        guard.ready()
        return True
