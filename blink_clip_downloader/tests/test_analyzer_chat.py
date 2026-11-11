"""Text-only questions to each provider (``BaseAnalyzer.chat``).

The OpenAI, Anthropic and Hugging Face SDKs and Ollama's HTTP client all run
for real against a small local server standing in for each API, so a status
code reaches the code under test as the SDK's own exception rather than as
whatever a mock was told to raise.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

import anthropic
import openai
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from blink_downloader.analyzer import (
    AnthropicAnalyzer,
    ClipAnalyzer,
    HuggingFaceAnalyzer,
    MoondreamCloudAnalyzer,
    OllamaCloudAnalyzer,
    OpenAIAnalyzer,
    ReplicateAnalyzer,
)
from blink_downloader.analyzer.chat import (
    DEFAULT_MAX_ANSWER_TOKENS,
    ChatError,
    ChatNotSupportedError,
    ChatReply,
    describe_failure,
)

SYSTEM = "You answer questions about a report."
MESSAGES = [
    {"role": "user", "content": "Anything odd?"},
    {"role": "assistant", "content": "Nothing."},
    {"role": "user", "content": "Really?"},
]

Handler = Callable[[dict[str, Any]], web.Response]


class Stand:
    """A local API: records each request body, replies from a queue."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: Handler = lambda _b: web.json_response({})

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        return self.reply(body)


