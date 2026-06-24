"""AI video analysis via ffmpeg frame extraction and Ollama vision models."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

_OLLAMA_TIMEOUT = aiohttp.ClientTimeout(total=120)
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=10)


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
        }


class ClipAnalyzer:
    """Extracts frames from clips and sends them to Ollama for analysis."""

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
        self._ollama_url = ollama_url.rstrip("/")
        self._model = model
        self._base_prompt = prompt
        self._car_description = car_description
        self._max_frames = max_frames
        self._frame_interval = frame_interval
        self._suspicious_keywords = [k.lower() for k in (suspicious_keywords or [])]
        self._session: aiohttp.ClientSession | None = None

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

    async def analyze_clip(
        self, clip_path: str, clip_id: str, camera: str
    ) -> AnalysisResult:
        """Full analysis pipeline: extract frames → call Ollama → parse."""
        from datetime import datetime, timezone

        start = time.monotonic()

        frames = await self.extract_frames(clip_path)
        if not frames:
            return AnalysisResult(
                clip_id=clip_id,
                camera=camera,
                model=self._model,
                response_text="",
                is_suspicious=False,
                confidence=0.0,
                summary="No frames could be extracted",
                frame_count=0,
                analysis_duration=time.monotonic() - start,
                analyzed_at=datetime.now(timezone.utc).isoformat(),
            )

        prompt = self._build_prompt(camera)
        response = await self.call_ollama(frames, prompt)
        is_suspicious, confidence, summary = self.parse_response(response)

        return AnalysisResult(
            clip_id=clip_id,
            camera=camera,
            model=self._model,
            response_text=response,
            is_suspicious=is_suspicious,
            confidence=confidence,
            summary=summary,
            frame_count=len(frames),
            analysis_duration=time.monotonic() - start,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
        )

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
        """Fetch available models from Ollama."""
        try:
            session = await self._get_session()
            async with session.get(
                f"{self._ollama_url}/api/tags", timeout=_HEALTH_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("models", [])
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
            json.JSONDecodeError,
        ):
            return []

    # ------------------------------------------------------------------
    # Frame extraction
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
    # Ollama API
    # ------------------------------------------------------------------

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
                timeout=_OLLAMA_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Ollama returned HTTP %d", resp.status)
                    return ""
                data = await resp.json()
                return str(data.get("response", ""))
        except asyncio.TimeoutError:
            _LOGGER.warning("Ollama request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Ollama request failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Response parsing
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
        """Parse Ollama response into (is_suspicious, confidence, summary).

        Tries JSON parsing first, falls back to keyword matching.
        """
        if not response:
            return False, 0.0, ""

        # Try to extract JSON from the response
        is_suspicious, confidence, summary = self._try_parse_json(response)
        if summary:
            return is_suspicious, confidence, summary

        # Fallback: keyword matching
        lower = response.lower()
        matched = [k for k in self._suspicious_keywords if k in lower]
        is_suspicious = len(matched) > 0
        confidence = min(1.0, len(matched) * 0.3) if matched else 0.1

        # Use the first ~200 chars as summary
        summary = response[:200].strip()
        if len(response) > 200:
            summary += "…"

        return is_suspicious, confidence, summary

    @staticmethod
    def _try_parse_json(response: str) -> tuple[bool, float, str]:
        """Attempt to extract a JSON object from the response."""
        # Find JSON-like content
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
