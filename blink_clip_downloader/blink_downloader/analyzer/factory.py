"""``create_analyzer()``: turn an ``AppConfig`` into a ready analyzer.

Also builds the optional tier-2 escalation analyzer and attaches it to the
tier-1 one, which is why this lives apart from any single provider: it is
the only module that needs to know about all six at once.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class AnalyzerSettings:
    """How a clip is analyzed, independent of which provider does it.

    Grouped because tier 1 and tier 2 always receive an identical copy:
    the two analyzers differ only in which API they talk to, never in how
    they read a clip. Passing these ten separately meant writing the same
    ten arguments at both call sites, which is one edit away from the two
    tiers quietly disagreeing about frame strategy or car cameras.
    """

    car_description: str = ""
    max_frames: int = 3
    frame_interval: float = 2.0
    suspicious_keywords: list[str] | None = None
    camera_prompts: dict[str, str] | None = None
    camera_descriptions: dict[str, str] | None = None
    frame_strategy: str = "smart"
    car_cameras: list[str] | None = None
    car_zones: dict[str, dict[str, Any]] | None = None
    security_settings: SecurityLayerSettings | None = None


@dataclass(frozen=True)
class ProviderCredentials:
    """Every provider's keys and model ids, in one object.

    All nine together because ``create_analyzer`` cannot know which pair
    it needs until it has looked at ``ai_provider`` — and because tier-2
    escalation may pick a *different* provider, so the whole set has to
    survive as far as the second analyzer is built.
    """

    ollama_url: str = ""
    ollama_model: str = ""
    ollama_cloud_api_key: str = ""
    moondream_api_key: str = ""
    moondream_finetune_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    openai_api_key: str = ""
    openai_model: str = ""


def _build_single_analyzer(
    ai_provider: str,
    prompt: str,
    settings: AnalyzerSettings,
    **credentials: Any,
) -> BaseAnalyzer | None:
    """Build a single analyzer for *ai_provider*, with no escalation attached.

    Shared by :func:`create_analyzer` to build both the tier-1 analyzer and,
    when cross-provider escalation is configured, the tier-2 analyzer — see
    that function's ``escalation_provider``/``escalation_model``.

    *credentials* are the provider-specific keys and model ids, exactly as
    ``create_analyzer`` already assembles them into one dict to hand to
    both tiers.
    """
    ollama_url = credentials.get("ollama_url", "")
    ollama_model = credentials.get("ollama_model", "")
    ollama_cloud_api_key = credentials.get("ollama_cloud_api_key", "")
    moondream_api_key = credentials.get("moondream_api_key", "")
    moondream_finetune_model = credentials.get("moondream_finetune_model", "")
    anthropic_api_key = credentials.get("anthropic_api_key", "")
    anthropic_model = credentials.get("anthropic_model", "")
    openai_api_key = credentials.get("openai_api_key", "")
    openai_model = credentials.get("openai_model", "")

    common: dict[str, Any] = {
        "prompt": prompt,
        "car_description": settings.car_description,
        "max_frames": settings.max_frames,
        "frame_interval": settings.frame_interval,
        "suspicious_keywords": settings.suspicious_keywords,
        "camera_prompts": settings.camera_prompts,
        "camera_descriptions": settings.camera_descriptions,
        "frame_strategy": settings.frame_strategy,
        "car_cameras": settings.car_cameras,
        "car_zones": settings.car_zones,
        "security_settings": settings.security_settings,
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
    settings: AnalyzerSettings | None = None,
    credentials: ProviderCredentials | None = None,
    *,
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
    settings = settings or AnalyzerSettings()
    shared_kwargs: dict[str, Any] = asdict(credentials or ProviderCredentials())

    analyzer = _build_single_analyzer(ai_provider, prompt, settings, **shared_kwargs)
    if analyzer is None:
        return None

    escalation_provider = (escalation_provider or "").strip().lower()
    if escalation_provider:
        tier2_kwargs = dict(shared_kwargs)
        model_kwarg = _ESCALATION_MODEL_KWARG.get(escalation_provider)
        if model_kwarg and escalation_model:
            tier2_kwargs[model_kwarg] = escalation_model

        # The same settings object, deliberately: tier 2 differs only in
        # provider and model, never in how it reads a clip.
        tier2 = _build_single_analyzer(
            escalation_provider, prompt, settings, **tier2_kwargs
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
