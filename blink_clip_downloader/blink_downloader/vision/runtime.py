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

Stage modules reach these through the module (``runtime.torch_cpu_``
``compatible()``) rather than importing the names, so there is exactly one
of each rather than a copied reference per stage — and exactly one place
for a test to substitute one.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import threading

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