@pytest.fixture
async def stand() -> AsyncGenerator[tuple[Stand, str]]:
    fake = Stand()
    app = web.Application()
    for path in ("/v1/messages", "/v1/chat/completions", "/api/chat"):
        app.router.add_post(path, fake.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield fake, str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


def _failure(status: int) -> Handler:
    return lambda _b: web.json_response(
        {"error": {"message": "secret-key-sk-123 leaked", "type": "x"}},
        status=status,
        headers={"x-should-retry": "false"},
    )


class TestChatVocabulary:
    def test_describe_failure_never_includes_the_exceptions_text(self) -> None:
        class WithStatus(Exception):
            status_code = 429

        assert (
            describe_failure("OpenAI", WithStatus("sk-123"))
            == "OpenAI could not answer: WithStatus (HTTP 429)"
        )
        assert (
            describe_failure("OpenAI", ValueError("sk-123"))
            == "OpenAI could not answer: ValueError"
        )

    def test_unsupported_is_a_kind_of_chat_error(self) -> None:
        assert issubclass(ChatNotSupportedError, ChatError)

    def test_a_reply_is_frozen_with_zero_default_counts(self) -> None:
        reply = ChatReply("hi")
        assert (reply.prompt_tokens, reply.completion_tokens) == (0, 0)
        with pytest.raises(AttributeError):
            reply.text = "x"  # type: ignore[misc]


class TestUnsupportedProviders:
    async def test_the_base_default_says_so_by_name(self) -> None:
        analyzer = MoondreamCloudAnalyzer(api_key="k", prompt="p")
        assert analyzer.supports_chat is False
        with pytest.raises(ChatNotSupportedError, match="cannot answer questions"):
            await analyzer.chat(SYSTEM, MESSAGES)

    async def test_replicate_cannot_chat_either(self) -> None:
        analyzer = ReplicateAnalyzer(
            api_key="k", model="google/gemini-3-flash", prompt="p"
        )
        assert analyzer.supports_chat is False
        with pytest.raises(ChatNotSupportedError):
            await analyzer.chat(SYSTEM, MESSAGES)


class TestAnthropic:
    def _analyzer(self, url: str, model: str = "claude-haiku-4-5") -> AnthropicAnalyzer:
        analyzer = AnthropicAnalyzer(api_key="k", model=model, prompt="p")
        analyzer._client = anthropic.AsyncAnthropic(
            api_key="k", base_url=url, max_retries=0
        )
        return analyzer

    async def test_a_question_is_text_only_and_returns_tokens(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response(
            {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [
                    {"type": "text", "text": " First. "},
                    {"type": "text", "text": "Second."},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 90, "output_tokens": 12},
            }
        )
        analyzer = self._analyzer(url)

        reply = await analyzer.chat(SYSTEM, MESSAGES)
        picked = await analyzer.chat(SYSTEM, MESSAGES, max_tokens=50, model="claude-x")

        assert reply == ChatReply("First. \nSecond.", 90, 12)
        body = fake.requests[0]
        assert body["model"] == "claude-haiku-4-5"
        assert body["system"] == SYSTEM
        assert body["messages"] == MESSAGES
        assert body["max_tokens"] == DEFAULT_MAX_ANSWER_TOKENS
        assert "image" not in json.dumps(body)
        assert fake.requests[1]["model"] == "claude-x"
        assert fake.requests[1]["max_tokens"] == 50
        assert picked.completion_tokens == 12

    async def test_a_failure_carries_only_its_class_and_status(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = _failure(401)
        with pytest.raises(ChatError) as info:
            await self._analyzer(url).chat(SYSTEM, MESSAGES)
        assert (
            str(info.value)
            == "Anthropic could not answer: AuthenticationError (HTTP 401)"
        )
        assert "secret" not in str(info.value)


class TestOpenAI:
    def _analyzer(self, url: str, model: str = "gpt-4o-mini") -> OpenAIAnalyzer:
        analyzer = OpenAIAnalyzer(api_key="k", model=model, prompt="p")
        analyzer._client = openai.AsyncOpenAI(
            api_key="k", base_url=f"{url}/v1", max_retries=0
        )
        return analyzer

    @staticmethod
    def _ok(content: str | None, usage: bool = True) -> Handler:
        def reply(_b: dict[str, Any]) -> web.Response:
            body: dict[str, Any] = {
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "x",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            }
            if usage:
                body["usage"] = {
                    "prompt_tokens": 70,
                    "completion_tokens": 9,
                    "total_tokens": 79,
                }
            return web.json_response(body)

        return reply

    async def test_a_question_puts_the_system_text_first(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = self._ok("  Answer.  ")
        reply = await self._analyzer(url).chat(SYSTEM, MESSAGES, model="gpt-4o")
        assert reply == ChatReply("Answer.", 70, 9)
        body = fake.requests[0]
        assert body["model"] == "gpt-4o"
        assert body["messages"][0] == {"role": "system", "content": SYSTEM}
        assert body["messages"][1:] == MESSAGES
        assert body["max_completion_tokens"] == DEFAULT_MAX_ANSWER_TOKENS

    @pytest.mark.parametrize(
        "model", ["o3-mini", "o1", "o4-mini", "gpt-5", "gpt-5.1-mini", "gpt-6"]
    )
    async def test_reasoning_models_get_headroom_for_their_hidden_thinking(
        self, stand: tuple[Stand, str], model: str
    ) -> None:
        fake, url = stand
        fake.reply = self._ok("ok")
        await self._analyzer(url, model).chat(SYSTEM, MESSAGES)
        assert (
            fake.requests[0]["max_completion_tokens"]
            == DEFAULT_MAX_ANSWER_TOKENS + 4096
        )

    async def test_no_content_or_usage_reads_as_empty_and_free(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = self._ok(None, usage=False)
        assert await self._analyzer(url).chat(SYSTEM, MESSAGES) == ChatReply("", 0, 0)

    async def test_no_choices_reads_as_empty(self, stand: tuple[Stand, str]) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response(
            {
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "x",
                "choices": [],
            }
        )
        assert (await self._analyzer(url).chat(SYSTEM, MESSAGES)).text == ""

    async def test_a_failure_is_a_chat_error_without_the_sdks_text(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = _failure(429)
        with pytest.raises(ChatError) as info:
            await self._analyzer(url).chat(SYSTEM, MESSAGES)
        assert str(info.value) == "OpenAI could not answer: RateLimitError (HTTP 429)"


class TestHuggingFace:
    def _analyzer(self, url: str) -> HuggingFaceAnalyzer:
        analyzer = HuggingFaceAnalyzer(
            api_key="hf_k", model="Qwen/Qwen3.5-9B:cheapest", prompt="p"
        )
        analyzer.router_url = f"{url}/v1"
        analyzer.hub_url = url
        return analyzer

    async def test_a_question_goes_through_the_router_without_thinking(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response(
            {
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "x",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "<think>hmm</think>All quiet.",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 6,
                    "total_tokens": 56,
                },
            }
        )
        analyzer = self._analyzer(url)
        try:
            reply = await analyzer.chat(SYSTEM, MESSAGES)
        finally:
            await analyzer.close()
        assert reply == ChatReply("All quiet.", 50, 6)
        body = fake.requests[0]
        assert body["model"] == "Qwen/Qwen3.5-9B:cheapest"
        assert body["messages"][0] == {"role": "system", "content": SYSTEM}
        assert body["max_tokens"] == DEFAULT_MAX_ANSWER_TOKENS

    async def test_a_missing_answer_is_empty_and_a_failure_is_a_chat_error(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response(
            {
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "x",
                "choices": [],
            }
        )
        analyzer = self._analyzer(url)
        try:
            assert await analyzer.chat(SYSTEM, MESSAGES, model="a/b") == ChatReply(
                "", 0, 0
            )
            fake.reply = _failure(500)
            with pytest.raises(ChatError, match="Hugging Face could not answer"):
                await analyzer.chat(SYSTEM, MESSAGES)
        finally:
            await analyzer.close()


class TestOllama:
    def _analyzer(self, url: str) -> ClipAnalyzer:
        return ClipAnalyzer(ollama_url=url, model="llama3.2", prompt="p")

    async def test_a_question_has_no_images_no_json_format_and_no_thinking(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response(
            {
                "message": {"role": "assistant", "content": "  Fine.  "},
                "prompt_eval_count": 300,
                "eval_count": 25,
            }
        )
        analyzer = self._analyzer(url)
        try:
            reply = await analyzer.chat(
                SYSTEM, MESSAGES, max_tokens=64, model="mistral"
            )
        finally:
            await analyzer.close()
        assert reply == ChatReply("Fine.", 300, 25)
        body = fake.requests[0]
        assert body["model"] == "mistral"
        assert body["stream"] is False
        assert body["think"] is False
        assert "format" not in body
        assert body["options"] == {"num_predict": 64}
        assert body["messages"][0] == {"role": "system", "content": SYSTEM}
        assert all("images" not in m for m in body["messages"])

    async def test_missing_counts_read_as_zero(self, stand: tuple[Stand, str]) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response({"message": {"content": "ok"}})
        analyzer = self._analyzer(url)
        try:
            assert await analyzer.chat(SYSTEM, MESSAGES) == ChatReply("ok", 0, 0)
        finally:
            await analyzer.close()

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"message": None},
            {"message": {"content": None}},
            {"message": {"content": "  "}},
            [],
        ],
    )
    async def test_an_empty_answer_is_an_error_not_a_blank_reply(
        self, stand: tuple[Stand, str], payload: Any
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response(payload)
        analyzer = self._analyzer(url)
        try:
            with pytest.raises(ChatError, match="empty answer"):
                await analyzer.chat(SYSTEM, MESSAGES)
        finally:
            await analyzer.close()

    async def test_an_http_error_names_the_status_only(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.Response(status=503, text="secret details")
        analyzer = self._analyzer(url)
        try:
            with pytest.raises(ChatError) as info:
                await analyzer.chat(SYSTEM, MESSAGES)
        finally:
            await analyzer.close()
        assert str(info.value) == "Ollama could not answer: HTTP 503"

    async def test_an_unreachable_server_is_a_chat_error(self) -> None:
        analyzer = self._analyzer("http://127.0.0.1:9")
        try:
            with pytest.raises(
                ChatError, match="Ollama could not answer: ClientConnector"
            ):
                await analyzer.chat(SYSTEM, MESSAGES)
        finally:
            await analyzer.close()

    async def test_an_answer_that_is_not_json_is_a_chat_error(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.Response(
            text="not json", content_type="application/json"
        )
        analyzer = self._analyzer(url)
        try:
            with pytest.raises(ChatError, match="could not answer"):
                await analyzer.chat(SYSTEM, MESSAGES)
        finally:
            await analyzer.close()

    async def test_ollama_cloud_answers_the_same_way_with_its_key(
        self, stand: tuple[Stand, str]
    ) -> None:
        fake, url = stand
        fake.reply = lambda _b: web.json_response({"message": {"content": "cloud"}})
        analyzer = OllamaCloudAnalyzer(api_key="key", model="gpt-oss:120b", prompt="p")
        analyzer._ollama_url = url
        try:
            assert analyzer.supports_chat is True
            assert (await analyzer.chat(SYSTEM, MESSAGES)).text == "cloud"
        finally:
            await analyzer.close()
