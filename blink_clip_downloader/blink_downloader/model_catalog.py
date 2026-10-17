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
from dataclasses import dataclass
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

# Recommended models are selected from Ollama's current Cloud catalog using
# the providers' published vision benchmarks. Only models returned by
# Ollama's Cloud API can receive a recommendation.
_OLLAMA_CLOUD_RECOMMENDED_MODELS: tuple[str, ...] = (
    "gemma4:31b",
    "glm-5.3-flash",
)


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


def model_entry(name: str) -> dict[str, str]:
    """One row of the shape every provider's ``fetch_models`` returns.

    All four fields are the same id on purpose: the Models tab shows the
    string a user must paste into their ``*_model`` option, so inventing a
    prettier ``display_name`` would show them something that does not work.
    Shared because both the OpenAI and Anthropic analyzers build it, from
    a live list and from their fallback list — four places that must not
    drift apart.
    """
    return {"name": name, "id": name, "display_name": name, "description": name}


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


#: Where Hugging Face's Inference Providers router lives. It speaks the
#: OpenAI Chat Completions dialect, and its public ``/models`` listing is
#: the only authoritative source for which models take an image and what
#: each backend charges for them — see :func:`huggingface_vision_entries`.
HUGGINGFACE_ROUTER_URL = "https://router.huggingface.co/v1"

#: The vision model used when ``huggingface_model`` is left empty. Picked from
#: the router's live catalog rather than from a benchmark: a mixture-of-experts
#: model with few active parameters, served by several backends (so one
#: backend being down does not take analysis with it) at a low per-token
#: price. The catalog changes weekly, so this is a starting point — the AI
#: tab's *Fetch Models* shows what is actually available and what it costs.
DEFAULT_HUGGINGFACE_MODEL = "google/gemma-4-26B-A4B-it"

#: Replicate's HTTP API. Official models are called at
#: ``/models/{owner}/{name}/predictions``; community models need a version id
#: this add-on does not ask for, so only official models are supported.
REPLICATE_API_URL = "https://api.replicate.com/v1"

_REPLICATE_GPT_4O_MINI = "openai/gpt-4o-mini"

#: The model used when ``replicate_model`` is left empty: the same model the
#: OpenAI provider defaults to, cheap and with a long-stable schema.
DEFAULT_REPLICATE_MODEL = _REPLICATE_GPT_4O_MINI

#: Official Replicate models that take an image and a prompt and answer in
#: text — the "Vision models" collection's official entries, cheapest-class
#: first. A list rather than a query because Replicate's API says nothing
#: about what a model *does*; whether one fits is decided at call time from
#: its own input schema (:func:`replicate_input_plan`).
REPLICATE_VISION_MODELS: tuple[str, ...] = (
    _REPLICATE_GPT_4O_MINI,
    "openai/gpt-4.1-mini",
    "google/gemini-2.5-flash",
    "google/gemini-3-flash",
    "openai/gpt-5.6-luna",
    "openai/gpt-4o",
    "openai/gpt-5.6-terra",
    "anthropic/claude-4.5-sonnet",
    "openai/gpt-5",
    "openai/gpt-5.4",
    "openai/gpt-5.6-sol",
)

#: Replicate's price per million tokens, (input, output), for the official
#: models in :data:`REPLICATE_VISION_MODELS` whose price could be established.
#: Replicate's API carries no pricing; its docs say a model's price is on "the
#: pricing section of the model", and the page is built from billing data it
#: embeds (``token_input_count`` / ``token_output_count`` at a price per million
#: or per thousand tokens). These figures were read there on 2026-10-12, with
#: the per-thousand ones converted, and each agrees with the model's own
#: vendor's list price. Replicate does not promise they stay fixed, so like the
#: OpenAI and Anthropic tables above they are estimates that can drift.
#:
#: ``openai/gpt-5.6-sol`` is left out on purpose: its page describes a
#: "50% off until Sept 18" promotion that has passed, and its billing data
#: disagrees with that text, so which figure is current cannot be told. A model
#: with no entry shows tokens and no cost on the AI Usage tab.
_REPLICATE_MODEL_PRICING: dict[str, tuple[float, float]] = {
    _REPLICATE_GPT_4O_MINI: (0.15, 0.60),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-3-flash": (0.50, 3.00),
    "openai/gpt-5.6-luna": (1.00, 6.00),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-5.6-terra": (2.50, 15.00),
    "anthropic/claude-4.5-sonnet": (3.00, 15.00),
    "openai/gpt-5": (1.25, 10.00),
    "openai/gpt-5.4": (2.50, 15.00),
}

