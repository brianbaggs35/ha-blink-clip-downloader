"""Ollama providers: a local/LAN server, and the hosted Ollama Cloud API.

``OllamaCloudAnalyzer`` subclasses ``ClipAnalyzer`` rather than
``BaseAnalyzer`` — the wire format is the same ``/api/chat`` request, only
the host, the bearer token and the health check differ.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import aiohttp

from ..model_catalog import _vision_model_score, is_vision_model
from .base import (
    _API_TIMEOUT,
    _HEALTH_TIMEOUT,
    _VISION_SYSTEM_PROMPT,
    BaseAnalyzer,
    SecurityLayerSettings,
)

_LOGGER = logging.getLogger(__name__)


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
        car_zones: dict[str, dict[str, Any]] | None = None,
        security_settings: SecurityLayerSettings | None = None,
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
            security_settings=security_settings,
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

    def _get_session(self) -> aiohttp.ClientSession:
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
            session = self._get_session()
            async with session.get(
                f"{self._ollama_url}/api/tags", timeout=_HEALTH_TIMEOUT
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, OSError):
            return False

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Fetch vision-capable models from Ollama, sorted best-first."""
        try:
            session = self._get_session()
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
        # TimeoutError is deliberately not listed: since 3.3 it derives from
        # OSError, so naming both catches nothing extra and reads as though
        # it did. A timed-out model listing still lands here.
        except (aiohttp.ClientError, OSError, json.JSONDecodeError):
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
            session = self._get_session()
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
        except TimeoutError:
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
        car_zones: dict[str, dict[str, Any]] | None = None,
        security_settings: SecurityLayerSettings | None = None,
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
            security_settings=security_settings,
        )
        self._api_key = api_key

    @property
    def provider_name(self) -> str:
        return "ollama_cloud"

    def _get_session(self) -> aiohttp.ClientSession:
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
