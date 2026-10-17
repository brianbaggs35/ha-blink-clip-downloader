"""The Hugging Face Inference Providers analyzer, against a stand-in router.

Nothing here mocks the OpenAI SDK. ``HuggingFaceAnalyzer`` takes its router
and Hub URLs as arguments, so these tests point the real SDK at a small
aiohttp server that answers the way ``router.huggingface.co`` does (the
catalog is a trimmed copy of its real ``/v1/models`` payload) — which means
a status code reaches the code under test as the SDK's own exception, not as
whatever a ``MagicMock`` was told to raise.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from blink_downloader.analyzer import (
    AnalyzerSettings,
    HuggingFaceAnalyzer,
    ProviderCredentials,
    create_analyzer,
    lookup_model_pricing,
)
from blink_downloader.model_catalog import (
    DEFAULT_HUGGINGFACE_MODEL,
    huggingface_price,
    huggingface_vision_entries,
    is_huggingface_vision_entry,
    split_huggingface_model,
)

_FRAME = b"\xff\xd8\xff\xe0not-really-a-jpeg\xff\xd9"
_VERDICT = (
    '{"suspicious": true, "confidence": 0.8, "description": "A person at the car."}'
)


def _provider(
    name: str,
    price_in: float | None,
    price_out: float | None,
    throughput: float,
    structured: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "provider": name,
        "status": "live",
        "throughput": throughput,
        "supports_structured_output": structured,
    }
    if price_in is not None:
        row["pricing"] = {"input": price_in, "output": price_out}
    return row


def _model(
    model_id: str,
    providers: list[dict[str, Any]],
    inputs: tuple[str, ...] = ("text", "image"),
    outputs: tuple[str, ...] = ("text",),
) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "architecture": {
            "input_modalities": list(inputs),
            "output_modalities": list(outputs),
        },
        "providers": providers,
    }


#: A trimmed copy of the router's catalog: the shapes that matter, not 127 rows.
_CATALOG = [
    _model(
        "google/gemma-4-26B-A4B-it",
        [
            _provider("novita", 0.13, 0.4, 60.0),
            _provider("scaleway", 0.285, 0.57, 90.0),
            _provider("deepinfra", 0.07, 0.34, 40.0),
            _provider("featherless-ai", None, None, 20.0),
        ],
    ),
    _model("Qwen/Qwen3.5-9B", [_provider("together", 0.17, 0.25, 70.0)]),
    # Takes an image but classifies it rather than describing it.
    _model("meta-llama/Llama-Guard-4-12B", [_provider("deepinfra", 0.18, 0.18, 30.0)]),
    # Text in, text out: nothing to look at a frame with.
    _model(
        "deepseek-ai/DeepSeek-R1",
        [_provider("novita", 0.5, 2.0, 50.0)],
        inputs=("text",),
    ),
    # Served, but no backend publishes a price for it.
    _model("zai-org/GLM-4.6V", [_provider("novita", None, None, 25.0)]),
    # "Sao10K" contains the substring "o1" -- see test_hugging_face_ids_are_never_priced_as_openai.
    _model("Sao10K/L3-8B-Stheno-v3.2", [_provider("novita", 0.1, 0.1, 10.0)]),
]


class FakeRouter:
    """The two Hugging Face hosts the analyzer talks to, on one local port."""

    def __init__(self) -> None:
        self.catalog: list[dict[str, Any]] = list(_CATALOG)
        self.catalog_status = 200
        # When set, served instead of {"data": catalog} -- for malformed payloads.
        self.catalog_payload: Any = None
        self.chat_requests: list[dict[str, Any]] = []
        self.chat_replies: list[Callable[[dict[str, Any]], web.Response]] = []
        self.whoami_status = 200
        self.whoami_auth: list[str] = []

    # -- replies a test can queue ------------------------------------------

    @staticmethod
    def ok(
        content: str | None = _VERDICT,
        prompt_tokens: int = 1200,
        completion_tokens: int = 40,
        finish_reason: str = "stop",
        usage: bool = True,
    ) -> Callable[[dict[str, Any]], web.Response]:
        def reply(_body: dict[str, Any]) -> web.Response:
            body: dict[str, Any] = {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "x",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            }
            if usage:
                body["usage"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                }
            return web.json_response(body)

        return reply

    @staticmethod
    def error(
        status: int, message: str, code: str = ""
    ) -> Callable[[dict[str, Any]], web.Response]:
        def reply(_body: dict[str, Any]) -> web.Response:
            return web.json_response(
                {"error": {"message": message, "type": "error", "code": code or None}},
                status=status,
                # Without this the SDK retries 429/5xx itself, with backoff,
                # and a test would wait seconds for an answer it already has.
                headers={"x-should-retry": "false"},
            )

        return reply

    # -- handlers -----------------------------------------------------------

    async def models(self, _request: web.Request) -> web.Response:
        if self.catalog_status != 200:
            return web.Response(status=self.catalog_status)
        if self.catalog_payload is not None:
            return web.json_response(self.catalog_payload)
        return web.json_response({"object": "list", "data": self.catalog})

    async def chat(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.chat_requests.append(body)
        reply = self.chat_replies.pop(0) if len(self.chat_replies) > 1 else None
        if reply is None:
            reply = self.chat_replies[0] if self.chat_replies else self.ok()
        return reply(body)

    async def whoami(self, request: web.Request) -> web.Response:
        self.whoami_auth.append(request.headers.get("Authorization", ""))
        return web.json_response({"name": "someone"}, status=self.whoami_status)


@pytest.fixture
async def router() -> AsyncGenerator[tuple[FakeRouter, TestServer]]:
    fake = FakeRouter()
    app = web.Application()
    app.router.add_get("/v1/models", fake.models)
    app.router.add_post("/v1/chat/completions", fake.chat)
    app.router.add_get("/api/whoami-v2", fake.whoami)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, server
    finally:
        await server.close()


def _analyzer(
    server: TestServer,
    model: str = DEFAULT_HUGGINGFACE_MODEL,
    api_key: str = "hf_test_token",
) -> HuggingFaceAnalyzer:
    base = str(server.make_url("")).rstrip("/")
    analyzer = HuggingFaceAnalyzer(
        api_key=api_key, model=model, prompt="Is anything suspicious?"
    )
    analyzer.router_url = f"{base}/v1"
    analyzer.hub_url = base
    return analyzer


@pytest.fixture
async def analyzer(
    router: tuple[FakeRouter, TestServer],
) -> AsyncGenerator[HuggingFaceAnalyzer]:
    instance = _analyzer(router[1])
    try:
        yield instance
    finally:
        await instance.close()


# ---------------------------------------------------------------------------
# The pure catalog functions
# ---------------------------------------------------------------------------


def test_split_huggingface_model_separates_the_routing_suffix() -> None:
    assert split_huggingface_model("Qwen/Qwen3.5-9B") == ("Qwen/Qwen3.5-9B", "")
    assert split_huggingface_model("Qwen/Qwen3.5-9B:cheapest") == (
        "Qwen/Qwen3.5-9B",
        "cheapest",
    )
    assert split_huggingface_model(" Qwen/Qwen3.5-9B:Novita ") == (
        "Qwen/Qwen3.5-9B",
        "novita",
    )
    assert split_huggingface_model("") == ("", "")


def test_only_models_that_take_an_image_and_describe_it_are_vision_entries() -> None:
    kept = {row["id"] for row in _CATALOG if is_huggingface_vision_entry(row)}
    assert "google/gemma-4-26B-A4B-it" in kept
    assert "meta-llama/Llama-Guard-4-12B" not in kept
    assert "deepseek-ai/DeepSeek-R1" not in kept


def test_price_defaults_to_the_fastest_backend_the_router_would_pick() -> None:
    gemma = _CATALOG[0]
    # scaleway has the highest throughput among the priced backends.
    assert huggingface_price(gemma) == (0.285, 0.57)
    assert huggingface_price(gemma, "fastest") == (0.285, 0.57)
    assert huggingface_price(gemma, "preferred") == (0.285, 0.57)


def test_price_can_be_pinned_to_the_cheapest_or_to_one_named_backend() -> None:
    gemma = _CATALOG[0]
    assert huggingface_price(gemma, "cheapest") == (0.07, 0.34)
    assert huggingface_price(gemma, "novita") == (0.13, 0.4)


def test_price_is_unknown_rather_than_guessed() -> None:
    gemma = _CATALOG[0]
    # Served by featherless-ai, but it publishes no price.
    assert huggingface_price(gemma, "featherless-ai") is None
    assert huggingface_price(gemma, "not-a-backend") is None
    assert huggingface_price(_CATALOG[4]) is None


def test_the_cheapest_models_are_listed_first_and_unpriced_ones_last() -> None:
    names = [e["name"] for e in huggingface_vision_entries(_CATALOG)]
    assert names == [
        "Sao10K/L3-8B-Stheno-v3.2",
        "Qwen/Qwen3.5-9B",
        "google/gemma-4-26B-A4B-it",
        "zai-org/GLM-4.6V",
    ]


def test_hugging_face_ids_are_never_priced_as_openai() -> None:
    """``Sao10K`` contains ``o1``, which the substring match priced as
    OpenAI o1 -- $15/$60 per million tokens for a model that costs a tenth of
    a dollar."""
    assert lookup_model_pricing("Sao10K/L3-8B-Stheno-v3.2") is None
    assert lookup_model_pricing("hf.co/someone/gpt-4o-clone:latest") is None
    # The providers the tables are for are unaffected.
    assert lookup_model_pricing("gpt-4o-mini") == (0.15, 0.60)
    assert lookup_model_pricing("claude-haiku-4-5") == (1.00, 5.00)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_provider_name_and_default_model() -> None:
    a = HuggingFaceAnalyzer(api_key="t", model="", prompt="p")
    assert a.provider_name == "huggingface"
    assert a.model_name() == DEFAULT_HUGGINGFACE_MODEL
    # What was configured is what labels the stored results, suffix included.
    b = HuggingFaceAnalyzer(api_key="t", model="Qwen/Qwen3.5-9B:cheapest", prompt="p")
    assert b.model_name() == "Qwen/Qwen3.5-9B:cheapest"


async def test_no_frames_means_no_request(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    assert await analyzer._call_model([], "prompt") == ""
    assert router[0].chat_requests == []


# ---------------------------------------------------------------------------
# fetch_models and prices
# ---------------------------------------------------------------------------


async def test_fetch_models_lists_vision_models_cheapest_first_with_prices(
    analyzer: HuggingFaceAnalyzer,
) -> None:
    models = await analyzer.fetch_models()
    assert [m["name"] for m in models][:3] == [
        "Sao10K/L3-8B-Stheno-v3.2",
        "Qwen/Qwen3.5-9B",
        "google/gemma-4-26B-A4B-it",
    ]
    assert models[1]["pricing"] == {"input": 0.17, "output": 0.25}
    assert models[-1]["pricing"] is None
    assert all(m["id"] == m["name"] for m in models)


async def test_fetch_models_falls_back_when_the_catalog_is_unreachable(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].catalog_status = 503
    models = await analyzer.fetch_models()
    assert DEFAULT_HUGGINGFACE_MODEL in [m["name"] for m in models]
    assert all("pricing" not in m for m in models)


async def test_the_catalog_is_fetched_once_and_then_reused(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    await analyzer.fetch_models()
    router[0].catalog_status = 500
    # A failed refresh must not throw away the prices already known.
    analyzer._catalog_fetched_at = 0.0
    await analyzer.refresh_pricing()
    assert analyzer.price_for_model("Qwen/Qwen3.5-9B") == (0.17, 0.25)


async def test_prices_are_unknown_until_the_catalog_has_been_loaded(
    analyzer: HuggingFaceAnalyzer,
) -> None:
    assert analyzer.price_for_model("Qwen/Qwen3.5-9B") is None
    await analyzer.refresh_pricing()
    assert analyzer.price_for_model("Qwen/Qwen3.5-9B") == (0.17, 0.25)
    # A routing suffix changes which backend is priced, not which model.
    assert analyzer.price_for_model("google/gemma-4-26B-A4B-it:cheapest") == (
        0.07,
        0.34,
    )
    assert analyzer.price_for_model("not/in-the-catalog") is None
    assert analyzer.price_for_model("zai-org/GLM-4.6V") is None


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


async def test_a_clip_is_sent_as_images_then_the_prompt(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [FakeRouter.ok(prompt_tokens=900, completion_tokens=30)]
    result = await analyzer._call_model([_FRAME, _FRAME], "Describe the clip.")

    assert json.loads(result)["suspicious"] is True
    assert analyzer.last_prompt_tokens == 900
    assert analyzer.last_completion_tokens == 30

    (sent,) = router[0].chat_requests
    assert sent["model"] == DEFAULT_HUGGINGFACE_MODEL
    system, user = sent["messages"]
    assert system["role"] == "system"
    parts = user["content"]
    assert [p["type"] for p in parts] == ["image_url", "image_url", "text"]
    expected = "data:image/jpeg;base64," + base64.b64encode(_FRAME).decode()
    assert parts[0]["image_url"]["url"] == expected
    assert parts[2]["text"] == "Describe the clip."
    assert sent["max_tokens"] == 1024


async def test_the_answer_is_asked_for_as_a_json_schema(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    await analyzer._call_model([_FRAME], "p")
    fmt = router[0].chat_requests[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"]["required"] == [
        "suspicious",
        "confidence",
        "description",
    ]


async def test_a_routing_suffix_is_passed_through_to_the_router(
    router: tuple[FakeRouter, TestServer],
) -> None:
    a = _analyzer(router[1], model="Qwen/Qwen3.5-9B:cheapest")
    try:
        await a._call_model([_FRAME], "p")
    finally:
        await a.close()
    assert router[0].chat_requests[0]["model"] == "Qwen/Qwen3.5-9B:cheapest"


async def test_the_token_is_sent_as_the_bearer_credential() -> None:
    seen: list[str] = []

    async def spy(request: web.Request) -> web.Response:
        seen.append(request.headers["Authorization"])
        return FakeRouter.ok()(await request.json())

    app = web.Application()
    app.router.add_post("/v1/chat/completions", spy)
    server = TestServer(app)
    await server.start_server()
    try:
        a = _analyzer(server, api_key="hf_secret")
        try:
            await a._call_model([_FRAME], "p")
        finally:
            await a.close()
    finally:
        await server.close()
    assert seen == ["Bearer hf_secret"]


async def test_a_thinking_block_is_not_mistaken_for_the_answer(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    thought = '<think>Should I answer {"suspicious": false}? Hmm.</think>\n' + _VERDICT
    router[0].chat_replies = [FakeRouter.ok(thought)]
    result = await analyzer._call_model([_FRAME], "p")
    suspicious, confidence, summary = analyzer.parse_response(result)
    assert (suspicious, confidence, summary) == (True, 0.8, "A person at the car.")


async def test_an_answer_cut_off_inside_its_thinking_is_no_answer(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [FakeRouter.ok('<think>The person is {"suspicious": tr')]
    assert await analyzer._call_model([_FRAME], "p") == ""


# ---------------------------------------------------------------------------
# A backend that refuses the response schema
# ---------------------------------------------------------------------------


async def test_a_backend_that_refuses_the_schema_is_asked_again_without_it(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [
        FakeRouter.error(400, "response_format json_schema is not supported"),
        FakeRouter.ok(),
    ]
    with caplog.at_level(logging.INFO):
        result = await analyzer._call_model([_FRAME], "p")

    assert json.loads(result)["suspicious"] is True
    first, second = router[0].chat_requests
    assert "response_format" in first
    assert "response_format" not in second
    assert "does not accept a response schema" in caplog.text


async def test_the_schema_is_not_sent_again_once_a_backend_has_refused_it(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [
        FakeRouter.error(400, "Unsupported: guided_json"),
        FakeRouter.ok(),
    ]
    await analyzer._call_model([_FRAME], "p")
    await analyzer._call_model([_FRAME], "p")
    assert ["response_format" in r for r in router[0].chat_requests] == [
        True,
        False,
        False,
    ]


async def test_a_400_about_something_else_is_not_retried_as_a_schema_problem(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [FakeRouter.error(400, "image input is not supported")]
    with caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert len(router[0].chat_requests) == 1
    assert analyzer.transient_error is False
    assert "accepts images" in caplog.text


async def test_a_model_that_takes_too_few_images_is_pointed_at_sequential(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [
        FakeRouter.error(400, "At most 1 image(s) may be provided in one prompt.")
    ]
    with caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME, _FRAME], "p") == ""
    assert "sequential" in caplog.text
    assert analyzer.transient_error is False


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_a_rejected_token_is_permanent(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [FakeRouter.error(401, "Invalid username or password.")]
    with caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.transient_error is False
    assert analyzer.rate_limited is False
    assert "the token was rejected" in caplog.text


async def test_a_token_without_permission_is_permanent(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [FakeRouter.error(403, "forbidden")]
    with caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.transient_error is False
    assert "Make calls to Inference Providers" in caplog.text


async def test_an_empty_balance_stops_the_batch_but_keeps_the_clips(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [
        FakeRouter.error(402, "You have exceeded your monthly included credits")
    ]
    with caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME], "p") == ""
    # Stop working through the batch (every clip would fail the same way)...
    assert analyzer.rate_limited is True
    # ...but a clip is not at fault for it, so it stays queued for later.
    assert analyzer.transient_error is True
    assert "no credit left" in caplog.text
    assert "huggingface.co/settings/billing" in caplog.text


async def test_a_rate_limit_is_transient(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [FakeRouter.error(429, "slow down")]
    assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.rate_limited is True
    assert analyzer.transient_error is True


async def test_an_unknown_model_is_permanent_and_says_which(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [
        FakeRouter.error(404, "The model `x/y` does not exist", code="model_not_found")
    ]
    with caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.transient_error is False
    assert "huggingface_model" in caplog.text


async def test_a_404_with_no_detail_is_worth_retrying(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [lambda _b: web.Response(status=404)]
    assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.transient_error is True


async def test_a_validation_failure_is_permanent(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [FakeRouter.error(422, "messages.0.content invalid")]
    assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.transient_error is False


async def test_a_backend_failure_is_retried_on_a_later_cycle(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [FakeRouter.error(503, "model is loading")]
    with caplog.at_level(logging.WARNING):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert analyzer.transient_error is True
    assert "HTTP 503" in caplog.text


async def test_an_unreachable_router_is_transient(
    router: tuple[FakeRouter, TestServer],
) -> None:
    a = _analyzer(router[1])
    await router[1].close()
    try:
        assert await a._call_model([_FRAME], "p") == ""
    finally:
        await a.close()
    assert a.transient_error is True


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


async def test_health_check_needs_a_token(
    router: tuple[FakeRouter, TestServer],
) -> None:
    a = _analyzer(router[1], api_key="")
    try:
        assert await a.health_check() is False
    finally:
        await a.close()
    assert router[0].whoami_auth == []


async def test_health_check_asks_the_hub_who_the_token_is(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    assert await analyzer.health_check() is True
    assert router[0].whoami_auth == ["Bearer hf_test_token"]


async def test_health_check_rejects_a_token_the_hub_does_not_know(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].whoami_status = 401
    with caplog.at_level(logging.ERROR):
        assert await analyzer.health_check() is False
    assert "the token was rejected" in caplog.text


async def test_health_check_accepts_a_token_too_narrow_to_read_its_own_profile(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    """A fine-grained token with only the inference permission is exactly the
    token the docs tell people to make; a 403 from whoami must not read as
    'AI offline'."""
    router[0].whoami_status = 403
    assert await analyzer.health_check() is True


async def test_health_check_is_offline_when_the_hub_is_down(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].whoami_status = 502
    assert await analyzer.health_check() is False


async def test_health_check_is_offline_when_the_hub_is_unreachable(
    router: tuple[FakeRouter, TestServer],
) -> None:
    a = _analyzer(router[1])
    await router[1].close()
    try:
        assert await a.health_check() is False
    finally:
        await a.close()


async def test_health_check_is_cached(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    await analyzer.health_check()
    await analyzer.health_check()
    assert len(router[0].whoami_auth) == 1


async def test_health_check_names_a_model_the_router_does_not_serve(
    router: tuple[FakeRouter, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    a = _analyzer(router[1], model="google/gemma-4-26B-A4B-itt:cheapest")
    try:
        with caplog.at_level(logging.WARNING):
            assert await a.health_check() is True
    finally:
        await a.close()
    assert "gemma-4-26B-A4B-itt" in caplog.text
    assert "not in the Inference Providers catalog" in caplog.text


async def test_health_check_stays_quiet_about_a_model_it_could_not_check(
    router: tuple[FakeRouter, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    router[0].catalog_status = 500
    a = _analyzer(router[1], model="anything/at-all")
    try:
        with caplog.at_level(logging.WARNING):
            assert await a.health_check() is True
    finally:
        await a.close()
    assert "catalog" not in caplog.text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create_analyzer_builds_a_huggingface_analyzer() -> None:
    a = create_analyzer(
        "huggingface",
        "prompt",
        AnalyzerSettings(),
        ProviderCredentials(
            huggingface_api_key="hf_x", huggingface_model="Qwen/Qwen3.5-9B"
        ),
    )
    assert isinstance(a, HuggingFaceAnalyzer)
    assert a.model_name() == "Qwen/Qwen3.5-9B"


def test_create_analyzer_without_a_token_disables_analysis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert create_analyzer("huggingface", "prompt") is None
    assert "requires huggingface_api_key" in caplog.text


def test_huggingface_can_be_the_escalation_tier_with_its_own_model() -> None:
    tier1 = create_analyzer(
        "ollama",
        "prompt",
        AnalyzerSettings(),
        ProviderCredentials(
            ollama_url="http://ollama:11434",
            ollama_model="llava:7b",
            huggingface_api_key="hf_x",
            huggingface_model="Qwen/Qwen3.5-9B",
        ),
        escalation_provider="huggingface",
        escalation_model="google/gemma-4-26B-A4B-it:cheapest",
    )
    assert tier1 is not None
    tier2 = tier1.escalation_analyzer
    assert isinstance(tier2, HuggingFaceAnalyzer)
    assert tier2.model_name() == "google/gemma-4-26B-A4B-it:cheapest"


# ---------------------------------------------------------------------------
# Documented catalog fields and health-check caching
# ---------------------------------------------------------------------------


def test_a_backend_that_is_currently_free_prices_at_zero() -> None:
    """The router documents ``is_free`` as a temporary promotion in which the
    backend charges nothing for the model, whatever ``pricing`` still lists."""
    entry = _model(
        "org/promo",
        [
            {
                "provider": "novita",
                "status": "live",
                "is_free": True,
                "pricing": {"input": 0.3, "output": 0.9},
            }
        ],
    )
    assert huggingface_price(entry) == (0.0, 0.0)
    # ...and a promo with no listed price at all is free too, not unknown.
    bare = _model(
        "org/bare-promo", [{"provider": "novita", "status": "live", "is_free": True}]
    )
    assert huggingface_price(bare) == (0.0, 0.0)


def test_is_free_false_changes_nothing() -> None:
    entry = _model(
        "org/paid",
        [
            {
                "provider": "novita",
                "status": "live",
                "is_free": False,
                "pricing": {"input": 0.3, "output": 0.9},
            }
        ],
    )
    assert huggingface_price(entry) == (0.3, 0.9)


def test_a_backend_in_error_is_not_priced() -> None:
    entry = _model(
        "org/down",
        [
            {
                "provider": "novita",
                "status": "error",
                "pricing": {"input": 0.3, "output": 0.9},
            }
        ],
    )
    assert huggingface_price(entry) is None


async def test_a_good_health_check_is_reused_for_ten_minutes(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    """whoami-v2 is rate limited on purpose (huggingface_hub says so and
    advises caching), and a token that works does not stop every 30 s."""
    import time

    assert await analyzer.health_check() is True
    analyzer._last_health_check_time = time.monotonic() - 300
    assert await analyzer.health_check() is True
    assert len(router[0].whoami_auth) == 1
    analyzer._last_health_check_time = time.monotonic() - 700
    assert await analyzer.health_check() is True
    assert len(router[0].whoami_auth) == 2


async def test_a_failed_health_check_is_retried_after_thirty_seconds(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    import time

    router[0].whoami_status = 502
    assert await analyzer.health_check() is False
    router[0].whoami_status = 200
    analyzer._last_health_check_time = time.monotonic() - 10
    assert await analyzer.health_check() is False  # still inside the short cache
    analyzer._last_health_check_time = time.monotonic() - 40
    assert await analyzer.health_check() is True  # recovery is noticed promptly
    assert len(router[0].whoami_auth) == 2


async def test_a_rate_limited_whoami_says_nothing_against_the_token(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].whoami_status = 429
    assert await analyzer.health_check() is True


# ---------------------------------------------------------------------------
# An answer the token limit cut off is not a verdict
# ---------------------------------------------------------------------------


async def test_an_answer_cut_off_by_the_token_limit_is_a_failed_call_not_a_verdict(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Parsed, ``{"suspicious": fal`` would fall back to keyword matching and
    be recorded as "not suspicious" -- a missed event. As a failed call it is
    a visible failed clip, which is what the shared pipeline does with an
    empty reply."""
    router[0].chat_replies = [
        FakeRouter.ok('{"suspicious": fal', finish_reason="length")
    ]
    with caplog.at_level(logging.WARNING):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert "cut off" in caplog.text
    assert "1024" in caplog.text
    # Worth another try later: the next sample of the model may finish in time.
    assert analyzer.transient_error is True


