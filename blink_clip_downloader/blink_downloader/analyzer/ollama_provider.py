"""Ollama providers: a local/LAN server, and the hosted Ollama Cloud API.

``OllamaCloudAnalyzer`` subclasses ``ClipAnalyzer`` rather than
``BaseAnalyzer`` — the wire format is the same ``/api/chat`` request, only
the host, the bearer token and the health check differ.

Every provider module here carries the ``_provider`` suffix, and that is
load-bearing rather than cosmetic: pyright resolves a bare ``import <x>``
to a same-named file in the *importing file's own directory* when the real
package is not installed. A ``moondream.py`` doing ``import moondream``
therefore resolved to itself and failed CI — where the optional, GPU-only
``moondream`` package is absent — while passing on a machine that has it.
``tests/test_module_names.py`` guards against the collision coming back.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import aiohttp

from ..model_catalog import (
    _OLLAMA_CLOUD_RECOMMENDED_MODELS,
    _vision_model_score,
)
from .base import (
    _API_TIMEOUT,
    _HEALTH_TIMEOUT,
    _VISION_SYSTEM_PROMPT,
    BaseAnalyzer,
    SecurityLayerSettings,
)

_LOGGER = logging.getLogger(__name__)
_MODEL_DETAILS_CONCURRENCY = 4


async def _log_http_error(resp: aiohttp.ClientResponse) -> None:
    """Log an Ollama error response without dumping an unbounded body."""
    try:
        data = await resp.json(content_type=None)
    except (aiohttp.ClientError, json.JSONDecodeError, UnicodeDecodeError):
        data = None

    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, str):
        detail = "".join(character for character in error if character.isprintable())
        detail = " ".join(detail.split())[:300]
        if detail:
            _LOGGER.warning("Ollama returned HTTP %d: %s", resp.status, detail)
            return
    _LOGGER.warning("Ollama returned HTTP %d", resp.status)


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
        """List installed local models or available Cloud vision models."""
        models = await self._fetch_ollama_model_list()
        if models is None:
            return []

        vision, other = await self._classify_models(models)
        if self.provider_name == "ollama_cloud":
            return self._sort_cloud_models(vision)

        vision.sort(key=lambda model: model["score"], reverse=True)
        return vision + other

    async def _fetch_ollama_model_list(self) -> list[dict[str, Any]] | None:
        """Fetch valid model summaries from Ollama's documented tags API."""
        try:
            session = self._get_session()
            async with session.get(
                f"{self._ollama_url}/api/tags", timeout=_HEALTH_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Ollama model listing returned HTTP %d", resp.status
                    )
                    return None
                data = await resp.json()
                if not isinstance(data, dict) or not isinstance(
                    data.get("models"), list
                ):
                    _LOGGER.warning("Ollama returned an invalid model-list response")
                    return None
                return [
                    model
                    for model in data["models"]
                    if isinstance(model, dict) and isinstance(model.get("name"), str)
                ]
        # TimeoutError is deliberately not listed: since 3.3 it derives from
        # OSError, so naming both catches nothing extra and reads as though
        # it did. A timed-out model listing still lands here.
        except (
            aiohttp.ClientError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            _LOGGER.warning("Failed to fetch Ollama models: %s", exc)
            return None

    async def _classify_models(
        self, models: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Group model summaries by the capability returned from ``/api/show``."""
        semaphore = asyncio.Semaphore(_MODEL_DETAILS_CONCURRENCY)

        async def has_vision(model: dict[str, Any]) -> bool | None:
            async with semaphore:
                return await self._model_supports_vision(model["name"])

        capabilities = await asyncio.gather(*(has_vision(model) for model in models))
        vision: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []
        for model, supports_vision in zip(models, capabilities, strict=True):
            if supports_vision is True:
                model["score"] = _vision_model_score(model["name"])
                vision.append(model)
            elif supports_vision is False or self.provider_name != "ollama_cloud":
                other.append(model)
        return vision, other

    @staticmethod
    def _sort_cloud_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Put documented Cloud recommendations first, then sort by model id."""
        recommended_order = {
            name: index for index, name in enumerate(_OLLAMA_CLOUD_RECOMMENDED_MODELS)
        }
        for model in models:
            if model["name"] in recommended_order:
                model["recommended"] = True
        return sorted(
            models,
            key=lambda model: (
                recommended_order.get(model["name"], len(recommended_order)),
                model["name"].casefold(),
            ),
        )

    async def _model_supports_vision(self, model_name: str) -> bool | None:
        """Read Ollama's documented capability metadata for one model."""
        try:
            async with self._get_session().post(
                f"{self._ollama_url}/api/show",
                json={"model": model_name},
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Ollama model details for %s returned HTTP %d",
                        model_name,
                        resp.status,
                    )
                    return None
                data = await resp.json()
                capabilities = (
                    data.get("capabilities") if isinstance(data, dict) else None
                )
                if not isinstance(capabilities, list) or any(
                    not isinstance(capability, str) for capability in capabilities
                ):
                    _LOGGER.warning(
                        "Ollama returned invalid capability details for %s",
                        model_name,
                    )
                    return None
                return "vision" in capabilities
        except (
            aiohttp.ClientError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            _LOGGER.warning(
                "Failed to fetch Ollama model details for %s: %s",
                model_name,
                exc,
            )
            return None

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        return await self.call_ollama(frames, prompt)

    async def call_ollama(self, frames: list[bytes], prompt: str) -> str:
        """Send frames to Ollama vision model and return the response text."""
        images = [base64.b64encode(f).decode("ascii") for f in frames]

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _VISION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt, "images": images},
            ],
            "stream": False,
            "format": "json",
        }

        try:
            session = self._get_session()
            async with session.post(
                f"{self._ollama_url}/api/chat",
                json=payload,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    await _log_http_error(resp)
                    return ""
                try:
                    data = await resp.json()
                except (aiohttp.ClientError, json.JSONDecodeError, UnicodeDecodeError):
                    _LOGGER.warning("Ollama returned an invalid JSON response")
                    return ""
                if not isinstance(data, dict):
                    _LOGGER.warning("Ollama returned an invalid chat response")
                    return ""
                message = data.get("message")
                if not isinstance(message, dict) or not isinstance(
                    message.get("content"), str
                ):
                    _LOGGER.warning("Ollama response did not contain message.content")
                    return ""
                try:
                    self._last_prompt_tokens = int(data.get("prompt_eval_count") or 0)
                    self._last_completion_tokens = int(data.get("eval_count") or 0)
                except (TypeError, ValueError):
                    _LOGGER.warning("Ollama response contained invalid token counts")
                    self._last_prompt_tokens = 0
                    self._last_completion_tokens = 0
                return message["content"]
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
    """Analyzes clips via the Ollama Cloud API (ollama.com).

    Behaves identically to :class:`ClipAnalyzer` (local Ollama) but targets
    the Ollama Cloud endpoint and authenticates every request with an API key
    via ``Authorization: Bearer <key>``.
    """

    _CLOUD_BASE_URL = "https://ollama.com"

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
