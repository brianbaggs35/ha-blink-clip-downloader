"""The provider-independent half of clip analysis.

:class:`BaseAnalyzer` owns everything either side of the call to an AI
provider: extracting and down-selecting frames from a clip, running the
optional vision pipeline and the security layer over them, assembling the
prompt, tracking prompt-cache behaviour and token spend, then parsing the
provider's reply back into an :class:`AnalysisResult` and applying the
risk-threshold override and face-recognition bypass to its verdict.

Concrete providers live in sibling ``*_provider`` modules and supply only
:meth:`BaseAnalyzer._call_model` and the handful of identity/health hooks
around it; :mod:`.factory` picks one from config.
The analyzer-wide constants below are shared by all of them, so they live
here rather than being duplicated per provider.
"""

from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC
from typing import TYPE_CHECKING, Any

import aiohttp

from .. import frame_motion, prompt_segments
from ..ffmpeg_output import (
    ANALYSIS_FRAME_WIDTH,
    format_ffmpeg_error,
    split_jpeg_frames,
)

# Imported eagerly, unlike vision below: the security package is pure
# stdlib — no torch, no opencv — so it costs nothing at import time and is
# available whether or not the optional CV extra is installed.
from ..security import (
    BYPASS_BLOCKING_EVENTS,
    ClipMeasurements,
    SecurityEvent,
    SecurityEventType,
    SecurityOutcome,
    Severity,
    assess_clip,
    detect_audio_events,
    summarize_assessment,
)
from ..security.geometry import box_overlap_coefficient

if TYPE_CHECKING:
    from ..database import ClipDatabase
    from ..vision import (
        DetectedObject,
        FaceRecognitionResult,
        VisionHints,
        VisionPipeline,
    )

_LOGGER = logging.getLogger(__name__)

_API_TIMEOUT = aiohttp.ClientTimeout(total=120)
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=10)
_CONTENT_TYPE_JSON = "application/json"

# Blink motion clips can run up to 60 seconds. Frame extraction must sample
# candidates across the *entire* clip length regardless of frame_strategy or
# max_frames, otherwise a fixed small frame count at the default interval only
# ever samples the leading portion of the clip (e.g. 5 frames * 2s = the first
# 10s of a 60s clip) and anything that happens later is never seen.
_MAX_CLIP_COVERAGE_SECONDS: float = 60.0

#: Hard ceiling on frames pulled from one clip, whatever its length. Each
#: extracted frame is a JPEG held in memory and, for the motion-ranking
#: strategies, a PIL decode and a per-pixel diff — so an hour-long file
#: dropped into the library must not be allowed to turn one clip's
#: analysis into thousands of both. At the default 2-second spacing this
#: still covers eight minutes, well past anything Blink itself records.
_MAX_EXTRACTED_FRAMES: int = 240

#: Finest spacing frame extraction will fall back to on a short clip, and
#: the same floor ``ai_frame_interval``'s own schema allows a user to ask
#: for. Sampling below it buys near-identical frames at full token price.
_MIN_FRAME_INTERVAL: float = 0.5

# Floor applied to a clip's confidence when the deterministic risk score
# overrides the model's "nothing unusual" verdict (see
# ai_risk_alert_threshold). Above the default notification threshold, since
# an override only fires on evidence strong enough to disagree with the
# model in the first place — a lower value would raise the flag and then
# silently suppress the notification it exists to send.
_RISK_OVERRIDE_CONFIDENCE: float = 0.7

# max_frames/frame_interval are honored exactly for clips at or under this
# length. Longer clips (Blink's ceiling is 60s) get their frame budget
# doubled — the configured budget alone tends to under-sample a 45-60s clip
# (missing the start, the end, or the middle), and a 60s clip covers twice
# the timeline of a 30s one, so it needs roughly twice the frames to keep the
# same effective sampling density across the whole event.
_LONG_CLIP_THRESHOLD_SECONDS: float = 30.0
_LONG_CLIP_FRAME_MULTIPLIER: int = 2

#: Share of a clip's total motion that has to fall inside one contiguous run
#: before the ``adaptive`` strategy treats that run as "the event".
_EVENT_MOTION_SHARE: float = 0.6

#: ...and the most of the clip that run may span while still counting as
#: localised. Both together are a concentration test, and the second is the
#: one that does the work: with motion spread evenly, or split between two
#: separate bursts, the narrowest run holding 60% of it is most of the clip,
#: which is precisely when there is nothing to aim at. 0.3 was picked by
#: measuring the alternatives against simulated clips -- at 0.7 a clip with
#: two bursts emptied most of its budget into the dead stretch between them.
_EVENT_MAX_WIDTH_SHARE: float = 0.3

# Minimum confidence for a "suspicious" verdict to block that clip's frame
# from being folded into the scene baseline (see analyze_clip()). A clip
# marked suspicious only as a low-confidence hedge — e.g. because the scene
# deviation hint above nudged the model to "look closer" at a newly parked
# car or a trash can put out for collection — should still teach the
# baseline what the camera's new normal looks like. Only a confident verdict
# withholds that frame, so a genuine intruder isn't absorbed into "normal".
_SCENE_BASELINE_SUSPICION_CONFIDENCE_THRESHOLD: float = 0.5

# The analysis prompt's OUTPUT RULES ask for one sentence, or at most two,
# but small vision models sometimes ignore that and emit a degenerate loop
# of near-identical sentences instead of stopping (e.g. repeating "the
# person is standing near the car's rear ___" once per body part in view).
# parse_response() caps every description to this many sentences, plus a
# hard character backstop for responses with no sentence punctuation to
# split on at all.
_MAX_SUMMARY_SENTENCES: int = 2
_MAX_SUMMARY_CHARS: int = 200

# How many consecutive model calls may report no prompt-cache activity at
# all before saying so once (see BaseAnalyzer._note_cache_usage). A single
# call proves nothing — the first request on a cold cache is a write with
# nothing to read, and on OpenAI a request that lands on a machine without
# the entry legitimately misses — but several in a row means the prompt is
# not being cached rather than merely missing.
_CACHE_INERT_CALLS: int = 3

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


def _audio_labels_json(vision_hints: VisionHints | None) -> str:
    """The clip's recognized sounds, as the JSON the analysis row stores.

    Empty when the audio stage is off, found no audio track, or heard
    nothing worth reporting — all three mean the same thing to the clip
    modal, which simply shows no sound chips.
    """
    tags = vision_hints.audio_tags if vision_hints else None
    if tags is None or not tags.labels:
        return ""
    return json.dumps(
        [{"label": label, "score": round(score, 4)} for label, score in tags.labels]
    )


#: A person box counts toward "people seen at once" only this confident...
_PERSON_COUNT_MIN_CONFIDENCE = 0.5
#: ...and only when less than this share of it lies inside a more confident
#: person box, which is the same person boxed twice (the upper body inside
#: the whole body, say), not a second one.
_PERSON_NESTED_OVERLAP = 0.7


#: Events meaning object tracking measured a subject at the protected vehicle
#: itself — within reach, touching, or striking it — rather than merely in
#: the frame or the zone.
_AT_ASSET_EVENTS = frozenset(
    {
        SecurityEventType.ASSET_PROXIMITY,
        SecurityEventType.ASSET_REACH,
        SecurityEventType.CONTACT_CANDIDATE,
        SecurityEventType.IMPACT_CANDIDATE,
        SecurityEventType.ANIMAL_ASSET_INTERACTION,
    }
)


def _people_in_one_frame(vision_hints: VisionHints) -> int:
    """Most people object detection confidently saw together in any one
    sampled frame; 0 when detection did not run.

    Gates the face bypass, so a lone resident miscounted as two loses a
    bypass they should have had, and the rules here are what keep that
    rare. Measured with the real detector on 100 real one-person scenes
    through H.264: counting every kept box saw a second "person" in 10 by
    day and 7 at night — mostly the same person boxed twice, the rest
    low-confidence shapes (an elephant's trunk). Counting only confident,
    un-nested boxes left 2 by day (one a man printed on a bus advert) and
    none at night, while still catching the second person in 66 of 100
    real two-person scenes by day and 50 at night. The per-frame peak, not
    track ids: tracking across frames sampled seconds apart can split one
    person into two ids.
    """
    per_frame: dict[int, list[DetectedObject]] = {}
    for d in vision_hints.detections or ():
        if d.label == "person" and d.confidence >= _PERSON_COUNT_MIN_CONFIDENCE:
            per_frame.setdefault(d.frame_index, []).append(d)
    peak = 0
    for boxes in per_frame.values():
        counted: list[DetectedObject] = []
        for d in sorted(boxes, key=lambda d: -d.confidence):
            if all(
                box_overlap_coefficient(d.box, c.box) < _PERSON_NESTED_OVERLAP
                for c in counted
            ):
                counted.append(d)
        peak = max(peak, len(counted))
    return peak


def _unaccounted_people(vision_hints: VisionHints, faces: FaceRecognitionResult) -> int:
    """People seen together beyond the approved members recognized.

    Faces alone cannot see a stranger who never shows one — back to the
    camera, hood up, too far away — so an approved face beside them read as
    "only approved people here". When object detection saw more people in
    one frame than there are approved members recognized anywhere in the
    clip, the difference is someone unaccounted for. 0 when detection did
    not run: recognition then vouches only for the faces it saw, as it
    always has.
    """
    return max(0, _people_in_one_frame(vision_hints) - len(faces.approved_names))


