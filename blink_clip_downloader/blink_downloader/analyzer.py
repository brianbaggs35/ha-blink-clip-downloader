"""AI video analysis via ffmpeg frame extraction and pluggable AI providers.

Four providers are supported:
- ``ollama``          – local/LAN Ollama server with a vision-capable model
- ``ollama_cloud``    – Ollama Cloud API (api.ollama.com) with an API key
- ``moondream_cloud`` – Moondream Cloud API (api.moondream.ai)
- ``moondream_local`` – Moondream 0.5B model running on-device (no cloud)

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


def is_moondream_installed() -> bool:
    """Return True if the moondream package is importable."""
    try:
        import moondream  # noqa: PLC0415, F401  # type: ignore[import-not-found]

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
    ) -> None:
        self._base_prompt = prompt
        self._car_description = car_description
        self._max_frames = max_frames
        self._frame_interval = frame_interval
        self._suspicious_keywords = [k.lower() for k in (suspicious_keywords or [])]
        # Token counts set by _call_model() implementations that support them.
        # Reset to 0 at the start of each analyze_clip() call.
        self._last_prompt_tokens: int = 0
        self._last_completion_tokens: int = 0

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
        self, clip_path: str, clip_id: str, camera: str
    ) -> AnalysisResult:
        """Full pipeline: extract frames → call AI → parse response."""
        from datetime import datetime, timezone

        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
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
            )

        prompt = self._build_prompt(camera)
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
        )

    # ------------------------------------------------------------------
    # Frame extraction (shared by all providers)
    # ------------------------------------------------------------------

    async def extract_frames(self, clip_path: str) -> list[bytes]:
        """Extract JPEG frames from an MP4 using ffmpeg."""
        cmd = [
            "ffmpeg",
            "-i",
            clip_path,
            "-vf",
            f"fps=1/{self._frame_interval}",
            "-frames:v",
            str(self._max_frames),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
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

    def _build_prompt(self, camera: str) -> str:
        prompt = self._base_prompt
        if self._car_description:
            prompt += (
                f"\n\nThe homeowner's car is: {self._car_description}. "
                "Pay special attention to any suspicious activity near "
                "this vehicle."
            )
        prompt += f"\n\nCamera: {camera}"
        return prompt

    def parse_response(self, response: str) -> tuple[bool, float, str]:
        """Parse AI response into (is_suspicious, confidence, summary).

        Tries JSON parsing first, falls back to keyword matching.
        """
        if not response:
            return False, 0.0, ""

        is_suspicious, confidence, summary = self._try_parse_json(response)
        if summary:
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
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
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
            "prompt": prompt,
            "images": images,
            "stream": False,
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
    ) -> None:
        super().__init__(
            ollama_url=self._CLOUD_BASE_URL,
            model=model,
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
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


class MoondreamCloudAnalyzer(BaseAnalyzer):
    """Analyzes clips via the Moondream Cloud API (api.moondream.ai)."""

    _BASE_URL = "https://api.moondream.ai/v1"
    _MODEL_ID = "moondream-cloud"

    def __init__(
        self,
        api_key: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
        )
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    @property
    def provider_name(self) -> str:
        return "moondream_cloud"

    def model_name(self) -> str:
        return self._MODEL_ID

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

    async def fetch_models(self) -> list[dict[str, Any]]:
        return [{"name": self._MODEL_ID, "description": "Moondream Cloud API"}]

    async def _call_api_frame(self, frame: bytes, prompt: str) -> str:
        """Send a single JPEG frame to the Moondream Cloud /query endpoint."""
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "question": prompt,
            "stream": False,
        }
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
                return str(data.get("answer", ""))
        except asyncio.TimeoutError:
            _LOGGER.warning("Moondream Cloud request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Moondream Cloud request failed: %s", exc)
            return ""

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Analyse the middle frame via Moondream Cloud (one request per clip)."""
        if not frames:
            return ""
        # Use the middle frame as the most representative snapshot.
        # The Cloud API accepts one image per request; sending just one keeps
        # us well within the base-tier 2 req/s rate limit.
        mid = len(frames) // 2
        return await self._call_api_frame(frames[mid], prompt)


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
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
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
        """Run the local model on the middle frame of the clip."""
        if not await self._ensure_model():
            return ""
        if not frames:
            return ""

        mid = len(frames) // 2
        frame = frames[mid]

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._run_inference_sync, frame, prompt
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Moondream local inference failed: %s", exc)
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
    *,
    ollama_url: str = "",
    ollama_model: str = "",
    ollama_cloud_api_key: str = "",
    moondream_api_key: str = "",
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
        )

    if ai_provider == "moondream_local":
        return MoondreamLocalAnalyzer(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
        )

    _LOGGER.warning(
        "Unknown ai_provider %r; expected 'ollama', 'ollama_cloud', "
        "'moondream_cloud', or 'moondream_local'. AI analysis disabled.",
        ai_provider,
    )
    return None
