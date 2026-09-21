"""AI video analysis via ffmpeg frame extraction and pluggable AI providers.

Six providers are supported:
- ``ollama``          – local/LAN Ollama server with a vision-capable model
- ``ollama_cloud``    – Ollama Cloud API (api.ollama.com) with an API key
- ``moondream_cloud`` – Moondream Cloud API (api.moondream.ai)
- ``moondream_local`` – Moondream 0.5B model running on-device (no cloud)
- ``anthropic``       – Anthropic Claude API (claude.ai) with an API key
- ``openai``          – OpenAI Chat Completions API (platform.openai.com) with an API key

Use :func:`create_analyzer` to instantiate the right provider from config.

This was a single 5,200-line module until it became a package; the split is
along the seams it already had. :mod:`.base` holds everything that is the
same whichever provider is configured — frame extraction, prompt assembly,
the vision/security layers, verdict handling — and each provider module
holds only what differs about talking to that one API. :mod:`.factory`
chooses between them. The provider modules depend on :mod:`.base` and on
nothing else in the package, so a provider can be read, changed or added
without reading the other five.

Importers are unaffected: every name the old module exposed is re-exported
here, so ``from .analyzer import ...`` keeps working unchanged.
"""

from __future__ import annotations

# Re-exported for the contract documented in model_catalog.py: that module
# was split out of this one, and `from .analyzer import lookup_model_pricing`
# (and friends) keeps working rather than every caller having to move.
from ..model_catalog import (
    _ANTHROPIC_DATED_SNAPSHOT_RE,
    _ANTHROPIC_FALLBACK_MODELS,
    _OPENAI_DATED_SNAPSHOT_RE,
    _OPENAI_FALLBACK_MODELS,
    _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
    _anthropic_supports_structured_output,
    _openai_model_rank,
    _vision_model_score,
    is_openai_vision_model,
    is_vision_model,
    lookup_model_pricing,
)
from .anthropic_provider import AnthropicAnalyzer
from .base import AnalysisResult, BaseAnalyzer, SecurityLayerSettings
from .factory import create_analyzer
from .moondream_provider import MoondreamCloudAnalyzer, MoondreamLocalAnalyzer
from .ollama_provider import ClipAnalyzer, OllamaCloudAnalyzer
from .openai_provider import OpenAIAnalyzer

__all__ = [
    "_ANTHROPIC_DATED_SNAPSHOT_RE",
    "_ANTHROPIC_FALLBACK_MODELS",
    "_OPENAI_DATED_SNAPSHOT_RE",
    "_OPENAI_FALLBACK_MODELS",
    "_OPENAI_STRUCTURED_OUTPUT_SCHEMA",
    "AnalysisResult",
    "AnthropicAnalyzer",
    "BaseAnalyzer",
    "ClipAnalyzer",
    "MoondreamCloudAnalyzer",
    "MoondreamLocalAnalyzer",
    "OllamaCloudAnalyzer",
    "OpenAIAnalyzer",
    "SecurityLayerSettings",
    "_anthropic_supports_structured_output",
    "_openai_model_rank",
    "_vision_model_score",
    "create_analyzer",
    "is_openai_vision_model",
    "is_vision_model",
    "lookup_model_pricing",
]