@dataclass
class _MostAlarming:
    """The most alarming per-frame result seen so far, across one clip.

    Two strategies ask an AI provider about each frame separately and
    then have to decide which frame's answer represents the clip: the
    ``sequential`` strategy (see
    :meth:`BaseAnalyzer._analyze_sequentially`) and the Moondream Cloud
    provider, which works frame-by-frame whatever the strategy. The rule,
    most significant first: any suspicious verdict beats a clear one, and
    between two of the same verdict the more confident wins.

    Shared rather than written twice because it is a three-clause
    comparison that reads the same at a glance whichever way the
    tie-break goes — the kind of rule that silently stops matching its
    twin the first time somebody adjusts one of them.
    """

    response: str = ""
    is_suspicious: bool = False
    confidence: float = 0.0
    #: The frame that produced :attr:`response`, when the caller tracks
    #: it. ``sequential`` uses it to escalate on that one frame rather
    #: than re-running the whole clip through tier 2.
    frame: bytes | None = None

    def offer_unranked(self, response: str, frame: bytes | None = None) -> None:
        """Take *response* only if nothing at all has been recorded yet.

        For answers that carry no verdict to rank — a frame with no
        subject in it, or one whose reply would not parse. They are
        better than returning nothing and worse than anything ranked.
        """
        if not self.response:
            self.response = response
            self.frame = frame

    def offer(
        self,
        suspicious: bool,
        confidence: float,
        response: str,
        frame: bytes | None = None,
    ) -> None:
        """Take *response* if it outranks what is already held."""
        if (
            not self.response
            or (suspicious and not self.is_suspicious)
            or (suspicious == self.is_suspicious and confidence > self.confidence)
        ):
            self.response = response
            self.is_suspicious = suspicious
            self.confidence = confidence
            self.frame = frame


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
    # Set when _face_bypass_applies cleared this clip's suspicious flag —
    # see BiometricsPage's face-bypass activity card, which lets a household
    # member audit whether the bypass is firing for the right people (and
    # catch it firing for the wrong one) instead of trusting it blindly.
    face_bypass_applied: bool = False
    # Comma-separated approved name(s) that triggered the bypass above.
    # Local-only — this never leaves the process the way the equivalent
    # name-free vision/faces.py hint sent to the AI provider does (see
    # BaseAnalyzer._personalize_summary's docstring) — it is written to this
    # add-on's own database and displayed only in this add-on's own web UI.
    face_bypass_names: str = ""
    # True whenever face recognition unambiguously matched only approved
    # household member(s) in this clip's sampled frames (the same
    # all-or-nothing condition _face_bypass_applies checks), computed
    # unconditionally rather than gated behind is_suspicious like the
    # bypass fields above. Most real matches are a household member's own
    # routine, already non-suspicious visit — the bypass is never even
    # consulted for those (see the `if is_suspicious and ...` gate below),
    # so without this separate field there would be no way to show a
    # library clip was actually recognized in the overwhelmingly common
    # case where nothing needed bypassing. Powers the Library's
    # face-recognized badge; face_bypass_applied above stays narrowly
    # about the safety bypass itself for the Biometrics audit card.
    approved_faces_seen: bool = False
    # Raw per-object detections from the optional computer-vision pipeline
    # (see vision/detection.py's ObjectDetector), when enabled and something was
    # found. Deliberately excluded from to_dict() below — that dict is the
    # analysis_results row/JSON contract, while detections are persisted
    # separately (see database.py's detected_objects table and
    # save_detected_objects) and served to the clip modal pre-aggregated
    # via get_detected_objects_summary, not as this raw per-box list.
    detected_objects: list[DetectedObject] = field(default_factory=list)
    # Seconds between the frames those detections came from, and the size of
    # those frames. Both are needed to draw a box back over the video — the
    # detector works on scaled frames sampled at its own interval, neither of
    # which the player knows anything about. Excluded from to_dict() with
    # detected_objects themselves.
    detection_interval: float = 0.0
    detection_frame_size: tuple[float, float] | None = None
    # Deterministic security assessment (see blink_downloader.security).
    # Zero/empty when the security layer produced nothing — the optional
    # object-detection pipeline is off, or nothing relevant was detected.
    risk_score: float = 0.0
    severity: str = str(Severity.ROUTINE)
    event_type: str = ""
    evidence_quality: float = 0.0
    # True when ai_risk_alert_threshold flagged this clip despite the AI
    # model having judged it unremarkable — surfaced so a user can see the
    # verdict did not come from the model.
    risk_override_applied: bool = False
    # Sounds the optional audio stage recognized, as a JSON array of
    # {label, score} (see vision/audio.py). A JSON string rather than a
    # list because this one *is* part of the analysis row -- unlike
    # detected_objects there are at most three of them and nothing joins
    # against them, so they ride along in a column instead of a table.
    audio_labels: str = ""
    # Structured events behind the score above. Excluded from to_dict()
    # like detected_objects: they are persisted to their own table (see
    # database.py's security_events) rather than into the analysis row.
    security_events: list[SecurityEvent] = field(default_factory=list)

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
            "face_bypass_applied": self.face_bypass_applied,
            "face_bypass_names": self.face_bypass_names,
            "approved_faces_seen": self.approved_faces_seen,
            "risk_score": self.risk_score,
            "severity": self.severity,
            "event_type": self.event_type,
            "evidence_quality": self.evidence_quality,
            "risk_override_applied": self.risk_override_applied,
            "audio_labels": self.audio_labels,
        }


@dataclass(frozen=True)
class SecurityLayerSettings:
    """How the deterministic security layer behaves for one analyzer.

    Passed as one object rather than two loose flags because every provider
    subclass forwards both, unchanged, straight to ``BaseAnalyzer`` — and
    because they only mean anything together: a risk threshold is
    meaningless with the layer switched off.
    """

    #: Whether the layer runs at all (``ai_security_events_enabled``).
    enabled: bool = True
    #: Risk score at or above which a clip is flagged regardless of the
    #: model's own verdict (``ai_risk_alert_threshold``); 0 disables it.
    risk_alert_threshold: int = 75


