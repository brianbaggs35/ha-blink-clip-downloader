"""Per-provider model catalogs: capability, ordering and pricing.

Reference data about *other people's* model line-ups — which Ollama tags can
see images, which OpenAI ids accept a structured-output schema, what a
million tokens costs — plus the small pure functions that read it. None of
it depends on anything in :mod:`blink_downloader.analyzer`, and all of it
changes on its own schedule, as providers ship and retire models, rather
than when this add-on's analysis logic does.

Kept separate for that reason: it is the part of the analyzer most likely to
need an edit for reasons that have nothing to do with how a clip is
analyzed. The ``analyzer`` package re-exports the names it uses itself — so
``from .analyzer import lookup_model_pricing`` and friends keep working —
but not the whole module: the pricing and pattern tables below have never
been reachable that way, and should be imported from here directly.
"""

from __future__ import annotations

import re
from typing import Any

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


#: The GPT-5.6 tier ids, named once: each appears in the pricing table, the
#: display-order list and the offline fallback list below, and all three
#: have to agree for a model to be priced *and* offered.
_GPT_56_SOL = "gpt-5.6-sol"
_GPT_56_TERRA = "gpt-5.6-terra"
_GPT_56_LUNA = "gpt-5.6-luna"
#: GPT-6's first release, named on the same footing for the same reason.
#: Its family prefix is "gpt-6", which shares no substring with "gpt-5" —
#: so every place that recognizes a model family by prefix needs it spelled
#: out separately rather than inheriting from the GPT-5 entry.
_GPT_6_ASTRA = "gpt-6-astra"

# OpenAI model pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://developers.openai.com/api/docs/pricing (standard tier)
# More specific model-family prefixes (mini/nano/dotted variants) must be
# listed before their shorter/bare prefix (e.g. "gpt-5.4-mini" before
# "gpt-5.4" before "gpt-5") since model_pricing() below returns the first
# matching entry in insertion order.
#
# SonarQube's S1192 flags several of these model-id strings as duplicated
# literals (they also appear in _OPENAI_VISION_PREFIXES and the model-list
# constants below) — deliberately left as plain strings rather than named
# constants: these are independent reference tables (pricing vs.
# vision-capability vs. UI model lists), each meant to read as a flat,
# visually-scannable list matching OpenAI's own docs. Routing every entry
# through a shared constant would trade that scannability for indirection
# without preventing any real drift — a typo in a constant name fails
# exactly as loudly (NameError) as a typo in the literal would look
# different from OpenAI's docs.
_OPENAI_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1-nano": (0.10, 0.40),  # NOSONAR
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),  # NOSONAR
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),  # NOSONAR
    "o1-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    "o3": (2.00, 8.00),
    "gpt-4-turbo": (10.00, 30.00),
    # GPT-5.6 (released 2026-07-09): three tiers — Sol (flagship), Terra
    # (mid), Luna (budget) — replacing the old size-suffix naming. Prices
    # verified live against the pricing page 2026-09-10, post the 2026-07-30
    # Luna/Terra price cuts.
    _GPT_56_SOL: (4.00, 20.00),
    _GPT_56_TERRA: (2.00, 12.00),
    _GPT_56_LUNA: (0.20, 1.20),
    # GPT-6 (Astra). Verified live against the pricing page 2026-09-17.
    _GPT_6_ASTRA: (10.00, 50.00),
    "gpt-5.4-nano": (0.20, 1.25),  # NOSONAR
    "gpt-5.4-mini": (0.75, 4.50),  # NOSONAR
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.4": (2.50, 15.00),  # NOSONAR
    "gpt-5.5": (5.00, 30.00),  # NOSONAR
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
        "gpt-6",
        "o1",
        "o3",
        "o4-mini",
    }
)

