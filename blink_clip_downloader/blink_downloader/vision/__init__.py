"""Optional computer-vision enhancement pipeline for clip analysis.

Five independently-toggleable stages, layered on top of the existing
AI-provider prompt pipeline (see the ``analyzer`` package) rather than
replacing it — each stage produces a bounded, code-computed hint that gets
appended to the same prompt the configured AI provider
(ollama/anthropic/openai/moondream) already reasons over, exactly like the
existing scene-baseline and zone-motion hints. The AI model still makes the
final suspicious/not call; this package only gives it better evidence to
work with.

One module per stage, in the order a clip passes through them:

- :mod:`.enhance` — OpenCV CLAHE contrast enhancement + denoising.
- :mod:`.detection` — YOLO object detection + ByteTrack tracking
  (Ultralytics), for precise person/vehicle/animal boxes instead of raw
  pixel-diff motion.
- :mod:`.depth` — monocular depth estimation (Depth Anything V2 via
  transformers), to tell "overlapping in the 2D frame" apart from
  "actually at the same distance from the camera".
- :mod:`.contact` — pixel-level segmentation (SAM2 via transformers) to
  refine a detected bounding-box overlap into an actual touching-or-not
  judgment.
- :mod:`.pose` — body posture (Ultralytics YOLO-pose): reaching, an arm
  raised, crouching.
- :mod:`.faces` — local-only face recognition (facenet-pytorch) to
  suppress alerts for enrolled household members. Enrollment data
  (photos, embeddings) never leaves this add-on.

with :mod:`.pipeline` sequencing them, :mod:`.runtime` holding the
process-wide concurrency limiter and availability checks every stage
consults, and :mod:`.imaging` the pure image arithmetic several of them
share.

Every stage lazily imports its own heavy dependency (torch, ultralytics,
opencv, transformers, facenet-pytorch — none of which are required to run
the add-on's core features) and reports itself unavailable rather than
raising if that dependency isn't installed or fails to load. Nothing here
is imported by ``analyzer`` at call time unless the corresponding config
option is enabled, and even then the heavy import itself is deferred to
first use — see each class's ``ensure_ready()``.

Importers are unaffected by the split: every name the old single module
exposed is re-exported here.
"""

from __future__ import annotations

from .contact import ContactResult, ContactSegmenter
from .depth import DepthComparison, DepthEstimator
from .detection import DetectedObject, ObjectDetector
from .enhance import FrameEnhancer
from .faces import (
    FaceEmbedder,
    FaceRecognitionResult,
    FaceRecognizer,
    cosine_similarity,
)
from .pipeline import (
    SOURCE_CONTACT_SEGMENTATION,
    SOURCE_DEPTH_ESTIMATION,
    SOURCE_FACE_RECOGNITION,
    SOURCE_OBJECT_DETECTION,
    SOURCE_POSE_ESTIMATION,
    VisionConfig,
    VisionHints,
    VisionPipeline,
)
from .pose import PoseEstimator, PostureResult
from .runtime import (
    CPUIncompatibleError,
    configure_cv_concurrency,
    is_face_recognition_available,
    torch_cpu_compatible,
)

__all__ = [
    "SOURCE_CONTACT_SEGMENTATION",
    "SOURCE_DEPTH_ESTIMATION",
    "SOURCE_FACE_RECOGNITION",
    "SOURCE_OBJECT_DETECTION",
    "SOURCE_POSE_ESTIMATION",
    "CPUIncompatibleError",
    "ContactResult",
    "ContactSegmenter",
    "DepthComparison",
    "DepthEstimator",
    "DetectedObject",
    "FaceEmbedder",
    "FaceRecognitionResult",
    "FaceRecognizer",
    "FrameEnhancer",
    "ObjectDetector",
    "PoseEstimator",
    "PostureResult",
    "VisionConfig",
    "VisionHints",
    "VisionPipeline",
    "configure_cv_concurrency",
    "cosine_similarity",
    "is_face_recognition_available",
    "torch_cpu_compatible",
]