async def test_a_model_that_used_the_whole_allowance_thinking_is_a_failed_call(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [FakeRouter.ok(None, finish_reason="length")]
    with caplog.at_level(logging.WARNING):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert "cut off" in caplog.text


async def test_a_complete_answer_that_happened_to_reach_the_limit_is_kept(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [FakeRouter.ok(_VERDICT, finish_reason="length")]
    assert json.loads(await analyzer._call_model([_FRAME], "p"))["suspicious"] is True


async def test_an_answer_that_simply_stopped_is_untouched(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    router[0].chat_replies = [FakeRouter.ok("not json at all", finish_reason="stop")]
    with caplog.at_level(logging.WARNING):
        assert await analyzer._call_model([_FRAME], "p") == "not json at all"
    assert "cut off" not in caplog.text


# ---------------------------------------------------------------------------
# Odd inputs, and failures with no better name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload", [{"data": "oops"}, ["not", "an", "object"], {"object": "list"}]
)
async def test_a_catalog_that_is_not_a_list_of_models_is_ignored(
    router: tuple[FakeRouter, TestServer],
    analyzer: HuggingFaceAnalyzer,
    payload: Any,
) -> None:
    router[0].catalog_payload = payload
    models = await analyzer.fetch_models()
    assert DEFAULT_HUGGINGFACE_MODEL in [m["name"] for m in models]
    assert analyzer.price_for_model("Qwen/Qwen3.5-9B") is None


def test_a_model_that_does_not_answer_in_text_is_not_a_vision_analyst() -> None:
    """An image generator takes an image prompt and gives back pixels."""
    generator = _model(
        "org/image-editor",
        [_provider("novita", 0.1, 0.1, 10.0)],
        outputs=("image",),
    )
    assert is_huggingface_vision_entry(generator) is False


async def test_a_missing_openai_package_is_reported_not_raised(
    analyzer: HuggingFaceAnalyzer, caplog: pytest.LogCaptureFixture
) -> None:
    with patch.dict(sys.modules, {"openai": None}), caplog.at_level(logging.ERROR):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert "openai package is not installed" in caplog.text


async def test_a_reply_without_usage_leaves_the_token_counts_alone(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [FakeRouter.ok(usage=False)]
    assert json.loads(await analyzer._call_model([_FRAME], "p"))["suspicious"] is True
    assert (analyzer.last_prompt_tokens, analyzer.last_completion_tokens) == (0, 0)


async def test_a_reply_with_no_choices_is_an_empty_answer(
    router: tuple[FakeRouter, TestServer], analyzer: HuggingFaceAnalyzer
) -> None:
    router[0].chat_replies = [lambda _b: web.json_response({"id": "x", "choices": []})]
    assert await analyzer._call_model([_FRAME], "p") == ""


async def test_an_unexpected_failure_is_logged_and_returns_nothing(
    analyzer: HuggingFaceAnalyzer, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(
            HuggingFaceAnalyzer,
            "_create_completion",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        caplog.at_level(logging.WARNING),
    ):
        assert await analyzer._call_model([_FRAME], "p") == ""
    assert "request failed: boom" in caplog.text