# Model-id substrings that mean "not a vision chat model" even though the id
# also matches one of the prefixes above — e.g. "gpt-4o-mini-transcribe" and
# "gpt-4o-mini-search-preview" both contain "gpt-4o" but are an audio
# transcription model and a web-search-tool model respectively, neither of
# which takes image input via Chat Completions. Checked before the prefix
# match so these never leak into fetch_models()'s picker list.
_OPENAI_NON_VISION_SUBSTRINGS: frozenset[str] = frozenset(
    {"transcribe", "search-preview", "audio", "realtime", "tts"}
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

# Newest-to-oldest OpenAI model family order. Used both to sort
# fetch_models()'s live-API results and to order the fallback list below —
# plain alphabetical order (the previous behavior) put "gpt-4-turbo" first
# purely because '-' sorts before '.' in ASCII, which looked like a
# recommendation for the oldest, most expensive model in the lineup.
_OPENAI_MODEL_DISPLAY_ORDER: list[str] = [
    _GPT_6_ASTRA,
    _GPT_56_SOL,
    _GPT_56_TERRA,
    _GPT_56_LUNA,
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "o4-mini",
    "o3",
    "o3-mini",
    "o1",
    "o1-mini",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
]

# Fallback model list when the OpenAI API cannot be reached — just the bare
# model ids a user needs to paste into the add-on's Configuration tab, in
# the same newest-to-oldest order as _OPENAI_MODEL_DISPLAY_ORDER above.
_OPENAI_FALLBACK_MODELS: list[str] = [
    _GPT_6_ASTRA,
    _GPT_56_SOL,
    _GPT_56_TERRA,
    _GPT_56_LUNA,
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
]

# Matches a trailing dated-snapshot suffix on an OpenAI model id, e.g. the
# "-2024-07-18" in "gpt-4o-mini-2024-07-18". fetch_models() skips these and
# keeps only the bare alias: a dated snapshot is a perfectly valid model id
# to run with, but pins to one snapshot forever instead of following
# improvements to the alias, and cluttering the picker with every snapshot
# of every model buries the ids someone would actually want to copy.
_OPENAI_DATED_SNAPSHOT_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _openai_model_rank(model_id: str) -> int:
    """Sort key for fetch_models(): position in _OPENAI_MODEL_DISPLAY_ORDER,
    or last place for a model this add-on doesn't recognize yet."""
    try:
        return _OPENAI_MODEL_DISPLAY_ORDER.index(model_id.lower())
    except ValueError:
        return len(_OPENAI_MODEL_DISPLAY_ORDER)


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
    if any(s in lower for s in _OPENAI_NON_VISION_SUBSTRINGS):
        return False
    return any(p in lower for p in _OPENAI_VISION_PREFIXES)


# Anthropic model pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://platform.claude.com/docs/en/about-claude/pricing
_ANTHROPIC_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # More specific ids before their shorter prefix (same convention as
    # _OPENAI_MODEL_PRICING above) — "claude-fable-5" is itself a substring
    # of "claude-fable-5-1", so the dotted release must be listed first or
    # lookup_model_pricing() would silently match the older entry instead.
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    # $2/$10 was announced at launch as introductory pricing through
    # 2026-08-31, originally scheduled to rise to $3/$15 on 2026-09-01 —
    # Anthropic since cancelled that increase and made $2/$10 the permanent
    # standard price instead (see the pricing page's own note on this).
    # Verified live against the pricing page 2026-09-10; the number below
    # was already correct, only this comment was stale.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Fallback model list when the Anthropic API cannot be reached — just the
# bare model ids a user needs to paste into the add-on's Configuration tab.
_ANTHROPIC_FALLBACK_MODELS: list[str] = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
]

# Matches a trailing dated-snapshot suffix on an Anthropic model id, e.g. the
# "-20250929" in "claude-sonnet-5-20250929" (Anthropic's real API can return
# either form). Anthropic accepts both the dated snapshot and the bare alias
# as a valid ``model`` value, and the alias is what a user should paste into
# the add-on's Configuration tab since it keeps resolving to the latest
# snapshot rather than pinning to one forever.
_ANTHROPIC_DATED_SNAPSHOT_RE = re.compile(r"-\d{8}$")


def _anthropic_supports_structured_output(model: str) -> bool:
    """Whether *model* is a recognized Claude model generation that
    supports schema-constrained ``output_config`` (see
    :meth:`AnthropicAnalyzer._build_anthropic_create_kwargs`).

    Structured outputs are documented as supported on Claude 4.5+
    generation models, not older 3.x snapshots — verified live against
    Anthropic's docs 2026-09-10, not assumed. Reuses
    :data:`_ANTHROPIC_MODEL_PRICING`'s keys as the "recognized
    current-generation model" allowlist rather than maintaining a
    parallel list — every model in that table is already 4.5 or newer.
    An unrecognized or older model id (e.g. a legacy pasted-in snapshot)
    safely falls back to the existing prompt-only JSON instructions
    instead of risking a request the API might reject.
    """
    lower = (model or "").lower()
    return any(prefix in lower for prefix in _ANTHROPIC_MODEL_PRICING)


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
