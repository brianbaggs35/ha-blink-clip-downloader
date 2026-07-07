"""AI video analysis via ffmpeg frame extraction and pluggable AI providers.

Six providers are supported:
- ``ollama``          – local/LAN Ollama server with a vision-capable model
- ``ollama_cloud``    – Ollama Cloud API (api.ollama.com) with an API key
- ``moondream_cloud`` – Moondream Cloud API (api.moondream.ai)
- ``moondream_local`` – Moondream 0.5B model running on-device (no cloud)
- ``anthropic``       – Anthropic Claude API (claude.ai) with an API key
- ``openai``          – OpenAI Chat Completions API (platform.openai.com) with an API key

Use :func:`create_analyzer` to instantiate the right provider from config.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .database import ClipDatabase
    from .vision import VisionHints, VisionPipeline

_LOGGER = logging.getLogger(__name__)

_API_TIMEOUT = aiohttp.ClientTimeout(total=120)
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Blink motion clips can run up to 60 seconds. Frame extraction must sample
# candidates across the *entire* clip length regardless of frame_strategy or
# max_frames, otherwise a fixed small frame count at the default interval only
# ever samples the leading portion of the clip (e.g. 5 frames * 2s = the first
# 10s of a 60s clip) and anything that happens later is never seen.
_MAX_CLIP_COVERAGE_SECONDS: float = 60.0

# max_frames/frame_interval are honored exactly for clips at or under this
# length. Longer clips (Blink's ceiling is 60s) get their frame budget
# doubled — the configured budget alone tends to under-sample a 45-60s clip
# (missing the start, the end, or the middle), and a 60s clip covers twice
# the timeline of a 30s one, so it needs roughly twice the frames to keep the
# same effective sampling density across the whole event.
_LONG_CLIP_THRESHOLD_SECONDS: float = 30.0
_LONG_CLIP_FRAME_MULTIPLIER: int = 2

# Fixed-size grayscale thumbnail used for the visual scene-baseline ("smart
# brain") comparison — see BaseAnalyzer._scene_thumbnail(). Small enough to
# store cheaply per-camera and compare quickly; large enough to notice a
# parked vehicle, a delivered package, or similar new object in frame.
_SCENE_THUMBNAIL_SIZE: tuple[int, int] = (16, 16)

# Deviation score (0.0-1.0, see ClipDatabase.get_scene_deviation) at or above
# which the prompt calls out that the scene looks different from usual.
_SCENE_DEVIATION_ALERT_THRESHOLD: float = 0.12

# Minimum confidence for a "suspicious" verdict to block that clip's frame
# from being folded into the scene baseline (see analyze_clip()). A clip
# marked suspicious only as a low-confidence hedge — e.g. because the scene
# deviation hint above nudged the model to "look closer" at a newly parked
# car or a trash can put out for collection — should still teach the
# baseline what the camera's new normal looks like. Only a confident verdict
# withholds that frame, so a genuine intruder isn't absorbed into "normal".
_SCENE_BASELINE_SUSPICION_CONFIDENCE_THRESHOLD: float = 0.5

# A clip at or under this length (seconds) is far more consistent with a
# single brief interaction — using a door, grabbing mail or a delivery,
# walking to/from a car — than with lingering, casing, or tampering, which
# typically take noticeably longer to unfold. See _build_prompt's SHORT
# EVENT hint below, added specifically to counter small vision models
# over-flagging ordinary quick coming-and-going as suspicious.
_SHORT_EVENT_DURATION_SECONDS: float = 10.0

# Motion-trajectory hint ("smart brain" movement reasoning) — see
# BaseAnalyzer._compute_motion_trajectory_hint(). Reuses the same 64x64
# grayscale thumbnail size _select_best_frames() already computes for its
# motion-diff scoring, applied to the frames actually selected for analysis.
_MOTION_TRAJECTORY_THUMB_SIZE: tuple[int, int] = (64, 64)
# Fewer than this many selected frames makes entry/peak/exit direction too
# noisy to trust — no hint is emitted below this count.
_MOTION_TRAJECTORY_MIN_FRAMES: int = 3
# Per-pixel average diff magnitude (0-255 raw byte scale, same as
# _select_best_frames()'s motion scoring — NOT normalized to 0.0-1.0) below
# which there's no real motion to characterize a direction for, just JPEG
# re-encoding noise between near-identical frames.
_MOTION_TRAJECTORY_DIFF_FLOOR: float = 3.0
# A lateral centroid shift of at least this fraction of the thumbnail width
# between the first and last frame pair is required to call a left/right
# direction — smaller shifts are treated as noise.
_MOTION_TRAJECTORY_LATERAL_SHIFT_FRACTION: float = 0.15
# Ratio between the second half's and first half's average diff magnitude
# required to call an intensity trend (approaching/retreating proxy).
_MOTION_TRAJECTORY_INTENSITY_RATIO: float = 1.3

# The analysis prompt's OUTPUT RULES ask for one sentence, or at most two,
# but small vision models sometimes ignore that and emit a degenerate loop
# of near-identical sentences instead of stopping (e.g. repeating "the
# person is standing near the car's rear ___" once per body part in view).
# parse_response() caps every description to this many sentences, plus a
# hard character backstop for responses with no sentence punctuation to
# split on at all.
_MAX_SUMMARY_SENTENCES: int = 2
_MAX_SUMMARY_CHARS: int = 200

# Shared system-prompt text for the three providers (Ollama, Anthropic,
# OpenAI) that support a separate system role — keeps the role and output
# format instructions apart from user content, improving JSON compliance and
# stopping the model from leaking internal analysis terms into the
# description field.
_VISION_SYSTEM_PROMPT: str = (
    "You are a security camera analyst. "
    "You respond ONLY with a single valid JSON object and nothing else. "
    "Write the description field in plain English as if speaking to a homeowner. "
    "Never include technical terms such as 'bounding box', 'normalized', "
    "'frame percentage', 'spatial data', 'INTERNAL', or decimal coordinates "
    "in the description field."
)

# Ollama model-name fragments that indicate vision capability.
# Checked case-insensitively as a substring of the full model name.
_VISION_MODEL_PATTERNS: frozenset[str] = frozenset(
    {
        "llava",
        "bakllava",
        "moondream",
        "minicpm-v",
        "cogvlm",
        "llava-phi3",
        "llava-llama3",
        "llava-mistral",
        "phi-3.5-vision",
        "phi3.5-vision",
        "phi3-vision",
        "qwen-vl",
        "qwen2-vl",
        "qwen2.5-vl",
        "internvl",
        "granite3.2-vision",
        "llama3.2-vision",
        "deepseek-vl",
        "pixtral",
        "idefics",
        "mllama",
        "paligemma",
        "llava3",
        "minicpm",
    }
)

# Priority patterns for ranking Ollama vision models (higher score = better).
# Longer/more specific patterns must come before shorter ones.
_VISION_PRIORITY_PATTERNS: list[tuple[str, int]] = [
    ("llama3.2-vision", 100),
    ("llava:34b", 95),
    ("llava:13b", 90),
    ("llava-llama3", 85),
    ("llava:7b", 80),
    ("qwen2.5-vl", 78),
    ("qwen2-vl", 76),
    ("bakllava:13b", 72),
    ("minicpm-v", 70),
    ("llava-phi3", 65),
    ("pixtral", 63),
    ("internvl", 60),
    ("bakllava", 58),
    ("granite3.2-vision", 55),
    ("moondream2", 52),
    ("moondream", 50),
    ("minicpm", 45),
]


def is_vision_model(model_name: str) -> bool:
    """Return True if an Ollama model name looks vision-capable."""
    lower = model_name.lower()
    return any(p in lower for p in _VISION_MODEL_PATTERNS)


def _vision_model_score(name: str) -> int:
    """Return a quality score for an Ollama vision model (higher = better)."""
    lower = name.lower()
    for pattern, score in _VISION_PRIORITY_PATTERNS:
        if pattern in lower:
            return score
    return 30


# OpenAI model pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://developers.openai.com/api/docs/pricing (standard tier)
# More specific model-family prefixes (mini/nano/dotted variants) must be
# listed before their shorter/bare prefix (e.g. "gpt-5.4-mini" before
# "gpt-5.4" before "gpt-5") since model_pricing() below returns the first
# matching entry in insertion order.
_OPENAI_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "o1-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    "o3": (2.00, 8.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.5": (5.00, 30.00),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5": (1.25, 10.00),
}

# Vision-capable OpenAI model ID prefixes (checked as substring of model id).
# "gpt-5" alone covers every dotted/mini/nano variant in that family (e.g.
# "gpt-5.4-mini", "gpt-5.5") since they all contain "gpt-5" as a substring.
_OPENAI_VISION_PREFIXES: frozenset[str] = frozenset(
    {
        "gpt-4o",
        "gpt-4-turbo",
        "gpt-4-vision",
        "gpt-4.1",
        "gpt-5",
        "o1",
        "o3",
        "o4-mini",
    }
)

# Structured Outputs (response_format=json_schema, strict=True) guarantees the
# response matches this schema exactly — unlike json_object mode, which only
# guarantees *some* valid JSON — so parsing never has to fall back to
# keyword-matching an incorrectly-shaped object. Supported on gpt-4o-2024-08-06+,
# gpt-4.1, gpt-5, and o4-mini; gpt-4-turbo predates Structured Outputs and only
# supports the older json_object mode.
_OPENAI_STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "name": "clip_analysis",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "suspicious": {"type": "boolean"},
            "confidence": {"type": "number"},
            "description": {"type": "string"},
        },
        "required": ["suspicious", "confidence", "description"],
        "additionalProperties": False,
    },
}

# Fallback model list when the OpenAI API cannot be reached.
_OPENAI_FALLBACK_MODELS: list[dict] = [
    {
        "name": "gpt-5.4-mini",
        "display_name": "GPT-5.4 mini — Best Value ($0.75/$4.50 per 1M tokens)",
    },
    {
        "name": "gpt-5.4-nano",
        "display_name": "GPT-5.4 nano — Lowest Cost ($0.20/$1.25 per 1M tokens)",
    },
    {
        "name": "gpt-5.4",
        "display_name": "GPT-5.4 ($2.50/$15 per 1M tokens)",
    },
    {
        "name": "gpt-5.5",
        "display_name": "GPT-5.5 — Highest Intelligence ($5/$30 per 1M tokens)",
    },
    {
        "name": "gpt-4o",
        "display_name": "GPT-4o ($2.50/$10 per 1M tokens)",
    },
    {
        "name": "gpt-4o-mini",
        "display_name": "GPT-4o mini ($0.15/$0.60 per 1M tokens)",
    },
    {
        "name": "gpt-4.1",
        "display_name": "GPT-4.1 ($2/$8 per 1M tokens)",
    },
    {
        "name": "gpt-4.1-mini",
        "display_name": "GPT-4.1 mini ($0.40/$1.60 per 1M tokens)",
    },
    {
        "name": "gpt-4.1-nano",
        "display_name": "GPT-4.1 nano ($0.10/$0.40 per 1M tokens)",
    },
    {
        "name": "gpt-4-turbo",
        "display_name": "GPT-4 Turbo ($10/$30 per 1M tokens)",
    },
]


def is_openai_vision_model(model_id: str) -> bool:
    """Return True if an OpenAI model id looks vision-capable via Chat Completions.

    "-pro" tier reasoning models (e.g. ``gpt-5.5-pro``) are excluded even
    though they match a vision prefix — they are only available via OpenAI's
    Responses/Batch APIs, not Chat Completions, which is the only API this
    analyzer speaks.
    """
    lower = model_id.lower()
    if lower.endswith("-pro"):
        return False
    return any(p in lower for p in _OPENAI_VISION_PREFIXES)


# Anthropic model pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://platform.claude.com/docs/en/about-claude/pricing
_ANTHROPIC_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    # Rate through 2026-08-31; rises to (3.00, 15.00) on 2026-09-01 per the
    # pricing page above — bump this when that date arrives.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Fallback model list when the Anthropic API cannot be reached.
_ANTHROPIC_FALLBACK_MODELS: list[dict] = [
    {
        "name": "claude-opus-4-8",
        "display_name": "Claude Opus 4.8 ($5/$25 per 1M tokens)",
    },
    {
        "name": "claude-sonnet-5",
        "display_name": "Claude Sonnet 5 ($2/$10 per 1M tokens through Aug 2026)",
    },
    {
        "name": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6 ($3/$15 per 1M tokens)",
    },
    {
        "name": "claude-sonnet-4-5",
        "display_name": "Claude Sonnet 4.5 ($3/$15 per 1M tokens)",
    },
    {
        "name": "claude-haiku-4-5",
        "display_name": "Claude Haiku 4.5 — Best Value ($1/$5 per 1M tokens)",
    },
]


def lookup_model_pricing(model: str) -> tuple[float, float] | None:
    """Best-effort (input, output) $/1M-token pricing lookup for *model*.

    Checks the OpenAI and Anthropic pricing tables — their model-id
    namespaces don't overlap, so no provider hint is needed. Returns None for
    models this add-on can't price per-token (Ollama, Moondream, or an
    unrecognized/fine-tuned id) so callers can render an explicit "N/A"
    instead of guessing at a price.
    """
    lower = (model or "").lower()
    for table in (_OPENAI_MODEL_PRICING, _ANTHROPIC_MODEL_PRICING):
        for prefix, pricing in table.items():
            if lower.startswith(prefix) or prefix in lower:
                return pricing
    return None


def is_moondream_installed() -> bool:
    """Return True if the moondream package is importable."""
    try:
        __import__("moondream")
        return True
    except ImportError:
        return False


@dataclass
class AnalysisResult:
    """Structured output from a clip analysis run."""

    clip_id: str
    camera: str
    model: str
    response_text: str
    is_suspicious: bool
    confidence: float
    summary: str
    frame_count: int
    analysis_duration: float
    analyzed_at: str
    tokens_prompt: int = 0
    tokens_completion: int = 0
    anomaly_score: float = 0.0
    # Set only when a tier-1 verdict escalated to a stronger tier-2 analyzer
    # (see BaseAnalyzer._maybe_escalate / create_analyzer's escalation_provider).
    # Tracked separately from tokens_prompt/tokens_completion (tier 1's own
    # usage) so the AI Usage tab can attribute and price each tier's tokens
    # correctly instead of folding tier 2's cost into tier 1's model row.
    escalation_model: str = ""
    escalation_tokens_prompt: int = 0
    escalation_tokens_completion: int = 0
    # Provider of the escalation model above (e.g. "moondream_cloud") — may
    # differ from this result's own provider since tier 2 can be a completely
    # different provider than tier 1. Empty when no escalation occurred.
    escalation_provider: str = ""
    # Exact prompt text sent to the model (excluding image frames), stored
    # only when ai_prompt_debug_enabled is on (see BaseAnalyzer.set_prompt_debug).
    # Empty when the feature is off, regardless of whether analysis ran.
    prompt_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "camera": self.camera,
            "model": self.model,
            "response_text": self.response_text,
            "is_suspicious": self.is_suspicious,
            "confidence": self.confidence,
            "summary": self.summary,
            "frame_count": self.frame_count,
            "analysis_duration": self.analysis_duration,
            "analyzed_at": self.analyzed_at,
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "anomaly_score": self.anomaly_score,
            "escalation_model": self.escalation_model,
            "escalation_tokens_prompt": self.escalation_tokens_prompt,
            "escalation_tokens_completion": self.escalation_tokens_completion,
            "escalation_provider": self.escalation_provider,
            "prompt_text": self.prompt_text,
        }


class BaseAnalyzer(abc.ABC):
    """Abstract base class shared by all AI analysis providers."""

    def __init__(
        self,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self._base_prompt = prompt
        self._car_description = car_description
        self._max_frames = max_frames
        self._frame_interval = frame_interval
        self._suspicious_keywords = [k.lower() for k in (suspicious_keywords or [])]
        self._camera_prompts: dict[str, str] = camera_prompts or {}
        self._camera_descriptions: dict[str, str] = camera_descriptions or {}
        # "smart" oversamples then picks entry/peak/exit frames via motion diff.
        # "sequential" analyses each frame individually and returns the most alarming.
        # "uniform" is the legacy behaviour: extract exactly max_frames at fixed intervals.
        self._frame_strategy = frame_strategy
        # If non-empty, car-protection distance rules are only injected for cameras
        # in this set.  Empty means "apply to every camera" (backward-compatible default).
        self._car_cameras: set[str] = set(car_cameras) if car_cameras else set()
        # Optional per-camera "car zone" — a fixed, user-drawn rectangle
        # (normalised 0-1 coords: x_min/y_min/x_max/y_max) marking roughly
        # where the protected vehicle normally sits. Unlike per-frame object
        # detection (which can disagree between two independent zero-shot
        # queries on the very same physical car — see
        # :meth:`_MoondreamDetectionMixin._detect_protected_vehicle`), a
        # user-drawn zone is fixed ground truth: Blink cameras don't move, so
        # it never needs to be re-derived per clip. Used to compute
        # zone-restricted motion evidence — see :meth:`_zone_motion_fraction`.
        self._car_zones: dict[str, dict[str, float]] = car_zones or {}
        # Token counts set by _call_model() implementations that support them.
        # Reset to 0 at the start of each analyze_clip() call.
        self._last_prompt_tokens: int = 0
        self._last_completion_tokens: int = 0
        # Set only by _maybe_escalate() when a tier-1 verdict escalates to a
        # tier-2 analyzer. Left at their defaults (empty/0) when no escalation
        # analyzer is attached, and reset at the start of each analyze_clip()
        # call alongside the tokens above.
        self._last_escalation_model: str = ""
        self._last_escalation_provider: str = ""
        self._last_escalation_prompt_tokens: int = 0
        self._last_escalation_completion_tokens: int = 0
        # Optional second analyzer used for two-tier escalation — see
        # create_analyzer()'s escalation_provider/escalation_model and
        # set_escalation_analyzer(). May be a completely different provider
        # than this analyzer (e.g. this one is openai, escalation is
        # moondream_cloud). None disables escalation entirely.
        self._escalation_analyzer: BaseAnalyzer | None = None
        # Off by default — see set_prompt_debug().
        self._store_prompt_debug: bool = False
        # Optional ClipDatabase for the visual scene-baseline ("smart brain")
        # feature. Unset (None) disables it entirely — set via
        # attach_scene_baseline_db() once the app has a database ready.
        self._scene_baseline_db: ClipDatabase | None = None
        # Optional VisionPipeline (see vision.py) providing the heavy,
        # off-by-default computer-vision enhancement stages (object
        # detection/tracking, depth estimation, contact segmentation, face
        # recognition). Unset (None) disables all of them — set via
        # attach_vision_pipeline() once the app has built one from config.
        self._vision_pipeline: VisionPipeline | None = None
        # analyze_clip() stashes per-call state (_current_camera,
        # _last_prompt_tokens/_last_completion_tokens) on self across many
        # awaited I/O calls, so two calls interleaving on the same analyzer
        # instance would corrupt each other's results. That interleaving is
        # reachable in practice: the background AnalysisQueue and the
        # media server's on-demand "Analyze Now"/"Test" HTTP handlers share
        # one analyzer instance. This lock serializes analyze_clip() so only
        # one runs at a time per instance.
        self._analyze_lock: asyncio.Lock | None = None
        # Cache for health_check() on providers that hit a real external API
        # (see _cached_health_check_result/_store_health_check_result below).
        self._last_health_check_time: float = 0.0
        self._last_health_check_result: bool = False

    def _get_analyze_lock(self) -> asyncio.Lock:
        if self._analyze_lock is None:
            self._analyze_lock = asyncio.Lock()
        return self._analyze_lock

    # Health-check results are cached for this many seconds on providers that
    # hit a real external API (OpenAI, Anthropic, Ollama Cloud, Moondream
    # Cloud). Without this, both the web UI (polling /api/ai/status every 10s
    # while the AI tab is open) and the background AnalysisQueue (polling
    # every ai_check_interval seconds) each trigger a fresh authenticated API
    # call — e.g. OpenAI's GET /v1/models — showing up as constant traffic in
    # logs and needlessly counting against provider rate limits. Local/LAN
    # providers (plain Ollama, Moondream local) don't use this since a cheap
    # LAN ping benefits from being fresh rather than cached.
    _HEALTH_CHECK_CACHE_SECONDS: float = 30.0

    def _cached_health_check_result(self) -> bool | None:
        """Return a still-fresh cached health_check() result, or None if stale."""
        if (
            time.monotonic() - self._last_health_check_time
            < self._HEALTH_CHECK_CACHE_SECONDS
        ):
            return self._last_health_check_result
        return None

    def _store_health_check_result(self, result: bool) -> bool:
        """Cache *result* as the current health_check() outcome and return it."""
        self._last_health_check_time = time.monotonic()
        self._last_health_check_result = result
        return result

    def update_camera_descriptions(self, descriptions: dict[str, str]) -> None:
        """Update per-camera descriptions at runtime without restart."""
        self._camera_descriptions = descriptions

    def update_camera_prompts(self, prompts: dict[str, str]) -> None:
        """Replace per-camera custom prompts at runtime without restart.

        Full replace, not a merge — a camera whose custom prompt was cleared
        in the AI tab must stop overriding the base prompt immediately rather
        than keep using the last non-empty value it was ever set to.
        """
        self._camera_prompts = prompts

    def update_car_cameras(self, car_cameras: set[str] | list[str]) -> None:
        """Replace the car-camera set at runtime without restart.

        Full replace, not a merge — unchecking every "protected vehicle" box
        in the AI tab must actually clear car-proximity rules from the live
        analyzer, not just leave the previous set in place.
        """
        self._car_cameras = set(car_cameras)

    def update_car_zones(self, car_zones: dict[str, dict[str, float]]) -> None:
        """Replace the per-camera car-zone map at runtime without restart.

        Full replace, not a merge — clearing a camera's zone in the AI tab
        must stop it from applying immediately, matching
        :meth:`update_car_cameras`'s behaviour for the same reason.
        """
        self._car_zones = dict(car_zones)

    @property
    def car_protection_active(self) -> bool:
        """True if the protected-vehicle distance rules will apply to any camera.

        Requires ``ai_car_description`` (Configuration tab) to be non-empty —
        checking "Protected vehicle visible from this camera" in the AI tab
        alone does nothing until that description is also set, since the
        rules need to know what vehicle to protect.
        """
        return bool(self._car_description)

    def _car_protection_applies(self, camera: str) -> bool:
        """True if protected-vehicle distance rules apply to *camera* specifically.

        Requires ``car_description`` to be set (see :attr:`car_protection_active`).
        When ``car_cameras`` is empty the rules apply to every camera (the
        documented default — see ``ai_car_cameras``); otherwise only to
        cameras in that set. Shared by :meth:`_build_prompt` (which cameras
        get the distance-rule text) and :meth:`_maybe_escalate` (which
        cameras get high-recall escalation).
        """
        return bool(
            self._car_description
            and (not self._car_cameras or camera in self._car_cameras)
        )

    def set_escalation_analyzer(self, analyzer: BaseAnalyzer | None) -> None:
        """Attach (or clear) the tier-2 analyzer used for two-tier escalation.

        Set by :func:`create_analyzer` when ``ai_escalation_provider`` is
        configured. The escalation analyzer may be of any provider, including
        a different one than this analyzer — see :meth:`_maybe_escalate`.
        """
        self._escalation_analyzer = analyzer

    @property
    def escalation_analyzer(self) -> BaseAnalyzer | None:
        """The attached tier-2 analyzer, if any — see set_escalation_analyzer()."""
        return self._escalation_analyzer

    def set_prompt_debug(self, enabled: bool) -> None:
        """Enable/disable storing this analyzer's exact prompt text per clip.

        Off by default (see ``ai_prompt_debug_enabled``) — prompts are long
        and this is a debugging/prompt-tuning aid, not something every
        install needs kept in the database. When enabled, each
        :class:`AnalysisResult` carries the exact text sent to the model
        (excluding image frames) in ``prompt_text``, inspectable via the
        web UI's "Prompt" button.
        """
        self._store_prompt_debug = enabled

    @property
    def last_prompt_tokens(self) -> int:
        """Prompt tokens used by this analyzer's most recent _call_model() call."""
        return self._last_prompt_tokens

    @property
    def last_completion_tokens(self) -> int:
        """Completion tokens used by this analyzer's most recent _call_model() call."""
        return self._last_completion_tokens

    async def run_tier_call(self, frames: list[bytes], prompt: str) -> str:
        """Public entry point for another analyzer to use this one as its tier 2.

        Thin passthrough to :meth:`_call_model` — exists so cross-analyzer
        escalation composition (see :meth:`_maybe_escalate`) doesn't need to
        reach into another instance's "private" ``_call_model``.
        """
        return await self._call_model(frames, prompt)

    def attach_scene_baseline_db(self, db: ClipDatabase) -> None:
        """Enable the visual scene-baseline ("smart brain") feature.

        Blink cameras are stationary, so each camera's background should look
        almost the same clip after clip. Once attached, analyze_clip() checks
        each clip's opening frame against this camera's learned baseline
        appearance and — once enough history has accumulated — tells the
        model whether the scene looks like it usually does. That is a cheap
        signal for flagging a genuinely new object or vehicle in frame, and
        just as importantly, for reassuring the model when a scene is
        unremarkable so it doesn't over-flag routine activity.
        """
        self._scene_baseline_db = db

    def attach_vision_pipeline(self, pipeline: VisionPipeline) -> None:
        """Enable the optional computer-vision enhancement pipeline (see vision.py).

        Unset (None, the default) means every stage — frame preprocessing,
        object detection/tracking, depth estimation, contact segmentation,
        face recognition — is skipped entirely and analysis proceeds
        exactly as it does today. Each stage within the attached pipeline
        is independently toggled via its own config option; attaching a
        pipeline does not by itself enable anything.
        """
        self._vision_pipeline = pipeline

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Short provider identifier: 'ollama', 'moondream_cloud', 'moondream_local'."""

    @abc.abstractmethod
    def model_name(self) -> str:
        """The model identifier used to label analysis results."""

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Return True if the AI backend is reachable and ready."""

    @abc.abstractmethod
    async def fetch_models(self) -> list[dict[str, Any]]:
        """List available models (fixed list for non-Ollama providers)."""

    @abc.abstractmethod
    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Send frames to the AI backend and return the raw response text."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP sessions, loaded models, etc.)."""

    # ------------------------------------------------------------------
    # Shared analysis pipeline
    # ------------------------------------------------------------------

    async def analyze_clip(
        self,
        clip_path: str,
        clip_id: str,
        camera: str,
        anomaly_score: float = 0.0,
        clip_timestamp: str = "",
        clip_duration: float = 0.0,
        recent_corrections: list[dict[str, Any]] | None = None,
    ) -> AnalysisResult:
        """Full pipeline: extract frames → select best → call AI → parse response.

        ``clip_duration`` is the clip's real length in seconds (from Blink API
        metadata), when the caller has it available — passing it lets
        :meth:`_target_frame_count` size the frame budget off the ground-truth
        duration instead of estimating from the extracted frame count.

        ``recent_corrections`` is an optional list of recent human feedback
        corrections for this camera (see ``ClipDatabase.get_prompt_corrections``)
        — folded into the prompt as bounded few-shot guidance, see
        :meth:`_build_prompt`.

        Serialized via ``_analyze_lock`` — see the comment in ``__init__`` for
        why concurrent calls on the same instance are unsafe.
        """
        async with self._get_analyze_lock():
            return await self._analyze_clip_locked(
                clip_path=clip_path,
                clip_id=clip_id,
                camera=camera,
                anomaly_score=anomaly_score,
                clip_timestamp=clip_timestamp,
                clip_duration=clip_duration,
                recent_corrections=recent_corrections,
            )

    async def _analyze_clip_locked(
        self,
        clip_path: str,
        clip_id: str,
        camera: str,
        anomaly_score: float = 0.0,
        clip_timestamp: str = "",
        clip_duration: float = 0.0,
        recent_corrections: list[dict[str, Any]] | None = None,
    ) -> AnalysisResult:
        from datetime import datetime, timezone

        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        self._last_escalation_model = ""
        self._last_escalation_provider = ""
        self._last_escalation_prompt_tokens = 0
        self._last_escalation_completion_tokens = 0
        # Store camera name so provider subclasses can access it in _call_model.
        # Safe because analyze_clip() holds _analyze_lock for its whole body.
        self._current_camera: str = camera
        start = time.monotonic()
        frames = await self.extract_frames(clip_path)

        if not frames:
            return AnalysisResult(
                clip_id=clip_id,
                camera=camera,
                model=self.model_name(),
                response_text="",
                is_suspicious=False,
                confidence=0.0,
                summary="No frames could be extracted",
                frame_count=0,
                analysis_duration=time.monotonic() - start,
                analyzed_at=datetime.now(timezone.utc).isoformat(),
                anomaly_score=anomaly_score,
            )

        # Visual scene-baseline ("smart brain") lookup — compares this clip's
        # opening frame (closest to the pre-motion scene) against the
        # camera's learned background, using whatever history was recorded
        # before this clip. Folded back into the baseline further down, once
        # we know whether this clip was suspicious.
        scene_thumbnail: list[float] | None = None
        scene_deviation: float | None = None
        if self._scene_baseline_db is not None:
            scene_thumbnail = self._scene_thumbnail(frames[0])
            if scene_thumbnail is not None:
                scene_deviation = await self._scene_baseline_db.get_scene_deviation(
                    camera, scene_thumbnail
                )

        # Down-select the oversampled pool to the frames actually sent to the
        # AI. "uniform" keeps frames evenly spread across the whole clip;
        # "smart" and "sequential" both benefit from motion-weighted picks
        # (entry/peak/exit) since sequential analyses each frame individually
        # and gets more value from a few well-chosen frames than an arbitrary
        # early slice of the clip. Clips longer than _LONG_CLIP_THRESHOLD_SECONDS
        # get their frame budget doubled — see _target_frame_count().
        target_frame_count = self._target_frame_count(
            len(frames), clip_duration=clip_duration
        )
        if len(frames) > target_frame_count:
            if self._frame_strategy == "uniform":
                frames = self._select_uniform_frames(frames, target_frame_count)
            else:
                frames = self._select_best_frames(frames, target_frame_count)

        # Optional computer-vision enhancement pipeline (see vision.py) — off
        # entirely unless attach_vision_pipeline() was called and at least
        # one of its stages is enabled in config. Runs before the
        # trajectory/zone-motion heuristics below so that, when frame
        # preprocessing is enabled, those heuristics see the same enhanced
        # frames that get sent to the AI model.
        vision_hints = None
        if self._vision_pipeline is not None:
            vision_hints = await self._vision_pipeline.process_clip(
                frames,
                car_description=self._car_description,
                car_protection_applies=self._car_protection_applies(camera),
            )
            if vision_hints.enhanced_frames is not None:
                frames = vision_hints.enhanced_frames

        trajectory_hint = self._compute_motion_trajectory_hint(frames)

        zone_motion_fraction: float | None = None
        car_zone = self._car_zones.get(camera)
        if car_zone and (not self._car_cameras or camera in self._car_cameras):
            zone_motion_fraction = self._zone_motion_fraction(frames, car_zone)

        prompt = self._build_prompt(
            camera,
            anomaly_score=anomaly_score,
            clip_timestamp=clip_timestamp,
            scene_deviation=scene_deviation,
            trajectory_hint=trajectory_hint,
            recent_corrections=recent_corrections,
            zone_motion_fraction=zone_motion_fraction,
            clip_duration=clip_duration,
            vision_hints=vision_hints,
        )

        if self._frame_strategy == "sequential":
            response, escalation_frame = await self._analyze_sequentially(
                frames, prompt
            )
            if escalation_frame is not None:
                response = await self._maybe_escalate(
                    [escalation_frame], prompt, response
                )
        else:
            response = await self._call_model_with_escalation(frames, prompt)

        is_suspicious, confidence, summary = self.parse_response(response)

        # Fold this clip's opening frame into the learned scene baseline
        # unless it was a *confident* suspicious call — a low-confidence
        # hedge (often just the scene-deviation hint above making the model
        # cautious about a persistent but benign change, e.g. a car parked
        # overnight or trash put out for collection) must not block the
        # baseline from ever learning that new normal, or the same hint
        # keeps firing on every future clip. A confidently suspicious clip
        # is still withheld so a genuine intruder is never absorbed into
        # "what's normal here".
        confident_suspicious = (
            is_suspicious
            and confidence >= _SCENE_BASELINE_SUSPICION_CONFIDENCE_THRESHOLD
        )
        if (
            scene_thumbnail is not None
            and self._scene_baseline_db is not None
            and not confident_suspicious
        ):
            await self._scene_baseline_db.record_scene_baseline(camera, scene_thumbnail)

        return AnalysisResult(
            clip_id=clip_id,
            camera=camera,
            model=self.model_name(),
            response_text=response,
            is_suspicious=is_suspicious,
            confidence=confidence,
            summary=summary,
            frame_count=len(frames),
            analysis_duration=time.monotonic() - start,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            tokens_prompt=self._last_prompt_tokens,
            tokens_completion=self._last_completion_tokens,
            anomaly_score=anomaly_score,
            escalation_model=self._last_escalation_model,
            escalation_tokens_prompt=self._last_escalation_prompt_tokens,
            escalation_tokens_completion=self._last_escalation_completion_tokens,
            escalation_provider=self._last_escalation_provider,
            prompt_text=prompt if self._store_prompt_debug else "",
        )

    # ------------------------------------------------------------------
    # Two-tier escalation (any provider may act as tier 2 for any other)
    # ------------------------------------------------------------------

    async def _call_model_with_escalation(
        self, frames: list[bytes], prompt: str
    ) -> str:
        """Run tier-1 analysis via ``_call_model``, then escalate if warranted.

        See :meth:`_maybe_escalate` for the escalation logic itself.
        """
        response = await self._call_model(frames, prompt)
        return await self._maybe_escalate(frames, prompt, response)

    async def _maybe_escalate(
        self, frames: list[bytes], prompt: str, response: str
    ) -> str:
        """Escalate a tier-1 verdict to the attached tier-2 analyzer.

        Two escalation policies apply, chosen by whether the camera being
        analyzed is under protected-vehicle asset protection (see
        :meth:`_car_protection_applies`):

        - **Asset-protection cameras** always get a tier-2 double-check,
          even when tier 1 said "clear". A missed contact/proximity event
          against a protected vehicle is worse than one extra API call, so
          tier 1's "clear" is not trusted on its own here — only a tier-2
          confirmation of "clear" is. If either tier says suspicious, that
          verdict wins (see below).
        - **Every other camera** keeps the original, cost-optimized
          behavior: tier 2 is only consulted to confirm/refute a tier-1
          suspicious call, and its well-formed response is authoritative. A
          non-suspicious tier-1 result is trusted outright and never
          escalated — these cameras should flag less, not more.

        Called with a tier-1 response already in hand — either from a single
        ``_call_model()`` call across all frames, or from
        ``_analyze_sequentially()``'s single winning frame/response, so
        tier-2 cost is bounded to at most one extra call regardless of frame
        strategy. No-op when no escalation analyzer is attached. A
        malformed/empty tier-2 response (e.g. a reasoning model's invisible
        thinking tokens ate its completion budget before the JSON closed)
        falls back to tier-1's own verdict rather than risk silently
        overriding it with nothing.
        """
        if not response or self._escalation_analyzer is None:
            return response

        suspicious, _, _ = self._try_parse_json(response)
        camera = getattr(self, "_current_camera", "")
        high_recall = self._car_protection_applies(camera)
        if not suspicious and not high_recall:
            return response

        tier2 = self._escalation_analyzer
        _LOGGER.info(
            "%s/%s %s; escalating to %s/%s for a closer look",
            self.provider_name,
            self.model_name(),
            "flagged a suspicious result"
            if suspicious
            else "cleared a protected-vehicle camera clip; double-checking",
            tier2.provider_name,
            tier2.model_name(),
        )
        escalated = await tier2.run_tier_call(frames, prompt)
        if not escalated or not self._is_well_formed_json_object(escalated):
            if escalated:
                _LOGGER.warning(
                    "Escalation model %s/%s returned a malformed/truncated "
                    "response; keeping tier-1's verdict",
                    tier2.provider_name,
                    tier2.model_name(),
                )
            return response

        self._last_escalation_provider = tier2.provider_name
        self._last_escalation_model = tier2.model_name()
        self._last_escalation_prompt_tokens = tier2.last_prompt_tokens
        self._last_escalation_completion_tokens = tier2.last_completion_tokens

        if high_recall and not suspicious:
            # Tier 1 said clear on a protected-vehicle camera — only trust
            # that if tier 2 agrees. A tier-2 suspicious verdict caught
            # something tier 1 missed and must win; two "clear" agreements
            # keep tier 1's own response rather than swap in an equivalent
            # one and lose its already-recorded frame/description pairing.
            tier2_suspicious, _, _ = self._try_parse_json(escalated)
            return escalated if tier2_suspicious else response

        return escalated

    # ------------------------------------------------------------------
    # Frame extraction (shared by all providers)
    # ------------------------------------------------------------------

    async def extract_frames(self, clip_path: str) -> list[bytes]:
        """Extract JPEG frames from an MP4 using ffmpeg.

        The extraction count is the larger of the strategy's own oversampling
        target (2× max_frames in ``smart`` mode, else max_frames) and however
        many frames are needed to cover a full :data:`_MAX_CLIP_COVERAGE_SECONDS`
        clip at ``frame_interval`` spacing.  This second term is what keeps a
        60-second Blink clip from only being sampled in its first few seconds —
        ffmpeg naturally emits fewer frames than requested for shorter clips, so
        this is a safe upper bound rather than a fixed count.  Down-selection to
        the frames actually sent to the AI happens afterwards in
        :meth:`_select_best_frames` / :meth:`_select_uniform_frames`.
        """
        base_count = (
            self._max_frames * 2
            if self._frame_strategy == "smart"
            else self._max_frames
        )
        coverage_count = math.ceil(_MAX_CLIP_COVERAGE_SECONDS / self._frame_interval)
        extract_count = max(base_count, coverage_count)
        cmd = [
            "ffmpeg",
            "-i",
            clip_path,
            "-vf",
            f"fps=1/{self._frame_interval},scale=640:-1",
            "-frames:v",
            str(extract_count),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "2",
            "pipe:1",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            _LOGGER.warning("ffmpeg not available: %s", exc)
            return []

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg timed out for %s", clip_path)
            # communicate() timing out leaves the child process running;
            # kill it and reap it so it doesn't linger as a zombie/orphan.
            proc.kill()
            await proc.wait()
            return []

        if proc.returncode != 0:
            _LOGGER.warning(
                "ffmpeg exited %d for %s: %s",
                proc.returncode,
                clip_path,
                (stderr or b"").decode(errors="replace")[:200],
            )
            return []

        return self._split_jpeg_frames(stdout or b"")

    def _target_frame_count(
        self, raw_frame_count: int, clip_duration: float = 0.0
    ) -> int:
        """How many frames to actually send the AI, given the raw extracted pool.

        ``max_frames``/``frame_interval`` are honored exactly for clips at or
        under :data:`_LONG_CLIP_THRESHOLD_SECONDS`. Longer clips get their
        frame budget doubled (:data:`_LONG_CLIP_FRAME_MULTIPLIER`) — the
        configured budget alone tends to under-sample a 45-60s clip, and
        doubling keeps sampling density roughly constant across the whole
        event instead of thinning out as clips approach Blink's 60s ceiling.

        ``clip_duration`` — the clip's real length in seconds, when known
        (e.g. from Blink API metadata already stored in the database) — is
        preferred over estimating from the extracted frame count, since a
        ground-truth duration is immune to ffmpeg under/over-emitting frames
        for a given clip. Pass ``0`` (the default) to fall back to estimating
        duration from ``raw_frame_count * frame_interval`` (frames are spaced
        ``frame_interval`` seconds apart) when the real duration isn't
        available, e.g. for direct/standalone analyzer calls.
        """
        estimated_duration = (
            clip_duration
            if clip_duration > 0
            else raw_frame_count * self._frame_interval
        )
        if estimated_duration <= _LONG_CLIP_THRESHOLD_SECONDS:
            return self._max_frames
        return self._max_frames * _LONG_CLIP_FRAME_MULTIPLIER

    @staticmethod
    def _scene_thumbnail(frame: bytes) -> list[float] | None:
        """Reduce a frame to a small grayscale thumbnail for scene-baseline comparison.

        Returns a flat list of pixel values normalized to 0.0-1.0, or ``None``
        if the frame can't be decoded (e.g. Pillow unavailable or a corrupt
        JPEG) — callers treat that as "scene baseline unavailable for this
        clip" rather than an error.
        """
        try:
            import io as _io  # noqa: PLC0415

            from PIL import Image as _Image  # noqa: PLC0415

            with _Image.open(_io.BytesIO(frame)) as img:
                gray = img.convert("L").resize(
                    _SCENE_THUMBNAIL_SIZE, _Image.Resampling.LANCZOS
                )
                return [p / 255.0 for p in gray.tobytes()]
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _select_best_frames(frames: list[bytes], target_count: int) -> list[bytes]:
        """Pick the *target_count* most informative frames using motion scoring.

        Strategy:
        - Always include the first frame (scene entry) and last frame (exit).
        - Find the frame with the largest inter-frame pixel diff (peak motion).
        - Fill remaining slots with the next highest-motion frames, preferring
          candidates spread across the clip's timeline over ones clustered
          around a single motion burst — a security clip's story (approach,
          event, departure) needs coverage across the whole duration, and the
          top few frames by raw pixel delta often sit right next to each
          other (e.g. three consecutive frames of the same door swinging
          open) which wastes frame budget on near-duplicates.

        Falls back to even-spaced selection if PIL is unavailable.
        """
        if len(frames) <= target_count:
            return frames

        try:
            import io as _io  # noqa: PLC0415

            from PIL import Image as _Image  # noqa: PLC0415

            _THUMB = (64, 64)
            # tobytes() returns raw pixel bytes (L mode = 1 byte/pixel)
            # which avoids the untyped ImagingCore returned by getdata()
            thumbs: list[bytes] = [
                _Image.open(_io.BytesIO(f))
                .convert("L")
                .resize(_THUMB, _Image.Resampling.LANCZOS)
                .tobytes()
                for f in frames
            ]

            # Inter-frame absolute difference (normalised per pixel)
            pixels = _THUMB[0] * _THUMB[1]
            diffs: list[float] = [
                sum(abs(a - b) for a, b in zip(thumbs[i - 1], thumbs[i])) / pixels
                for i in range(1, len(thumbs))
            ]

            selected: set[int] = {0, len(frames) - 1}

            # Peak-motion frame (index into frames, not diffs)
            if diffs:
                peak = max(range(len(diffs)), key=lambda i: diffs[i]) + 1
                selected.add(peak)

            # Fill remaining slots by motion score, spreading picks across the
            # timeline: require each new pick to be at least min_gap frames
            # from every frame already selected, then relax that constraint
            # only if it left slots unfilled (small pools/short clips).
            ranked = sorted(
                (
                    (diffs[i], i + 1)
                    for i in range(len(diffs))
                    if (i + 1) not in selected
                ),
                reverse=True,
            )
            min_gap = max(1, len(frames) // target_count)
            for _, idx in ranked:
                if len(selected) >= target_count:
                    break
                if all(abs(idx - s) >= min_gap for s in selected):
                    selected.add(idx)
            if len(selected) < target_count:
                for _, idx in ranked:
                    if len(selected) >= target_count:
                        break
                    selected.add(idx)

            return [frames[i] for i in sorted(selected)]

        except Exception:  # noqa: BLE001
            # PIL unavailable or processing error — fall back to even spacing
            # anchored at first and last to preserve entry/exit coverage
            if target_count <= 1:
                return [frames[0]]
            anchored: set[int] = {0, len(frames) - 1}
            gap = target_count - len(anchored)
            if gap > 0:
                step = max(1, len(frames) // target_count)
                idx = step
                while len(anchored) < target_count and idx < len(frames) - 1:
                    anchored.add(idx)
                    idx += step
            return [frames[i] for i in sorted(anchored)[:target_count]]

    @staticmethod
    def _compute_motion_trajectory_hint(frames: list[bytes]) -> str | None:
        """Classify a coarse movement direction/intensity trend across *frames*.

        Reuses the same grayscale-thumbnail-diff approach as
        :meth:`_select_best_frames`'s motion scoring, applied to the frames
        actually selected for analysis (whatever the frame strategy). For
        each consecutive frame pair, computes the diff mask's weighted x
        centroid in addition to the overall diff magnitude already used for
        motion scoring — a shift in that centroid's x position across the
        sequence is a coarse "moving across frame" signal; a rising diff
        magnitude trend (the moving region occupies more pixels as it nears
        the camera) is a coarse "may be approaching" proxy. Deliberately
        coarse and conservative — returns ``None`` (no hint) far more often
        than not, since :meth:`_build_prompt` frames whatever is returned as
        a rough estimate, not a precise tracked path.

        Returns ``None`` when there are too few frames, no clear motion
        signal, or PIL is unavailable.
        """
        if len(frames) < _MOTION_TRAJECTORY_MIN_FRAMES:
            return None

        try:
            import io as _io  # noqa: PLC0415

            from PIL import Image as _Image  # noqa: PLC0415

            width, height = _MOTION_TRAJECTORY_THUMB_SIZE
            thumbs: list[bytes] = [
                _Image.open(_io.BytesIO(f))
                .convert("L")
                .resize((width, height), _Image.Resampling.LANCZOS)
                .tobytes()
                for f in frames
            ]
            pixels = width * height

            diff_magnitudes: list[float] = []
            centroids_x: list[float] = []
            for i in range(1, len(thumbs)):
                total = 0
                weighted_x = 0
                for idx, (pa, pb) in enumerate(zip(thumbs[i - 1], thumbs[i])):
                    d = abs(pa - pb)
                    if d:
                        total += d
                        weighted_x += d * (idx % width)
                diff_magnitudes.append(total / pixels)
                centroids_x.append((weighted_x / total) if total else -1.0)

            if (
                not diff_magnitudes
                or max(diff_magnitudes) < _MOTION_TRAJECTORY_DIFF_FLOOR
            ):
                return None

            valid_centroids = [cx for cx in centroids_x if cx >= 0]
            if len(valid_centroids) >= 2:
                first_x, last_x = valid_centroids[0], valid_centroids[-1]
                if (
                    abs(last_x - first_x)
                    >= width * _MOTION_TRAJECTORY_LATERAL_SHIFT_FRACTION
                ):
                    return (
                        "moving left to right across the frame"
                        if last_x > first_x
                        else "moving right to left across the frame"
                    )

            if len(diff_magnitudes) >= 2:
                midpoint = len(diff_magnitudes) // 2
                first_half = diff_magnitudes[:midpoint] or diff_magnitudes[:1]
                second_half = diff_magnitudes[midpoint:]
                avg_first = sum(first_half) / len(first_half)
                avg_second = sum(second_half) / len(second_half)
                if avg_second > avg_first * _MOTION_TRAJECTORY_INTENSITY_RATIO:
                    return (
                        "movement intensity increasing over time (may be approaching)"
                    )
                if avg_first > avg_second * _MOTION_TRAJECTORY_INTENSITY_RATIO:
                    return "movement intensity decreasing over time (may be retreating)"

            return None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _zone_motion_fraction(
        frames: list[bytes], zone: dict[str, float]
    ) -> float | None:
        """Return the fraction (0.0-1.0) of this clip's total pixel motion
        that fell inside *zone* — a normalised ``x_min``/``y_min``/``x_max``/
        ``y_max`` rectangle, e.g. a user-drawn "car zone".

        Reuses the same grayscale-thumbnail-diff approach as
        :meth:`_compute_motion_trajectory_hint`, but instead of
        characterising direction, buckets each diff pixel's magnitude into
        "inside the zone" vs. "outside" and reports what share of the
        clip's total motion energy fell inside it. This gives
        :meth:`_build_prompt` a code-computed, structured signal —
        "most of what moved was actually in the car zone" vs. "the car
        zone barely moved; the motion is background activity elsewhere" —
        instead of asking the model to judge that from raw pixels alone.

        Returns ``None`` when there are fewer than 2 frames, *zone* is
        empty, PIL is unavailable, or the clip has too little overall
        motion to attribute meaningfully.
        """
        if len(frames) < 2 or not zone:
            return None

        try:
            import io as _io  # noqa: PLC0415

            from PIL import Image as _Image  # noqa: PLC0415

            width, height = _MOTION_TRAJECTORY_THUMB_SIZE
            thumbs: list[bytes] = [
                _Image.open(_io.BytesIO(f))
                .convert("L")
                .resize((width, height), _Image.Resampling.LANCZOS)
                .tobytes()
                for f in frames
            ]

            zx1 = max(0, min(width - 1, round(zone.get("x_min", 0.0) * width)))
            zy1 = max(0, min(height - 1, round(zone.get("y_min", 0.0) * height)))
            zx2 = max(zx1 + 1, min(width, round(zone.get("x_max", 1.0) * width)))
            zy2 = max(zy1 + 1, min(height, round(zone.get("y_max", 1.0) * height)))

            total_motion = 0
            zone_motion = 0
            for i in range(1, len(thumbs)):
                for idx, (pa, pb) in enumerate(zip(thumbs[i - 1], thumbs[i])):
                    d = abs(pa - pb)
                    if not d:
                        continue
                    total_motion += d
                    x, y = idx % width, idx // width
                    if zx1 <= x < zx2 and zy1 <= y < zy2:
                        zone_motion += d

            pixels_per_pair = width * height
            if total_motion < _MOTION_TRAJECTORY_DIFF_FLOOR * pixels_per_pair:
                return None

            return zone_motion / total_motion
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _select_uniform_frames(frames: list[bytes], target_count: int) -> list[bytes]:
        """Evenly space *target_count* frames across the whole extracted pool.

        Used by the "uniform" strategy, which intentionally skips motion
        scoring — this keeps its output spread across the full clip (including
        a 60-second one) rather than motion-weighted, while still respecting
        ``max_frames``.
        """
        if len(frames) <= target_count:
            return frames
        if target_count <= 1:
            return [frames[0]]

        step = (len(frames) - 1) / (target_count - 1)
        indices = sorted({round(i * step) for i in range(target_count)})
        return [frames[i] for i in indices]

    async def _analyze_sequentially(
        self, frames: list[bytes], prompt: str
    ) -> tuple[str, bytes | None]:
        """Analyse frames one at a time and return the most alarming response.

        Each frame is sent to the AI individually via :meth:`_call_model`.
        The result with the highest concern level (suspicious > non-suspicious;
        higher confidence when tied) is returned, along with the specific
        frame that produced it — used by ``_analyze_clip_locked`` to escalate
        (see :meth:`_maybe_escalate`) on just that one frame rather than
        re-running every frame through tier 2. This mode is especially
        effective for providers that perform better on single images than on
        batches (e.g. Ollama with small models, or when per-frame clarity
        matters more than temporal context).
        """
        best_response = ""
        best_suspicious = False
        best_confidence = 0.0
        best_frame: bytes | None = None

        for frame in frames:
            response = await self._call_model([frame], prompt)
            if not response:
                continue
            suspicious, confidence, desc = self._try_parse_json(response)
            if not desc:
                if not best_response:
                    best_response = response
                    best_frame = frame
                continue
            if (
                not best_response
                or (suspicious and not best_suspicious)
                or (suspicious == best_suspicious and confidence > best_confidence)
            ):
                best_response = response
                best_suspicious = suspicious
                best_confidence = confidence
                best_frame = frame

        return best_response, best_frame

    @staticmethod
    def _split_jpeg_frames(data: bytes) -> list[bytes]:
        """Split concatenated JPEG data into individual frames."""
        frames: list[bytes] = []
        soi = b"\xff\xd8"
        eoi = b"\xff\xd9"
        pos = 0
        while pos < len(data):
            start = data.find(soi, pos)
            if start == -1:
                break
            end = data.find(eoi, start + 2)
            if end == -1:
                break
            frames.append(data[start : end + 2])
            pos = end + 2
        return frames

    # ------------------------------------------------------------------
    # Response parsing (shared by all providers)
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        camera: str,
        anomaly_score: float = 0.0,
        clip_timestamp: str = "",
        scene_deviation: float | None = None,
        trajectory_hint: str | None = None,
        recent_corrections: list[dict[str, Any]] | None = None,
        zone_motion_fraction: float | None = None,
        clip_duration: float = 0.0,
        vision_hints: VisionHints | None = None,
    ) -> str:
        """Build a rich analysis prompt with camera context, temporal context,
        anomaly alert, scene-baseline signal, movement hint, recent human
        corrections, zone-motion evidence, short-event hint, optional
        computer-vision pipeline hints, and asset-protection distance rules."""
        base = self._camera_prompts.get(camera, self._base_prompt)
        parts = [base]

        # Camera location / purpose. Explicitly ties the description to what
        # counts as normal here, so e.g. a mailbox camera cares about someone
        # lingering at the box while a backyard camera treats wildlife as
        # routine — the same activity can be normal on one camera and worth
        # flagging on another.
        camera_desc = self._camera_descriptions.get(camera, "")
        if camera_desc:
            parts.append(
                f"\n\nCamera location and purpose — {camera}: {camera_desc}\n"
                "Use this specific purpose to calibrate what is normal versus "
                "suspicious for this camera — what it is meant to watch for is "
                "what deserves scrutiny; everything else on this camera is routine."
            )
        else:
            parts.append(f"\n\nCamera: {camera}")

        # Time-of-day context (helps AI calibrate what's "normal")
        if clip_timestamp:
            try:
                from datetime import datetime as _dt  # noqa: PLC0415

                dt = _dt.fromisoformat(clip_timestamp.replace("Z", "+00:00"))
                hour = dt.hour
                if hour < 5:
                    tod = "late night"
                elif hour < 9:
                    tod = "early morning"
                elif hour < 12:
                    tod = "morning"
                elif hour < 17:
                    tod = "afternoon"
                elif hour < 20:
                    tod = "evening"
                else:
                    tod = "night"
                parts.append(
                    f"\n\nTime of day: {tod} ({dt.strftime('%H:%M')} UTC). "
                    "Factor this into your assessment of whether the activity is normal."
                )
            except Exception:  # noqa: BLE001
                pass

        # Behavior anomaly alert (temporal — unusual hour/duration for this camera)
        if anomaly_score >= 0.6:
            parts.append(
                f"\n\nBEHAVIOR ALERT: This event is statistically unusual for "
                f"this camera at this time (anomaly score {anomaly_score:.2f}/1.00). "
                "Apply heightened scrutiny to any persons or vehicles in the frame."
            )

        # Short-event hint — a code-computed signal from the clip's real
        # duration (see _SHORT_EVENT_DURATION_SECONDS) that a quick, single
        # interaction is far more consistent with routine coming-and-going
        # than with lingering, casing, or tampering. Framed as a hint the
        # model can override, not a rule, since a genuinely short but
        # visibly suspicious clip (e.g. a quick tamper-and-flee) must still
        # be flagged on its own visible content.
        if 0 < clip_duration <= _SHORT_EVENT_DURATION_SECONDS:
            parts.append(
                "\n\nSHORT EVENT: This entire clip lasts only "
                f"{clip_duration:.0f} seconds. A brief, single interaction "
                "like this — walking up, using a door, grabbing mail or a "
                "delivery, or heading to a car — is far more consistent "
                "with ordinary coming and going than with lingering, "
                "casing, or tampering, which typically take noticeably "
                "longer to unfold. Treat this as a hint favoring a routine "
                "read, not a verdict on its own — only set suspicious=true "
                "if the frames themselves clearly show concrete suspicious "
                "behavior (forced entry, lock tampering, casing, etc.), "
                "not brevity alone."
            )

        # Protected vehicle applicability is needed here (as well as below)
        # to gate the scene-baseline "favor calm" framing immediately after —
        # see that block's comment for why.
        car_applies = self._car_protection_applies(camera)

        # Visual scene-baseline ("smart brain") signal. This camera is fixed in
        # place, so its background should look almost the same clip after clip.
        # A large deviation suggests something new (object, vehicle, obstruction)
        # is in frame; a close match is a strong signal this is routine, which
        # helps suppress false positives on ordinary daily activity.
        if scene_deviation is not None:
            if scene_deviation >= _SCENE_DEVIATION_ALERT_THRESHOLD:
                parts.append(
                    "\n\nSCENE BASELINE: This camera's view currently differs from its "
                    f"usual background (deviation {scene_deviation:.2f}/1.00). Something "
                    "not normally present — an object, vehicle, or obstruction — may be "
                    "in frame. Treat this as a hint to look closer, not a verdict on its own. "
                    "This deviation is frequently just lighting, weather, shadows, or the "
                    "day/night transition — none of which are suspicious by themselves. "
                    "Only raise suspicion if the frames themselves clearly show a specific "
                    "new person, animal, or vehicle; if you cannot identify one, describe "
                    "the scene plainly and set suspicious=false."
                )
            elif not car_applies:
                # Deliberately omitted when car_applies: the protected vehicle
                # parked in its usual spot IS this camera's learned "usual
                # background" by definition, so a low deviation score here
                # means nothing more than "the car is where it always is" —
                # it says nothing about whether a person is *currently* right
                # next to it, since the opening frame this score is computed
                # from is captured at or before the moment motion begins,
                # often before someone has walked into contact with the car.
                # A "favor calm read" framing on every single clip from this
                # camera would actively work against the strict PROTECTED
                # VEHICLE distance rules below, which must be the sole
                # authority for this camera's verdict — this was found to
                # measurably suppress genuine close-proximity notifications
                # (e.g. a person leaning on or standing within a foot of the
                # vehicle) that the distance rules alone correctly catch.
                parts.append(
                    "\n\nSCENE BASELINE: This camera's view closely matches its usual "
                    "background — no unexplained new objects. Favor a calm, routine read "
                    "of the activity unless the frames themselves show something genuinely "
                    "concerning."
                )

        # Movement-trajectory hint ("smart brain" motion reasoning) — a rough
        # automated estimate of direction/intensity trend across the selected
        # frames, derived from frame-to-frame pixel differences already
        # computed during frame selection. Explicitly framed as a coarse hint
        # so the model relies on what it can actually see, not this estimate,
        # for its final judgment of approaching/retreating/lingering behavior.
        if trajectory_hint:
            parts.append(
                "\n\nMOVEMENT: Across these frames, the main area of motion "
                f"appears to be {trajectory_hint}. This is a rough automated "
                "estimate from frame-to-frame pixel differences, not a precise "
                "tracked path — use it only as a coarse hint about direction "
                "of travel, and rely on what you can actually see in the "
                "frames for your judgment of approaching/retreating/lingering "
                "behavior."
            )

        # Recent human corrections for this camera (adaptive learning — see
        # ClipDatabase.get_prompt_corrections). Bounded to a handful of the
        # most recent notes so this can't grow unbounded or drown out the
        # rest of the prompt, and framed as a hint rather than a rule so the
        # model still judges this specific clip on its own visible content.
        if recent_corrections:
            correction_lines = "\n".join(
                "- A past clip on this camera was marked "
                f"{'suspicious' if c.get('original_suspicious') else 'not suspicious'} "
                "by the AI, but a human reviewer said this was WRONG. "
                f'Reviewer\'s note: "{str(c.get("correction_note", ""))[:200]}"'
                for c in recent_corrections[:3]
                if c.get("correction_note")
            )
            if correction_lines:
                parts.append(
                    "\n\nRECENT HUMAN CORRECTIONS on this camera (learn from "
                    "these, but judge THIS clip on its own visible content — "
                    "do not assume the same pattern repeats):\n" + correction_lines
                )

        # Zone-motion evidence — a code-computed signal (see
        # BaseAnalyzer._zone_motion_fraction) telling the model what share of
        # this clip's overall pixel motion actually fell inside the
        # configured car zone, versus happening elsewhere in the frame. Only
        # emitted when a zone is configured and there's enough clip motion to
        # attribute meaningfully.
        if zone_motion_fraction is not None:
            if zone_motion_fraction >= 0.5:
                parts.append(
                    "\n\nZONE MOTION: "
                    f"{zone_motion_fraction:.0%} of this clip's overall motion occurred "
                    "within the configured car zone — the activity is concentrated at or "
                    "near the protected vehicle's usual spot, not just passing through the "
                    "wider frame."
                )
            else:
                parts.append(
                    "\n\nZONE MOTION: "
                    f"Only {zone_motion_fraction:.0%} of this clip's overall motion occurred "
                    "within the configured car zone — most of the activity is happening "
                    "elsewhere in the frame, away from the protected vehicle's usual spot. "
                    "Do not assume the vehicle is involved just because something moved "
                    "somewhere in frame."
                )

        # Optional computer-vision pipeline hints (see vision.py) — each is
        # independently toggled in config and already fully formatted with
        # its own section header and hedging language; simply append
        # whichever ones a given clip actually produced. None of these
        # exist unless attach_vision_pipeline() was called and the
        # corresponding stage is enabled.
        if vision_hints is not None:
            for hint in (
                vision_hints.detection_hint,
                vision_hints.tracking_hint,
                vision_hints.depth_hint,
                vision_hints.contact_hint,
                vision_hints.recognized_resident_hint,
            ):
                if hint:
                    parts.append(hint)

        # Protected vehicle with precise distance rules — only for cameras that
        # can see the car (all cameras when car_cameras is empty, otherwise only
        # the cameras explicitly listed in car_cameras). car_applies was
        # already computed above, before the scene-baseline block.
        if car_applies:
            parts.append(
                f"\n\nPROTECTED VEHICLE: {self._car_description}\n"
                "This description identifies the SPECIFIC vehicle to protect. If other "
                "vehicles are also visible (e.g. a neighbor's car, street parking, passing "
                "traffic), apply the distance and behavior rules below ONLY to the vehicle "
                "matching this description — a different vehicle parked or passing nearby is "
                "not itself suspicious no matter how close it parks, and should only be "
                "flagged if a person or animal is also involved per the rules below.\n"
                "IMPORTANT — matching under real camera conditions: if only ONE vehicle is "
                "visible anywhere in the frame, treat it as the protected vehicle by default, "
                "even if you cannot personally confirm every detail of the description. Do "
                "NOT withhold or soften a contact/proximity finding just because a color, "
                "make/model, or license plate isn't clearly confirmable — night vision and "
                "infrared footage are often grayscale (colors are unreliable or invisible), "
                "and a license plate is rarely legible at security-camera resolution even in "
                "daylight. Never let an unconfirmed detail override what you can plainly see: "
                "a person's hand, foot, or body touching or leaning against the one visible "
                "vehicle. Only treat a vehicle as NOT the protected one when a second, clearly "
                "distinct vehicle is also visible and the two can actually be told apart.\n"
                "First identify whether each subject is a person, a vehicle, or an "
                "animal — never apply these vehicle-distance rules to a person unless a "
                "vehicle is also genuinely visible in frame. Then apply these distance "
                "and behavior rules STRICTLY. Only a person or animal near the vehicle can "
                "make a scene suspicious — a second vehicle parked or stopped near the "
                "protected one is always described, never flagged, on its own. The single "
                "most important distinction is LINGERING OR CONTACT versus simply PASSING "
                "THROUGH:\n"
                "• Touching, pressing against, reaching into, or within 1 foot of the "
                "vehicle (person, animal, or object): suspicious=true, confidence ≥0.8\n"
                "• A person 1–3 feet from the vehicle who stops, lingers, circles it, "
                "faces it, or reaches toward it: suspicious=true, confidence ≥0.6\n"
                "• An animal 1–3 feet from the vehicle that stops to sniff, paw at, jump "
                "on, urinate/defecate on, or otherwise investigate it: suspicious=true, "
                "confidence ≥0.5\n"
                "• Another vehicle parking, stopping, or passing near the protected "
                "vehicle, with no person or animal on foot approaching either vehicle: "
                "suspicious=false, no matter how close the vehicles appear — a second "
                "household car, a visitor parking, a neighbor's car, or ordinary curbside "
                "parking is routine. Describe it plainly, e.g. 'a second car is parked in "
                "the driveway near the protected vehicle', without alarm language.\n"
                "• Another vehicle making physical contact with the protected vehicle — "
                "bumping, scraping, or sideswiping it while parking, passing, or backing "
                "out — is ALWAYS suspicious=true, confidence ≥0.8, even a minor low-speed "
                "contact and even if the other vehicle stops and its driver gets out "
                "afterward, or drives off without stopping (hit-and-run). This is the one "
                "case where contact with another vehicle, not just a person, makes the "
                "protected vehicle's safety the concern.\n"
                "• A person, animal, bicycle, or other vehicle simply walking, running, "
                "or driving past — even within a few feet — WITHOUT stopping, lingering, "
                "or reaching toward the protected vehicle: suspicious=false. Passing near "
                "a parked car on a sidewalk, driveway, or yard is completely normal and "
                "must NOT be flagged just because the path happens to run close to it — "
                "distance alone is never enough, only distance combined with stopping or "
                "reaching matters.\n"
                "• A vehicle driving past on the street without slowing or stopping near "
                "the protected vehicle: suspicious=false — ordinary through-traffic is NOT "
                "suspicious no matter how frequent. Describe it plainly as simply driving "
                "up or down the street; do not say it was 'near' the protected vehicle or "
                "anything else just because it passed through the frame.\n"
                "• Anyone or anything more than 3 feet from the vehicle and not actively "
                "approaching it: suspicious=false unless there is other clear evidence of "
                "tampering\n"
                "• Lawn equipment (a mower, trimmer, or blower) merely being operated near "
                "the vehicle by a neighbor or landscaper, or a trash can, lid, or branch "
                "merely coming to rest against the vehicle in the wind, with no person "
                "involved and no visible impact: suspicious=false — routine yard "
                "maintenance and wind-blown debris resting nearby are not tampering, even "
                "though they bring an object right up to the vehicle.\n"
                "• A mower/trimmer flinging a rock, stick, or debris that visibly strikes "
                "the vehicle with force, or wind-blown debris (a trash can, lid, branch, or "
                "similar) visibly striking, bouncing off, or denting/scraping the vehicle — "
                "not just resting against it: suspicious=true, confidence ≥0.6, even with "
                "no person at fault and even if unintentional. Any event that visibly "
                "damages or risks damaging the protected vehicle is worth reporting "
                "regardless of whether a person caused it.\n"
                "Reference: a car door handle is about 4 feet off the ground; a typical car "
                "is about 6 feet wide.\n"
                "When something is genuinely close to or lingering near the vehicle, always "
                "include a natural-language distance estimate in your description, such as "
                "'right next to the car', 'about 2 feet from the driver door', or 'well away "
                "from the vehicle'. For ordinary passing traffic, pedestrians, or animals "
                "that don't stop, skip distance language entirely and just describe the "
                "movement — do not call routine passers-by suspicious merely because they "
                "were near the vehicle at some point."
            )
        elif self._car_description and self._car_cameras:
            # A protected vehicle exists elsewhere on the property, but this camera
            # is not one of the cameras configured to see it (is_car_camera unchecked
            # for this camera in the AI tab). State this explicitly so the model
            # doesn't borrow car/driveway language meant for a different camera.
            parts.append(
                f"\n\nThis camera ({camera}) does not view the protected vehicle. "
                "Do not mention a car, driveway, or vehicle distance in your description "
                "unless a vehicle is clearly visible in the frames you were given."
            )

        # Ensure the description stays human-readable, never exposes internal data,
        # and never borrows scenery from a different camera. The example phrase is
        # only about vehicle distance when this specific camera has car-distance
        # rules in play (car_applies) — otherwise it uses a camera-agnostic example
        # so smaller models aren't nudged into inventing a car or driveway that this
        # camera cannot actually see (each camera has its own field of view).
        example_phrase = (
            "Use natural phrases like 'standing about 2 feet from the car' or 'walking past the driveway'. "
            if car_applies
            else "Use natural phrases like 'standing near the front steps' or 'walking across the yard'. "
        )
        parts.append(
            "\n\nOUTPUT RULES: The 'description' field must be written in plain English "
            "as if explaining to a homeowner what happened, and must describe ONLY what is "
            f"actually visible in these specific frames from the {camera} camera — never assume "
            "objects or areas seen by other cameras on the property are visible here. "
            "Keep it SHORT: one sentence, or at most two only when genuinely necessary — "
            "about the length of 'A person is walking past the car' or 'A person is standing "
            "very close to the car and appears to be looking inside it'. State only the "
            "notable person, vehicle, or animal and what they are doing, from a security-"
            "monitoring perspective focused on this property's assets. Do NOT list or "
            "describe static background scenery that isn't part of the notable activity — "
            "other parked vehicles that aren't involved, houses, weather, foliage or grass, "
            "utility poles, power lines, and general neighborhood description all add nothing "
            "to a security summary and must be left out entirely. "
            + example_phrase
            + "Identify subjects accurately: state plainly whether you see a person, a "
            "vehicle (car, truck, van, motorcycle), or an animal — never describe a "
            "vehicle's movement as if it were a person, or vice versa. A vehicle simply "
            "moving along the street without stopping, parking, or approaching a person "
            "or property is routine traffic — describe it plainly, e.g. 'a car drove up "
            "the street', and do not say it was 'near' anyone or anything unless it "
            "actually stopped or slowed close to a specific person, vehicle, or entryway. "
            "A person or animal simply walking, running, or otherwise passing through the "
            "frame — on a sidewalk, street, or yard — without stopping, lingering, or "
            "interacting with anything on the property is routine and must be marked "
            "suspicious=false: merely being visible to a security camera is never "
            "suspicious by itself, only stopping, lingering, tampering, or clearly "
            "unusual behavior is. "
            "Write in the calm, factual tone of a professional security analyst — state "
            "only what is observable, avoid speculation or alarmist language, and reserve "
            "words like 'suspicious' for genuine cause for concern. "
            "If you set suspicious=true, confidence must be at least 0.5 — that value is "
            "not a hedge, it means you identified a specific person, animal, or vehicle "
            "actually behaving in a concerning way (lingering, tampering, or approaching "
            "closely as described above). An ambient change alone — lighting, weather, "
            "shadows, or a shift in background scenery — is never sufficient for "
            "suspicious=true. If your certainty is below 0.5, set suspicious=false instead "
            "of reporting a low-confidence guess. "
            "NEVER include any of these technical terms in the description: "
            "'bounding box', 'normalized', 'frame width', 'frame percentage', 'spatial data', "
            "'INTERNAL', 'CONTEXT', 'proximity analysis', 'overlap', 'gap 0.', or any decimal coordinates. "
            "Any internal proximity hints provided are for your reasoning only — do not quote them."
        )

        return "".join(parts)

    def parse_response(self, response: str) -> tuple[bool, float, str]:
        """Parse AI response into (is_suspicious, confidence, summary).

        Tries JSON parsing first, falls back to keyword matching.
        """
        if not response:
            return False, 0.0, ""

        is_suspicious, confidence, summary = self._try_parse_json(response)
        if summary:
            # Small models (e.g. Moondream) often return confidence=0.0 even
            # when marking something suspicious because they don't calibrate
            # scores. Derive a non-zero confidence from keyword matching so
            # downstream thresholds and Discord embeds show a useful value.
            if is_suspicious and confidence <= 0.0:
                lower = (summary + " " + response).lower()
                matched = [k for k in self._suspicious_keywords if k in lower]
                confidence = min(1.0, len(matched) * 0.3) if matched else 0.5
            return is_suspicious, confidence, self._clean_summary(summary)

        # Fallback: keyword matching
        lower = response.lower()
        matched = [k for k in self._suspicious_keywords if k in lower]
        is_suspicious = len(matched) > 0
        confidence = min(1.0, len(matched) * 0.3) if matched else 0.1

        summary = self._clean_summary(response)

        return is_suspicious, confidence, summary

    @staticmethod
    def _clean_summary(text: str) -> str:
        """Cap a description to the short form the OUTPUT RULES prompt asks for.

        Keeps at most :data:`_MAX_SUMMARY_SENTENCES` sentences, dropping
        everything from the point a sentence repeats one already kept
        (case/punctuation-insensitive) — the signature of the degenerate
        repetition loop described above. Falls back to a hard character cap
        for text with no sentence-ending punctuation to split on.
        """
        text = text.strip()
        if not text:
            return text

        sentences = re.split(r"(?<=[.!?])\s+", text)
        kept: list[str] = []
        seen: set[str] = set()
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            normalized = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
            if normalized in seen:
                break
            seen.add(normalized)
            kept.append(sentence)
            if len(kept) >= _MAX_SUMMARY_SENTENCES:
                break

        result = " ".join(kept)
        if len(result) > _MAX_SUMMARY_CHARS:
            result = result[:_MAX_SUMMARY_CHARS].rstrip() + "…"
        return result

    @staticmethod
    def _try_parse_json(response: str) -> tuple[bool, float, str]:
        """Attempt to extract a JSON object from the response."""
        start = response.find("{")
        end = response.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return False, 0.0, ""

        try:
            obj = json.loads(response[start : end + 1])
        except json.JSONDecodeError:
            return False, 0.0, ""

        suspicious = bool(obj.get("suspicious", False))
        try:
            confidence = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        description = str(obj.get("description", "") or "")
        return suspicious, confidence, description

    @staticmethod
    def _is_well_formed_json_object(response: str) -> bool:
        """Return True if *response* contains a syntactically complete JSON object.

        Distinguishes a genuine "not suspicious" verdict from a truncated or
        malformed response (e.g. a reasoning model's invisible thinking tokens
        ate its completion budget before the JSON closed) — ``_try_parse_json``
        collapses both cases to ``(False, 0.0, "")``, which is indistinguishable
        from a real negative verdict without this check.
        """
        start = response.find("{")
        end = response.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return False
        try:
            json.loads(response[start : end + 1])
        except json.JSONDecodeError:
            return False
        return True


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------


class ClipAnalyzer(BaseAnalyzer):
    """Extracts frames from clips and sends them to an Ollama vision model."""

    def __init__(
        self,
        ollama_url: str,
        model: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model
        self._session: aiohttp.ClientSession | None = None

    @property
    def provider_name(self) -> str:
        return "ollama"

    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._ollama_url}/api/tags", timeout=_HEALTH_TIMEOUT
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Fetch vision-capable models from Ollama, sorted best-first."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._ollama_url}/api/tags", timeout=_HEALTH_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                all_models = data.get("models", [])
                vision = [m for m in all_models if is_vision_model(m.get("name", ""))]
                for m in vision:
                    m["score"] = _vision_model_score(m.get("name", ""))
                return sorted(vision, key=lambda m: m.get("score", 0), reverse=True)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            json.JSONDecodeError,
        ):
            return []

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        return await self.call_ollama(frames, prompt)

    async def call_ollama(self, frames: list[bytes], prompt: str) -> str:
        """Send frames to Ollama vision model and return the response text."""
        images = [base64.b64encode(f).decode("ascii") for f in frames]

        payload = {
            "model": self._model,
            "system": _VISION_SYSTEM_PROMPT,
            "prompt": prompt,
            "images": images,
            "stream": False,
            "format": "json",
        }

        try:
            session = await self._get_session()
            async with session.post(
                f"{self._ollama_url}/api/generate",
                json=payload,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Ollama returned HTTP %d", resp.status)
                    return ""
                data = await resp.json()
                self._last_prompt_tokens = int(data.get("prompt_eval_count") or 0)
                self._last_completion_tokens = int(data.get("eval_count") or 0)
                return str(data.get("response", ""))
        except asyncio.TimeoutError:
            _LOGGER.warning("Ollama request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Ollama request failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# Ollama Cloud provider
# ---------------------------------------------------------------------------


class OllamaCloudAnalyzer(ClipAnalyzer):
    """Analyzes clips via the Ollama Cloud API (api.ollama.com).

    Behaves identically to :class:`ClipAnalyzer` (local Ollama) but targets
    the Ollama Cloud endpoint and authenticates every request with an API key
    via ``Authorization: Bearer <key>``.
    """

    _CLOUD_BASE_URL = "https://api.ollama.com"

    def __init__(
        self,
        api_key: str,
        model: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
    ) -> None:
        super().__init__(
            ollama_url=self._CLOUD_BASE_URL,
            model=model,
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "ollama_cloud"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def health_check(self) -> bool:
        """Return False immediately if no API key is configured.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits Ollama
        Cloud's real API — see the cache helpers on :class:`BaseAnalyzer` for
        why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("Ollama Cloud: no API key configured")
            return self._store_health_check_result(False)
        return self._store_health_check_result(await super().health_check())


# ---------------------------------------------------------------------------
# Moondream Cloud provider
# ---------------------------------------------------------------------------

# Moondream Cloud pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://docs.moondream.ai/pricing/
_MOONDREAM_CLOUD_PRICING: tuple[float, float] = (0.30, 2.50)


class _MoondreamDetectionMixin:
    """Shared detect-augmented reasoning helpers for Moondream analyzers.

    Both :class:`MoondreamCloudAnalyzer` and :class:`MoondreamLocalAnalyzer`
    run the same phased "detect objects, then reason about them" pipeline —
    only how each fetches a detection (HTTP vs. local package call) differs.
    This mixin holds the provider-agnostic pieces: bounding-box math and the
    natural-language hints built from it.
    """

    # Matches a plate/license-plate mention and its adjacent token(s), e.g.
    # "plate ABC1234", "license plate: XYZ-999", "plate # 7GHK123" —
    # case-insensitive, tolerant of an optional "license" prefix and a
    # colon/dash/# separator before the plate value itself.
    _PLATE_MENTION_RE = re.compile(
        r"(?:license\s+)?plate\s*[:#-]?\s*[A-Za-z0-9][A-Za-z0-9 -]{0,10}[A-Za-z0-9]",
        re.IGNORECASE,
    )

    @classmethod
    def _visual_detect_query(cls, car_description: str) -> str:
        """Return *car_description* with any license-plate mention removed.

        The protected-vehicle description is shown to the model verbatim in
        the text prompt (a plate number there is useful reasoning context —
        e.g. confirming a match when it happens to be legible). But this
        same string is also sent as a zero-shot ``/detect`` query to visually
        *locate* the vehicle's bounding box, and a plate number isn't a
        visual feature Moondream's detector can ground — including it can
        derail an otherwise simple "silver Kia Forte" query onto the wrong
        region, or onto nothing at all, corrupting the disambiguation this
        whole mechanism depends on. Falls back to the original description
        if stripping the plate mention would leave nothing usable.
        """
        stripped = cls._PLATE_MENTION_RE.sub("", car_description)
        stripped = re.sub(r"\s{2,}", " ", stripped)
        stripped = re.sub(r"\s*,\s*,\s*", ", ", stripped).strip(" ,.-")
        return stripped or car_description

    @staticmethod
    def _bbox_gap(a: dict[str, float], b: dict[str, float]) -> float:
        """Return the Euclidean gap between two bounding boxes.

        0.0 means the boxes overlap; 1.0 means maximum separation.
        Uses normalised coordinates (0–1 relative to image width/height).
        """
        ax1, ay1 = a.get("x_min", 0.0), a.get("y_min", 0.0)
        ax2, ay2 = a.get("x_max", 1.0), a.get("y_max", 1.0)
        bx1, by1 = b.get("x_min", 0.0), b.get("y_min", 0.0)
        bx2, by2 = b.get("x_max", 1.0), b.get("y_max", 1.0)
        x_gap = max(0.0, max(ax1, bx1) - min(ax2, bx2))
        y_gap = max(0.0, max(ay1, by1) - min(ay2, by2))
        return (x_gap**2 + y_gap**2) ** 0.5

    @classmethod
    def _bbox_min_gap(
        cls,
        boxes_a: list[dict[str, float]],
        boxes_b: list[dict[str, float]],
    ) -> float:
        """Return the minimum gap between any pair of boxes across two lists."""
        min_gap = 1.0
        for a in boxes_a:
            for b in boxes_b:
                min_gap = min(min_gap, cls._bbox_gap(a, b))
        return min_gap

    @classmethod
    def _bbox_min_pairwise_gap(cls, boxes: list[dict[str, float]]) -> float:
        """Return the minimum gap between any two distinct boxes in one list.

        Used to detect a second vehicle parked or stopped close to the
        protected vehicle when no person or animal is in frame — e.g. two
        detected "car" boxes sitting right next to each other.
        """
        if len(boxes) < 2:
            return 1.0
        min_gap = 1.0
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                min_gap = min(min_gap, cls._bbox_gap(boxes[i], boxes[j]))
        return min_gap

    @staticmethod
    def _bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
        """Return the Intersection-over-Union overlap ratio of two boxes.

        Unlike :meth:`_bbox_gap` (which measures separation and is 0.0 for
        both "identical box" and "merely touching, no area in common"), IoU
        distinguishes those two cases — needed to tell "the same physical
        vehicle detected twice" (high IoU) apart from "two different
        vehicles parked flush against each other" (near-zero IoU despite a
        zero gap).
        """
        ax1, ay1 = a.get("x_min", 0.0), a.get("y_min", 0.0)
        ax2, ay2 = a.get("x_max", 1.0), a.get("y_max", 1.0)
        bx1, by1 = b.get("x_min", 0.0), b.get("y_min", 0.0)
        bx2, by2 = b.get("x_max", 1.0), b.get("y_max", 1.0)
        inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
        intersection = inter_w * inter_h
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - intersection
        if union <= 0.0:
            return 0.0
        return intersection / union

    @classmethod
    def _dedupe_boxes(
        cls, boxes: list[dict[str, float]], iou_threshold: float = 0.3
    ) -> list[dict[str, float]]:
        """Collapse duplicate detections of the same physical object.

        Zero-shot open-vocabulary detectors like Moondream's ``/detect`` can
        return more than one box for a single physical object — e.g. a
        generic "car" query matching both the vehicle's full body and a
        tighter crop of the same vehicle as two separate boxes. Left
        undeduplicated, two boxes for one parked car get treated by
        :meth:`_other_vehicle_boxes` as two distinct vehicles, producing a
        false "another vehicle stopped right next to the protected vehicle"
        alert for a car that is simply parked alone in its own driveway.
        Boxes with IoU at or above *iou_threshold* are treated as the same
        object; the larger (by area) box of each overlapping pair suppresses
        the smaller one. Surviving boxes are returned in their original
        input order — only overlap decides what's removed, never reordering
        the untouched majority of non-overlapping boxes.
        """
        if len(boxes) <= 1:
            return boxes

        def area(b: dict[str, float]) -> float:
            return max(0.0, b.get("x_max", 1.0) - b.get("x_min", 0.0)) * max(
                0.0, b.get("y_max", 1.0) - b.get("y_min", 0.0)
            )

        priority = sorted(range(len(boxes)), key=lambda i: area(boxes[i]), reverse=True)
        suppressed: set[int] = set()
        for pos, i in enumerate(priority):
            if i in suppressed:
                continue
            for j in priority[pos + 1 :]:
                if (
                    j not in suppressed
                    and cls._bbox_iou(boxes[i], boxes[j]) >= iou_threshold
                ):
                    suppressed.add(j)
        return [b for idx, b in enumerate(boxes) if idx not in suppressed]

    @classmethod
    def _other_vehicle_boxes(
        cls,
        protected_boxes: list[dict[str, float]],
        all_boxes: list[dict[str, float]],
    ) -> list[dict[str, float]]:
        """Return boxes from *all_boxes* that are not the protected vehicle.

        A box counts as "the protected vehicle" when it substantially
        overlaps (IoU ≥ 0.5) one of *protected_boxes* — the generic "car"
        detect and the description-specific detect each produce their own
        box for the same physical vehicle, with minor jitter, so this
        cross-matches them by overlap rather than identity. Returns
        *all_boxes* unchanged when *protected_boxes* is empty, since there's
        nothing yet to distinguish "other" from.
        """
        if not protected_boxes:
            return all_boxes
        return [
            box
            for box in all_boxes
            if max(cls._bbox_iou(box, p) for p in protected_boxes) < 0.5
        ]

    @staticmethod
    def _proximity_hint(gap: float, subject: str) -> str:
        """Build an [INTERNAL PROXIMITY HINT] string tiering *subject*'s
        distance from the protected vehicle from touching down to several
        feet away. Used for person/animal proximity, where the subject
        shares the vehicle's ground plane and 2D bounding-box distance is a
        reasonable proxy for real-world distance. Vehicle-to-vehicle
        proximity uses :meth:`_vehicle_proximity_hint` instead — see its
        docstring for why the two cases need different calibration.
        """
        prefix = (
            "[INTERNAL PROXIMITY HINT — use for reasoning only, "
            "do NOT copy this text into the description]: "
        )
        if gap <= 0.0:
            return (
                f"{prefix}The {subject} appears to be directly touching or pressed "
                "against the vehicle. Describe this as 'right next to' "
                "or 'touching the car' in plain English."
            )
        if gap < 0.05:
            return (
                f"{prefix}The {subject} appears to be less than 1 foot from the vehicle. "
                "Describe this as 'very close to the car' in plain English."
            )
        if gap < 0.15:
            return (
                f"{prefix}The {subject} appears to be roughly 1–3 feet from the vehicle. "
                "This is close but not touching — do NOT flag as suspicious "
                "unless actively lingering, reaching for, or touching the car. "
                "Describe this as 'a couple of feet from the car' in plain English."
            )
        return (
            f"{prefix}The {subject} appears to be several feet from the vehicle — "
            "this distance is NOT suspicious on its own. "
            "Set suspicious=false unless there is other clear evidence of tampering. "
            "Describe this as 'well away from the car' in plain English."
        )

    @staticmethod
    def _vehicle_proximity_hint(gap: float) -> str:
        """Build an [INTERNAL VEHICLE PROXIMITY HINT] for another vehicle
        detected near the protected vehicle.

        Unlike a person or animal (which stands on the ground right next to
        the car, so 2D bounding-box distance tracks real-world distance
        reasonably well), two vehicle boxes can appear close or even
        touching in a 2D frame while being many feet apart in real depth —
        a car driving past on the street behind or beside a parked car
        routinely overlaps it in screen space purely from camera
        perspective. More fundamentally, even a vehicle that genuinely does
        park or stop close to the protected one — a second household car, a
        visitor, a neighbor — is routine and not a security concern by
        itself; only a person or animal actually near the vehicle makes a
        scene worth flagging (see :meth:`_proximity_hint`). This hint is
        therefore unconditional rather than gap-dependent: it never asks the
        model to weigh the bounding-box distance, only to describe the scene
        accurately and leave suspicious=false. The analyzer also enforces
        this in code (see the ``multiple_vehicles``-and-no-subjects override
        in ``_call_model``), so an instruction-following slip here — small
        vision models don't reliably honor negative constraints — can't by
        itself produce a false alert.
        """
        return (
            "[INTERNAL VEHICLE PROXIMITY HINT — use for reasoning only, do "
            "NOT copy this text into the description]: Another vehicle's "
            "bounding box is close to or overlapping the protected "
            f"vehicle's in this single 2D frame (estimated gap {gap:.2f}). "
            "This overlap can happen purely from camera perspective — a car "
            "driving past on the street behind or beside the parked vehicle "
            "commonly appears adjacent to or touching it in the frame while "
            "actually being many feet away in real distance. Even when the "
            "other vehicle genuinely is parked or stopped close by, that is "
            "routine (a second household car, a visitor, a neighbor) and "
            "NOT suspicious on its own — set suspicious=false regardless of "
            "how close the vehicles appear or whether one is stopping, "
            "parking, or backing up. Describe the scene plainly, e.g. 'a "
            "second car is parked near the protected vehicle' or 'a car "
            "drove up the street', without alarm language. Only set "
            "suspicious=true if a person or animal is also visible actually "
            "touching, lingering near, or reaching toward either vehicle."
        )

    @staticmethod
    def _position_hint(subjects: list[tuple[str, dict[str, float]]]) -> str:
        """Build an [INTERNAL POSITION HINT] locating up to 3 detected
        subjects within the frame, for cameras with no protected-vehicle
        proximity rules in play. Each entry is a ``(label, box)`` pair, e.g.
        ``("Person", box)``, ``("Animal", box)``, or ``("Vehicle", box)`` —
        the label lets one hint cover people, animals, and incidental
        vehicles on cameras that aren't watching a protected vehicle.
        Returns "" when *subjects* is empty.
        """
        position_notes = []
        label_counts: dict[str, int] = {}
        for label, box in subjects[:3]:
            label_counts[label] = label_counts.get(label, 0) + 1
            cx = (box.get("x_min", 0.0) + box.get("x_max", 1.0)) / 2
            cy = (box.get("y_min", 0.0) + box.get("y_max", 1.0)) / 2
            if cx < 0.33:
                side = "left"
            elif cx > 0.67:
                side = "right"
            else:
                side = "centre"
            if cy < 0.33:
                vert = "top"
            elif cy > 0.67:
                vert = "bottom"
            else:
                vert = "middle"
            position_notes.append(
                f"{label} {label_counts[label]} is in the {vert}-{side} of the frame"
            )
        if not position_notes:
            return ""
        return (
            "[INTERNAL POSITION HINT — use for reasoning only, "
            "do NOT copy this text into the description]: "
            + "; ".join(position_notes)
            + "."
        )

    @staticmethod
    def _vehicle_hint() -> str:
        """Build an [INTERNAL VEHICLE HINT] for cameras with no protected-
        vehicle rules in play, when a vehicle is detected with no person
        present. Nudges the model to describe ordinary passing or parking
        traffic plainly — e.g. "a car drove up the street" — instead of
        reflexively treating any vehicle in frame as noteworthy just
        because it appeared on a motion-triggered clip.
        """
        return (
            "[INTERNAL VEHICLE HINT — use for reasoning only, do NOT copy "
            "this text into the description]: A vehicle is visible in this "
            "frame. If it is simply passing through, driving by, or parking "
            "normally with no person involved, describe it plainly (e.g. 'a "
            "car drove up the street') and set suspicious=false unless it is "
            "clearly behaving abnormally (e.g. stopping to case the property)."
        )

    @staticmethod
    def _no_subject_response() -> str:
        """Hardcoded JSON result for a frame with no person, animal, or second
        vehicle detected — skips the expensive query/caption calls entirely.
        """
        return (
            '{"suspicious": false, "confidence": 0.9, '
            '"description": "No person detected in this frame. '
            'Motion likely caused by a vehicle, animal, or environmental factor."}'
        )

    @staticmethod
    def _force_not_suspicious(response: str) -> str:
        """Rewrite a raw model JSON response to force ``suspicious: false``.

        Called when detection evidence shows the only thing that could have
        driven a "suspicious" verdict for this frame was vehicle-to-vehicle
        proximity with no person or animal present (see
        :meth:`_vehicle_proximity_hint`). That hint asks the model to always
        answer suspicious=false in this case, but small vision-language
        models don't reliably follow negative instructions, so this enforces
        the policy in code instead of trusting the model's own fields.
        Returns *response* unchanged if it isn't parseable JSON or is already
        not suspicious.
        """
        start = response.find("{")
        end = response.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return response
        try:
            obj = json.loads(response[start : end + 1])
        except json.JSONDecodeError:
            return response
        if not obj.get("suspicious"):
            return response
        obj["suspicious"] = False
        try:
            obj["confidence"] = min(0.3, float(obj.get("confidence", 0.0) or 0.0))
        except (TypeError, ValueError):
            obj["confidence"] = 0.0
        return json.dumps(obj)


class MoondreamCloudAnalyzer(_MoondreamDetectionMixin, BaseAnalyzer):
    """Analyzes clips via the Moondream Cloud API (api.moondream.ai).

    Pass ``finetune_model`` (e.g. ``"moondream3-preview/abc123@50"``) to use a
    fine-tuned checkpoint for inference instead of the base model.  Build the
    model ID with :meth:`MoondreamFineTuneManager.get_model_id` after training.
    """

    _BASE_URL = "https://api.moondream.ai/v1"
    # Model identifier returned by the Moondream API as of mid-2025.
    _MODEL_ID = "moondream3-preview"

    def __init__(
        self,
        api_key: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
        finetune_model: str = "",
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )
        self._api_key = api_key
        self._finetune_model = finetune_model
        self._session: aiohttp.ClientSession | None = None

    @property
    def provider_name(self) -> str:
        return "moondream_cloud"

    def model_name(self) -> str:
        return self._finetune_model or self._MODEL_ID

    def set_finetune_model(self, model_id: str) -> None:
        """Switch inference to a fine-tuned checkpoint at runtime, no restart.

        *model_id* is the value returned by
        :meth:`MoondreamFineTuneManager.get_model_id` (e.g.
        ``"moondream3-preview/abc123@50"``). Pass an empty string to revert
        to the base model. Mirrors the hot-swap pattern used by
        ``update_camera_descriptions``/``update_camera_prompts`` elsewhere on
        :class:`BaseAnalyzer`.
        """
        self._finetune_model = model_id

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health_check(self) -> bool:
        """Return True if the API key is set and the cloud endpoint is reachable.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits Moondream
        Cloud's real API — see the cache helpers on :class:`BaseAnalyzer` for
        why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("Moondream Cloud: no API key configured")
            return self._store_health_check_result(False)
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.moondream.ai/",
                timeout=_HEALTH_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                return self._store_health_check_result(resp.status < 500)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream Cloud health check failed: %s", exc)
            return self._store_health_check_result(False)

    def model_pricing(self) -> tuple[float, float]:
        """Return (input_price, output_price) per 1M tokens for Moondream Cloud."""
        return _MOONDREAM_CLOUD_PRICING

    async def fetch_models(self) -> list[dict[str, Any]]:
        inp, out = _MOONDREAM_CLOUD_PRICING
        models: list[dict[str, Any]] = [
            {
                "name": self._MODEL_ID,
                "display_name": f"Moondream Cloud (${inp:.2f}/${out:.2f} per 1M tokens)",
                "description": f"Moondream Cloud API (${inp:.2f}/${out:.2f} per 1M tokens)",
            }
        ]
        if self._finetune_model:
            models.append(
                {
                    "name": self._finetune_model,
                    "display_name": f"Moondream Fine-tuned: {self._finetune_model}",
                    "description": "Custom fine-tuned model via Moondream Cloud fine-tuning API",
                }
            )
        return models

    # Image encoder cost per 640px JPEG frame (empirical, based on observed
    # Moondream Cloud usage: ~869 tokens for a single-frame request with a
    # short prompt, leaving ~800 tokens for the image after subtracting text).
    # The Moondream API does not return usage stats, so these are estimates.
    _IMAGE_TOKENS_PER_FRAME: int = 800

    async def _detect_objects(
        self, frame: bytes, object_name: str
    ) -> list[dict[str, float]]:
        """Call the Moondream /detect endpoint for ``object_name``.

        Returns a list of normalised bounding boxes
        ``[{x_min, y_min, x_max, y_max}]``.  Returns ``[]`` on any error so
        callers can safely skip detect-based logic when it fails.
        """
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload: dict[str, Any] = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "object": object_name,
        }
        if self._finetune_model:
            payload["model"] = self._finetune_model
        headers = {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._BASE_URL}/detect",
                json=payload,
                headers=headers,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Moondream /detect returned HTTP %d for %r",
                        resp.status,
                        object_name,
                    )
                    return []
                data = await resp.json()
                objects = data.get("objects", [])
                return [o for o in objects if isinstance(o, dict)]
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream /detect failed for %r: %s", object_name, exc)
            return []

    async def _caption_frame(self, frame: bytes) -> str:
        """Call Moondream Cloud's dedicated ``/caption`` endpoint for a
        factual, grounding description of *frame*.

        The caption skill is tuned specifically for scene description
        (elements, context, colors, positioning) rather than free-form Q&A,
        so injecting its output into the ``/query`` prompt as grounding
        context measurably reduces hallucination in the final structured
        description compared to relying on ``/query`` alone. Uses
        ``length="short"`` rather than ``"normal"`` — a short caption
        grounds the query with the notable subject and its immediate
        surroundings in one concise sentence instead of an exhaustive,
        multi-sentence inventory of everything in frame (background
        vehicles, foliage, utility poles, etc.), which was leaking into the
        final description and driving up completion tokens for little
        security value. Returns "" on any error so callers can skip the
        grounding hint.
        """
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload: dict[str, Any] = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "length": "short",
            "stream": False,
        }
        if self._finetune_model:
            payload["model"] = self._finetune_model
        headers = {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._BASE_URL}/caption",
                json=payload,
                headers=headers,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Moondream /caption returned HTTP %d", resp.status)
                    return ""
                data = await resp.json()
                caption = str(data.get("caption", ""))
                self._last_prompt_tokens += self._IMAGE_TOKENS_PER_FRAME
                self._last_completion_tokens += max(1, len(caption) // 4)
                return caption
        except asyncio.TimeoutError:
            _LOGGER.debug("Moondream /caption request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream /caption request failed: %s", exc)
            return ""

    async def _detect_protected_vehicle(
        self, frame: bytes, all_car_boxes: list[dict[str, float]]
    ) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        """Disambiguate the protected vehicle from other cars in *frame*.

        Moondream's ``/detect`` accepts free-text zero-shot object queries,
        so when more than one "car" box is present, the protected vehicle's
        own description (e.g. "silver Kia Forte") is used as a second,
        targeted detect query instead of treating every car box the same —
        this distinguishes the actual protected vehicle from a visitor's
        car, a passing vehicle, or a second car parked nearby. Skipped
        entirely when there's only 0-1 car boxes, since there's nothing to
        disambiguate. Falls back to treating all boxes as the protected
        vehicle (no "other" vehicles) if the description-specific detect
        finds nothing usable — an unusually worded description Moondream
        can't match should not manufacture a false "other vehicle" alert.

        Returns ``(protected_vehicle_boxes, other_vehicle_boxes)``.
        """
        all_car_boxes = self._dedupe_boxes(all_car_boxes)
        if len(all_car_boxes) <= 1 or not self._car_description:
            return all_car_boxes, []

        protected_boxes = await self._detect_objects(
            frame, self._visual_detect_query(self._car_description)
        )
        await asyncio.sleep(0.55)
        if not protected_boxes:
            return all_car_boxes, []

        other_boxes = self._other_vehicle_boxes(protected_boxes, all_car_boxes)
        return protected_boxes, other_boxes

    async def _call_api_frame(self, frame: bytes, prompt: str) -> str:
        """Send a single JPEG frame to the Moondream Cloud /query endpoint.

        Reasoning mode is always enabled — it adds 10-20 % latency but
        substantially improves multi-step spatial analysis (proximity
        estimates, evasive behaviour detection) with no extra cost.

        Token counts are not returned by the Moondream API; we accumulate
        estimates in ``_last_prompt_tokens`` / ``_last_completion_tokens``
        so the usage table shows approximate figures instead of N/A.
        """
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload: dict[str, Any] = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "question": prompt,
            "stream": False,
            "reasoning": True,
        }
        if self._finetune_model:
            payload["model"] = self._finetune_model
        headers = {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._BASE_URL}/query",
                json=payload,
                headers=headers,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    _LOGGER.warning("Moondream Cloud: rate limit hit")
                    return ""
                if resp.status == 401:
                    _LOGGER.error("Moondream Cloud: invalid API key (HTTP 401)")
                    return ""
                if resp.status != 200:
                    _LOGGER.warning("Moondream Cloud returned HTTP %d", resp.status)
                    return ""
                data = await resp.json()
                answer = str(data.get("answer", ""))
                # Accumulate estimated token counts across frames.
                # Prompt: image encoder tokens + text tokens (4 chars ≈ 1 token).
                self._last_prompt_tokens += self._IMAGE_TOKENS_PER_FRAME + max(
                    1, len(prompt) // 4
                )
                # Completion: output text tokens.
                self._last_completion_tokens += max(1, len(answer) // 4)
                return answer
        except asyncio.TimeoutError:
            _LOGGER.warning("Moondream Cloud request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Moondream Cloud request failed: %s", exc)
            return ""

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Analyse frames via Moondream Cloud with detect-augmented analysis.

        For each frame:
        1. Run ``/detect`` for "person". A frame with no person still gets
           checked for an animal or a vehicle before being written off —
           every camera's job is to describe what's actually happening, not
           just track people, and a car camera additionally needs to catch
           anyone or anything getting close to the protected vehicle.
        2. If nothing of interest was found, skip the expensive ``/caption``
           and ``/query`` calls and record a clear result for that frame.
        3. For car cameras: also run ``/detect`` for "car". When more than
           one car box is found, a second targeted detect using the
           protected vehicle's own description disambiguates it from any
           other vehicle in frame (see :meth:`_detect_protected_vehicle`),
           and precise bounding-box proximity data is injected into the
           query prompt so the model can make an evidence-based
           suspicious/clear decision. Non-car cameras instead run a plain
           "vehicle" detect — there's no specific vehicle to disambiguate,
           just an incidental one worth captioning accurately.
        4. Run ``/caption`` once on the frame and inject its factual scene
           description as grounding context — a dedicated captioning call
           tends to describe *what's actually there* more reliably than
           relying solely on the free-form ``/query`` answer, which reduces
           hallucination in the final structured description.
        5. Run ``/query`` (with reasoning=True) on the augmented prompt and
           pick the most alarming result across all frames.

        Respects the 2 req/s rate limit with a 0.55 s delay between requests.
        """
        if not frames:
            return ""

        camera = getattr(self, "_current_camera", "")
        car_applies = self._car_protection_applies(camera)

        best_response = ""
        best_is_suspicious = False
        best_confidence = 0.0

        for i, frame in enumerate(frames):
            is_last = i == len(frames) - 1

            # ── Phase 1: detect persons ──────────────────────────────────
            persons = await self._detect_objects(frame, "person")
            await asyncio.sleep(0.55)

            # ── Phase 1b: with no person, still check for an animal or a
            # vehicle before writing the frame off — otherwise a dog
            # sniffing at the car, another car pulling up tight beside it,
            # or a car simply driving past a non-car camera would be
            # silently reduced to a generic "no person detected" result
            # with no caption at all.
            animals: list[dict[str, float]] = []
            protected_boxes: list[dict[str, float]] = []
            other_vehicle_boxes: list[dict[str, float]] = []
            generic_vehicles: list[dict[str, float]] = []
            if not persons:
                animals = await self._detect_objects(frame, "animal")
                await asyncio.sleep(0.55)
                if car_applies:
                    all_car_boxes = await self._detect_objects(frame, "car")
                    await asyncio.sleep(0.55)
                    (
                        protected_boxes,
                        other_vehicle_boxes,
                    ) = await self._detect_protected_vehicle(frame, all_car_boxes)
                else:
                    generic_vehicles = await self._detect_objects(frame, "vehicle")
                    await asyncio.sleep(0.55)

            multiple_vehicles = car_applies and bool(other_vehicle_boxes)
            if (
                not persons
                and not animals
                and not multiple_vehicles
                and not generic_vehicles
            ):
                # Nothing of interest in this frame — motion likely caused by
                # something else. Record a clear result and move on without
                # spending query tokens.
                if not best_response:
                    best_response = self._no_subject_response()
                if not is_last:
                    await asyncio.sleep(0.55)
                continue

            # ── Phase 2: augment prompt with spatial context ─────────────
            augmented_prompt = prompt
            subjects = persons + animals

            if car_applies:
                if not protected_boxes and not other_vehicle_boxes:
                    all_car_boxes = await self._detect_objects(frame, "car")
                    await asyncio.sleep(0.55)
                    (
                        protected_boxes,
                        other_vehicle_boxes,
                    ) = await self._detect_protected_vehicle(frame, all_car_boxes)
                    multiple_vehicles = car_applies and bool(other_vehicle_boxes)

                if subjects and (protected_boxes or other_vehicle_boxes):
                    # Gap is measured against EVERY detected car box, not just
                    # the one disambiguation labelled "protected" — two
                    # independent zero-shot detect calls (generic "car" vs.
                    # the description-specific query) can each draw a
                    # slightly different box for the SAME physical vehicle,
                    # and a person leaning on/touching the car shifts that
                    # box enough to push its IoU with the earlier "protected"
                    # box below the match threshold. That misclassifies the
                    # real vehicle as "other" at the exact moment contact is
                    # happening — the one moment this hint must not miss.
                    # Identity of which box is "the protected one" only
                    # matters for the vehicle-vs-vehicle case below.
                    gap = self._bbox_min_gap(
                        subjects, protected_boxes + other_vehicle_boxes
                    )
                    augmented_prompt += (
                        f"\n\n{self._proximity_hint(gap, 'person or animal')}"
                    )
                elif multiple_vehicles:
                    gap = (
                        self._bbox_min_gap(protected_boxes, other_vehicle_boxes)
                        if protected_boxes
                        else self._bbox_min_pairwise_gap(other_vehicle_boxes)
                    )
                    augmented_prompt += f"\n\n{self._vehicle_proximity_hint(gap)}"
                elif subjects and self._car_zones.get(camera):
                    # Car detect found no car box at all this frame (e.g. the
                    # vehicle is partly out of view or detect simply missed
                    # it), but a fixed car zone is configured for this
                    # camera — use it as a fallback proximity reference so a
                    # person standing where the car normally is still gets
                    # flagged instead of silently falling through with no
                    # hint at all. The zone dict uses the same
                    # x_min/y_min/x_max/y_max keys as a detected box, so it
                    # can be passed to _bbox_min_gap directly.
                    gap = self._bbox_min_gap(subjects, [self._car_zones[camera]])
                    augmented_prompt += (
                        f"\n\n{self._proximity_hint(gap, 'person or animal')}"
                    )
                # else: car detect returned nothing and no zone is configured —
                # no proximity hint; the base prompt's vehicle-distance rules
                # still apply if the model can see the car in the frames.
                # Explicit suppression here caused missed alerts when detect
                # failed despite the car being visibly in frame.

            else:
                # Non-car camera: inject subject positions (person, animal,
                # incidental vehicle) to help the model describe the scene
                # accurately without borrowing protected-vehicle rules.
                labeled_subjects = (
                    [("Person", p) for p in persons]
                    + [("Animal", a) for a in animals]
                    + [("Vehicle", v) for v in generic_vehicles]
                )
                position_hint = self._position_hint(labeled_subjects)
                if position_hint:
                    augmented_prompt += f"\n\n{position_hint}"
                if generic_vehicles and not persons:
                    augmented_prompt += f"\n\n{self._vehicle_hint()}"

            # ── Phase 2b: caption grounding ───────────────────────────────
            caption = await self._caption_frame(frame)
            await asyncio.sleep(0.55)
            if caption:
                augmented_prompt += (
                    "\n\n[INTERNAL SCENE CAPTION — factual grounding only, do "
                    f"NOT copy this text verbatim into your description]: {caption}"
                )

            # ── Phase 3: full query with augmented prompt ────────────────
            resp = await self._call_api_frame(frame, augmented_prompt)
            if not resp:
                if not is_last:
                    await asyncio.sleep(0.55)
                continue

            if multiple_vehicles and not subjects:
                resp = self._force_not_suspicious(resp)

            susp, conf, desc = self._try_parse_json(resp)
            if not desc:
                if not best_response:
                    best_response = resp
            elif (
                not best_response
                or (susp and not best_is_suspicious)
                or (susp == best_is_suspicious and conf > best_confidence)
            ):
                best_response = resp
                best_is_suspicious = susp
                best_confidence = conf

            if not is_last:
                await asyncio.sleep(0.55)

        return best_response


# ---------------------------------------------------------------------------
# Moondream local provider (0.5B INT8, runs on-device)
# ---------------------------------------------------------------------------


class MoondreamLocalAnalyzer(_MoondreamDetectionMixin, BaseAnalyzer):
    """Runs the Moondream 0.5B INT8 model locally using the moondream package.

    The model (~430 MB) is downloaded from Moondream's servers on the first
    run and cached in the default moondream cache directory.  Subsequent
    starts reuse the cached file.  Inference runs in a thread executor so the
    asyncio event loop is never blocked.

    Mirrors :class:`MoondreamCloudAnalyzer`'s detect-augmented pipeline
    (person/animal/vehicle detection feeding proximity hints, plus a
    caption-grounded query) using the local package's ``detect``/``caption``/
    ``query`` methods instead of HTTP calls — see
    :meth:`_analyze_frame_sync` for the per-frame pipeline.
    """

    _MODEL_ID = "moondream-0_5b-int8"

    def __init__(
        self,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )
        self._md_model: Any = None
        self._model_lock: asyncio.Lock | None = None
        self._model_ready = False

    def _get_lock(self) -> asyncio.Lock:
        if self._model_lock is None:
            self._model_lock = asyncio.Lock()
        return self._model_lock

    @property
    def provider_name(self) -> str:
        return "moondream_local"

    def model_name(self) -> str:
        return self._MODEL_ID

    async def close(self) -> None:
        self._md_model = None
        self._model_ready = False

    def _load_model_sync(self) -> None:
        """Load the Moondream model for on-device inference (blocking — run in executor).

        ``local=True`` is passed explicitly and is load-bearing: the
        ``moondream`` package silently falls back to Moondream *Cloud* when
        ``local`` is omitted, which would send every camera frame off-device
        to api.moondream.ai — with no API key configured, unauthenticated —
        while this provider is meant to be fully on-device. On-device
        inference requires a CUDA or Apple Silicon GPU (via the package's
        "Photon" engine); hosts without one raise cleanly here instead of
        silently degrading into a cloud leak, and ``_ensure_model`` reports
        the provider as unavailable rather than a false "ready".
        """
        import moondream as md  # noqa: PLC0415  # type: ignore[import-not-found]

        _LOGGER.info("Loading Moondream local model '%s'", self._MODEL_ID)
        self._md_model = md.vl(model=self._MODEL_ID, local=True)
        self._model_ready = True
        _LOGGER.info("Moondream local model ready")

    async def _ensure_model(self) -> bool:
        """Ensure the model is loaded. Returns True when ready."""
        if self._model_ready:
            return True
        lock = self._get_lock()
        async with lock:
            if self._model_ready:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_model_sync)
                return True
            except ImportError as exc:
                _LOGGER.exception(
                    "moondream package (or a local-inference dependency it "
                    "requires, such as kestrel or torch) is not installed: %s. "
                    "Install it with: pip install moondream",
                    exc,
                )
                return False
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception(
                    "Failed to load Moondream local model: %s. On-device "
                    "Moondream inference requires a CUDA or Apple Silicon GPU — "
                    "if this host doesn't have one, use moondream_cloud or "
                    "ollama instead.",
                    exc,
                )
                return False

    async def health_check(self) -> bool:
        """Return True once the model is loaded (triggers download on first call)."""
        return await self._ensure_model()

    async def fetch_models(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self._MODEL_ID,
                "description": "Moondream 0.5B INT8 (local)",
            }
        ]

    def _local_detect(self, encoded: Any, object_name: str) -> list[dict[str, float]]:
        """Call the local model's ``detect`` and filter to well-formed boxes.

        Mirrors :meth:`MoondreamCloudAnalyzer._detect_objects` for behavioural
        parity between the two providers. Returns ``[]`` on any error so
        callers can safely skip detect-based logic when it fails.
        """
        try:
            objects = self._md_model.detect(encoded, object_name).get("objects", [])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Moondream local detect failed for %r: %s", object_name, exc)
            return []
        return [o for o in objects if isinstance(o, dict)]

    def _local_caption(self, encoded: Any) -> str:
        """Call the local model's ``caption`` for factual grounding context.

        Mirrors :meth:`MoondreamCloudAnalyzer._caption_frame`, including the
        ``length="short"`` choice — see that method's docstring for why.
        Returns "" on any error so callers can skip the grounding hint.
        """
        try:
            return str(
                self._md_model.caption(encoded, length="short").get("caption", "")
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Moondream local caption failed: %s", exc)
            return ""

    def _detect_protected_vehicle_sync(
        self, encoded: Any, all_car_boxes: list[dict[str, float]]
    ) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        """Local-inference counterpart to
        :meth:`MoondreamCloudAnalyzer._detect_protected_vehicle` —
        disambiguates the protected vehicle from other cars in frame using
        its own description, but only when more than one car box is
        present (see that method for the full rationale). Returns
        ``(protected_vehicle_boxes, other_vehicle_boxes)``.
        """
        all_car_boxes = self._dedupe_boxes(all_car_boxes)
        if len(all_car_boxes) <= 1 or not self._car_description:
            return all_car_boxes, []

        protected_boxes = self._local_detect(
            encoded, self._visual_detect_query(self._car_description)
        )
        if not protected_boxes:
            return all_car_boxes, []

        other_boxes = self._other_vehicle_boxes(protected_boxes, all_car_boxes)
        return protected_boxes, other_boxes

    def _analyze_frame_sync(
        self, frame_bytes: bytes, prompt: str, car_applies: bool
    ) -> str:
        """Run the full detect + caption + query pipeline for one frame.

        Runs entirely inside the thread executor since every Moondream local
        call is blocking. The image is encoded once via ``encode_image`` and
        the encoded result is reused across every detect/caption/query call
        on this frame — encoding is the expensive vision-tower pass, so
        reusing it avoids repeating that work up to five times per frame.
        Mirrors :meth:`MoondreamCloudAnalyzer._call_model`'s per-frame
        pipeline; see that method's docstring for the phase-by-phase
        rationale.
        """
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        image = Image.open(io.BytesIO(frame_bytes))
        encoded = self._md_model.encode_image(image)

        persons = self._local_detect(encoded, "person")

        animals: list[dict[str, float]] = []
        protected_boxes: list[dict[str, float]] = []
        other_vehicle_boxes: list[dict[str, float]] = []
        generic_vehicles: list[dict[str, float]] = []
        if not persons:
            animals = self._local_detect(encoded, "animal")
            if car_applies:
                all_car_boxes = self._local_detect(encoded, "car")
                protected_boxes, other_vehicle_boxes = (
                    self._detect_protected_vehicle_sync(encoded, all_car_boxes)
                )
            else:
                generic_vehicles = self._local_detect(encoded, "vehicle")

        multiple_vehicles = car_applies and bool(other_vehicle_boxes)
        if (
            not persons
            and not animals
            and not multiple_vehicles
            and not generic_vehicles
        ):
            return self._no_subject_response()

        augmented_prompt = prompt
        subjects = persons + animals

        if car_applies:
            if not protected_boxes and not other_vehicle_boxes:
                all_car_boxes = self._local_detect(encoded, "car")
                protected_boxes, other_vehicle_boxes = (
                    self._detect_protected_vehicle_sync(encoded, all_car_boxes)
                )
                multiple_vehicles = car_applies and bool(other_vehicle_boxes)

            if subjects and (protected_boxes or other_vehicle_boxes):
                # See MoondreamCloudAnalyzer._call_model's matching comment:
                # gap uses every detected car box, not just the one
                # disambiguation labelled "protected", so a person touching
                # the vehicle is never missed just because the two
                # independent detect calls' boxes disagree at the exact
                # moment of contact.
                gap = self._bbox_min_gap(
                    subjects, protected_boxes + other_vehicle_boxes
                )
                augmented_prompt += (
                    f"\n\n{self._proximity_hint(gap, 'person or animal')}"
                )
            elif multiple_vehicles:
                gap = (
                    self._bbox_min_gap(protected_boxes, other_vehicle_boxes)
                    if protected_boxes
                    else self._bbox_min_pairwise_gap(other_vehicle_boxes)
                )
                augmented_prompt += f"\n\n{self._vehicle_proximity_hint(gap)}"
            elif subjects and self._car_zones.get(getattr(self, "_current_camera", "")):
                # See MoondreamCloudAnalyzer._call_model's matching comment:
                # fall back to the fixed car zone when detect found no car
                # box at all this frame, so a person standing where the car
                # normally is still gets flagged.
                zone = self._car_zones[getattr(self, "_current_camera", "")]
                gap = self._bbox_min_gap(subjects, [zone])
                augmented_prompt += (
                    f"\n\n{self._proximity_hint(gap, 'person or animal')}"
                )
            # else: car detect returned nothing and no zone is configured — no
            # proximity hint; the base prompt's vehicle-distance rules still
            # apply if the model can see the car in the frames.
        else:
            labeled_subjects = (
                [("Person", p) for p in persons]
                + [("Animal", a) for a in animals]
                + [("Vehicle", v) for v in generic_vehicles]
            )
            position_hint = self._position_hint(labeled_subjects)
            if position_hint:
                augmented_prompt += f"\n\n{position_hint}"
            if generic_vehicles and not persons:
                augmented_prompt += f"\n\n{self._vehicle_hint()}"

        caption = self._local_caption(encoded)
        if caption:
            augmented_prompt += (
                "\n\n[INTERNAL SCENE CAPTION — factual grounding only, do NOT "
                f"copy this text verbatim into your description]: {caption}"
            )

        # No reasoning=True here (unlike MoondreamCloudAnalyzer._call_api_frame):
        # the local `moondream` package's query() signature is
        # query(image, question, stream=False) — it has no reasoning parameter.
        # Reasoning mode is a Moondream Cloud-only capability, not an oversight.
        result = self._md_model.query(encoded, augmented_prompt)
        answer = str(result.get("answer", ""))
        if multiple_vehicles and not subjects:
            answer = self._force_not_suspicious(answer)
        return answer

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Run the local model on all extracted frames and return the most alarming result."""
        if not await self._ensure_model():
            return ""
        if not frames:
            return ""

        camera = getattr(self, "_current_camera", "")
        car_applies = self._car_protection_applies(camera)

        best_response = ""
        best_is_suspicious = False
        best_confidence = 0.0

        for frame in frames:
            try:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None, self._analyze_frame_sync, frame, prompt, car_applies
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Moondream local inference failed: %s", exc)
                continue

            if not resp:
                continue

            susp, conf, desc = self._try_parse_json(resp)
            if not desc:
                if not best_response:
                    best_response = resp
            elif (
                not best_response
                or (susp and not best_is_suspicious)
                or (susp == best_is_suspicious and conf > best_confidence)
            ):
                best_response = resp
                best_is_suspicious = susp
                best_confidence = conf

        return best_response


# ---------------------------------------------------------------------------
# Moondream fine-tune manager
# ---------------------------------------------------------------------------


class MoondreamFineTuneManager:
    """HTTP API wrapper for Moondream Cloud fine-tuning operations.

    Fine-tunes train entirely in Moondream Cloud — no local GPU required.
    After training, call :meth:`get_model_id` and pass the result as
    ``finetune_model`` to :class:`MoondreamCloudAnalyzer` to run inference
    with the fine-tuned model.

    Supports both RL (Reinforcement Learning) and SFT (Supervised Fine-tuning)
    training modes across three skills: ``query``, ``point``, and ``detect``.
    """

    _TUNING_BASE_URL = "https://api.moondream.ai/v1/tuning"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Finetune management
    # ------------------------------------------------------------------

    async def create_finetune(self, name: str, rank: int = 16) -> str | None:
        """Create a new fine-tune and return its finetune_id.

        Args:
            name: Unique identifier (alphanumeric, dots, hyphens, underscores).
            rank: LoRA rank — 8, 16, 24, or 32.  Higher = more capacity but
                  longer training time.

        Returns:
            ``finetune_id`` string on success, ``None`` on error.
        """
        if rank not in (8, 16, 24, 32):
            _LOGGER.error("Moondream create_finetune: rank must be 8, 16, 24, or 32")
            return None
        payload: dict[str, Any] = {"name": name, "rank": rank}
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/finetunes",
                json=payload,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Moondream create_finetune returned HTTP %d", resp.status
                    )
                    return None
                data = await resp.json()
                fid = str(data.get("finetune_id", ""))
                return fid or None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.exception("Moondream create_finetune failed: %s", exc)
            return None

    async def list_finetunes(
        self, limit: int = 20, cursor: str = ""
    ) -> list[dict[str, Any]]:
        """List all fine-tunes for the current API key."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._TUNING_BASE_URL}/finetunes",
                params=params,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return list(data.get("finetunes", []))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream list_finetunes failed: %s", exc)
            return []

    async def get_finetune(self, finetune_id: str) -> dict[str, Any] | None:
        """Return details for a specific fine-tune, or ``None`` if not found."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status in (404, 400):
                    return None
                if resp.status != 200:
                    return None
                return dict(await resp.json())
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream get_finetune failed: %s", exc)
            return None

    async def delete_finetune(self, finetune_id: str) -> bool:
        """Soft-delete a fine-tune and all its checkpoints."""
        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream delete_finetune failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    async def generate_rollouts(
        self,
        finetune_id: str,
        image: bytes,
        question: str,
        num_rollouts: int = 4,
        ground_truth: str | None = None,
        skill: str = "query",
    ) -> dict[str, Any]:
        """Generate multiple model outputs for a single request.

        Args:
            finetune_id: ID of the fine-tune to generate rollouts for.
            image: JPEG frame bytes.
            question: Question to ask the model (skill='query') or object name
                      to locate (skill='point'/'detect').
            num_rollouts: Number of outputs to generate (1–16).
            ground_truth: Expected answer for automatic reward computation.
                          Supported for 'query', 'point', and 'detect' skills.
            skill: One of ``'query'``, ``'point'``, or ``'detect'``.

        Returns:
            Dict with ``'rollouts'`` list and optional ``'rewards'`` list when
            ``ground_truth`` was provided.  Empty dict on error.
        """
        image_b64 = base64.b64encode(image).decode("ascii")
        request: dict[str, Any] = {
            "skill": skill,
            "image_url": f"data:image/jpeg;base64,{image_b64}",
        }
        if skill == "query":
            request["question"] = question
        else:
            request["object"] = question

        payload: dict[str, Any] = {
            "finetune_id": finetune_id,
            "num_rollouts": min(max(1, num_rollouts), 16),
            "request": request,
        }
        if ground_truth is not None:
            payload["ground_truth"] = ground_truth

        try:
            session = await self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/rollouts",
                json=payload,
                headers=self._headers(),
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Moondream generate_rollouts returned HTTP %d", resp.status
                    )
                    return {}
                return dict(await resp.json())
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.warning("Moondream generate_rollouts failed: %s", exc)
            return {}

    async def train_step(
        self,
        finetune_id: str,
        request: dict[str, Any],
        rollouts: list[str],
        rewards: list[float] | None = None,
        mode: str = "rl",
        learning_rate: float = 2e-4,
    ) -> dict[str, Any]:
        """Execute one RL or SFT training step.

        Args:
            finetune_id: Target fine-tune ID.
            request: The rollout request dict (same structure used in
                     :meth:`generate_rollouts`).
            rollouts: Model outputs from :meth:`generate_rollouts`.
            rewards: Score per rollout (0.0–1.0) for RL mode.  Not used in
                     SFT mode (first rollout is treated as the target).
            mode: ``'rl'`` for reinforcement learning (requires ``rewards``),
                  ``'sft'`` for supervised fine-tuning.
            learning_rate: Optimizer learning rate (default 2e-4).

        Returns:
            Dict with training metrics such as ``kl_divergence`` and
            ``gradient_norm``.  Empty dict on error.
        """
        group: dict[str, Any] = {
            "mode": mode,
            "request": request,
            "rollouts": rollouts,
        }
        if mode == "rl":
            group["rewards"] = rewards or []
        elif mode == "sft" and rollouts:
            group["target"] = rollouts[0]

        payload: dict[str, Any] = {
            "finetune_id": finetune_id,
            "groups": [group],
            "learning_rate": learning_rate,
        }
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/train_step",
                json=payload,
                headers=self._headers(),
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Moondream train_step returned HTTP %d", resp.status
                    )
                    return {}
                return dict(await resp.json())
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.warning("Moondream train_step failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    async def save_checkpoint(self, finetune_id: str) -> bool:
        """Persist the current model state as a named checkpoint."""
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/checkpoints/save",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream save_checkpoint failed: %s", exc)
            return False

    async def list_checkpoints(
        self, finetune_id: str, limit: int = 20, cursor: str = ""
    ) -> list[dict[str, Any]]:
        """List saved checkpoints for a fine-tune."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/checkpoints",
                params=params,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return list(data.get("checkpoints", []))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream list_checkpoints failed: %s", exc)
            return []

    async def delete_checkpoint(self, finetune_id: str, step: int) -> bool:
        """Delete a specific checkpoint by training step number."""
        try:
            session = await self._get_session()
            async with session.delete(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/checkpoints/{step}",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream delete_checkpoint failed: %s", exc)
            return False

    async def log_metrics(
        self, finetune_id: str, step: int, metrics: dict[str, float]
    ) -> bool:
        """Record custom evaluation metrics for a given training step."""
        payload: dict[str, Any] = {"step": step, "metrics": metrics}
        try:
            session = await self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/metrics",
                json=payload,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream log_metrics failed: %s", exc)
            return False

    @staticmethod
    def get_model_id(finetune_id: str, step: int) -> str:
        """Return the inference model identifier for a saved checkpoint.

        Pass the returned string as ``finetune_model`` to
        :class:`MoondreamCloudAnalyzer` to run inference with your fine-tuned
        model instead of the base ``moondream3-preview``.

        Example::

            model_id = MoondreamFineTuneManager.get_model_id("abc123", 50)
            # → "moondream3-preview/abc123@50"
        """
        return f"moondream3-preview/{finetune_id}@{step}"


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------


class AnthropicAnalyzer(BaseAnalyzer):
    """Analyzes clips via the Anthropic Claude API (claude.ai).

    Sends JPEG frames as base64 image content to a Claude vision model and
    extracts token usage for cost tracking.  Authentication errors are logged
    clearly so the user knows to check ``anthropic_api_key`` in the add-on
    settings.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )
        self._api_key = api_key
        self._model = model or "claude-haiku-4-5"
        self._client: Any = None

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def model_name(self) -> str:
        return self._model

    def model_pricing(self) -> tuple[float, float]:
        """Return (input_price, output_price) per 1M tokens for the current model."""
        return lookup_model_pricing(self._model) or (
            3.00,
            15.00,
        )  # Sonnet-level fallback for unknown models

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic as _anthropic  # noqa: PLC0415

            self._client = _anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def health_check(self) -> bool:
        """Return True when the API key is valid and the Anthropic API is reachable.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits Anthropic's
        real API — see the cache helpers on :class:`BaseAnalyzer` for why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("Anthropic: no API key configured")
            return self._store_health_check_result(False)
        try:
            import anthropic as _anthropic  # noqa: PLC0415
        except ImportError:
            _LOGGER.error(
                "anthropic package is not installed. "
                "Install it with: pip install anthropic"
            )
            return self._store_health_check_result(False)
        try:
            client = self._get_client()
            await client.models.list(limit=1)
            return self._store_health_check_result(True)
        except _anthropic.AuthenticationError:
            _LOGGER.error(
                "Anthropic: invalid API key (AuthenticationError) — "
                "check your anthropic_api_key in the add-on settings"
            )
            return self._store_health_check_result(False)
        except _anthropic.PermissionDeniedError:
            _LOGGER.error(
                "Anthropic: API key does not have permission to access this resource"
            )
            return self._store_health_check_result(False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Anthropic health check failed: %s", exc)
            return self._store_health_check_result(False)

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Fetch available models from the Anthropic API; falls back to a hardcoded list."""
        if self._api_key:
            try:
                import anthropic as _anthropic  # noqa: PLC0415
            except ImportError:
                pass
            else:
                try:
                    client = self._get_client()
                    page = await client.models.list()
                    result = []
                    for m in page.data:
                        # Prefix match (not exact dict lookup) in case the API
                        # returns a dated snapshot id rather than the bare alias.
                        inp, out = lookup_model_pricing(m.id) or (3.00, 15.00)
                        display = getattr(m, "display_name", m.id)
                        result.append(
                            {
                                "name": m.id,
                                "id": m.id,
                                "display_name": display,
                                "description": f"{display} (${inp:.0f}/${out:.0f} per 1M tokens)",
                            }
                        )
                    return result
                except _anthropic.AuthenticationError:
                    _LOGGER.error(
                        "Anthropic: invalid API key — "
                        "check your anthropic_api_key in the add-on settings"
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Failed to fetch Anthropic models from API: %s", exc)

        return [
            {
                "name": m["name"],
                "id": m["name"],
                "display_name": m["display_name"],
                "description": m["display_name"],
            }
            for m in _ANTHROPIC_FALLBACK_MODELS
        ]

    @staticmethod
    def _resize_frame(frame_bytes: bytes, max_dimension: int = 1568) -> bytes:
        """Resize a JPEG frame so its longest side is at most max_dimension pixels.

        Anthropic resizes images server-side to 1568px anyway; doing it client-side
        reduces upload bandwidth for high-resolution security cameras.
        Returns the original bytes unchanged if the image cannot be opened.
        """
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        try:
            img = Image.open(io.BytesIO(frame_bytes))
            if max(img.width, img.height) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
        except Exception:  # noqa: BLE001
            pass
        return frame_bytes

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Send frames to Claude via the Anthropic Messages API."""
        if not frames:
            return ""

        try:
            import anthropic as _anthropic  # noqa: PLC0415
        except ImportError:
            _LOGGER.error(
                "anthropic package is not installed. "
                "Install it with: pip install anthropic"
            )
            return ""

        try:
            client = self._get_client()

            resized = [self._resize_frame(f) for f in frames]
            content: list[dict[str, Any]] = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(frame).decode("ascii"),
                    },
                }
                for frame in resized
            ]
            content.append({"type": "text", "text": prompt})

            # System prompt keeps the role and output format instructions
            # separate from user content, improving JSON compliance and
            # preventing the model from leaking internal analysis terms.
            system_prompt = _VISION_SYSTEM_PROMPT

            response = await client.messages.create(
                model=self._model,
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )

            if response.usage:
                self._last_prompt_tokens = int(response.usage.input_tokens or 0)
                self._last_completion_tokens = int(response.usage.output_tokens or 0)

            return "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )

        except _anthropic.AuthenticationError:
            _LOGGER.error(
                "Anthropic: invalid API key (AuthenticationError) — "
                "check your anthropic_api_key in the add-on settings"
            )
            return ""
        except _anthropic.PermissionDeniedError:
            _LOGGER.error(
                "Anthropic: permission denied — "
                "check that your API key has access to model '%s'",
                self._model,
            )
            return ""
        except _anthropic.RateLimitError:
            _LOGGER.warning(
                "Anthropic: rate limit hit — API quota exceeded; "
                "analysis will resume on the next cycle"
            )
            return ""
        except _anthropic.BadRequestError as exc:
            _LOGGER.exception(
                "Anthropic: bad request (HTTP 400) — %s; "
                "check that the selected model supports vision",
                exc.message,
            )
            return ""
        except _anthropic.APIStatusError as exc:
            _LOGGER.exception(
                "Anthropic API error HTTP %d: %s", exc.status_code, exc.message
            )
            return ""
        except _anthropic.APIConnectionError as exc:
            _LOGGER.warning("Anthropic: connection error — %s", exc)
            return ""
        except asyncio.TimeoutError:
            _LOGGER.warning("Anthropic request timed out")
            return ""
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Anthropic request failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------


class OpenAIAnalyzer(BaseAnalyzer):
    """Analyzes clips via the OpenAI Chat Completions API (platform.openai.com).

    Sends JPEG frames as base64 image_url content to a GPT-4o / GPT-4.1 / GPT-5
    model and extracts token usage for cost tracking.  Authentication errors are
    logged clearly so the user knows to check ``openai_api_key`` in the add-on
    settings.

    Two-tier escalation (any provider re-checking a suspicious verdict from
    this one) is handled generically by ``BaseAnalyzer`` — see
    ``create_analyzer()``'s ``escalation_provider``/``escalation_model`` and
    ``BaseAnalyzer._maybe_escalate``. This class only implements the tier-1
    (or tier-2) call itself.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, float]] | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )
        self._api_key = api_key
        self._model = model or "gpt-4o-mini"
        self._client: Any = None

    @property
    def provider_name(self) -> str:
        return "openai"

    def model_name(self) -> str:
        return self._model

    def model_pricing(self) -> tuple[float, float]:
        """Return (input_price, output_price) per 1M tokens for the current model."""
        return lookup_model_pricing(self._model) or (
            2.50,
            10.00,
        )  # gpt-4o level fallback for unknown models

    def _get_client(self) -> Any:
        if self._client is None:
            import openai as _openai  # noqa: PLC0415  # type: ignore[import-not-found]

            self._client = _openai.AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def health_check(self) -> bool:
        """Return True when the API key is valid and the OpenAI API is reachable.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits OpenAI's real
        API — see the cache helpers on :class:`BaseAnalyzer` for why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("OpenAI: no API key configured")
            return self._store_health_check_result(False)
        try:
            import openai as _openai  # noqa: PLC0415  # type: ignore[import-not-found]
        except ImportError:
            _LOGGER.error(
                "openai package is not installed. Install it with: pip install openai"
            )
            return self._store_health_check_result(False)
        try:
            client = self._get_client()
            await client.models.list()
            return self._store_health_check_result(True)
        except _openai.AuthenticationError:
            _LOGGER.error(
                "OpenAI: invalid API key (AuthenticationError) — "
                "check your openai_api_key in the add-on settings"
            )
            return self._store_health_check_result(False)
        except _openai.PermissionDeniedError:
            _LOGGER.error(
                "OpenAI: API key does not have permission to access this resource"
            )
            return self._store_health_check_result(False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("OpenAI health check failed: %s", exc)
            return self._store_health_check_result(False)

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Fetch vision-capable models from the OpenAI API; falls back to a hardcoded list."""
        if self._api_key:
            try:
                import openai as _openai  # noqa: PLC0415  # type: ignore[import-not-found]
            except ImportError:
                pass
            else:
                try:
                    client = self._get_client()
                    pages = await client.models.list()
                    result = []
                    for m in pages.data:
                        if not is_openai_vision_model(m.id):
                            continue
                        # Prefix match (not exact dict lookup) since the API
                        # often returns dated snapshot ids like
                        # "gpt-4o-mini-2024-07-18" rather than the bare alias.
                        inp, out = lookup_model_pricing(m.id) or (2.50, 10.00)
                        # Use a friendly pricing suffix only for known models
                        suffix = f" (${inp:.2f}/${out:.2f} per 1M tokens)"
                        result.append(
                            {
                                "name": m.id,
                                "id": m.id,
                                "display_name": m.id + suffix,
                                "description": m.id + suffix,
                            }
                        )
                    if result:
                        return sorted(result, key=lambda m: m["name"])
                except _openai.AuthenticationError:
                    _LOGGER.error(
                        "OpenAI: invalid API key — "
                        "check your openai_api_key in the add-on settings"
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Failed to fetch OpenAI models from API: %s", exc)

        return [
            {
                "name": m["name"],
                "id": m["name"],
                "display_name": m["display_name"],
                "description": m["display_name"],
            }
            for m in _OPENAI_FALLBACK_MODELS
        ]

    @staticmethod
    def _resize_frame(frame_bytes: bytes, max_dimension: int = 2048) -> bytes:
        """Resize a JPEG frame so its longest side is at most max_dimension pixels.

        OpenAI resizes images server-side to 2048px; doing it client-side reduces
        upload bandwidth for high-resolution security cameras.
        Returns the original bytes unchanged if the image cannot be opened.
        """
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        try:
            img = Image.open(io.BytesIO(frame_bytes))
            if max(img.width, img.height) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
        except Exception:  # noqa: BLE001
            pass
        return frame_bytes

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        return await self._call_openai_model(frames, prompt, self._model)

    async def _call_openai_model(
        self, frames: list[bytes], prompt: str, model: str
    ) -> str:
        """Send frames to a specific OpenAI Chat Completions model."""
        if not frames:
            return ""

        try:
            import openai as _openai  # noqa: PLC0415  # type: ignore[import-not-found]
        except ImportError:
            _LOGGER.error(
                "openai package is not installed. Install it with: pip install openai"
            )
            return ""

        try:
            client = self._get_client()

            resized = [self._resize_frame(f) for f in frames]
            content: list[dict[str, Any]] = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64.b64encode(frame).decode('ascii')}",
                        "detail": "high",
                    },
                }
                for frame in resized
            ]
            content.append({"type": "text", "text": prompt})

            # System message keeps role and format rules separate from user
            # content, improving JSON compliance and stopping the model from
            # leaking internal analysis terms into the description field.
            system_content = _VISION_SYSTEM_PROMPT
            messages_to_send: list[dict[str, Any]] = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": content},
            ]

            model_lower = model.lower()
            is_reasoning_model = model_lower.startswith(("o1", "o3", "o4", "gpt-5"))
            supports_structured_outputs = any(
                prefix in model_lower
                for prefix in ("gpt-4o", "gpt-4.1", "gpt-5", "o4-mini")
            )
            create_kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages_to_send,
            }
            # The o1/o3/o4-mini reasoning models and the gpt-5 family reject the
            # legacy `max_tokens` param (HTTP 400 "unsupported_parameter") and
            # require `max_completion_tokens` instead. Their invisible reasoning
            # tokens are billed from the same budget as the visible completion,
            # so give them extra headroom over the 512 used for non-reasoning
            # models to avoid a truncated/empty response on a harder clip.
            if is_reasoning_model:
                create_kwargs["max_completion_tokens"] = 1024
                # This is a short, well-defined classification task (suspicious
                # yes/no + one-sentence description), not multi-step reasoning,
                # so the lowest effort setting keeps latency/cost down without
                # sacrificing verdict quality. "-pro" tier reasoning models
                # (e.g. gpt-5.2-pro) reject anything but "high", so they're
                # excluded from this optimization.
                if not model_lower.endswith("-pro"):
                    create_kwargs["reasoning_effort"] = "low"
            else:
                create_kwargs["max_tokens"] = 512
            if supports_structured_outputs:
                create_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
                }
            elif "gpt-4-turbo" in model_lower:
                create_kwargs["response_format"] = {"type": "json_object"}

            response = await client.chat.completions.create(**create_kwargs)

            if response.usage:
                self._last_prompt_tokens = int(response.usage.prompt_tokens or 0)
                self._last_completion_tokens = int(
                    response.usage.completion_tokens or 0
                )

            choice = response.choices[0] if response.choices else None
            if choice and choice.message and choice.message.content:
                return str(choice.message.content)
            return ""

        except _openai.AuthenticationError:
            _LOGGER.error(
                "OpenAI: invalid API key (AuthenticationError) — "
                "check your openai_api_key in the add-on settings"
            )
            return ""
        except _openai.PermissionDeniedError:
            _LOGGER.error(
                "OpenAI: permission denied — "
                "check that your API key has access to model '%s'",
                model,
            )
            return ""
        except _openai.RateLimitError:
            _LOGGER.warning(
                "OpenAI: rate limit hit — API quota exceeded; "
                "analysis will resume on the next cycle"
            )
            return ""
        except _openai.BadRequestError as exc:
            _LOGGER.exception(
                "OpenAI: bad request (HTTP 400) — %s; "
                "check that the selected model supports vision",
                exc.message,
            )
            return ""
        except _openai.APIStatusError as exc:
            _LOGGER.exception(
                "OpenAI API error HTTP %d: %s", exc.status_code, exc.message
            )
            return ""
        except _openai.APIConnectionError as exc:
            _LOGGER.warning("OpenAI: connection error — %s", exc)
            return ""
        except asyncio.TimeoutError:
            _LOGGER.warning("OpenAI request timed out")
            return ""
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("OpenAI request failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


# Which kwarg of _build_single_analyzer selects the "model" for a given
# provider, used by create_analyzer() to override just that one field when
# building a tier-2 escalation analyzer of a possibly-different provider.
# moondream_local has no selectable model (fixed 0.5B model) so it has no
# entry — an escalation_model set alongside escalation_provider="moondream_local"
# is simply ignored.
_ESCALATION_MODEL_KWARG: dict[str, str] = {
    "ollama": "ollama_model",
    "ollama_cloud": "ollama_model",
    "moondream_cloud": "moondream_finetune_model",
    "anthropic": "anthropic_model",
    "openai": "openai_model",
}


def _build_single_analyzer(
    ai_provider: str,
    prompt: str,
    car_description: str = "",
    max_frames: int = 3,
    frame_interval: float = 2.0,
    suspicious_keywords: list[str] | None = None,
    camera_prompts: dict[str, str] | None = None,
    camera_descriptions: dict[str, str] | None = None,
    frame_strategy: str = "smart",
    car_cameras: list[str] | None = None,
    car_zones: dict[str, dict[str, float]] | None = None,
    *,
    ollama_url: str = "",
    ollama_model: str = "",
    ollama_cloud_api_key: str = "",
    moondream_api_key: str = "",
    moondream_finetune_model: str = "",
    anthropic_api_key: str = "",
    anthropic_model: str = "",
    openai_api_key: str = "",
    openai_model: str = "",
) -> BaseAnalyzer | None:
    """Build a single analyzer for *ai_provider*, with no escalation attached.

    Shared by :func:`create_analyzer` to build both the tier-1 analyzer and,
    when cross-provider escalation is configured, the tier-2 analyzer — see
    that function's ``escalation_provider``/``escalation_model``.
    """
    if ai_provider == "ollama":
        if not ollama_url:
            _LOGGER.warning(
                "ai_provider='ollama' requires ollama_url to be set; "
                "AI analysis disabled"
            )
            return None
        return ClipAnalyzer(
            ollama_url=ollama_url,
            model=ollama_model,
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )

    if ai_provider == "ollama_cloud":
        if not ollama_cloud_api_key:
            _LOGGER.warning(
                "ai_provider='ollama_cloud' requires ollama_cloud_api_key to be set; "
                "AI analysis disabled"
            )
            return None
        return OllamaCloudAnalyzer(
            api_key=ollama_cloud_api_key,
            model=ollama_model,
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )

    if ai_provider == "moondream_cloud":
        if not moondream_api_key:
            _LOGGER.warning(
                "ai_provider='moondream_cloud' requires moondream_api_key to be set; "
                "AI analysis disabled"
            )
            return None
        return MoondreamCloudAnalyzer(
            api_key=moondream_api_key,
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
            finetune_model=moondream_finetune_model,
        )

    if ai_provider == "moondream_local":
        return MoondreamLocalAnalyzer(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )

    if ai_provider == "anthropic":
        if not anthropic_api_key:
            _LOGGER.warning(
                "ai_provider='anthropic' requires anthropic_api_key to be set; "
                "AI analysis disabled"
            )
            return None
        return AnthropicAnalyzer(
            api_key=anthropic_api_key,
            model=anthropic_model or "claude-haiku-4-5",
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )

    if ai_provider == "openai":
        if not openai_api_key:
            _LOGGER.warning(
                "ai_provider='openai' requires openai_api_key to be set; "
                "AI analysis disabled"
            )
            return None
        return OpenAIAnalyzer(
            api_key=openai_api_key,
            model=openai_model or "gpt-4o-mini",
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
        )

    _LOGGER.warning(
        "Unknown ai_provider %r; expected 'ollama', 'ollama_cloud', "
        "'moondream_cloud', 'moondream_local', 'anthropic', or 'openai'. "
        "AI analysis disabled.",
        ai_provider,
    )
    return None


def create_analyzer(
    ai_provider: str,
    prompt: str,
    car_description: str = "",
    max_frames: int = 3,
    frame_interval: float = 2.0,
    suspicious_keywords: list[str] | None = None,
    camera_prompts: dict[str, str] | None = None,
    camera_descriptions: dict[str, str] | None = None,
    frame_strategy: str = "smart",
    car_cameras: list[str] | None = None,
    car_zones: dict[str, dict[str, float]] | None = None,
    *,
    ollama_url: str = "",
    ollama_model: str = "",
    ollama_cloud_api_key: str = "",
    moondream_api_key: str = "",
    moondream_finetune_model: str = "",
    anthropic_api_key: str = "",
    anthropic_model: str = "",
    openai_api_key: str = "",
    openai_model: str = "",
    escalation_provider: str = "",
    escalation_model: str = "",
    store_prompt_debug: bool = False,
) -> BaseAnalyzer | None:
    """Return an analyzer for *ai_provider*, or ``None`` if configuration is invalid.

    When *escalation_provider* is set, a second ("tier 2") analyzer is built
    for that provider — which may be entirely different from *ai_provider*,
    e.g. tier 1 = openai/gpt-5-mini, tier 2 = moondream_cloud/moondream3 —
    and attached via :meth:`BaseAnalyzer.set_escalation_analyzer` so every
    suspicious tier-1 verdict is re-checked by tier 2 before being trusted
    (see :meth:`BaseAnalyzer._maybe_escalate`). Tier 2 reuses tier 1's own
    already-loaded provider credentials (``ollama_url``, ``moondream_api_key``,
    ``anthropic_api_key``, ``openai_api_key``, ...) passed to this same call —
    no separate credential fields are needed for escalation.
    """
    shared_kwargs: dict[str, Any] = {
        "ollama_url": ollama_url,
        "ollama_model": ollama_model,
        "ollama_cloud_api_key": ollama_cloud_api_key,
        "moondream_api_key": moondream_api_key,
        "moondream_finetune_model": moondream_finetune_model,
        "anthropic_api_key": anthropic_api_key,
        "anthropic_model": anthropic_model,
        "openai_api_key": openai_api_key,
        "openai_model": openai_model,
    }

    analyzer = _build_single_analyzer(
        ai_provider,
        prompt,
        car_description=car_description,
        max_frames=max_frames,
        frame_interval=frame_interval,
        suspicious_keywords=suspicious_keywords,
        camera_prompts=camera_prompts,
        camera_descriptions=camera_descriptions,
        frame_strategy=frame_strategy,
        car_cameras=car_cameras,
        car_zones=car_zones,
        **shared_kwargs,
    )
    if analyzer is None:
        return None

    escalation_provider = (escalation_provider or "").strip().lower()
    if escalation_provider:
        tier2_kwargs = dict(shared_kwargs)
        model_kwarg = _ESCALATION_MODEL_KWARG.get(escalation_provider)
        if model_kwarg and escalation_model:
            tier2_kwargs[model_kwarg] = escalation_model

        tier2 = _build_single_analyzer(
            escalation_provider,
            prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
            **tier2_kwargs,
        )
        if tier2 is None:
            _LOGGER.warning(
                "ai_escalation_provider=%r could not be initialized (missing "
                "credentials?) — escalation disabled, tier-1 analysis only",
                escalation_provider,
            )
        elif (
            tier2.provider_name == analyzer.provider_name
            and tier2.model_name() == analyzer.model_name()
        ):
            _LOGGER.debug(
                "ai_escalation_provider/model matches tier-1 exactly; "
                "escalation would be a no-op, leaving it disabled"
            )
        else:
            analyzer.set_escalation_analyzer(tier2)

    analyzer.set_prompt_debug(store_prompt_debug)
    return analyzer