@dataclass(frozen=True)
class _Verdict:
    """A clip's verdict as it passes through the two local adjustments.

    Only exists so ``_adjust_verdict`` can hand six related values back
    without a positional tuple nobody can read at the call site.
    """

    is_suspicious: bool
    confidence: float
    summary: str
    face_bypass_applied: bool = False
    face_bypass_names: str = ""
    risk_override_applied: bool = False


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
        car_zones: dict[str, dict[str, Any]] | None = None,
        security_settings: SecurityLayerSettings | None = None,
    ) -> None:
        self._base_prompt = prompt
        self._car_description = car_description
        self._max_frames = max_frames
        self._frame_interval = frame_interval
        # The spacing the *current* clip's frames were actually extracted
        # at, which is finer than the configured one on a clip too short to
        # fill the candidate pool (see _extraction_interval). Everything
        # that converts a frame index into a clip time has to use this one
        # or every offset, speed and duration it derives is wrong. Set per
        # clip in _analyze_clip_locked, which holds _analyze_lock for its
        # whole body — the same reasoning as _current_camera below.
        self._current_frame_interval = frame_interval
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
        # zone-restricted motion evidence — see ``frame_motion.zone_motion_fraction``.
        self._car_zones: dict[str, dict[str, Any]] = car_zones or {}
        # Whether the deterministic security layer runs at all (see
        # blink_downloader.security). It costs no extra model inference on
        # top of object detection, so it is on by default and simply
        # produces nothing when detection is off.
        settings = security_settings or SecurityLayerSettings()
        self._security_events_enabled = settings.enabled
        # Deterministic risk score (0-100) at or above which a clip is
        # flagged suspicious even when the AI model said otherwise. 0
        # disables the override entirely. This is the same high-recall
        # asymmetry the tier-2 escalation path already enforces: strong,
        # code-computed evidence of physical interference with a protected
        # asset must not be silently dismissed by a weak local model, while
        # nothing here can move a verdict in the reassuring direction.
        self._risk_alert_threshold = settings.risk_alert_threshold
        # Token counts set by _call_model() implementations that support them.
        # Reset to 0 at the start of each analyze_clip() call.
        self._last_prompt_tokens: int = 0
        self._last_completion_tokens: int = 0
        # Set True by a provider's error handler when the most recent
        # _call_model() failed specifically due to a rate limit (as opposed
        # to any other failure) — lets AnalysisQueue._process_pending stop
        # working through the rest of a batch immediately instead of
        # re-attempting each remaining clip, all doomed to hit the same
        # limit. Reset to False at the start of each analyze_clip() call.
        self._last_rate_limited: bool = False
        # Whether the most recent _call_model() failure (if any) looks
        # retry-worthy rather than a fixed, permanent misconfiguration —
        # see the transient_error property below for the full contract.
        # Defaults True (retry-favoring); reset at the start of each
        # analyze_clip() call.
        self._last_transient_error: bool = True
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
        # attach_database() once the app has a database ready.
        self._db: ClipDatabase | None = None
        # Optional VisionPipeline (see vision/pipeline.py) providing the heavy,
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
        # Prompt-cache accounting for the two providers that support it
        # (see _note_cache_usage). Recorded per model call so the per-clip
        # summary line can say whether caching actually did anything, and
        # so a provider that silently declines to cache — the normal
        # outcome when the reusable prefix is below the model's minimum —
        # is reported once rather than left to be discovered on a billing
        # dashboard.
        self._last_cache_read_tokens: int = 0
        self._last_cache_write_tokens: int = 0
        self._cache_inert_calls: int = 0
        self._cache_inert_reported: bool = False

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

    def rename_camera(self, old_name: str, new_name: str) -> None:
        """Move in-memory per-camera settings to a newly renamed Blink camera."""
        for settings in (self._camera_descriptions, self._camera_prompts):
            if old_name in settings and new_name not in settings:
                settings[new_name] = settings.pop(old_name)
            elif old_name in settings:
                settings.pop(old_name)
        if old_name in self._car_zones and new_name not in self._car_zones:
            self._car_zones[new_name] = self._car_zones.pop(old_name)
        elif old_name in self._car_zones:
            self._car_zones.pop(old_name)
        self._car_cameras = {
            new_name if camera == old_name else camera for camera in self._car_cameras
        }

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

    def update_car_zones(self, car_zones: dict[str, dict[str, Any]]) -> None:
        """Replace the per-camera car-zone map at runtime without restart.

        Full replace, not a merge — clearing a camera's zone in the AI tab
        must stop it from applying immediately, matching
        :meth:`update_car_cameras`'s behaviour for the same reason.
        """
        self._car_zones = dict(car_zones)

    @staticmethod
    def _car_zone_bbox(zone: dict[str, Any]) -> dict[str, float]:
        """Reduce a ``car_zone`` (rectangle or freeform polygon) to a plain
        ``x_min``/``y_min``/``x_max``/``y_max`` box.

        Used by :class:`_MoondreamDetectionMixin` (with
        :meth:`_bbox_gap`/:meth:`_bbox_min_gap`, which only understand
        this shape — a rough fallback proximity reference used only when
        per-frame car detection found no box at all, not exact geometry)
        and by :meth:`_downselect_frames`'s zone-aware frame selection
        (converting the same normalized zone into thumbnail-space
        coordinates for ``frame_motion.frame_motion_diffs``). For a polygon this
        is its axis-aligned bounding box — an approximation, but every
        current caller already only needs a coarse reference, not exact
        geometry. A rectangle zone already has the right keys and is
        returned unchanged.
        """
        if zone.get("shape") != "polygon":
            return zone
        points = zone.get("points") or []
        if not points:
            return {"x_min": 0.0, "y_min": 0.0, "x_max": 0.0, "y_max": 0.0}
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return {"x_min": min(xs), "y_min": min(ys), "x_max": max(xs), "y_max": max(ys)}

    def update_car_description(self, description: str) -> None:
        """Replace the protected-vehicle description at runtime without restart.

        Full replace, not a merge — clearing the description in the Vehicles
        tab must immediately deactivate every car-protection rule (see
        :attr:`car_protection_active`), matching :meth:`update_car_cameras`'s
        behaviour for the same reason.
        """
        self._car_description = description

    @property
    def car_description(self) -> str:
        """The currently active protected-vehicle description, if any.

        Lets the Vehicles tab's settings endpoint read back the value the
        live analyzer was started with (from ``ai_car_description``) the
        first time ``/data/vehicle_settings.json`` doesn't exist yet, the
        same "web UI file overrides the config.yaml option" precedent
        already used for camera prompts/descriptions.
        """
        return self._car_description

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

    @staticmethod
    def _faces_all_approved(faces: FaceRecognitionResult) -> bool:
        """True when every face found in this clip is an approved member.

        Deliberately all-or-nothing and fail-safe: a positive approved match
        is required (absence of any face at all does not qualify — no
        enrollment ever matched means ``approved_names`` stays empty), AND
        zero unrecognized or recognized-but-not-approved faces may appear
        anywhere in the clip's sampled frames. A single stranger — or a
        recognized person who isn't approved — standing next to an approved
        family member must still allow the clip to be flagged; this is a
        hard safety requirement, not a convenience default, so do not loosen
        this condition without equally strong justification.
        """
        return (
            bool(faces.approved_names)
            and not faces.other_names
            and not faces.unrecognized_present
        )

    @classmethod
    def _face_match_is_unambiguous(cls, vision_hints: VisionHints | None) -> bool:
        """True when everyone seen in this clip is an approved member: every
        face (:meth:`_faces_all_approved`) and every person object detection
        found (:func:`_unaccounted_people`). Only ever stricter than faces
        alone, never looser."""
        if vision_hints is None or vision_hints.face_recognition is None:
            return False
        faces = vision_hints.face_recognition
        return cls._faces_all_approved(faces) and not _unaccounted_people(
            vision_hints, faces
        )

    @classmethod
    def _face_bypass_applies(
        cls,
        vision_hints: VisionHints | None,
        events: list[SecurityEvent] | None = None,
    ) -> bool:
        """True if the clip should have its suspicious flag auto-cleared
        because every face detected across the clip belongs to an approved,
        locally-enrolled household member — and nothing happened that a
        household member's identity cannot explain.

        The identity condition is :meth:`_face_match_is_unambiguous`: every
        face approved, and nobody seen whose face was not. The second
        condition is narrow: an event in
        :data:`~blink_downloader.security.BYPASS_BLOCKING_EVENTS` blocks the
        bypass outright, because some things a familiar face simply cannot
        account for. A recognized person denting the car is still a dented
        car; and a recognized person standing in the driveway is no
        evidence at all about the window heard breaking at the back of the
        house, because sound carries from places the camera cannot see.
        That set is deliberately tiny — see its own comment for why
        ordinary contact with one's own vehicle, and every everyday sound,
        are *not* in it and must not be added.
        """
        if vision_hints is None or vision_hints.face_recognition is None:
            return False
        faces = vision_hints.face_recognition
        if not cls._faces_all_approved(faces):
            return False
        unaccounted = _unaccounted_people(vision_hints, faces)
        if unaccounted:
            _LOGGER.info(
                "Face-recognition bypass withheld despite an approved match: "
                "%d more %s seen at once than approved faces recognized",
                unaccounted,
                "person" if unaccounted == 1 else "people",
            )
            return False
        blocking = [e for e in (events or []) if e.event_type in BYPASS_BLOCKING_EVENTS]
        if blocking:
            _LOGGER.info(
                "Face-recognition bypass withheld despite an approved match: %s",
                ", ".join(sorted({str(e.event_type) for e in blocking})),
            )
            return False
        return True

    @staticmethod
    def _personalization_names(vision_hints: VisionHints | None) -> list[str]:
        """Names to use when personalizing the AI's summary text — a purely
        cosmetic rewrite, safe regardless of ``is_suspicious`` (see
        ``_personalize_summary``'s docstring), and deliberately **not** the
        same eligibility test as :meth:`_face_bypass_applies`.

        A person whose *per-enrollment* "Approved for bypass" toggle is off
        (``face_enrollments.approved=False``, see the Biometrics tab) still
        counts here — they were positively recognized, so their clips should
        still read "Brian walked up the driveway" instead of the generic "A
        person...", even though that recognition alone must never clear a
        suspicious flag. That safety gate stays exactly as strict as
        ``_face_bypass_applies`` already requires; only the cosmetic
        rewrite widens from *approved-only* to *every confidently recognized
        enrollment, approved or not*.

        Still returns no names at all if a genuinely unrecognized face
        (``unrecognized_present``) also appears — an unidentified stranger
        sharing the frame means it's not safe to attribute the AI's summary
        to the person(s) who *were* identified. Likewise when object
        detection saw more people at once than there are names: the one
        whose face never showed may be the one the summary describes.
        """
        if vision_hints is None or vision_hints.face_recognition is None:
            return []
        fr = vision_hints.face_recognition
        if fr.unrecognized_present:
            return []
        names = sorted({*fr.approved_names, *fr.other_names})
        if _people_in_one_frame(vision_hints) > len(names):
            return []
        return names

    # Leading generic-subject phrases a vision model commonly opens a
    # description with — matched case-insensitively, anchored to the start
    # of the summary, so a confident, natural rewrite ("Brian walked up the
    # driveway") is possible without risking a mismatched/garbled rewrite
    # when the model phrased things differently (see the fallback below).
    _GENERIC_SUBJECT_RE = re.compile(
        r"^((a|an|the)\s+(person|individual|figure|man|woman|subject|resident)"
        r"|someone|somebody)\b\.?",
        re.IGNORECASE,
    )

    @classmethod
    def _personalize_summary(cls, summary: str, names: list[str]) -> str:
        """Rewrite *summary* to name the recognized approved household
        member(s) instead of a generic "a person" — entirely locally, using
        only the AI's own already-returned text. Never called with anything
        but approved, unambiguous names (see :meth:`_face_bypass_applies`);
        the name itself never leaves this process, let alone the network —
        it is not sent to any AI provider, only used to rewrite text that
        has already been generated.
        """
        if not names or not summary:
            return summary
        names_joined = " and ".join(names) if len(names) <= 2 else ", ".join(names)
        match = cls._GENERIC_SUBJECT_RE.match(summary)
        if match:
            return names_joined + summary[match.end() :]
        return f"{names_joined}: {summary}"

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

    @property
    def rate_limited(self) -> bool:
        """True when the most recent _call_model() call failed on a rate limit.

        Checked by AnalysisQueue after each clip to decide whether to stop
        working through the rest of the current batch — see the comment on
        ``_last_rate_limited`` in ``__init__``.
        """
        return self._last_rate_limited

    @property
    def transient_error(self) -> bool:
        """Whether the most recent failed _call_model() call looks
        retry-worthy (network/timeout/rate-limit/unrecognized) rather than
        a fixed, permanent misconfiguration (bad API key, no model access,
        a malformed request).

        Checked by ``AnalysisQueue._process_one`` to decide whether a
        failed clip gets automatically requeued (bounded retries) instead
        of marked permanently ``failed``. Defaults True: only the specific
        provider error handlers that recognize a genuinely permanent
        failure class (Anthropic/OpenAI auth, permission, and bad-request
        errors; Moondream Cloud's HTTP 401) set this False. Everything
        else — including a failure this doesn't specifically
        recognize — is treated as worth retrying a bounded number of
        times: incorrectly giving up on a real security event is worse
        than a few wasted retry attempts.
        """
        return self._last_transient_error

    async def run_tier_call(self, frames: list[bytes], prompt: str) -> str:
        """Public entry point for another analyzer to use this one as its tier 2.

        Thin passthrough to :meth:`_call_model` — exists so cross-analyzer
        escalation composition (see :meth:`_maybe_escalate`) doesn't need to
        reach into another instance's "private" ``_call_model``.
        """
        return await self._call_model(frames, prompt)

    def attach_database(self, db: ClipDatabase) -> None:
        """Enable every analysis feature that needs to remember something.

        Two of them, today. The visual scene-baseline ("smart brain"):
        Blink cameras are stationary, so each camera's background should
        look almost the same clip after clip, and once enough history has
        accumulated the model can be told whether this clip's scene looks
        like it usually does — a cheap signal for a genuinely new object in
        frame, and just as valuable for reassuring the model when a scene is
        unremarkable so it doesn't over-flag routine activity. And the
        learned per-camera vehicle signature, which is how the protected
        vehicle stays identifiable when the drawn zone alone cannot separate
        it from the car parked beside it (see
        :mod:`blink_downloader.security.vehicles`).
        """
        self._db = db

    def attach_vision_pipeline(self, pipeline: VisionPipeline) -> None:
        """Enable the optional computer-vision enhancement pipeline (see ``vision``).

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
        from datetime import datetime

        self._reset_analysis_state(camera)
        start = time.monotonic()
        self._current_frame_interval = self._extraction_interval(clip_duration)
        frames = await self.extract_frames(clip_path, clip_duration)

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
                analyzed_at=datetime.now(UTC).isoformat(),
                anomaly_score=anomaly_score,
            )

        scene_thumbnail, scene_deviation = await self._lookup_scene_baseline(
            camera, frames
        )

        # Captured before down-selection narrows `frames` to the handful
        # actually sent to the AI model — face recognition gets this wider
        # pool (see _apply_vision_pipeline), everything else still runs
        # against the down-selected set below.
        raw_frame_pool = frames
        target_frame_count = self._target_frame_count(
            len(raw_frame_pool), clip_duration=clip_duration
        )
        frames = await self._downselect_frames(frames, clip_duration, camera)
        frames, vision_hints = await self._apply_vision_pipeline(
            frames, camera, raw_frames=raw_frame_pool, clip_path=clip_path
        )
        await self._store_vehicle_signature(camera, vision_hints)

        security = self._assess_security(
            camera,
            vision_hints,
            clip_timestamp=clip_timestamp,
            scene_deviation=scene_deviation,
            frames_analyzed=len(frames),
            target_frames=target_frame_count,
        )

        # Shared precomputation for the two motion hints below — see
        # _maybe_compute_motion_thumbnails's own docstring for why this
        # replaced each of them independently redoing the same PIL work.
        motion_thumbs = await self._maybe_compute_motion_thumbnails(frames, camera)

        zone_motion_fraction = self._maybe_compute_zone_motion(motion_thumbs, camera)
        # The car-zone box drawn in the Vehicles tab only ever showed up as
        # a difference in the AI's final prompt text — nothing logged
        # whether a zone was even found for this camera, let alone what
        # fraction it computed, making it impossible to tell from the logs
        # alone whether the box is wired up at all versus just not mattering
        # for this particular clip.
        _LOGGER.debug(
            "Car-zone check for camera=%r: zone_configured=%s, "
            "car_protection_applies=%s, zone_motion_fraction=%s",
            camera,
            camera in self._car_zones,
            self._car_protection_applies(camera),
            zone_motion_fraction,
        )

        # The motion-trajectory ("smart brain") entry/peak/exit frame
        # selection and approaching/retreating hint had the same silent-
        # unless-you-read-a-stored-prompt problem as the car-zone check
        # above — this is the only place that ever showed its actual
        # per-clip output.
        trajectory_hint = frame_motion.motion_trajectory_hint(motion_thumbs)
        _LOGGER.debug(
            "Motion-trajectory hint for camera=%r: %s",
            camera,
            trajectory_hint,
        )

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
            security=security,
        )

        response = await self._generate_response(frames, prompt)
        if not response:
            # frames were successfully extracted above, so an empty response
            # here can only mean the provider call itself failed (rate limit,
            # auth error, timeout, connection drop, ...) — every _call_model
            # implementation's error handler returns "" rather than raising,
            # already logging the specific reason. parse_response("") would
            # silently produce is_suspicious=False with an empty summary,
            # and the caller (AnalysisQueue._process_one) would then mark
            # the clip "completed" — which get_pending_analysis never
            # reselects (status='pending' only) — permanently recording a
            # false "not suspicious" verdict for a clip that was never
            # actually analyzed. Raising instead routes this through
            # _process_one's existing except-block, which marks the clip
            # "failed" (visible in the Queue Status card) with no bogus
            # result persisted, so it's at least surfaced instead of lost.
            raise RuntimeError(
                f"{self.provider_name} returned an empty response — see the "
                "warning logged above for the specific cause"
            )
        is_suspicious, confidence, summary = self.parse_response(response)

        # Computed once, unconditionally: whether every face recognition
        # found across this clip's sampled frames belongs to an approved
        # household member (same all-or-nothing condition the safety
        # bypass below requires). Reused for the bypass gate (only when the
        # clip is also suspicious) and approved_faces_seen (always — see its
        # field docstring for why that has to be unconditional). Summary
        # personalization below uses the deliberately *wider*
        # _personalization_names instead — see its docstring for why a
        # recognized-but-not-bypass-approved person (e.g. a nanny) still
        # gets named in the summary even though they can never clear a
        # suspicious flag.
        bypass_condition_met = self._face_bypass_applies(
            vision_hints, security.events if security else None
        )
        personalization_names = self._personalization_names(vision_hints)
        # Computed before the bypass below so the two can't contradict each
        # other: a clip the deterministic layer rates high enough to force an
        # alert must not also be reported as having had its flag cleared.
        risk_forces_alert = self._risk_forces_alert(security)

        verdict = self._adjust_verdict(
            _Verdict(is_suspicious, confidence, summary),
            bypass_condition_met=bypass_condition_met,
            personalization_names=personalization_names,
            risk_forces_alert=risk_forces_alert,
            vision_hints=vision_hints,
            security=security,
            clip_id=clip_id,
            camera=camera,
        )
        is_suspicious = verdict.is_suspicious
        confidence = verdict.confidence
        summary = verdict.summary
        face_bypass_applied = verdict.face_bypass_applied
        face_bypass_names = verdict.face_bypass_names
        risk_override_applied = verdict.risk_override_applied

        await self._maybe_update_scene_baseline(
            camera, scene_thumbnail, is_suspicious, confidence
        )

        self._log_analysis_summary(
            clip_id, camera, frames, vision_hints, security, is_suspicious
        )

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
            analyzed_at=datetime.now(UTC).isoformat(),
            tokens_prompt=self._last_prompt_tokens,
            tokens_completion=self._last_completion_tokens,
            anomaly_score=anomaly_score,
            escalation_model=self._last_escalation_model,
            escalation_tokens_prompt=self._last_escalation_prompt_tokens,
            escalation_tokens_completion=self._last_escalation_completion_tokens,
            escalation_provider=self._last_escalation_provider,
            prompt_text=prompt if self._store_prompt_debug else "",
            face_bypass_applied=face_bypass_applied,
            face_bypass_names=face_bypass_names,
            approved_faces_seen=self._face_match_is_unambiguous(vision_hints),
            detected_objects=(vision_hints.detections or []) if vision_hints else [],
            detection_interval=vision_hints.scan_interval if vision_hints else 0.0,
            detection_frame_size=vision_hints.frame_size if vision_hints else None,
            risk_score=security.risk_score if security else 0.0,
            severity=str(security.severity) if security else str(Severity.ROUTINE),
            event_type=self._primary_event_type(security),
            evidence_quality=security.evidence.score if security else 0.0,
            risk_override_applied=risk_override_applied,
            audio_labels=_audio_labels_json(vision_hints),
            security_events=security.events if security else [],
        )

    def _adjust_verdict(
        self,
        verdict: _Verdict,
        *,
        bypass_condition_met: bool,
        personalization_names: list[str],
        risk_forces_alert: bool,
        vision_hints: VisionHints | None,
        security: SecurityOutcome | None,
        clip_id: str,
        camera: str,
    ) -> _Verdict:
        """Apply the two local, post-model adjustments to the model's verdict.

        Extracted whole, in order, from ``_analyze_clip_locked``: the face
        bypass may only *clear* a suspicious flag and the risk override may
        only *raise* one, and the override is evaluated first precisely so
        the two can never contradict each other. That ordering is the safety
        property — see CLAUDE.md's face-recognition section — so it is kept
        here as one unit rather than split across the caller.
        """
        is_suspicious = verdict.is_suspicious
        confidence = verdict.confidence
        summary = verdict.summary
        face_bypass_applied = False
        face_bypass_names = ""

        # Personalizing the summary text is safe regardless of is_suspicious
        # or bypass eligibility — it only ever rewrites text the AI already
        # generated, entirely locally, and never feeds back into the AI
        # prompt (see _personalize_summary's docstring). Clearing the
        # suspicious flag itself must stay gated on bypass_condition_met and
        # is_suspicious below: that's the actual safety bypass, not just a
        # text rewrite.
        if personalization_names:
            summary = self._personalize_summary(summary, personalization_names)
        if bypass_condition_met:
            assert (
                vision_hints is not None and vision_hints.face_recognition is not None
            )
            approved_names = vision_hints.face_recognition.approved_names
            if is_suspicious and not risk_forces_alert:
                is_suspicious = False
                face_bypass_applied = True
                face_bypass_names = ", ".join(approved_names)
                # Unlike vision/faces.py's name-free hint sent to the AI provider,
                # this log line stays entirely local — naming who was
                # matched is exactly what lets a household member audit
                # whether the bypass is firing correctly (see
                # BiometricsPage's bypass activity card) versus clearing a
                # flag it shouldn't have.
                _LOGGER.info(
                    "Face-recognition bypass cleared suspicious flag for "
                    "clip=%r camera=%r: matched approved member(s) %s",
                    clip_id,
                    camera,
                    face_bypass_names,
                )

        risk_override_applied = False
        if risk_forces_alert and not is_suspicious:
            assert security is not None
            is_suspicious = True
            risk_override_applied = True
            confidence = max(confidence, _RISK_OVERRIDE_CONFIDENCE)
            summary = self._risk_override_summary(summary, security)
            _LOGGER.info(
                "Risk override flagged clip=%r camera=%r: score %.0f/100 (%s) "
                "despite the model reporting nothing unusual",
                clip_id,
                camera,
                security.risk_score,
                security.severity,
            )

        return _Verdict(
            is_suspicious=is_suspicious,
            confidence=confidence,
            summary=summary,
            face_bypass_applied=face_bypass_applied,
            face_bypass_names=face_bypass_names,
            risk_override_applied=risk_override_applied,
        )

    @staticmethod
    def _primary_event_type(security: SecurityOutcome | None) -> str:
        """The event a clip gets labelled with where there is room for one."""
        primary = security.assessment.primary_event if security else None
        return str(primary.event_type) if primary else ""

    def _risk_forces_alert(self, security: SecurityOutcome | None) -> bool:
        """Whether the deterministic score alone justifies flagging this clip.

        The override is one-directional by design: a high score can raise a
        verdict the model missed, but nothing here can lower one it made.
        That asymmetry is the same one the tier-2 escalation path already
        enforces, for the same reason — a missed intrusion costs far more
        than an extra notification, and a small local vision model
        overlooking someone at a car window is a real, observed failure
        rather than a hypothetical one.
        """
        return (
            security is not None
            and self._risk_alert_threshold > 0
            and security.risk_score >= self._risk_alert_threshold
        )

    @staticmethod
    def _risk_override_summary(summary: str, security: SecurityOutcome) -> str:
        """Prefix the model's description with what actually raised the flag.

        Without this the clip appears in the library flagged as suspicious
        alongside a summary saying nothing happened, which reads as a bug
        rather than as the two independent judgements it really is.
        """
        primary = security.assessment.primary_event
        detail = primary.detail if primary else "Unusual activity was detected."
        return f"{detail} {summary}".strip() if summary else detail

    def _log_analysis_summary(
        self,
        clip_id: str,
        camera: str,
        frames: list[bytes],
        vision_hints: Any,
        security: SecurityOutcome | None,
        is_suspicious: bool,
    ) -> None:
        """One structured line per analysis, so a run can be understood.

        Before this, the only per-clip evidence any of the optional stages
        had run was whatever hint text happened to reach a stored prompt.
        Deliberately free of recognized names and of prompt text: what gets
        logged here is counts and verdicts, matching the same privacy line
        the prompt itself is held to.
        """
        _LOGGER.info(
            "Analyzed clip=%r camera=%r provider=%s frames=%d suspicious=%s "
            "detections=%d tracks=%d scan_frames=%d vehicle=%s missing=%s "
            "n/a=%s %s%s",
            clip_id,
            camera,
            self.provider_name,
            len(frames),
            is_suspicious,
            len(getattr(vision_hints, "detections", None) or []),
            len(getattr(vision_hints, "tracks", None) or []),
            getattr(vision_hints, "scan_frame_count", 0),
            self._describe_vehicle_identification(vision_hints),
            ",".join(getattr(vision_hints, "unavailable_sources", []) or []) or "none",
            # Separate from missing= on purpose: these are stages that had
            # nothing in this clip to measure, which reads very differently
            # from a stage that could not run. See VisionHints.
            ",".join(getattr(vision_hints, "not_applicable_sources", []) or [])
            or "none",
            summarize_assessment(security.assessment) if security else "risk=n/a",
            self._cache_summary_token(),
        )

    @staticmethod
    def _describe_vehicle_identification(vision_hints: Any) -> str:
        """Short "which car did we protect" token for the summary log."""
        asset = getattr(vision_hints, "asset", None)
        if asset is None:
            return "n/a"
        if asset.identification is None or asset.identification.protected is None:
            return f"absent({asset.location})"
        return f"{'confident' if asset.confident else 'tentative'}({asset.location})"

    def _reset_analysis_state(self, camera: str) -> None:
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        self._last_rate_limited = False
        self._last_transient_error = True
        self._last_escalation_model = ""
        self._last_escalation_provider = ""
        self._last_escalation_prompt_tokens = 0
        self._last_escalation_completion_tokens = 0
        # Zeroed alongside the token counters so the summary line reports
        # this clip's cache activity rather than the previous clip's after a
        # call that never reached the provider.
        self._last_cache_read_tokens = 0
        self._last_cache_write_tokens = 0
        # Store camera name so provider subclasses can access it in _call_model.
        # Safe because analyze_clip() holds _analyze_lock for its whole body.
        self._current_camera: str = camera

    async def _lookup_scene_baseline(
        self, camera: str, frames: list[bytes]
    ) -> tuple[list[float] | None, float | None]:
        """Compare this clip's opening frame against the camera's learned background.

        The opening frame is closest to the pre-motion scene. The result is
        folded back into the baseline by ``_maybe_update_scene_baseline()``
        once we know whether this clip was suspicious.
        """
        if self._db is None:
            return None, None
        scene_thumbnail = frame_motion.scene_thumbnail(frames[0])
        if scene_thumbnail is None:
            return None, None
        scene_deviation = await self._db.get_scene_deviation(camera, scene_thumbnail)
        return scene_thumbnail, scene_deviation

    async def _downselect_frames(
        self, frames: list[bytes], clip_duration: float, camera: str
    ) -> list[bytes]:
        """Down-select the oversampled pool to the frames actually sent to the AI.

        "uniform" keeps frames evenly spread across the whole clip.
        "smart" and "sequential" both take motion-weighted picks
        (peak/entry/exit, then spread out) — sequential analyses each frame
        individually and gets more value from a few well-chosen frames than
        an arbitrary early slice of the clip. "adaptive" uses the same
        motion scores but concentrates the budget on the clip's one busiest
        stretch instead of spreading it, falling back to "smart" when there
        is no single such stretch — see :meth:`_select_frames_around_event`.
        Clips longer than _LONG_CLIP_THRESHOLD_SECONDS get their frame
        budget doubled — see _target_frame_count().

        When a car zone is configured for *camera* and protected-vehicle
        rules actually apply to it (the same gating
        :meth:`_maybe_compute_zone_motion` already uses), motion-weighted
        selection is biased toward motion concentrated inside that zone
        rather than the whole frame — see :meth:`_select_best_frames`'s
        own docstring for why. The actual selection is CPU-bound (PIL
        decode + per-pixel diffing), so it runs in a thread executor
        rather than blocking the event loop, matching every CPU-bound
        stage in the ``vision`` package.
        """
        target_frame_count = self._target_frame_count(
            len(frames), clip_duration=clip_duration
        )
        if len(frames) <= target_frame_count:
            return frames
        if self._frame_strategy == "uniform":
            return self._select_uniform_frames(frames, target_frame_count)

        zone_box: tuple[float, float, float, float] | None = None
        if self._car_zones.get(camera) and self._car_protection_applies(camera):
            bbox = self._car_zone_bbox(self._car_zones[camera])
            zone_box = (
                bbox.get("x_min", 0.0),
                bbox.get("y_min", 0.0),
                bbox.get("x_max", 1.0),
                bbox.get("y_max", 1.0),
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._select_best_frames,
            frames,
            target_frame_count,
            zone_box,
            self._frame_strategy == "adaptive",
        )

    async def _apply_vision_pipeline(
        self,
        frames: list[bytes],
        camera: str,
        raw_frames: list[bytes] | None = None,
        clip_path: str = "",
    ) -> tuple[list[bytes], Any]:
        """Run the optional computer-vision enhancement pipeline (see ``vision``).

        Off entirely unless attach_vision_pipeline() was called and at least
        one of its stages is enabled in config. Runs before the
        trajectory/zone-motion heuristics so that, when frame preprocessing
        is enabled, those heuristics see the same enhanced frames that get
        sent to the AI model.

        *raw_frames*, when given, is the full pre-down-selection extraction
        pool — passed through to face recognition only (see
        VisionPipeline.process_clip's face_recognition_frames parameter).
        Every other stage still runs against *frames*, the same set the AI
        model sees, so their hints stay describing what's actually in the
        prompt.

        *clip_path* is needed by the audio stage alone: sound is in the
        file, not in the frames every other stage works from.
        """
        if self._vision_pipeline is None:
            return frames, None
        vision_hints = await self._vision_pipeline.process_clip(
            frames,
            car_description=self._car_description,
            car_protection_applies=self._car_protection_applies(camera),
            raw_frames=raw_frames,
            car_zone=self._car_zones.get(camera),
            camera=camera,
            frame_interval=self._current_frame_interval,
            vehicle_signature=await self._load_vehicle_signature(camera),
            clip_path=clip_path,
        )
        if vision_hints.enhanced_frames is not None:
            frames = vision_hints.enhanced_frames
        return frames, vision_hints

    async def _load_vehicle_signature(self, camera: str) -> Any:
        """Read this camera's learned protected-vehicle signature, if any."""
        if self._db is None or not self._car_protection_applies(camera):
            return None
        try:
            return await self._db.get_vehicle_signature(camera)
        except Exception:  # noqa: BLE001
            # A signature is an optimisation, not a requirement: without it
            # identification falls back to the drawn zone exactly as it did
            # before signatures existed.
            _LOGGER.debug("Vehicle signature lookup failed for %r", camera)
            return None

    async def _store_vehicle_signature(self, camera: str, hints: Any) -> None:
        """Persist an updated signature after a confident identification."""
        update = getattr(hints, "vehicle_signature_update", None)
        if update is None or self._db is None:
            return
        try:
            await self._db.save_vehicle_signature(camera, update)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Vehicle signature save failed for %r", camera)

    def _assess_security(
        self,
        camera: str,
        vision_hints: Any,
        clip_timestamp: str,
        scene_deviation: float | None,
        frames_analyzed: int,
        target_frames: int,
    ) -> SecurityOutcome | None:
        """Run the deterministic security layer over this clip's evidence.

        ``None`` when there is nothing to reason about: the feature is off,
        the object-detection pipeline is off, or it found nothing. Every
        caller treats that as "analysis proceeds exactly as it did before
        this layer existed".
        """
        if not self._security_events_enabled or vision_hints is None:
            return None
        # Tracks *or* something actually heard. Requiring tracks meant a
        # clip with breaking glass on it and nobody in frame produced no
        # security event at all -- and "nobody in frame" describes the
        # dark, the far side of the house, and every clip on an install
        # that leaves object detection off. That is the case the
        # microphone exists for.
        #
        # Deliberately "would these sounds raise an event", not merely
        # "the audio stage heard something". Assessing a clip with no
        # tracks and only routine sounds on it concludes nothing, but it
        # still applies the unusual-hour factor and stores an
        # evidence-quality score where that same clip previously stored
        # zero -- so a night clip with a dog barking on it would go from a
        # risk of 0 to about 7, which is invisible at the default alert
        # threshold of 75 and is not invisible to someone who set it to 5.
        # Checking the rule the labels are about to be put through is the
        # only guard that is exactly as wide as the behaviour it enables.
        audio = getattr(vision_hints, "audio_tags", None)
        if not getattr(vision_hints, "tracks", None) and not detect_audio_events(
            audio.labels if audio else None
        ):
            return None
        try:
            return self._assess_security_locked(
                camera,
                vision_hints,
                clip_timestamp,
                scene_deviation,
                frames_analyzed,
                target_frames,
            )
        except Exception:
            # This layer is additive: every optional stage in the pipeline
            # reports itself unavailable rather than raising, and a bug in
            # the security rules must not turn a perfectly analyzable clip
            # into a failed, retried one. Logged loudly because unlike the
            # CV stages this is our own pure code — an exception here is a
            # defect, not a missing dependency.
            _LOGGER.exception(
                "Security assessment failed for camera %r; continuing without it",
                camera,
            )
            return None

    def _assess_security_locked(
        self,
        camera: str,
        vision_hints: Any,
        clip_timestamp: str,
        scene_deviation: float | None,
        frames_analyzed: int,
        target_frames: int,
    ) -> SecurityOutcome:
        """The assessment itself — see :meth:`_assess_security`'s guard."""
        posture = vision_hints.posture
        return assess_clip(
            ClipMeasurements(
                camera=camera,
                # `or []` because a clip can now reach here on audio alone,
                # and VisionHints.tracks is None until object detection has
                # run. ClipMeasurements wants a list, and everything
                # downstream iterates it.
                tracks=vision_hints.tracks or [],
                frame_interval=(
                    vision_hints.scan_interval or self._current_frame_interval
                ),
                frame_count=vision_hints.scan_frame_count,
                frames_analyzed=frames_analyzed,
                target_frames=target_frames,
                asset=vision_hints.asset,
                contact_touching=vision_hints.contact_touching,
                contact_track_id=vision_hints.contact_track_id,
                depth_similar=vision_hints.depth_similar,
                scene_deviation=scene_deviation,
                appearance_change=vision_hints.asset_appearance_change,
                posture_reaching=posture.reaching if posture else None,
                posture_arm_raised=posture.arm_raised if posture else None,
                posture_crouching=posture.crouching if posture else None,
                unavailable_sources=vision_hints.unavailable_sources,
                not_applicable_sources=vision_hints.not_applicable_sources,
                is_night=self._is_night(clip_timestamp),
                approved_person_recognized=self._face_match_is_unambiguous(
                    vision_hints
                ),
                audio_labels=(
                    vision_hints.audio_tags.labels if vision_hints.audio_tags else None
                ),
            )
        )

    @staticmethod
    def _is_night(clip_timestamp: str) -> bool:
        """True for the overnight hours, in the *local* timezone.

        Blink timestamps are UTC, but "is this an odd hour to be in someone's
        driveway" is inherently a local question — the same reasoning as
        :func:`prompt_segments.time_of_day_segment`, and the same hour
        boundaries, so the risk score and the prompt never disagree about
        whether it was night.
        """
        if not clip_timestamp:
            return False
        try:
            from datetime import datetime as _dt

            hour = _dt.fromisoformat(clip_timestamp).astimezone().hour
        except Exception:  # noqa: BLE001
            return False
        return hour < 5 or hour >= 20

    def _maybe_compute_zone_motion(
        self, thumbs: list[bytes] | None, camera: str
    ) -> float | None:
        """Only computed when protected-vehicle rules actually apply to
        *camera* (see :meth:`_car_protection_applies`) — the emitted
        ZONE MOTION prompt segment talks about "the protected vehicle's
        usual spot" (see :meth:`_zone_motion_segment`), so it must not fire
        just because a zone is configured while ``ai_car_description`` is
        still unset, matching every other car-zone code path in this class.

        *thumbs* are precomputed by :meth:`_maybe_compute_motion_thumbnails`.
        """
        car_zone = self._car_zones.get(camera)
        if car_zone and self._car_protection_applies(camera):
            return frame_motion.zone_motion_fraction(thumbs, car_zone)
        return None

    async def _maybe_compute_motion_thumbnails(
        self, frames: list[bytes], camera: str
    ) -> list[bytes] | None:
        """Precompute the grayscale thumbnails :meth:`_maybe_compute_zone_motion`
        and ``frame_motion.motion_trajectory_hint`` both need, once, so
        neither independently repeats the same PIL decode/grayscale/resize
        work for the same frame set — see ``frame_motion.grayscale_thumbnails``.
        Runs in a thread executor since this is CPU-bound, matching every
        CPU-bound stage in the ``vision`` package.

        Returns ``None`` without doing any work when neither caller would
        actually use the result (no configured car zone for *camera*, and
        too few frames for a trajectory hint) — the common case for most
        cameras — or if the decode itself fails; both callers already
        treat ``None`` as "no hint available", matching their behavior
        before this shared precomputation existed.
        """
        zone_motion_possible = bool(
            self._car_zones.get(camera) and self._car_protection_applies(camera)
        )
        trajectory_possible = len(frames) >= frame_motion.MOTION_TRAJECTORY_MIN_FRAMES
        if not zone_motion_possible and not trajectory_possible:
            return None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, frame_motion.grayscale_thumbnails, frames
            )
        except Exception:  # noqa: BLE001
            return None

    async def _generate_response(self, frames: list[bytes], prompt: str) -> str:
        if self._frame_strategy == "sequential":
            response, escalation_frame = await self._analyze_sequentially(
                frames, prompt
            )
            if escalation_frame is not None:
                response = await self._maybe_escalate(
                    [escalation_frame], prompt, response
                )
            return response
        return await self._call_model_with_escalation(frames, prompt)

    async def _maybe_update_scene_baseline(
        self,
        camera: str,
        scene_thumbnail: list[float] | None,
        is_suspicious: bool,
        confidence: float,
    ) -> None:
        """Fold this clip's opening frame into the learned scene baseline.

        Skipped for a *confident* suspicious call — a low-confidence hedge
        (often just the scene-deviation hint above making the model cautious
        about a persistent but benign change, e.g. a car parked overnight or
        trash put out for collection) must not block the baseline from ever
        learning that new normal, or the same hint keeps firing on every
        future clip. A confidently suspicious clip is still withheld so a
        genuine intruder is never absorbed into "what's normal here".
        """
        confident_suspicious = (
            is_suspicious
            and confidence >= _SCENE_BASELINE_SUSPICION_CONFIDENCE_THRESHOLD
        )
        if (
            scene_thumbnail is not None
            and self._db is not None
            and not confident_suspicious
        ):
            await self._db.record_scene_baseline(camera, scene_thumbnail)

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
          neither tier's "clear" is trusted alone here — only agreement is.
          If either tier says suspicious, that verdict wins, symmetrically:
          a tier-2 suspicious catch overrides a tier-1 "clear" just as much
          as a tier-1 suspicious catch survives a tier-2 "clear" — this
          mode exists specifically so one tier's disagreement can never
          silently erase the other's catch.
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
        # tier2 never goes through its own _reset_analysis_state() (that only
        # runs for an analyzer's own _analyze_clip_locked()), so its
        # _current_camera would otherwise stay unset/stale. Providers whose
        # _call_model() reads _current_camera to decide car-protection
        # rules (Moondream cloud/local) would then silently evaluate
        # car_applies for the wrong (or no) camera on every escalated call —
        # propagate it explicitly so tier-2 sees the same camera tier-1 did.
        tier2._current_camera = camera
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

        if high_recall:
            # Asset-protection camera: "if either tier says suspicious,
            # that verdict wins" applies symmetrically, not just to the
            # "tier 1 said clear" direction it was originally written for.
            # A tier-1 suspicious catch on a protected-vehicle camera must
            # not be silently erased just because tier 2 happened to
            # disagree — that would be exactly the missed contact/
            # proximity event this whole high-recall mode exists to catch,
            # the same failure mode already guarded against below when the
            # tiers disagree the other way around. Agreement (both
            # suspicious or both clear) keeps tier 1's own response rather
            # than swap in an equivalent one and lose its already-recorded
            # frame/description pairing.
            tier2_suspicious, _, _ = self._try_parse_json(escalated)
            if suspicious == tier2_suspicious:
                return response
            return escalated if tier2_suspicious else response

        return escalated

    # ------------------------------------------------------------------
    # Frame extraction (shared by all providers)
    # ------------------------------------------------------------------

    async def extract_frames(
        self, clip_path: str, clip_duration: float = 0.0
    ) -> list[bytes]:
        """Extract JPEG frames from an MP4 using ffmpeg.

        The extraction count is the larger of the strategy's own oversampling
        target (2× max_frames for the motion-ranking strategies, else
        max_frames) and however many frames are needed to cover the whole
        clip at ``frame_interval`` spacing.  That second term is what keeps a
        60-second Blink clip from only being sampled in its first few seconds
        — ffmpeg naturally emits fewer frames than requested for shorter
        clips, so this is a safe upper bound rather than a fixed count.
        Down-selection to the frames actually sent to the AI happens
        afterwards in :meth:`_select_best_frames` /
        :meth:`_select_uniform_frames`.

        *clip_duration* is the clip's real length when the caller knows it.
        It does two things.  It sets the span to cover, which without it is
        :data:`_MAX_CLIP_COVERAGE_SECONDS` — Blink's own recording ceiling,
        and so right for every clip this add-on downloads itself.  And it
        lets :meth:`_extraction_interval` tighten the spacing on a clip too
        short to fill the pool at the configured one.  Capped at
        :data:`_MAX_EXTRACTED_FRAMES` so a pathologically long file cannot
        turn one clip's analysis into a thousand PIL decodes.
        """
        base_count = self._extraction_pool_size()
        interval = self._extraction_interval(clip_duration)
        coverage_seconds = max(_MAX_CLIP_COVERAGE_SECONDS, clip_duration)
        coverage_count = math.ceil(coverage_seconds / interval)
        extract_count = min(max(base_count, coverage_count), _MAX_EXTRACTED_FRAMES)
        cmd = [
            "ffmpeg",
            # Without these, ffmpeg's multi-line version/build banner is the
            # first thing on stderr, and the truncated copy captured below
            # on failure contains nothing but that banner — the actual error
            # never reaches the log. Matches the other ffmpeg call sites
            # (downloader.py, live_view.py).
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            clip_path,
            "-vf",
            f"fps=1/{interval},scale={ANALYSIS_FRAME_WIDTH}:-1",
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
        except TimeoutError:
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
                format_ffmpeg_error(stderr),
            )
            return []

        return split_jpeg_frames(stdout or b"")

    def _extraction_pool_size(self) -> int:
        """How many candidate frames the configured strategy wants to rank.

        The motion-ranking strategies oversample so there is something to
        choose between; the others only ever need the budget itself.
        """
        return (
            self._max_frames * 2
            if self._frame_strategy in ("smart", "adaptive")
            else self._max_frames
        )

    def _extraction_interval(self, clip_duration: float) -> float:
        """Seconds between extracted frames, tightened for a short clip.

        ``frame_interval`` is a *maximum* spacing, not a fixed one.
        ffmpeg's ``fps`` filter can only emit ``ceil(duration / interval)``
        frames, so on a clip shorter than the pool the strategy asked for,
        the configured spacing quietly caps the candidate pool below the
        budget.  At the defaults that starts at 20 seconds and gets worse
        all the way down: a 10-second clip yields exactly 5 candidates for
        a 5-frame budget, leaving ``smart`` and ``adaptive`` nothing to
        choose between and making all three strategies behave identically,
        and a 5-second clip sends 3 frames when ``ai_max_frames`` asked for
        5.  Blink clips are commonly 10-20 seconds long, so that is the
        ordinary case rather than a corner of it.

        So when the clip is too short to supply the pool at the configured
        spacing, sample finer until it can — never coarser, and never finer
        than :data:`_MIN_FRAME_INTERVAL` (or the configured spacing itself,
        whichever is smaller, so a caller asking for something finer than
        the floor still gets it).  How many frames are *sent* to the AI is
        decided separately by :meth:`_target_frame_count` and is unchanged,
        so this costs nothing extra under every strategy but ``sequential``
        — which sends one request per frame, and on a clip this short was
        sending fewer than the configured budget.

        Returns ``frame_interval`` unchanged when the duration is unknown,
        which keeps every caller that has no metadata to offer exactly
        where it was.
        """
        pool = self._extraction_pool_size()
        if (
            clip_duration <= 0
            or math.ceil(clip_duration / self._frame_interval) >= pool
        ):
            return self._frame_interval
        floor = min(_MIN_FRAME_INTERVAL, self._frame_interval)
        return max(floor, clip_duration / pool)

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
    def _select_best_frames(
        frames: list[bytes],
        target_count: int,
        zone_box: tuple[float, float, float, float] | None = None,
        prefer_event: bool = False,
    ) -> list[bytes]:
        """Pick the *target_count* most informative frames using motion scoring.

        *prefer_event* selects the ``adaptive`` strategy's concentration on
        the clip's busiest stretch instead of the default spread across the
        whole timeline — see :meth:`_select_frames_around_event`. The motion
        diffing is identical either way; only what is done with the scores
        differs, which is why the choice lands here rather than in a second
        extraction path.

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

        *zone_box* (a normalized 0-1 ``x1, y1, x2, y2`` rectangle — see
        :meth:`_downselect_frames`), when given, restricts the motion
        score each frame pair is ranked by to pixels inside that zone
        instead of the whole frame. Without this, a camera that also sees
        a busy street can let background traffic dominate every
        candidate's raw motion score, starving the down-selected set of
        the frame(s) actually showing activity at the protected vehicle —
        this makes selection agree with what :func:`_build_prompt`'s ZONE
        MOTION hint already tells the model about *after* selection has
        already happened.

        Falls back to even-spaced selection if PIL is unavailable.
        """
        if len(frames) <= target_count:
            return frames

        try:
            diffs = frame_motion.frame_motion_diffs(frames, zone_box)
            if prefer_event:
                return BaseAnalyzer._select_frames_around_event(
                    frames, diffs, target_count
                )
            return BaseAnalyzer._select_frames_by_motion(frames, diffs, target_count)
        except Exception:  # noqa: BLE001
            # PIL unavailable or processing error — fall back to even spacing
            # anchored at first and last to preserve entry/exit coverage
            return BaseAnalyzer._select_frames_evenly_spaced(frames, target_count)

    @staticmethod
    def _select_frames_by_motion(
        frames: list[bytes], diffs: list[float], target_count: int
    ) -> list[bytes]:
        """Select frames by motion score, spreading picks across the timeline.

        Includes the peak-motion frame, the first frame (scene entry) and
        the last frame (exit), then fills remaining slots with the next
        highest-motion frames, requiring each new pick be at least min_gap
        frames from every frame already selected, then relaxing that
        constraint only if it left slots unfilled (small pools/short clips).

        Those three are taken in that order, and only while the budget
        lasts. ``ai_max_frames`` accepts values as low as 1, and adding
        all three unconditionally returned *three* frames to a caller
        asking for one — a third more images than requested on a paid
        provider, on every clip. The peak comes first because it is where
        the event is: a one-frame budget spent on the scene-entry frame
        is usually a photograph of an empty driveway. At a budget of
        three or more all three are included regardless of order, so this
        changes nothing for any ordinary configuration.
        """
        selected: set[int] = set()
        peak = max(range(len(diffs)), key=lambda i: diffs[i]) + 1 if diffs else None
        for idx in (peak, 0, len(frames) - 1):
            if len(selected) >= target_count:
                break
            if idx is not None:
                selected.add(idx)

        ranked = sorted(
            ((diffs[i], i + 1) for i in range(len(diffs)) if (i + 1) not in selected),
            reverse=True,
        )
        selected = BaseAnalyzer._fill_by_motion(
            selected, ranked, target_count, min_gap=max(1, len(frames) // target_count)
        )
        return [frames[i] for i in sorted(selected)]

    @staticmethod
    def _fill_by_motion(
        selected: set[int],
        ranked: list[tuple[float, int]],
        target_count: int,
        min_gap: int,
    ) -> set[int]:
        """Top *selected* up to *target_count* from the highest-motion frames.

        Two passes over the same ranking. The first requires every new
        pick to sit at least *min_gap* from everything already chosen, so
        the result spreads across the clip's timeline rather than
        clustering on one burst — the top few frames by raw pixel delta
        are often consecutive, three views of the same door swinging open,
        which spends the budget on near-duplicates. The second pass drops
        that requirement, and runs only if the first left slots unfilled,
        which is what happens on a short clip or a small pool where no
        candidate can satisfy the gap.
        """
        for _, index in ranked:
            if len(selected) >= target_count:
                return selected
            if all(abs(index - chosen) >= min_gap for chosen in selected):
                selected.add(index)
        for _, index in ranked:
            if len(selected) >= target_count:
                break
            selected.add(index)
        return selected

    @staticmethod
    def _motion_window(diffs: list[float]) -> tuple[int, int] | None:
        """The narrowest run of frames holding :data:`_EVENT_MOTION_SHARE` of
        the clip's motion, as inclusive *frame* indices, or ``None``.

        ``None`` means "no single localised event here", which is the
        answer for a clip whose motion is spread evenly (wind in a tree,
        a slow camera pan, rain) and for one containing two separate
        bursts — the narrowest run covering both of those also covers the
        dead stretch between them, so it fails the width test below and
        the caller falls back to ordinary motion selection rather than
        emptying the frame budget into the gap.
        """
        total = sum(diffs)
        if total <= 0:
            return None
        needed = total * _EVENT_MOTION_SHARE
        # Seeded with the whole span rather than None. Once there is any
        # motion at all the whole clip trivially holds `needed` of it, so
        # the loop below can only ever narrow this — a "nothing found"
        # branch here would be unreachable, and unreachable branches are
        # worse than no branch.
        best = (0, len(diffs) - 1)
        window = 0.0
        start = 0
        for end, value in enumerate(diffs):
            window += value
            while window - diffs[start] >= needed:
                window -= diffs[start]
                start += 1
            if window >= needed and end - start < best[1] - best[0]:
                best = (start, end)
        # A diff at index i describes the change between frames i and i+1,
        # so the run of *frames* it covers is one wider.
        return best[0], best[1] + 1

    @staticmethod
    def _select_frames_around_event(
        frames: list[bytes], diffs: list[float], target_count: int
    ) -> list[bytes]:
        """Spend the frame budget on the clip's one busiest stretch.

        The ``adaptive`` strategy. ``smart`` deliberately spreads its picks
        across the whole timeline, enforcing a minimum gap so they cannot
        cluster — which is right for reading a clip as a story, and wrong
        when the thing worth seeing lasted three seconds. Measured on a
        60-second clip sampled every 2 seconds with a six-second event in
        it, ``smart`` sends exactly one frame of that event whether the
        budget is 5 or 10; doubling the budget on a long clip buys no
        extra look at what actually happened.

        This picks the peak-motion frame, the first and last for context,
        then works outward from the peak through the event window before
        spending anything elsewhere. Leftover budget goes to whichever
        frames are furthest from everything already chosen, so a generous
        budget still ends up spread rather than piled up next to the peak.

        Falls back to :meth:`_select_frames_by_motion` — identically, not
        approximately — whenever there is no single concentrated event to
        aim at, so the strategy is never worse than ``smart``, only
        different when it has something to work with.
        """
        count = len(frames)
        if count <= target_count or not diffs or target_count <= 2:
            return BaseAnalyzer._select_frames_by_motion(frames, diffs, target_count)

        window = BaseAnalyzer._motion_window(diffs)
        if window is None:
            return BaseAnalyzer._select_frames_by_motion(frames, diffs, target_count)
        start, end = window
        if (end - start + 1) > count * _EVENT_MAX_WIDTH_SHARE:
            # Concentrated enough to be one event? If the motion needs most
            # of the clip to accumulate, there is nothing to adapt to.
            return BaseAnalyzer._select_frames_by_motion(frames, diffs, target_count)

        peak = max(range(len(diffs)), key=lambda i: diffs[i]) + 1
        preference = [peak, 0, count - 1]
        preference += sorted(range(start, end + 1), key=lambda i: abs(i - peak))

        chosen: list[int] = []
        seen: set[int] = set()
        for index in preference:
            if index not in seen:
                seen.add(index)
                chosen.append(index)
            if len(chosen) == target_count:
                break
        while len(chosen) < target_count:
            furthest = max(
                (i for i in range(count) if i not in seen),
                key=lambda i: min(abs(i - c) for c in chosen),
            )
            seen.add(furthest)
            chosen.append(furthest)
        return [frames[i] for i in sorted(chosen)]

    @staticmethod
    def _select_frames_evenly_spaced(
        frames: list[bytes], target_count: int
    ) -> list[bytes]:
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
        best = _MostAlarming()
        for frame in frames:
            response = await self._call_model([frame], prompt)
            if not response:
                continue
            suspicious, confidence, description = self._try_parse_json(response)
            if description:
                best.offer(suspicious, confidence, response, frame)
            else:
                best.offer_unranked(response, frame)
        return best.response, best.frame

    def base_prompt_for_camera(self, camera: str) -> str:
        """Return the camera-scoped analysis prompt with no per-clip context.

        A thin public wrapper around :meth:`_build_prompt` with every
        optional per-clip signal (anomaly score, scene deviation, vision
        hints, recent corrections, ...) left at its default — used where a
        representative "what would we normally ask this camera" question is
        needed without a specific clip to analyze, e.g. building Moondream
        fine-tuning training examples from stored human feedback.
        """
        return self._build_prompt(camera)

    # ------------------------------------------------------------------
    # Prompt caching (Anthropic + OpenAI)
    # ------------------------------------------------------------------

    def _prompt_cache_prefix(self) -> str:
        """The camera-scoped static portion of the analysis prompt (base
        prompt + camera context) — mirrors the first two segments
        ``_build_prompt`` unconditionally appends before any per-clip
        content, so this reconstructs (not re-derives from a shared call)
        exactly the same text.

        ``_call_model`` only ever sees the already-flattened prompt string
        ``_build_prompt`` returns, not the per-clip parameters that built
        it — restructuring that shared signature so every provider's
        ``_call_model`` could receive the pieces separately would touch
        all 6 providers' request-building for a benefit only the two
        caching ones can use. Reconstructing the known-static prefix here
        instead keeps prompt caching contained to those providers' own
        request construction, at the cost of only being able to cache this
        leading portion (not the car-protection/output-rules segments
        further down the prompt, which aren't contiguous with it) — still
        typically the largest single static block, since it's the full
        configured/per-camera ``ai_prompt`` text.

        Deliberately *not* extended to cover those trailing segments by
        reordering ``_build_prompt``. The two candidates are the
        protected-vehicle rules and the output rules, and neither is
        actually static: both are rendered differently depending on
        ``vehicle_absent``, a per-clip signal, so hoisting them would split
        the cache into two variants that alternate with whether the car
        happened to be in frame — while breaking the deliberate "security
        evidence immediately before the protected-vehicle rules" ordering
        that block documents, and moving OUTPUT RULES away from the end of
        the prompt.

        The practical consequence is that this prefix is around 1.4K tokens
        including the system message, and whether that is enough is the
        provider's decision, not ours. Anthropic publishes a per-model
        minimum: 1024 on Sonnet-class models and ``claude-opus-4-8``, 512 on
        ``claude-opus-5``, but 4096 on the default ``claude-haiku-4-5``.
        OpenAI publishes nothing usable for the models Chat Completions
        reaches; measured, ``gpt-5.4-nano`` needs just under 1,900 tokens
        before its first cache boundary, so the shipped prefix falls about
        450 short. ``_note_cache_usage`` reports it when a model turns out
        not to cache this prefix, rather than leaving it to be noticed on a
        billing dashboard.
        """
        camera = getattr(self, "_current_camera", "")
        return self._camera_prompts.get(
            camera, self._base_prompt
        ) + self._camera_context_segment(camera)

    #: Whether this provider has a prompt cache at all. Only the two that
    #: do put a ``cache=`` field on the per-clip summary line — on every
    #: other provider it would be a permanent ``0/0`` that says nothing.
    _supports_prompt_caching: bool = False

    @staticmethod
    def _api_error_code(exc: Exception) -> str:
        """The provider's own machine-readable error code, or "" if absent.

        Both SDKs hang the decoded JSON error body off the exception, but
        only OpenAI fills in a ``code``; Anthropic names the failure class
        in ``type`` instead, so that is the fallback. An HTTP error with no
        body at all — which is what an infrastructure failure in front of
        the API looks like, as opposed to a considered refusal from it —
        returns "".
        """
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return str(error.get("code") or error.get("type") or "")
        return ""

    @staticmethod
    def _api_error_message(exc: Exception) -> str:
        """The provider's human-readable error text, however it arrived."""
        return str(getattr(exc, "message", "") or exc)

    def _prompt_cache_key(self) -> str:
        """A stable cache-routing key for the current camera's prompt prefix.

        OpenAI keeps prompt-cache entries on individual machines, and on
        models before GPT-5.6 a request only reads an entry if it happens
        to land on the machine holding one; ``prompt_cache_key`` is the
        documented way to steer related requests to the same place. It is
        derived from the reusable prefix itself rather than from the camera
        name, which gets several things at once: it is stable for exactly
        as long as the prefix is (a re-worded prompt moves to a new key
        rather than reporting misses against the old one), cameras with
        different prompts route separately while cameras sharing one prompt
        share a key, and no camera name or other detail of the install is
        recoverable from it.

        Not the fix for an uncached prefix, and not claimed as one — see
        ``_build_openai_create_kwargs`` for what actually decides that.
        """
        digest = hashlib.sha256(self._prompt_cache_prefix().encode("utf-8")).hexdigest()
        return f"blink-clip-downloader-{digest[:16]}"

    def _note_cache_usage(self, read_tokens: int, write_tokens: int) -> None:
        """Record what the provider reported about its prompt cache.

        Both providers answer "this prompt was not cached" the same silent
        way — zero read *and* zero written, no error — and the reason is
        always the same: the reusable prefix is too short to be cacheable on
        this model. Anthropic publishes the figure per model (4096 tokens on
        the default ``claude-haiku-4-5``); OpenAI does not, and places the
        boundary itself at fixed intervals, measured at just under 1,900
        tokens for the first one on ``gpt-5.4-nano``. Left alone this is
        invisible short of reading a billing dashboard, so it gets said
        once, after enough calls that a single cold start can't trigger it.
        """
        self._last_cache_read_tokens = max(0, read_tokens)
        self._last_cache_write_tokens = max(0, write_tokens)
        if read_tokens or write_tokens:
            self._cache_inert_calls = 0
            return
        self._cache_inert_calls += 1
        if self._cache_inert_reported or self._cache_inert_calls < _CACHE_INERT_CALLS:
            return
        self._cache_inert_reported = True
        _LOGGER.info(
            "Prompt caching is having no effect on %s/%s: %d consecutive "
            "request(s) reported neither a cached nor a cache-written token. "
            "The reusable part of the prompt — the ai_prompt text plus this "
            "camera's description, about %d characters — is below the length "
            "this model can cache, so every request is paying full price for "
            "it. A longer ai_prompt, or a model with a lower minimum, is what "
            "changes that; nothing is otherwise wrong.",
            self.provider_name,
            self.model_name(),
            self._cache_inert_calls,
            len(self._prompt_cache_prefix()),
        )

    def _cache_summary_token(self) -> str:
        """``cache=`` field for the per-clip summary line, or "" if N/A."""
        if not self._supports_prompt_caching:
            return ""
        return (
            f" cache={self._last_cache_read_tokens}r/{self._last_cache_write_tokens}w"
        )

    def _split_cache_prefix(self, prompt: str) -> tuple[str, str]:
        """Split *prompt* into its cacheable static prefix and per-clip tail.

        Returns ``("", prompt)`` when the prompt does not actually start
        with the reconstructed prefix (it always will in real use — see
        :meth:`_prompt_cache_prefix` for why this cannot be verified more
        directly — but a test double or an unexpected future prompt shape
        safely falls back to one flat, uncached block), so callers can
        treat that as "no split available" rather than special-casing it.
        """
        prefix = self._prompt_cache_prefix()
        if prefix and prompt.startswith(prefix) and len(prefix) < len(prompt):
            return prefix, prompt[len(prefix) :]
        return "", prompt

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
        security: SecurityOutcome | None = None,
    ) -> str:
        """Build a rich analysis prompt with camera context, temporal context,
        anomaly alert, scene-baseline signal, movement hint, recent human
        corrections, zone-motion evidence, short-event hint, optional
        computer-vision pipeline hints, structured security evidence, and
        asset-protection distance rules."""
        base = self._camera_prompts.get(camera, self._base_prompt)
        parts = [base, self._camera_context_segment(camera)]

        time_segment = prompt_segments.time_of_day_segment(clip_timestamp)
        if time_segment:
            parts.append(time_segment)

        anomaly_segment = prompt_segments.anomaly_alert_segment(anomaly_score)
        if anomaly_segment:
            parts.append(anomaly_segment)

        short_event_segment = prompt_segments.short_event_segment(clip_duration)
        if short_event_segment:
            parts.append(short_event_segment)

        # Protected vehicle applicability is needed here (as well as below)
        # to gate the scene-baseline "favor calm" framing immediately after —
        # see that block's comment for why.
        car_applies = self._car_protection_applies(camera)

        scene_segment = prompt_segments.scene_baseline_segment(
            scene_deviation, car_applies
        )
        if scene_segment:
            parts.append(scene_segment)

        trajectory_segment = prompt_segments.trajectory_segment(trajectory_hint)
        if trajectory_segment:
            parts.append(trajectory_segment)

        corrections_segment = prompt_segments.corrections_segment(recent_corrections)
        if corrections_segment:
            parts.append(corrections_segment)

        zone_segment = prompt_segments.zone_motion_segment(
            zone_motion_fraction,
            subject_at_asset=security is not None
            and any(e.event_type in _AT_ASSET_EVENTS for e in security.events),
        )
        if zone_segment:
            parts.append(zone_segment)

        parts.extend(prompt_segments.vision_hint_segments(vision_hints))

        # Structured security evidence: which vehicle is protected, what the
        # deterministic layer observed, and how good the evidence behind it
        # was. Placed immediately before the protected-vehicle rules so the
        # model reads "here is which car is yours" right before "here is what
        # counts as too close to it".
        if security is not None:
            parts.extend(security.prompt_segments)

        # Protected vehicle with precise distance rules — only for cameras that
        # can see the car (all cameras when car_cameras is empty, otherwise only
        # the cameras explicitly listed in car_cameras). car_applies was
        # already computed above, before the scene-baseline block.
        vehicle_absent = prompt_segments.vehicle_absent(vision_hints)
        car_segment = self._car_protection_segment(
            camera, car_applies, vehicle_absent=vehicle_absent
        )
        if car_segment:
            parts.append(car_segment)

        # Same absence check: the OUTPUT RULES example phrase talks about
        # distance from "the car", which is the wrong thing to model a
        # description on when the protected vehicle is not in frame.
        parts.append(
            prompt_segments.output_rules_segment(
                camera, car_applies and not vehicle_absent
            )
        )

        return "".join(parts)

    def _camera_context_segment(self, camera: str) -> str:
        """Camera location/purpose framing, or a plain camera name fallback.

        Explicitly ties the description to what counts as normal here, so
        e.g. a mailbox camera cares about someone lingering at the box while
        a backyard camera treats wildlife as routine — the same activity can
        be normal on one camera and worth flagging on another.
        """
        camera_desc = self._camera_descriptions.get(camera, "")
        if camera_desc:
            return (
                f"\n\nCamera location and purpose — {camera}: {camera_desc}\n"
                "Use this specific purpose to calibrate what is normal versus "
                "suspicious for this camera — what it is meant to watch for is "
                "what deserves scrutiny; everything else on this camera is routine."
            )
        return f"\n\nCamera: {camera}"

    def _car_protection_segment(
        self, camera: str, car_applies: bool, vehicle_absent: bool = False
    ) -> str | None:
        """Protected-vehicle distance rules, or a "this camera can't see it"
        note, or None when no protected vehicle is configured at all.

        *vehicle_absent* suppresses the distance rules entirely: when
        identification has concluded the protected vehicle is not in these
        frames (see ``AssetLocation.ZONE_ABSENT``), the WHICH VEHICLE
        section immediately above already says so, and following it with
        "apply these rules to the vehicle matching this description" hands
        the model two contradictory instructions about a car that isn't
        there.
        """
        if vehicle_absent:
            return None
        if car_applies:
            return (
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
        if self._car_description and self._car_cameras:
            # A protected vehicle exists elsewhere on the property, but this camera
            # is not one of the cameras configured to see it (is_car_camera unchecked
            # for this camera in the AI tab). State this explicitly so the model
            # doesn't borrow car/driveway language meant for a different camera.
            return (
                f"\n\nThis camera ({camera}) does not view the protected vehicle. "
                "Do not mention a car, driveway, or vehicle distance in your description "
                "unless a vehicle is clearly visible in the frames you were given."
            )
        return None

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
                # Defensive only: every piece re.split produces here either ends
                # in one of [.!?] (required by the pattern's lookbehind, so it
                # can't strip to empty) or is the final leftover, which can't be
                # all-whitespace either since `text` was already stripped above.
                # Verified by exhaustively brute-forcing every string up to
                # length 6 over {.!?, space/tab/newline, a letter} with no
                # empty piece found.
                continue  # pragma: no cover
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

        raw_suspicious = obj.get("suspicious", False)
        if isinstance(raw_suspicious, str):
            # A looser vision model (Ollama-local, Moondream) can emit
            # "suspicious": "false" as a JSON *string* rather than a
            # boolean. bool("false") is True in Python, which would flip a
            # model's clearly-intended "not suspicious" into a false
            # positive. Only the string "true" (case-insensitive) counts.
            suspicious = raw_suspicious.strip().lower() == "true"
        else:
            suspicious = bool(raw_suspicious)
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