#: The input names Replicate's official language models use for the same
#: four things, in the order they are tried. Read off the models' published
#: schemas: Gemini takes ``images``/``system_instruction``/
#: ``max_output_tokens``, the GPT models ``image_input``/``system_prompt``/
#: ``max_completion_tokens``, Claude a single ``image`` with ``system_prompt``
#: and ``max_tokens``.
_REPLICATE_IMAGE_LIST_FIELDS: tuple[str, ...] = ("images", "image_input")
_REPLICATE_IMAGE_FIELDS: tuple[str, ...] = ("image", "media")
_REPLICATE_SYSTEM_FIELDS: tuple[str, ...] = (
    "system_prompt",
    "system_instruction",
    "system",
)
_REPLICATE_MAX_TOKEN_FIELDS: tuple[str, ...] = (
    "max_completion_tokens",
    "max_output_tokens",
    "max_tokens",
)


@dataclass(frozen=True)
class ReplicateInputPlan:
    """Which of a Replicate model's inputs carry what this add-on sends.

    ``images_field`` takes the frames; ``images_are_list`` says whether it
    takes all of them in one request (a list) or one (a single file, which
    means one request per frame). The two optional fields are ``None`` when
    the model has no such input, and nothing is sent for them.
    """

    prompt_field: str
    images_field: str
    images_are_list: bool
    system_field: str | None
    max_tokens_field: str | None


def replicate_input_plan(properties: dict[str, Any]) -> ReplicateInputPlan | None:
    """Work out how to call a Replicate model from its ``Input`` schema.

    *properties* is the schema's ``properties`` mapping. ``None`` when the
    model cannot be used here: no ``prompt`` input, or no input a picture
    can go in. Models differ in all of these names, so a fixed set would
    break on the first model that spells one differently; reading them off
    the model's own schema is what lets a newly published model work.
    """
    if "prompt" not in properties:
        return None

    def first_of(names: tuple[str, ...]) -> str | None:
        return next((name for name in names if name in properties), None)

    def spec_of(name: str) -> dict[str, Any]:
        spec = properties.get(name)
        return spec if isinstance(spec, dict) else {}

    def takes_files(name: str) -> bool:
        spec = spec_of(name)
        items = spec.get("items")
        item_format = items.get("format") if isinstance(items, dict) else None
        return spec.get("format") == "uri" or item_format == "uri"

    list_field = next(
        (
            name
            for name in _REPLICATE_IMAGE_LIST_FIELDS
            if spec_of(name).get("type") == "array" and takes_files(name)
        ),
        None,
    )
    single_field = next(
        (name for name in _REPLICATE_IMAGE_FIELDS if takes_files(name)),
        None,
    )
    images_field = list_field or single_field
    if images_field is None:
        return None
    return ReplicateInputPlan(
        prompt_field="prompt",
        images_field=images_field,
        images_are_list=list_field is not None,
        system_field=first_of(_REPLICATE_SYSTEM_FIELDS),
        max_tokens_field=first_of(_REPLICATE_MAX_TOKEN_FIELDS),
    )


#: Suffixes the router reads as a routing *policy* rather than a backend name
#: (``model:cheapest``). Anything else after the colon names a backend
#: (``model:novita``).
_HUGGINGFACE_POLICIES: frozenset[str] = frozenset({"fastest", "cheapest", "preferred"})

#: Model-id substrings that mean "takes an image but does not answer the
#: question" — safety classifiers return a verdict on the content of the
#: picture rather than a description of it, so offering one would produce an
#: analyzer that can never say what happened.
_HUGGINGFACE_NON_ANALYST_SUBSTRINGS: frozenset[str] = frozenset({"guard"})


def split_huggingface_model(model: str) -> tuple[str, str]:
    """Split ``org/name:selector`` into ``("org/name", "selector")``.

    The selector is what follows the first colon — a routing policy
    (``fastest``, ``cheapest``, ``preferred``) or the name of one backend —
    and is ``""`` when absent. Repository ids never contain a colon, so the
    first one is always the selector's.
    """
    base, _, selector = (model or "").strip().partition(":")
    return base, selector.strip().lower()


def is_huggingface_vision_entry(entry: dict[str, Any]) -> bool:
    """Whether a router catalog row takes an image and answers in text."""
    architecture = entry.get("architecture") or {}
    if "image" not in (architecture.get("input_modalities") or []):
        return False
    if "text" not in (architecture.get("output_modalities") or []):
        return False
    lower = str(entry.get("id", "")).lower()
    return not any(s in lower for s in _HUGGINGFACE_NON_ANALYST_SUBSTRINGS)


