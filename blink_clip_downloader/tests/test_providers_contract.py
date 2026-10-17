"""Every provider, through the doors the rest of the add-on uses.

Hugging Face and Replicate (6.1.2) came with changes to code all eight
providers share: the base class gained two pricing hooks, ``create_analyzer``
and ``ProviderCredentials`` gained fields, the usage page now asks the
analyzers to price a model before falling back to the static tables, and
``lookup_model_pricing`` stopped matching ids that contain a slash. None of
that is supposed to change what an existing provider does, and this module is
where that is held to: the same assertions, run once per provider, against the
real factory, the real analyzer classes and the real usage route.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader.analyzer import (
    AnalyzerSettings,
    BaseAnalyzer,
    HuggingFaceAnalyzer,
    ProviderCredentials,
    create_analyzer,
)
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer

PROVIDERS = [
    "ollama",
    "ollama_cloud",
    "moondream_cloud",
    "moondream_local",
    "anthropic",
    "openai",
    "huggingface",
    "replicate",
]

_CREDENTIALS = ProviderCredentials(
    ollama_url="http://ollama.invalid:11434",
    ollama_model="llava:7b",
    ollama_cloud_api_key="key",
    moondream_api_key="key",
    anthropic_api_key="key",
    anthropic_model="claude-haiku-4-5",
    openai_api_key="key",
    openai_model="gpt-4o-mini",
    huggingface_api_key="key",
    huggingface_model="Qwen/Qwen3.5-9B",
    replicate_api_key="key",
    replicate_model="openai/gpt-4o-mini",
)


def _build(provider: str) -> BaseAnalyzer:
    analyzer = create_analyzer(provider, "prompt", AnalyzerSettings(), _CREDENTIALS)
    assert analyzer is not None
    if isinstance(analyzer, HuggingFaceAnalyzer):
        # Its catalog is fetched from the network on first use; these tests
        # are about the shared plumbing, so start it loaded (and empty).
        analyzer._catalog = []
        analyzer._catalog_fetched_at = time.monotonic()
    return analyzer


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_factory_builds_every_provider_under_its_own_name(provider: str) -> None:
    analyzer = _build(provider)
    assert analyzer.provider_name == provider
    assert analyzer.model_name()
    assert analyzer.escalation_analyzer is None


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_every_provider_answers_the_pricing_hooks_without_the_network(
    provider: str,
) -> None:
    analyzer = _build(provider)
    assert await analyzer.refresh_pricing() is None
    # Never raises, and an id nobody here knows is "unknown", not a guess.
    assert analyzer.price_for_model("nobody/nothing") is None
    assert analyzer.price_for_model("") is None


@pytest.mark.parametrize(
    "provider", ["ollama", "anthropic", "openai", "moondream_cloud"]
)
def test_the_providers_that_always_used_the_static_tables_still_do(
    provider: str,
) -> None:
    """The base hook is the old behaviour: look the name up in the OpenAI and
    Anthropic tables. Providers that do not override it must give exactly
    what ``lookup_model_pricing`` gave before the hook existed."""
    analyzer = _build(provider)
    assert analyzer.price_for_model("gpt-4o-mini") == (0.15, 0.60)
    assert analyzer.price_for_model("claude-haiku-4-5") == (1.00, 5.00)
    assert analyzer.price_for_model("llava:7b") is None


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_usage_page_prices_every_providers_rows_as_it_always_did(
    db: ClipDatabase, provider: str
) -> None:
    """Rows from a model that is no longer configured are still priced from
    the static tables, whichever provider is active now -- and a local model
    is still N/A, never an invented $0."""
    now = datetime.now(UTC).isoformat()
    for clip_id, model in (("u1", "gpt-4o-mini"), ("u2", "llava:7b")):
        await db.add_clip(
            {
                "id": clip_id,
                "camera": "Front Door",
                "path": f"/data/{clip_id}.mp4",
                "timestamp": "2024-06-01T08:00:00+00:00",
                "size_bytes": 1,
                "duration": 5,
                "source": "pir",
                "network_id": 1,
            }
        )
        await db.add_analysis_result(
            {
                "clip_id": clip_id,
                "camera": "Front Door",
                "model": model,
                "response_text": "",
                "is_suspicious": False,
                "confidence": 0.1,
                "summary": "ok",
                "frame_count": 1,
                "analysis_duration": 1.0,
                "analyzed_at": now,
                "tokens_prompt": 1_000_000,
                "tokens_completion": 1_000_000,
            }
        )

    server = MediaServer(db=db, port=0, analyzer=_build(provider))
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()
    try:
        usage = await client.get("/api/ai/usage")
        periods = await client.get("/api/ai/usage/periods")
        assert usage.status == 200
        assert periods.status == 200
        data = await usage.json()
        month = (await periods.json())["monthly"][0]
    finally:
        await client.close()

    assert data["provider"] == provider
    by_model = {row["model"]: row["cost"] for row in data["by_model"]}
    assert by_model["gpt-4o-mini"] == pytest.approx(0.15 + 0.60)
    assert by_model["llava:7b"] is None
    assert month["cost"] == pytest.approx(0.15 + 0.60)


@pytest.mark.parametrize(
    "provider", ["ollama", "anthropic", "openai", "huggingface", "replicate"]
)
def test_each_provider_with_a_model_option_can_be_the_escalation_tier(
    provider: str,
) -> None:
    tier1 = create_analyzer(
        "moondream_local",
        "prompt",
        AnalyzerSettings(),
        _CREDENTIALS,
        escalation_provider=provider,
        escalation_model="chosen/model"
        if provider in ("huggingface", "replicate")
        else "chosen-model",
    )
    assert tier1 is not None
    tier2 = tier1.escalation_analyzer
    assert tier2 is not None
    assert tier2.provider_name == provider
    assert tier2.model_name().endswith(("chosen/model", "chosen-model"))


def test_an_unknown_provider_still_disables_analysis_with_a_clear_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert create_analyzer("mystery", "prompt") is None
    assert "Unknown ai_provider 'mystery'" in caplog.text
    for provider in PROVIDERS:
        assert f"'{provider}'" in caplog.text
