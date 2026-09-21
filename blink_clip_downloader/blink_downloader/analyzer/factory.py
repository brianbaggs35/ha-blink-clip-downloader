"""``create_analyzer()``: turn an ``AppConfig`` into a ready analyzer.

Also builds the optional tier-2 escalation analyzer and attaches it to the
tier-1 one, which is why this lives apart from any single provider: it is
the only module that needs to know about all six at once.
"""

from __future__ import annotations

import logging
from typing import Any

from .anthropic_provider import AnthropicAnalyzer
from .base import BaseAnalyzer, SecurityLayerSettings
from .moondream_provider import MoondreamCloudAnalyzer, MoondreamLocalAnalyzer
from .ollama_provider import ClipAnalyzer, OllamaCloudAnalyzer
from .openai_provider import OpenAIAnalyzer

_LOGGER = logging.getLogger(__name__)


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
    car_zones: dict[str, dict[str, Any]] | None = None,
    security_settings: SecurityLayerSettings | None = None,
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
    common: dict[str, Any] = {
        "prompt": prompt,
        "car_description": car_description,
        "max_frames": max_frames,
        "frame_interval": frame_interval,
        "suspicious_keywords": suspicious_keywords,
        "camera_prompts": camera_prompts,
        "camera_descriptions": camera_descriptions,
        "frame_strategy": frame_strategy,
        "car_cameras": car_cameras,
        "car_zones": car_zones,
        "security_settings": security_settings,
    }

    if ai_provider == "ollama":
        return _build_ollama_analyzer(common, ollama_url, ollama_model)
    if ai_provider == "ollama_cloud":
        return _build_ollama_cloud_analyzer(common, ollama_cloud_api_key, ollama_model)
    if ai_provider == "moondream_cloud":
        return _build_moondream_cloud_analyzer(
            common, moondream_api_key, moondream_finetune_model
        )
    if ai_provider == "moondream_local":
        return MoondreamLocalAnalyzer(**common)
    if ai_provider == "anthropic":
        return _build_anthropic_analyzer(common, anthropic_api_key, anthropic_model)
    if ai_provider == "openai":
        return _build_openai_analyzer(common, openai_api_key, openai_model)

    _LOGGER.warning(
        "Unknown ai_provider %r; expected 'ollama', 'ollama_cloud', "
        "'moondream_cloud', 'moondream_local', 'anthropic', or 'openai'. "
        "AI analysis disabled.",
        ai_provider,
    )
    return None


def _build_ollama_analyzer(
    common: dict[str, Any], ollama_url: str, ollama_model: str
) -> BaseAnalyzer | None:
    if not ollama_url:
        _LOGGER.warning(
            "ai_provider='ollama' requires ollama_url to be set; AI analysis disabled"
        )
        return None
    return ClipAnalyzer(ollama_url=ollama_url, model=ollama_model, **common)


def _build_ollama_cloud_analyzer(
    common: dict[str, Any], ollama_cloud_api_key: str, ollama_model: str
) -> BaseAnalyzer | None:
    if not ollama_cloud_api_key:
        _LOGGER.warning(
            "ai_provider='ollama_cloud' requires ollama_cloud_api_key to be set; "
            "AI analysis disabled"
        )
        return None
    return OllamaCloudAnalyzer(
        api_key=ollama_cloud_api_key, model=ollama_model, **common
    )


def _build_moondream_cloud_analyzer(
    common: dict[str, Any], moondream_api_key: str, moondream_finetune_model: str
) -> BaseAnalyzer | None:
    if not moondream_api_key:
        _LOGGER.warning(
            "ai_provider='moondream_cloud' requires moondream_api_key to be set; "
            "AI analysis disabled"
        )
        return None
    return MoondreamCloudAnalyzer(
        api_key=moondream_api_key, finetune_model=moondream_finetune_model, **common
    )


def _build_anthropic_analyzer(
    common: dict[str, Any], anthropic_api_key: str, anthropic_model: str
) -> BaseAnalyzer | None:
    if not anthropic_api_key:
        _LOGGER.warning(
            "ai_provider='anthropic' requires anthropic_api_key to be set; "
            "AI analysis disabled"
        )
        return None
    return AnthropicAnalyzer(
        api_key=anthropic_api_key,
        model=anthropic_model or "claude-haiku-4-5",
        **common,
    )


def _build_openai_analyzer(
    common: dict[str, Any], openai_api_key: str, openai_model: str
) -> BaseAnalyzer | None:
    if not openai_api_key:
        _LOGGER.warning(
            "ai_provider='openai' requires openai_api_key to be set; "
            "AI analysis disabled"
        )
        return None
    return OpenAIAnalyzer(
        api_key=openai_api_key, model=openai_model or "gpt-4o-mini", **common
    )


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
    car_zones: dict[str, dict[str, Any]] | None = None,
    security_settings: SecurityLayerSettings | None = None,
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
        security_settings=security_settings,
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
            security_settings=security_settings,
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
