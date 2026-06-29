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
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_API_TIMEOUT = aiohttp.ClientTimeout(total=120)
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=10)

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
# Source: https://platform.openai.com/docs/pricing
_OPENAI_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "o1-mini": (3.00, 12.00),
    "o1": (15.00, 60.00),
    "o3": (10.00, 40.00),
    "gpt-4-turbo": (10.00, 30.00),
}

# Vision-capable OpenAI model ID prefixes (checked as substring of model id).
_OPENAI_VISION_PREFIXES: frozenset[str] = frozenset(
    {
        "gpt-4o",
        "gpt-4-turbo",
        "gpt-4-vision",
        "gpt-4.1",
        "o1",
        "o3",
        "o4-mini",
    }
)

# Fallback model list when the OpenAI API cannot be reached.
_OPENAI_FALLBACK_MODELS: list[dict] = [
    {
        "name": "gpt-4o",
        "display_name": "GPT-4o ($2.50/$10 per 1M tokens)",
    },
    {
        "name": "gpt-4o-mini",
        "display_name": "GPT-4o mini — Best Value ($0.15/$0.60 per 1M tokens)",
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
        "display_name": "GPT-4.1 nano — Lowest Cost ($0.10/$0.40 per 1M tokens)",
    },
    {
        "name": "gpt-4-turbo",
        "display_name": "GPT-4 Turbo ($10/$30 per 1M tokens)",
    },
]


