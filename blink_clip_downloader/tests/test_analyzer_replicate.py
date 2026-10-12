"""The Replicate analyzer, against a stand-in for Replicate's HTTP API.

``ReplicateAnalyzer`` takes its base URL as an argument, so these tests run
the real code against a small aiohttp server instead of mocking the client.
The model schemas are the ``Input`` properties Replicate publishes on each
model's API page (read off the pages themselves, trimmed to the fields that
matter), which is what the provider's field mapping is built from — so a
change to how a real model spells an input shows up here as a failing test
rather than as a clip that analyzes without its picture.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from blink_downloader.analyzer import (
    AnalyzerSettings,
    ProviderCredentials,
    ReplicateAnalyzer,
    create_analyzer,
    replicate_provider,
)
from blink_downloader.model_catalog import (
    DEFAULT_REPLICATE_MODEL,
    REPLICATE_VISION_MODELS,
    replicate_input_plan,
)

_FRAME = b"\xff\xd8\xff\xe0not-really-a-jpeg\xff\xd9"
_OTHER_FRAME = b"\xff\xd8\xff\xe0a-different-frame\xff\xd9"
_CALM = '{"suspicious": false, "confidence": 0.9, "description": "A car passes."}'
_ALARM = (
    '{"suspicious": true, "confidence": 0.7, "description": "A person at the car."}'
)
_URI = {"type": "string", "format": "uri"}
_URIS = {"type": "array", "items": {"type": "string", "format": "uri"}}

#: ``Input`` properties of real official models, as Replicate publishes them.
_SCHEMAS: dict[str, dict[str, Any]] = {
    "google/gemini-2.5-flash": {
        "prompt": {"type": "string"},
        "images": _URIS,
        "videos": _URIS,
        "system_instruction": {"type": "string"},
        "max_output_tokens": {"type": "integer"},
        "temperature": {"type": "number"},
    },
    "openai/gpt-4o-mini": {
        "prompt": {"type": "string"},
        "messages": {"type": "array", "items": {"type": "object"}},
        "image_input": _URIS,
        "system_prompt": {"type": "string"},
        "max_completion_tokens": {"type": "integer"},
    },
    "anthropic/claude-4.5-sonnet": {
        "prompt": {"type": "string"},
        "image": _URI,
        "system_prompt": {"type": "string"},
        "max_tokens": {"type": "integer"},
        "max_image_resolution": {"type": "number"},
    },
    # Takes a picture and a prompt but nothing else.
    "someone/bare-vlm": {"prompt": {"type": "string"}, "media": _URI},
    # Text only.
    "meta/text-only": {"prompt": {"type": "string"}, "max_tokens": {"type": "integer"}},
    # An image but no way to ask it anything.
    "someone/captioner": {"image": _URI},
}


def _prediction(
    output: Any = None,
    status: str = "succeeded",
    metrics: Any = None,
    prediction_id: str = "p1abc",
    error: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": prediction_id,
        "status": status,
        "output": output if output is not None else [_CALM[:20], _CALM[20:]],
        "error": error,
        "urls": {
            "get": "https://evil.example/steal",
            "cancel": "https://evil.example/c",
        },
    }
    if metrics is not None:
        body["metrics"] = metrics
    return body


class FakeReplicate:
    """The three Replicate endpoints the provider uses, on one local port."""

    def __init__(self) -> None:
        self.schemas = dict(_SCHEMAS)
        self.valid_token = "r8_good"
        self.model_requests: list[str] = []
        self.created: list[dict[str, Any]] = []
        self.create_headers: list[dict[str, str]] = []
        self.polled: list[str] = []
        # Queued replies: each returns (status, json body).
        self.create_replies: list[Callable[[dict[str, Any]], tuple[int, Any]]] = []
        self.poll_replies: list[Callable[[], tuple[int, Any]]] = []
        self.create_delay = 0.0

    def authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {self.valid_token}"

    async def model(self, request: web.Request) -> web.Response:
        name = f"{request.match_info['owner']}/{request.match_info['name']}"
        self.model_requests.append(name)
        if not self.authorized(request):
            return web.json_response({"title": "Unauthenticated"}, status=401)
        if name not in self.schemas:
            return web.json_response({"detail": "Not found"}, status=404)
        return web.json_response(
            {
                "owner": request.match_info["owner"],
                "latest_version": {
                    "openapi_schema": {
                        "components": {
                            "schemas": {"Input": {"properties": self.schemas[name]}}
                        }
                    }
                },
            }
        )

    async def create(self, request: web.Request) -> web.Response:
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        body = await request.json()
        self.created.append(body["input"])
        self.create_headers.append(dict(request.headers))
        reply = self.create_replies.pop(0) if len(self.create_replies) > 1 else None
        if reply is None:
            reply = (
                self.create_replies[0]
                if self.create_replies
                else (lambda _b: (201, _prediction()))
            )
        status, payload = reply(body["input"])
        if isinstance(payload, str):
            return web.Response(text=payload, status=status, content_type="text/html")
        return web.json_response(payload, status=status)

    async def poll(self, request: web.Request) -> web.Response:
        self.polled.append(request.match_info["prediction_id"])
        reply = self.poll_replies.pop(0) if len(self.poll_replies) > 1 else None
        if reply is None:
            reply = (
                self.poll_replies[0]
                if self.poll_replies
                else (lambda: (200, _prediction()))
            )
        status, payload = reply()
        return web.json_response(payload, status=status)


@pytest.fixture
async def replicate() -> AsyncGenerator[tuple[FakeReplicate, TestServer]]:
    fake = FakeReplicate()
    app = web.Application()
    app.router.add_get("/v1/models/{owner}/{name}", fake.model)
    app.router.add_post("/v1/models/{owner}/{name}/predictions", fake.create)
    app.router.add_get("/v1/predictions/{prediction_id}", fake.poll)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, server
    finally:
        await server.close()


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(replicate_provider, "_POLL_SECONDS", 0.0)


def _analyzer(
    server: TestServer,
    model: str = "openai/gpt-4o-mini",
    api_key: str = "r8_good",
) -> ReplicateAnalyzer:
    analyzer = ReplicateAnalyzer(
        api_key=api_key, model=model, prompt="Is anything suspicious?"
    )
    analyzer.api_url = f"{str(server.make_url('')).rstrip('/')}/v1"
    return analyzer


def _uri(frame: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(frame).decode()


# ---------------------------------------------------------------------------
# Reading a model's schema
# ---------------------------------------------------------------------------


def test_gemini_style_schema_takes_a_list_of_images() -> None:
    plan = replicate_input_plan(_SCHEMAS["google/gemini-2.5-flash"])
    assert plan is not None
    assert (plan.images_field, plan.images_are_list) == ("images", True)
    assert plan.system_field == "system_instruction"
    assert plan.max_tokens_field == "max_output_tokens"


def test_gpt_style_schema_takes_a_list_of_images() -> None:
    plan = replicate_input_plan(_SCHEMAS["openai/gpt-4o-mini"])
    assert plan is not None
    assert (plan.images_field, plan.images_are_list) == ("image_input", True)
    assert plan.system_field == "system_prompt"
    assert plan.max_tokens_field == "max_completion_tokens"


def test_claude_style_schema_takes_one_image() -> None:
    plan = replicate_input_plan(_SCHEMAS["anthropic/claude-4.5-sonnet"])
    assert plan is not None
    assert (plan.images_field, plan.images_are_list) == ("image", False)
    assert plan.max_tokens_field == "max_tokens"


def test_a_model_with_no_system_or_token_input_is_still_usable() -> None:
    plan = replicate_input_plan(_SCHEMAS["someone/bare-vlm"])
    assert plan is not None
    assert plan.images_field == "media"
    assert plan.system_field is None
    assert plan.max_tokens_field is None


def test_a_model_that_cannot_look_at_a_picture_has_no_plan() -> None:
    assert replicate_input_plan(_SCHEMAS["meta/text-only"]) is None


def test_a_model_that_cannot_be_asked_anything_has_no_plan() -> None:
    assert replicate_input_plan(_SCHEMAS["someone/captioner"]) is None


def test_an_images_input_that_takes_text_is_not_an_image_input() -> None:
    """A list of strings is not a list of files: ``format: uri`` is the claim."""
    props = {
        "prompt": {"type": "string"},
        "images": {"type": "array", "items": {"type": "string"}},
    }
    assert replicate_input_plan(props) is None


def test_a_list_input_beats_a_single_one_when_a_model_has_both() -> None:
    props = {"prompt": {"type": "string"}, "images": _URIS, "image": _URI}
    plan = replicate_input_plan(props)
    assert plan is not None
    assert (plan.images_field, plan.images_are_list) == ("images", True)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------


def test_provider_name_and_default_model() -> None:
    a = ReplicateAnalyzer(api_key="t", model="", prompt="p")
    assert a.provider_name == "replicate"
    assert a.model_name() == DEFAULT_REPLICATE_MODEL


async def test_fetch_models_offers_the_official_vision_models() -> None:
    a = ReplicateAnalyzer(api_key="t", model="", prompt="p")
    models = await a.fetch_models()
    assert [m["name"] for m in models] == list(REPLICATE_VISION_MODELS)
    assert DEFAULT_REPLICATE_MODEL in [m["name"] for m in models]
    await a.close()


async def test_replicate_has_no_pricing_to_offer() -> None:
    """Replicate publishes no rate this add-on can read, so the usage page
    says N/A rather than showing a cost nobody established."""
    a = ReplicateAnalyzer(api_key="t", model="", prompt="p")
    await a.refresh_pricing()
    assert a.price_for_model(DEFAULT_REPLICATE_MODEL) is None


async def test_no_frames_means_no_request(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    a = _analyzer(replicate[1])
    assert await a._call_model([], "p") == ""
    assert replicate[0].model_requests == []


# ---------------------------------------------------------------------------
# A model that takes a list of images: one request for the clip
# ---------------------------------------------------------------------------


async def test_a_gemini_clip_is_one_request_under_geminis_own_input_names(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    fake.create_replies = [
        lambda _i: (
            201,
            _prediction(metrics={"input_token_count": 910, "output_token_count": 31}),
        )
    ]
    a = _analyzer(server, model="google/gemini-2.5-flash")

    text = await a._call_model([_FRAME, _OTHER_FRAME], "Describe the clip.")

    assert json.loads(text)["suspicious"] is False
    (sent,) = fake.created
    assert sent["prompt"] == "Describe the clip."
    assert sent["images"] == [_uri(_FRAME), _uri(_OTHER_FRAME)]
    assert sent["system_instruction"]
    assert sent["max_output_tokens"] == 2048
    assert (a.last_prompt_tokens, a.last_completion_tokens) == (910, 31)


async def test_a_gpt_clip_uses_gpts_input_names(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    a = _analyzer(server)
    await a._call_model([_FRAME], "p")
    (sent,) = fake.created
    assert sent["image_input"] == [_uri(_FRAME)]
    assert sent["system_prompt"]
    assert sent["max_completion_tokens"] == 2048
    assert "images" not in sent


async def test_the_request_carries_the_token_and_asks_to_wait_and_to_be_cancelled(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    a = _analyzer(server)
    await a._call_model([_FRAME], "p")
    headers = {k.lower(): v for k, v in fake.create_headers[0].items()}
    assert headers["authorization"] == "Bearer r8_good"
    assert headers["prefer"] == "wait=60"
    assert headers["cancel-after"] == "4m"


async def test_inputs_a_model_does_not_have_are_not_sent(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    a = _analyzer(server, model="someone/bare-vlm")
    fake.schemas["someone/bare-vlm"] = _SCHEMAS["someone/bare-vlm"]
    await a._call_model([_FRAME], "p")
    assert set(fake.created[0]) == {"prompt", "media"}


async def test_the_schema_is_read_once_per_analyzer(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    a = _analyzer(server)
    await a._call_model([_FRAME], "p")
    await a._call_model([_FRAME], "p")
    assert fake.model_requests == ["openai/gpt-4o-mini"]


async def test_output_may_arrive_whole_rather_than_as_chunks(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    fake.create_replies = [lambda _i: (201, _prediction(output=f"  {_ALARM}\n"))]
    assert await _analyzer(server)._call_model([_FRAME], "p") == _ALARM


async def test_a_prediction_with_no_output_is_an_empty_answer(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    body = _prediction()
    body["output"] = None
    fake.create_replies = [lambda _i: (201, body)]
    assert await _analyzer(server)._call_model([_FRAME], "p") == ""


@pytest.mark.parametrize(
    "metrics",
    [None, "nope", {}, {"input_token_count": "12", "output_token_count": -3}],
)
async def test_tokens_are_zero_when_replicate_does_not_report_them(
    replicate: tuple[FakeReplicate, TestServer], metrics: Any
) -> None:
    fake, server = replicate
    body = _prediction()
    if metrics is not None:
        body["metrics"] = metrics
    fake.create_replies = [lambda _i: (201, body)]
    a = _analyzer(server)
    a._last_prompt_tokens = 99  # a stale count from the previous call
    await a._call_model([_FRAME], "p")
    assert (a.last_prompt_tokens, a.last_completion_tokens) == (0, 0)


# ---------------------------------------------------------------------------
# A model that takes one image: one request per frame
# ---------------------------------------------------------------------------


async def test_a_claude_clip_is_one_request_per_frame_and_the_most_alarming_wins(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate

    def by_frame(sent: dict[str, Any]) -> tuple[int, Any]:
        verdict = _ALARM if sent["image"] == _uri(_OTHER_FRAME) else _CALM
        return 201, _prediction(
            output=verdict, metrics={"input_token_count": 100, "output_token_count": 10}
        )

    fake.create_replies = [by_frame]
    a = _analyzer(server, model="anthropic/claude-4.5-sonnet")

    text = await a._call_model([_FRAME, _OTHER_FRAME, _FRAME], "p")

    assert json.loads(text)["suspicious"] is True
    assert sorted(sent["image"] for sent in fake.created) == sorted(
        [_uri(_FRAME), _uri(_OTHER_FRAME), _uri(_FRAME)]
    )
    assert all("images" not in sent for sent in fake.created)
    assert (a.last_prompt_tokens, a.last_completion_tokens) == (300, 30)


async def test_frames_whose_answers_do_not_parse_still_beat_no_answer(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    answers = iter(["", "something unstructured", _CALM])
    fake.create_replies = [lambda _i: (201, _prediction(output=next(answers)))]
    a = _analyzer(server, model="anthropic/claude-4.5-sonnet")
    text = await a._call_model([_FRAME, _FRAME, _FRAME], "p")
    assert json.loads(text)["suspicious"] is False


async def test_a_frame_that_fails_fails_the_clip(
    replicate: tuple[FakeReplicate, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = replicate
    fake.create_replies = [lambda _i: (500, {"detail": "boom"})]
    a = _analyzer(server, model="anthropic/claude-4.5-sonnet")
    with caplog.at_level(logging.WARNING):
        assert await a._call_model([_FRAME, _OTHER_FRAME], "p") == ""
    assert a.transient_error is True


# ---------------------------------------------------------------------------
# A prediction that outlives the wait
# ---------------------------------------------------------------------------


async def test_an_unfinished_prediction_is_polled_at_our_own_url(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    fake.create_replies = [lambda _i: (201, _prediction(status="starting"))]
    fake.poll_replies = [
        lambda: (200, _prediction(status="processing")),
        lambda: (200, _prediction(output=_ALARM)),
    ]
    text = await _analyzer(server)._call_model([_FRAME], "p")
    assert json.loads(text)["suspicious"] is True
    # Polled by id at the configured host -- the response's own `urls` point
    # at evil.example, which would have been sent the account's token.
    assert fake.polled == ["p1abc", "p1abc"]


async def test_a_prediction_with_an_unusable_id_is_not_polled(
    replicate: tuple[FakeReplicate, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = replicate
    fake.create_replies = [
        lambda _i: (201, _prediction(status="starting", prediction_id="../../etc"))
    ]
    with caplog.at_level(logging.WARNING):
        assert await _analyzer(server)._call_model([_FRAME], "p") == ""
    assert fake.polled == []
    assert "no usable id" in caplog.text


async def test_a_prediction_that_never_finishes_is_given_up_on(
    replicate: tuple[FakeReplicate, TestServer],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake, server = replicate
    monkeypatch.setattr(replicate_provider, "_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(replicate_provider, "_POLL_SECONDS", 0.01)
    fake.create_replies = [lambda _i: (201, _prediction(status="processing"))]
    fake.poll_replies = [lambda: (200, _prediction(status="processing"))]
    a = _analyzer(server)
    with caplog.at_level(logging.WARNING):
        assert await a._call_model([_FRAME], "p") == ""
    assert "did not finish in time" in caplog.text
    assert a.transient_error is True


@pytest.mark.parametrize("status", ["failed", "canceled"])
async def test_a_prediction_that_ends_badly_names_why(
    replicate: tuple[FakeReplicate, TestServer],
    caplog: pytest.LogCaptureFixture,
    status: str,
) -> None:
    fake, server = replicate
    fake.create_replies = [
        lambda _i: (201, _prediction(status=status, error="content flagged"))
    ]
    a = _analyzer(server)
    with caplog.at_level(logging.WARNING):
        assert await a._call_model([_FRAME], "p") == ""
    assert "content flagged" in caplog.text
    assert a.transient_error is True


async def test_a_prediction_that_ends_without_a_reason_is_named_by_its_status(
    replicate: tuple[FakeReplicate, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = replicate
    fake.create_replies = [lambda _i: (201, _prediction(status="canceled"))]
    with caplog.at_level(logging.WARNING):
        assert await _analyzer(server)._call_model([_FRAME], "p") == ""
    assert "canceled" in caplog.text


# ---------------------------------------------------------------------------
# HTTP failures
# ---------------------------------------------------------------------------


async def test_a_rejected_token_is_permanent(
    replicate: tuple[FakeReplicate, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    a = _analyzer(replicate[1], api_key="r8_wrong")
    with caplog.at_level(logging.ERROR):
        assert await a._call_model([_FRAME], "p") == ""
    assert a.transient_error is False
    assert "replicate_api_key" in caplog.text


@pytest.mark.parametrize(
    ("status", "body", "permanent", "rate_limited", "needle"),
    [
        (402, {"detail": "Insufficient credit"}, False, True, "no credit left"),
        (404, {"detail": "Not found"}, True, False, "no such official model"),
        (422, {"detail": "image: not a uri"}, True, False, "image: not a uri"),
        (429, {"detail": "Request was throttled. ~30s"}, False, True, "throttled"),
        (500, {"title": "Internal"}, False, False, "HTTP 500"),
        (503, {"error": "overloaded"}, False, False, "overloaded"),
        (502, "<html>bad gateway</html>", False, False, "HTTP 502"),
    ],
)
async def test_each_failure_status_is_classified_by_what_to_do_about_it(
    replicate: tuple[FakeReplicate, TestServer],
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: Any,
    permanent: bool,
    rate_limited: bool,
    needle: str,
) -> None:
    fake, server = replicate
    fake.create_replies = [lambda _i: (status, body)]
    a = _analyzer(server)
    with caplog.at_level(logging.WARNING):
        assert await a._call_model([_FRAME], "p") == ""
    assert a.transient_error is (not permanent)
    assert a.rate_limited is rate_limited
    assert needle in caplog.text


async def test_a_success_that_is_not_an_object_is_not_an_answer(
    replicate: tuple[FakeReplicate, TestServer], caplog: pytest.LogCaptureFixture
) -> None:
    fake, server = replicate
    fake.create_replies = [lambda _i: (201, ["not", "an", "object"])]
    with caplog.at_level(logging.WARNING):
        assert await _analyzer(server)._call_model([_FRAME], "p") == ""
    assert "other than an object" in caplog.text


async def test_an_unreachable_replicate_is_transient(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    a = _analyzer(replicate[1])
    await replicate[1].close()
    assert await a._call_model([_FRAME], "p") == ""
    assert a.transient_error is True


async def test_a_request_that_takes_too_long_is_transient(
    replicate: tuple[FakeReplicate, TestServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, server = replicate
    fake.create_delay = 0.5
    monkeypatch.setattr(
        replicate_provider, "_TIMEOUT", aiohttp.ClientTimeout(total=0.1)
    )
    a = _analyzer(server)
    assert await a._call_model([_FRAME], "p") == ""
    assert a.transient_error is True


# ---------------------------------------------------------------------------
# Models that cannot be used
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model", ["gpt-4o-mini", "openai/gpt-4o-mini:abc123", "a/b/c", "/name", "owner/"]
)
async def test_only_official_owner_slash_name_models_are_accepted(
    replicate: tuple[FakeReplicate, TestServer],
    caplog: pytest.LogCaptureFixture,
    model: str,
) -> None:
    a = _analyzer(replicate[1], model=model)
    with caplog.at_level(logging.ERROR):
        assert await a._call_model([_FRAME], "p") == ""
    assert "owner/name" in caplog.text
    assert a.transient_error is False
    assert replicate[0].model_requests == []


@pytest.mark.parametrize("model", ["meta/text-only", "someone/captioner"])
async def test_a_model_that_cannot_analyze_a_clip_says_so(
    replicate: tuple[FakeReplicate, TestServer],
    caplog: pytest.LogCaptureFixture,
    model: str,
) -> None:
    a = _analyzer(replicate[1], model=model)
    with caplog.at_level(logging.ERROR):
        assert await a._call_model([_FRAME], "p") == ""
    assert "does not take both a prompt and an image" in caplog.text
    assert a.transient_error is False


async def test_a_model_replicate_does_not_have_is_permanent(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    a = _analyzer(replicate[1], model="nobody/nothing")
    assert await a._call_model([_FRAME], "p") == ""
    assert a.transient_error is False


async def test_a_schema_with_no_input_section_is_unusable(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    fake.schemas["x/empty"] = {}
    a = _analyzer(server, model="x/empty")
    assert await a._call_model([_FRAME], "p") == ""
    assert a.transient_error is False


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


async def test_health_check_needs_a_token() -> None:
    assert (
        await ReplicateAnalyzer(api_key="", model="", prompt="p").health_check()
        is False
    )


async def test_health_check_passes_when_token_and_model_both_work(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    fake, server = replicate
    a = _analyzer(server)
    assert await a.health_check() is True
    assert await a.health_check() is True
    assert fake.model_requests == ["openai/gpt-4o-mini"]


async def test_health_check_fails_for_a_bad_token(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    assert await _analyzer(replicate[1], api_key="r8_wrong").health_check() is False


async def test_health_check_fails_for_a_model_that_does_not_exist(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    assert await _analyzer(replicate[1], model="nobody/nothing").health_check() is False


async def test_health_check_fails_when_replicate_is_unreachable(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    a = _analyzer(replicate[1])
    await replicate[1].close()
    assert await a.health_check() is False


async def test_a_failed_health_check_does_not_touch_the_last_calls_flags(
    replicate: tuple[FakeReplicate, TestServer],
) -> None:
    a = _analyzer(replicate[1], api_key="r8_wrong")
    await a.health_check()
    assert a.transient_error is True
    assert a.rate_limited is False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create_analyzer_builds_a_replicate_analyzer() -> None:
    a = create_analyzer(
        "replicate",
        "prompt",
        AnalyzerSettings(),
        ProviderCredentials(replicate_api_key="r8_x", replicate_model="openai/gpt-4o"),
    )
    assert isinstance(a, ReplicateAnalyzer)
    assert a.model_name() == "openai/gpt-4o"


def test_create_analyzer_without_a_token_disables_analysis(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert create_analyzer("replicate", "prompt") is None
    assert "requires replicate_api_key" in caplog.text


def test_replicate_can_be_the_escalation_tier_with_its_own_model() -> None:
    tier1 = create_analyzer(
        "ollama",
        "prompt",
        AnalyzerSettings(),
        ProviderCredentials(
            ollama_url="http://ollama:11434",
            ollama_model="llava:7b",
            replicate_api_key="r8_x",
        ),
        escalation_provider="replicate",
        escalation_model="anthropic/claude-4.5-sonnet",
    )
    assert tier1 is not None
    tier2 = tier1.escalation_analyzer
    assert isinstance(tier2, ReplicateAnalyzer)
    assert tier2.model_name() == "anthropic/claude-4.5-sonnet"