#: The two keys of a price, in the order they are stored and shown.
_RATE_KEYS = ("input", "output")


def _rate(provider: dict[str, Any]) -> tuple[float, float] | None:
    """A backend row's (input, output) $/1M tokens, or ``None`` when it
    publishes no complete price for the model.

    The router documents ``pricing`` as USD per million tokens, and
    ``is_free`` as the backend currently charging nothing for the model (a
    temporary promotion) — which is $0 whatever ``pricing`` still says.
    """
    if provider.get("is_free") is True:
        return 0.0, 0.0
    pricing = provider.get("pricing")
    if not isinstance(pricing, dict) or not all(key in pricing for key in _RATE_KEYS):
        return None
    return float(pricing[_RATE_KEYS[0]]), float(pricing[_RATE_KEYS[1]])


def _priced_live_providers(
    entry: dict[str, Any],
) -> list[tuple[dict[str, Any], tuple[float, float]]]:
    """Backends currently serving this model that publish a price for it,
    each with that price."""
    priced = []
    for provider in entry.get("providers") or []:
        rate = _rate(provider)
        if provider.get("status", "live") == "live" and rate is not None:
            priced.append((provider, rate))
    return priced


def huggingface_price(
    entry: dict[str, Any], selector: str = ""
) -> tuple[float, float] | None:
    """Estimated (input, output) $/1M-token price of one router catalog row.

    Which backend serves a request is the router's choice — by default the
    one with the highest throughput, which is the backend this estimate
    prices — unless *selector* narrows it: ``cheapest`` prices the cheapest
    backend, and a backend name prices that backend. ``None`` when nothing
    in the catalog prices it (a backend that publishes no price, or a name
    the model is not served by), so a caller can show "N/A" instead of a
    number that was never established.
    """
    priced = _priced_live_providers(entry)
    if selector and selector not in _HUGGINGFACE_POLICIES:
        priced = [(p, r) for p, r in priced if str(p.get("provider", "")) == selector]
    if not priced:
        return None
    if selector == "cheapest":
        return min((rate for _, rate in priced), key=sum)
    # Highest throughput; between equals, the cheaper backend.
    return max(
        priced, key=lambda pr: (float(pr[0].get("throughput") or 0.0), -sum(pr[1]))
    )[1]


def huggingface_vision_entries(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The vision-capable rows of the router catalog, shaped for the Models tab.

    Cheapest first, with models nothing prices last: this add-on analyzes
    every clip, so what a model costs is the deciding axis, and unlike
    Ollama's list there is no single "best" for the first row to be. The
    ``pricing`` field carries the estimate from :func:`huggingface_price`.
    """
    ranked: list[tuple[tuple[bool, float, str], dict[str, Any]]] = []
    for row in catalog:
        if not is_huggingface_vision_entry(row):
            continue
        entry: dict[str, Any] = model_entry(str(row["id"]))
        price = huggingface_price(row)
        entry["pricing"] = dict(zip(_RATE_KEYS, price)) if price is not None else None
        ranked.append(((price is None, sum(price or ()), entry["name"]), entry))
    return [entry for _, entry in sorted(ranked, key=lambda pair: pair[0])]


def lookup_model_pricing(model: str) -> tuple[float, float] | None:
    """Best-effort (input, output) $/1M-token pricing lookup for *model*.

    Checks the OpenAI and Anthropic pricing tables — their model-id
    namespaces don't overlap, so no provider hint is needed. Returns None for
    models this add-on can't price per-token (Ollama, Moondream, or an
    unrecognized/fine-tuned id) so callers can render an explicit "N/A"
    instead of guessing at a price.

    An id with a ``/`` in it (``org/name``, how Hugging Face, Replicate and the
    Ollama registry spell a model) is never one of those two providers' —
    neither publishes such an id — and the substring match below would
    otherwise price it as one: ``Sao10K/L3-8B-Stheno-v3.2`` contains ``o1`` and
    came out at OpenAI o1's $15/$60. Replicate's official models are the
    exception, looked up by their exact id first.
    """
    lower = (model or "").lower()
    replicate_price = _REPLICATE_MODEL_PRICING.get(lower)
    if replicate_price is not None:
        return replicate_price
    if "/" in lower:
        return None
    for table in (_OPENAI_MODEL_PRICING, _ANTHROPIC_MODEL_PRICING):
        for prefix, pricing in table.items():
            if lower.startswith(prefix) or prefix in lower:
                return pricing
    return None