def is_openai_vision_model(model_id: str) -> bool:
    """Return True if an OpenAI model id looks vision-capable."""
    lower = model_id.lower()
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
        # Token counts set by _call_model() implementations that support them.
        # Reset to 0 at the start of each analyze_clip() call.
        self._last_prompt_tokens: int = 0
        self._last_completion_tokens: int = 0

    def update_camera_descriptions(self, descriptions: dict[str, str]) -> None:
        """Update per-camera descriptions at runtime without restart."""
        self._camera_descriptions = descriptions

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
    ) -> AnalysisResult:
        """Full pipeline: extract frames → select best → call AI → parse response."""
        from datetime import datetime, timezone

        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        # Store camera name so provider subclasses can access it in _call_model.
        # Safe under asyncio's single-threaded event loop — only one analyze_clip
        # runs at a time per provider instance.
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

        # Smart frame selection: oversample then pick entry/peak/exit frames
        if self._frame_strategy == "smart" and len(frames) > self._max_frames:
            frames = self._select_best_frames(frames, self._max_frames)

        prompt = self._build_prompt(
            camera,
            anomaly_score=anomaly_score,
            clip_timestamp=clip_timestamp,
        )

        if self._frame_strategy == "sequential":
            response = await self._analyze_sequentially(frames, prompt)
        else:
            response = await self._call_model(frames, prompt)

        is_suspicious, confidence, summary = self.parse_response(response)

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
        )

    # ------------------------------------------------------------------
    # Frame extraction (shared by all providers)
    # ------------------------------------------------------------------

    async def extract_frames(self, clip_path: str) -> list[bytes]:
        """Extract JPEG frames from an MP4 using ffmpeg.

        In ``smart`` mode, 2× max_frames are extracted so that
        :meth:`_select_best_frames` has enough material to pick the entry,
        peak-motion, and exit frames.  In all other modes exactly
        ``max_frames`` are extracted.
        """
        # Oversample in smart mode so _select_best_frames has enough to work with
        extract_count = (
            self._max_frames * 2
            if self._frame_strategy == "smart"
            else self._max_frames
        )
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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg timed out for %s", clip_path)
            return []
        except OSError as exc:
            _LOGGER.warning("ffmpeg not available: %s", exc)
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

    @staticmethod
    def _select_best_frames(frames: list[bytes], target_count: int) -> list[bytes]:
        """Pick the *target_count* most informative frames using motion scoring.

        Strategy:
        - Always include the first frame (scene entry) and last frame (exit).
        - Find the frame with the largest inter-frame pixel diff (peak motion).
        - Fill remaining slots with the next highest-motion frames.

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

            # Fill remaining slots by motion score
            remaining = target_count - len(selected)
            ranked = sorted(
                (
                    (diffs[i], i + 1)
                    for i in range(len(diffs))
                    if (i + 1) not in selected
                ),
                reverse=True,
            )
            for _, idx in ranked[:remaining]:
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

    async def _analyze_sequentially(self, frames: list[bytes], prompt: str) -> str:
        """Analyse frames one at a time and return the most alarming response.

        Each frame is sent to the AI individually via :meth:`_call_model`.
        The result with the highest concern level (suspicious > non-suspicious;
        higher confidence when tied) is returned.  This mode is especially
        effective for providers that perform better on single images than on
        batches (e.g. Ollama with small models, or when per-frame clarity
        matters more than temporal context).
        """
        best_response = ""
        best_suspicious = False
        best_confidence = 0.0

        for frame in frames:
            response = await self._call_model([frame], prompt)
            if not response:
                continue
            suspicious, confidence, desc = self._try_parse_json(response)
            if not desc:
                if not best_response:
                    best_response = response
                continue
            if (
                not best_response
                or (suspicious and not best_suspicious)
                or (suspicious == best_suspicious and confidence > best_confidence)
            ):
                best_response = response
                best_suspicious = suspicious
                best_confidence = confidence

        return best_response

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
    ) -> str:
        """Build a rich analysis prompt with camera context, temporal context,
        anomaly alert, and asset-protection distance rules."""
        base = self._camera_prompts.get(camera, self._base_prompt)
        parts = [base]

        # Camera location / purpose
        camera_desc = self._camera_descriptions.get(camera, "")
        if camera_desc:
            parts.append(f"\n\nCamera location and purpose — {camera}: {camera_desc}")
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

        # Behavior anomaly alert
        if anomaly_score >= 0.6:
            parts.append(
                f"\n\nBEHAVIOR ALERT: This event is statistically unusual for "
                f"this camera at this time (anomaly score {anomaly_score:.2f}/1.00). "
                "Apply heightened scrutiny to any persons or vehicles in the frame."
            )

        # Protected vehicle with precise distance rules — only for cameras that
        # can see the car (all cameras when car_cameras is empty, otherwise only
        # the cameras explicitly listed in car_cameras).
        car_applies = self._car_description and (
            not self._car_cameras or camera in self._car_cameras
        )
        if car_applies:
            parts.append(
                f"\n\nPROTECTED VEHICLE: {self._car_description}\n"
                "Apply these distance rules STRICTLY:\n"
                "• Person touching or within 1 foot of the vehicle: suspicious=true, confidence ≥0.8\n"
                "• Person 1–2 feet away AND facing or reaching toward the vehicle: suspicious=true, confidence ≥0.6\n"
                "• Person walking past more than 2 feet from the vehicle: suspicious=false\n"
                "• Person more than 5 feet from the vehicle: suspicious=false unless actively tampering\n"
                "Reference: a car door handle is about 4 feet off the ground; a typical car is about 6 feet wide.\n"
                "In your description, always include a natural-language distance estimate such as "
                "'right next to the car', 'about 2 feet from the driver door', or 'well away from the vehicle'."
            )

        # Ensure the description stays human-readable and never exposes internal data.
        parts.append(
            "\n\nOUTPUT RULES: The 'description' field must be written in plain English "
            "as if explaining to a homeowner what happened. "
            "Use natural phrases like 'standing about 2 feet from the car' or 'walking past the driveway'. "
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
            if is_suspicious and confidence == 0.0:
                lower = (summary + " " + response).lower()
                matched = [k for k in self._suspicious_keywords if k in lower]
                confidence = min(1.0, len(matched) * 0.3) if matched else 0.5
            return is_suspicious, confidence, summary

        # Fallback: keyword matching
        lower = response.lower()
        matched = [k for k in self._suspicious_keywords if k in lower]
        is_suspicious = len(matched) > 0
        confidence = min(1.0, len(matched) * 0.3) if matched else 0.1

        summary = response[:200].strip()
        if len(response) > 200:
            summary += "…"

        return is_suspicious, confidence, summary

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
        confidence = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
        description = str(obj.get("description", "") or "")
        return suspicious, confidence, description


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
            "system": (
                "You are a security camera analyst. "
                "You respond ONLY with a single valid JSON object and nothing else. "
                "Write the description field in plain English as if speaking to a homeowner. "
                "Never include technical terms such as 'bounding box', 'normalized', "
                "'frame percentage', 'spatial data', 'INTERNAL', or decimal coordinates "
                "in the description field."
            ),
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
        """Return False immediately if no API key is configured."""
        if not self._api_key:
            _LOGGER.warning("Ollama Cloud: no API key configured")
            return False
        return await super().health_check()


# ---------------------------------------------------------------------------
# Moondream Cloud provider
# ---------------------------------------------------------------------------

# Moondream Cloud pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://docs.moondream.ai/pricing/
_MOONDREAM_CLOUD_PRICING: tuple[float, float] = (0.30, 2.50)


class MoondreamCloudAnalyzer(BaseAnalyzer):
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
        )
        self._api_key = api_key
        self._finetune_model = finetune_model
        self._session: aiohttp.ClientSession | None = None

    @property
    def provider_name(self) -> str:
        return "moondream_cloud"

    def model_name(self) -> str:
        return self._finetune_model or self._MODEL_ID

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health_check(self) -> bool:
        """Return True if the API key is set and the cloud endpoint is reachable."""
        if not self._api_key:
            _LOGGER.warning("Moondream Cloud: no API key configured")
            return False
        try:
            session = await self._get_session()
            async with session.get(
                "https://api.moondream.ai/",
                timeout=_HEALTH_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                return resp.status < 500
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            _LOGGER.debug("Moondream Cloud health check failed: %s", exc)
            return False

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

    @staticmethod
    def _bbox_min_gap(
        boxes_a: list[dict[str, float]], boxes_b: list[dict[str, float]]
    ) -> float:
        """Return the minimum Euclidean gap between any pair of bounding boxes.

        0.0 means the boxes overlap; 1.0 means maximum separation.
        Uses normalised coordinates (0–1 relative to image width/height).
        """
        min_gap = 1.0
        for a in boxes_a:
            ax1, ay1 = a.get("x_min", 0.0), a.get("y_min", 0.0)
            ax2, ay2 = a.get("x_max", 1.0), a.get("y_max", 1.0)
            for b in boxes_b:
                bx1, by1 = b.get("x_min", 0.0), b.get("y_min", 0.0)
                bx2, by2 = b.get("x_max", 1.0), b.get("y_max", 1.0)
                x_gap = max(0.0, max(ax1, bx1) - min(ax2, bx2))
                y_gap = max(0.0, max(ay1, by1) - min(ay2, by2))
                gap = (x_gap**2 + y_gap**2) ** 0.5
                min_gap = min(min_gap, gap)
        return min_gap

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
        1. Run ``/detect`` for "person" — if no person found, skip the
           expensive ``/query`` and record a clear result for that frame.
        2. For car cameras: also run ``/detect`` for "car" and inject
           precise bounding-box proximity data into the query prompt so the
           model can make an evidence-based suspicious/clear decision.
        3. Run ``/query`` (with reasoning=True) on the augmented prompt and
           pick the most alarming result across all frames.

        Respects the 2 req/s rate limit with a 0.55 s delay between requests.
        """
        if not frames:
            return ""

        camera = getattr(self, "_current_camera", "")
        car_applies = bool(
            self._car_description
            and (not self._car_cameras or camera in self._car_cameras)
        )

        best_response = ""
        best_is_suspicious = False
        best_confidence = 0.0

        for i, frame in enumerate(frames):
            # ── Phase 1: detect persons ──────────────────────────────────
            persons = await self._detect_objects(frame, "person")
            await asyncio.sleep(0.55)

            if not persons:
                # No person in this frame — motion likely caused by something else.
                # Record a clear result and move on without spending query tokens.
                no_person_resp = (
                    '{"suspicious": false, "confidence": 0.9, '
                    '"description": "No person detected in this frame. '
                    'Motion likely caused by a vehicle, animal, or environmental factor."}'
                )
                if not best_response:
                    best_response = no_person_resp
                if i < len(frames) - 1:
                    await asyncio.sleep(0.55)
                continue

            # ── Phase 2: augment prompt with spatial context ─────────────
            augmented_prompt = prompt

            if car_applies:
                car_boxes = await self._detect_objects(frame, "car")
                await asyncio.sleep(0.55)

                if car_boxes:
                    gap = self._bbox_min_gap(persons, car_boxes)
                    if gap == 0.0:
                        spatial_note = (
                            "[INTERNAL PROXIMITY HINT — use for reasoning only, "
                            "do NOT copy this text into the description]: "
                            "The person appears to be directly touching or pressed "
                            "against the vehicle. Describe this as 'right next to' "
                            "or 'touching the car' in plain English."
                        )
                    elif gap < 0.05:
                        spatial_note = (
                            "[INTERNAL PROXIMITY HINT — use for reasoning only, "
                            "do NOT copy this text into the description]: "
                            "The person appears to be less than 1 foot from the vehicle. "
                            "Describe this as 'very close to the car' in plain English."
                        )
                    elif gap < 0.15:
                        spatial_note = (
                            "[INTERNAL PROXIMITY HINT — use for reasoning only, "
                            "do NOT copy this text into the description]: "
                            "The person appears to be roughly 1–3 feet from the vehicle. "
                            "This is close but not touching — do NOT flag as suspicious "
                            "unless actively reaching for or touching the car. "
                            "Describe this as 'a couple of feet from the car' in plain English."
                        )
                    else:
                        spatial_note = (
                            "[INTERNAL PROXIMITY HINT — use for reasoning only, "
                            "do NOT copy this text into the description]: "
                            "The person appears to be several feet from the vehicle — "
                            "this distance is NOT suspicious on its own. "
                            "Set suspicious=false unless there is other clear evidence of tampering. "
                            "Describe this as 'well away from the car' in plain English."
                        )
                    augmented_prompt += f"\n\n{spatial_note}"
                else:
                    # Car not detected in this frame — suppress car rules to
                    # avoid the model hallucinating car proximity.
                    augmented_prompt += (
                        "\n\n[INTERNAL PROXIMITY HINT — use for reasoning only]: "
                        "The protected vehicle is not visible in this frame. "
                        "Evaluate the person's behaviour based on the camera "
                        "location description only."
                    )

            else:
                # Non-car camera: inject person position to help AI.
                position_notes = []
                for j, person in enumerate(persons[:3], 1):
                    cx = (person.get("x_min", 0.0) + person.get("x_max", 1.0)) / 2
                    cy = (person.get("y_min", 0.0) + person.get("y_max", 1.0)) / 2
                    side = "left" if cx < 0.33 else ("right" if cx > 0.67 else "centre")
                    vert = "top" if cy < 0.33 else ("bottom" if cy > 0.67 else "middle")
                    position_notes.append(
                        f"Person {j} is in the {vert}-{side} of the frame"
                    )
                if position_notes:
                    augmented_prompt += (
                        "\n\n[INTERNAL POSITION HINT — use for reasoning only, "
                        "do NOT copy this text into the description]: "
                        + "; ".join(position_notes)
                        + "."
                    )

            # ── Phase 3: full query with augmented prompt ────────────────
            resp = await self._call_api_frame(frame, augmented_prompt)
            if not resp:
                if i < len(frames) - 1:
                    await asyncio.sleep(0.55)
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

            if i < len(frames) - 1:
                await asyncio.sleep(0.55)

        return best_response


# ---------------------------------------------------------------------------
# Moondream local provider (0.5B INT8, runs on-device)
# ---------------------------------------------------------------------------


class MoondreamLocalAnalyzer(BaseAnalyzer):
    """Runs the Moondream 0.5B INT8 model locally using the moondream package.

    The model (~430 MB) is downloaded from Moondream's servers on the first
    run and cached in the default moondream cache directory.  Subsequent
    starts reuse the cached file.  Inference runs in a thread executor so the
    asyncio event loop is never blocked.
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
        """Load / download the Moondream model (blocking — run in executor)."""
        import moondream as md  # noqa: PLC0415  # type: ignore[import-not-found]

        _LOGGER.info(
            "Loading Moondream local model '%s' "
            "(may download ~430 MB on first run — this can take a few minutes)",
            self._MODEL_ID,
        )
        self._md_model = md.vl(model=self._MODEL_ID)
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
            except ImportError:
                _LOGGER.error(
                    "moondream package is not installed. "
                    "Install it with: pip install moondream"
                )
                return False
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Failed to load Moondream local model: %s", exc)
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

    def _run_inference_sync(self, frame_bytes: bytes, prompt: str) -> str:
        """Run moondream inference synchronously (called from thread executor)."""
        import io  # noqa: PLC0415

        from PIL import Image  # noqa: PLC0415

        image = Image.open(io.BytesIO(frame_bytes))
        result = self._md_model.query(image, prompt)
        return str(result.get("answer", ""))

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Run the local model on all extracted frames and return the most alarming result."""
        if not await self._ensure_model():
            return ""
        if not frames:
            return ""

        best_response = ""
        best_is_suspicious = False
        best_confidence = 0.0

        for frame in frames:
            try:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None, self._run_inference_sync, frame, prompt
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Moondream local inference failed: %s", exc)
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
            _LOGGER.error("Moondream create_finetune failed: %s", exc)
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
        lower = self._model.lower()
        for prefix, pricing in _ANTHROPIC_MODEL_PRICING.items():
            if lower.startswith(prefix) or prefix in lower:
                return pricing
        return (3.00, 15.00)  # Sonnet-level fallback for unknown models

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
        """Return True when the API key is valid and the Anthropic API is reachable."""
        if not self._api_key:
            _LOGGER.warning("Anthropic: no API key configured")
            return False
        try:
            import anthropic as _anthropic  # noqa: PLC0415
        except ImportError:
            _LOGGER.error(
                "anthropic package is not installed. "
                "Install it with: pip install anthropic"
            )
            return False
        try:
            client = self._get_client()
            await client.models.list(limit=1)
            return True
        except _anthropic.AuthenticationError:
            _LOGGER.error(
                "Anthropic: invalid API key (AuthenticationError) — "
                "check your anthropic_api_key in the add-on settings"
            )
            return False
        except _anthropic.PermissionDeniedError:
            _LOGGER.error(
                "Anthropic: API key does not have permission to access this resource"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Anthropic health check failed: %s", exc)
            return False

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
                        inp, out = _ANTHROPIC_MODEL_PRICING.get(m.id, (3.00, 15.00))
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
            system_prompt = (
                "You are a security camera analyst. "
                "You respond ONLY with a single valid JSON object and nothing else. "
                "Write the description field in plain English as if speaking to a homeowner. "
                "Never include technical terms such as 'bounding box', 'normalized', "
                "'frame percentage', 'spatial data', 'INTERNAL', or decimal coordinates "
                "in the description field."
            )

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
            _LOGGER.error(
                "Anthropic: bad request (HTTP 400) — %s; "
                "check that the selected model supports vision",
                exc.message,
            )
            return ""
        except _anthropic.APIStatusError as exc:
            _LOGGER.error(
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

    Sends JPEG frames as base64 image_url content to a GPT-4o / GPT-4.1 model
    and extracts token usage for cost tracking.  Authentication errors are logged
    clearly so the user knows to check ``openai_api_key`` in the add-on settings.
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
        lower = self._model.lower()
        for prefix, pricing in _OPENAI_MODEL_PRICING.items():
            if lower.startswith(prefix) or prefix in lower:
                return pricing
        return (2.50, 10.00)  # gpt-4o level fallback for unknown models

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
        """Return True when the API key is valid and the OpenAI API is reachable."""
        if not self._api_key:
            _LOGGER.warning("OpenAI: no API key configured")
            return False
        try:
            import openai as _openai  # noqa: PLC0415  # type: ignore[import-not-found]
        except ImportError:
            _LOGGER.error(
                "openai package is not installed. Install it with: pip install openai"
            )
            return False
        try:
            client = self._get_client()
            await client.models.list()
            return True
        except _openai.AuthenticationError:
            _LOGGER.error(
                "OpenAI: invalid API key (AuthenticationError) — "
                "check your openai_api_key in the add-on settings"
            )
            return False
        except _openai.PermissionDeniedError:
            _LOGGER.error(
                "OpenAI: API key does not have permission to access this resource"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("OpenAI health check failed: %s", exc)
            return False

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
                        inp, out = _OPENAI_MODEL_PRICING.get(m.id, (2.50, 10.00))
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
        """Send frames to the OpenAI Chat Completions API."""
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
            system_content = (
                "You are a security camera analyst. "
                "You respond ONLY with a single valid JSON object and nothing else. "
                "Write the description field in plain English as if speaking to a homeowner. "
                "Never include technical terms such as 'bounding box', 'normalized', "
                "'frame percentage', 'spatial data', 'INTERNAL', or decimal coordinates "
                "in the description field."
            )
            messages_to_send: list[dict[str, Any]] = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": content},
            ]

            # json_object response format is supported on gpt-4o, gpt-4.1, and
            # gpt-4-turbo families — it guarantees the model returns valid JSON.
            model_lower = self._model.lower()
            supports_json_object = any(
                prefix in model_lower
                for prefix in ("gpt-4o", "gpt-4.1", "gpt-4-turbo", "o4-mini")
            )
            create_kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": messages_to_send,
                "max_tokens": 512,
            }
            if supports_json_object:
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
                self._model,
            )
            return ""
        except _openai.RateLimitError:
            _LOGGER.warning(
                "OpenAI: rate limit hit — API quota exceeded; "
                "analysis will resume on the next cycle"
            )
            return ""
        except _openai.BadRequestError as exc:
            _LOGGER.error(
                "OpenAI: bad request (HTTP 400) — %s; "
                "check that the selected model supports vision",
                exc.message,
            )
            return ""
        except _openai.APIStatusError as exc:
            _LOGGER.error("OpenAI API error HTTP %d: %s", exc.status_code, exc.message)
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
    """Return an analyzer for *ai_provider*, or ``None`` if configuration is invalid."""
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
        )

    _LOGGER.warning(
        "Unknown ai_provider %r; expected 'ollama', 'ollama_cloud', "
        "'moondream_cloud', 'moondream_local', 'anthropic', or 'openai'. "
        "AI analysis disabled.",
        ai_provider,
    )
    return None
