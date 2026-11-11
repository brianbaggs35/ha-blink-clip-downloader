"""The Security Assistant tab's API (media_server/assistant.py)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader.analyzer.chat import (
    ChatError,
    ChatNotSupportedError,
    ChatReply,
)
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer
from blink_downloader.media_server import assistant as assistant_routes


class FakeAnalyzer:
    """A chat-capable analyzer: records every question, answers from a script."""

    provider_name = "openai"
    supports_chat = True
    escalation_analyzer = None

    def __init__(self) -> None:
        self.models: Any = [
            {"name": "gpt-4o-mini"},
            {"name": "gpt-4o"},
            {"name": "gpt-4o"},
            {"name": ""},
            "junk",
        ]
        self.reply: Any = ChatReply(
            "All quiet.", prompt_tokens=100, completion_tokens=20
        )
        self.calls: list[dict[str, Any]] = []
        self.online: Any = True
        self.gate: asyncio.Event | None = None

    def model_name(self) -> str:
        return "gpt-4o-mini"

    def price_for_model(self, _model: str) -> None:
        return None

    async def refresh_pricing(self) -> None:
        return None

    async def health_check(self) -> bool:
        if isinstance(self.online, Exception):
            raise self.online
        return bool(self.online)

    async def fetch_models(self) -> list[Any]:
        if isinstance(self.models, Exception):
            raise self.models
        return self.models

    async def chat(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = 0,
        model: str | None = None,
    ) -> ChatReply:
        self.calls.append({"system": system, "messages": messages, "model": model})
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@dataclass
class Api:
    client: TestClient
    server: MediaServer
    analyzer: FakeAnalyzer | None
    settings_file: Path
    db: ClipDatabase
    extra: dict[str, Any] = field(default_factory=dict)

    def saved(self) -> dict[str, Any]:
        return json.loads(self.settings_file.read_text())


async def _make(
    db: ClipDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    analyzer: FakeAnalyzer | None,
    **kwargs: Any,
) -> AsyncGenerator[Api]:
    settings_file = tmp_path / "assistant_settings.json"
    monkeypatch.setattr(MediaServer, "_ASSISTANT_SETTINGS_FILE", settings_file)
    server = MediaServer(db=db, port=0, analyzer=analyzer, **kwargs)  # type: ignore[arg-type]
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()
    try:
        yield Api(client, server, analyzer, settings_file, db)
    finally:
        await client.close()


@pytest.fixture
async def api(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Api]:
    async for a in _make(db, tmp_path, monkeypatch, FakeAnalyzer()):
        yield a


@pytest.fixture
async def bare(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Api]:
    async for a in _make(db, tmp_path, monkeypatch, None):
        yield a


async def _chat(api: Api, title: str | None = None) -> dict[str, Any]:
    resp = await api.client.post(
        "/api/assistant/chats", json={} if title is None else {"title": title}
    )
    assert resp.status == 201
    return await resp.json()


async def _ask(api: Api, chat_id: str, question: Any = "How is everything?") -> Any:
    return await api.client.post(
        f"/api/assistant/chats/{chat_id}/ask", json={"question": question}
    )


class TestStatusAndModels:
    async def test_status_without_a_provider(self, bare: Api) -> None:
        data = await (await bare.client.get("/api/assistant/status")).json()
        assert data["enabled"] is False
        assert "provider" not in data
        assert data["settings"] == {"model": "", "days": 7}
        assert data["days_choices"] == [1, 3, 7, 14, 30]

    async def test_status_names_the_provider_and_whether_it_leaves_the_device(
        self, api: Api
    ) -> None:
        data = await (await api.client.get("/api/assistant/status")).json()
        assert data["enabled"] is True
        assert data["provider"] == "openai"
        assert data["default_model"] == "gpt-4o-mini"
        assert data["supported"] is True
        assert data["sends_off_device"] is True
        assert data["chat_limit"] == assistant_routes.MAX_CHATS
        assert data["message_limit"] == assistant_routes.MAX_MESSAGES_PER_CHAT

    async def test_a_local_provider_does_not_send_off_device(self, api: Api) -> None:
        api.analyzer.provider_name = "ollama"  # type: ignore[union-attr]
        data = await (await api.client.get("/api/assistant/status")).json()
        assert data["sends_off_device"] is False

    async def test_models_come_from_the_provider_deduplicated(self, api: Api) -> None:
        data = await (await api.client.get("/api/assistant/models")).json()
        assert data["models"] == ["gpt-4o-mini", "gpt-4o"]
        assert data["default"] == "gpt-4o-mini"
        assert data["provider"] == "openai"

    async def test_models_without_a_provider(self, bare: Api) -> None:
        data = await (await bare.client.get("/api/assistant/models")).json()
        assert data == {"enabled": False, "models": [], "default": ""}

    async def test_a_provider_that_cannot_list_models_gives_an_empty_list(
        self, api: Api
    ) -> None:
        api.analyzer.models = RuntimeError("boom")  # type: ignore[union-attr]
        data = await (await api.client.get("/api/assistant/models")).json()
        assert data["models"] == []

    async def test_a_saved_model_the_provider_dropped_stays_listed(
        self, api: Api
    ) -> None:
        api.settings_file.write_text(json.dumps({"model": "retired-model"}))
        data = await (await api.client.get("/api/assistant/models")).json()
        assert data["models"][-1] == "retired-model"


class TestSettings:
    async def test_defaults_when_the_file_is_missing_or_damaged(self, api: Api) -> None:
        assert api.server._read_assistant_settings() == {"model": "", "days": 7}
        api.settings_file.write_text("{nope")
        assert api.server._read_assistant_settings() == {"model": "", "days": 7}
        api.settings_file.write_text("[1, 2]")
        assert api.server._read_assistant_settings() == {"model": "", "days": 7}
        api.settings_file.write_text(json.dumps({"model": 5, "days": "x"}))
        assert api.server._read_assistant_settings() == {"model": "", "days": 7}

    async def test_a_listed_model_and_window_are_saved(self, api: Api) -> None:
        resp = await api.client.put(
            "/api/assistant/settings", json={"model": " gpt-4o ", "days": 14}
        )
        assert resp.status == 200
        assert (await resp.json())["settings"] == {"model": "gpt-4o", "days": 14}
        assert api.saved() == {"model": "gpt-4o", "days": 14}
        status = await (await api.client.get("/api/assistant/status")).json()
        assert status["settings"] == {"model": "gpt-4o", "days": 14}

    async def test_without_a_provider_only_the_default_can_be_saved(
        self, bare: Api
    ) -> None:
        resp = await bare.client.put("/api/assistant/settings", json={"model": "x"})
        assert resp.status == 400
        resp = await bare.client.put("/api/assistant/settings", json={"days": 3})
        assert resp.status == 200

    async def test_the_default_model_can_always_be_chosen(self, api: Api) -> None:
        api.analyzer.models = RuntimeError("down")  # type: ignore[union-attr]
        resp = await api.client.put("/api/assistant/settings", json={"model": ""})
        assert resp.status == 200
        assert api.saved() == {"model": "", "days": 7}

    async def test_a_model_outside_the_list_is_refused(self, api: Api) -> None:
        resp = await api.client.put("/api/assistant/settings", json={"model": "gpt-9"})
        assert resp.status == 400
        assert "not in the provider's list" in await resp.text()
        assert not api.settings_file.exists()

    async def test_the_model_already_saved_may_be_kept_when_the_list_is_down(
        self, api: Api
    ) -> None:
        api.settings_file.write_text(json.dumps({"model": "kept", "days": 3}))
        api.analyzer.models = RuntimeError("down")  # type: ignore[union-attr]
        resp = await api.client.put(
            "/api/assistant/settings", json={"model": "kept", "days": 1}
        )
        assert resp.status == 200
        assert api.saved() == {"model": "kept", "days": 1}

    @pytest.mark.parametrize("days", [0, 2, 31, "7", True, 7.5, None])
    async def test_days_must_be_one_of_the_choices(self, api: Api, days: Any) -> None:
        resp = await api.client.put(
            "/api/assistant/settings", json={"model": "", "days": days}
        )
        assert resp.status == 400
        assert "days must be one of" in await resp.text()

    async def test_bad_bodies(self, api: Api) -> None:
        resp = await api.client.put(
            "/api/assistant/settings",
            data="{nope",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await api.client.put("/api/assistant/settings", json=[1])).status == 400
        resp = await api.client.put("/api/assistant/settings", json={"model": 5})
        assert resp.status == 400
        assert "model must be text" in await resp.text()

    async def test_an_unwritable_file_is_reported_not_swallowed(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            MediaServer,
            "_ASSISTANT_SETTINGS_FILE",
            api.settings_file.parent / "no" / "x.json",
        )
        resp = await api.client.put("/api/assistant/settings", json={"model": ""})
        assert resp.status == 500


class TestChats:
    async def test_create_list_get_rename_delete(self, api: Api) -> None:
        chat = await _chat(api)
        assert chat["title"] == "New chat"
        named = await _chat(api, "  Porch\nquestions  ")
        assert named["title"] == "Porch questions"

        listing = await (await api.client.get("/api/assistant/chats")).json()
        assert {c["id"] for c in listing} == {chat["id"], named["id"]}

        got = await (await api.client.get(f"/api/assistant/chats/{chat['id']}")).json()
        assert got["messages"] == []

        resp = await api.client.patch(
            f"/api/assistant/chats/{chat['id']}", json={"title": "Night\nwatch"}
        )
        assert await resp.json() == {"title": "Night watch"}

        resp = await api.client.delete(f"/api/assistant/chats/{chat['id']}")
        assert await resp.json() == {"deleted": True}
        assert (
            await api.client.get(f"/api/assistant/chats/{chat['id']}")
        ).status == 404

    async def test_create_without_a_body_or_with_a_blank_title(self, api: Api) -> None:
        resp = await api.client.post("/api/assistant/chats")
        assert resp.status == 201
        assert (await resp.json())["title"] == "New chat"
        blank = await _chat(api, "   ")
        assert blank["title"] == "New chat"
        non_dict = await api.client.post("/api/assistant/chats", json=["x"])
        assert (await non_dict.json())["title"] == "New chat"

    async def test_create_with_invalid_json(self, api: Api) -> None:
        resp = await api.client.post(
            "/api/assistant/chats",
            data="{nope",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_there_is_a_limit_on_chats(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(assistant_routes, "MAX_CHATS", 2)
        await _chat(api)
        await _chat(api)
        resp = await api.client.post("/api/assistant/chats", json={})
        assert resp.status == 409
        assert "2 chats" in (await resp.json())["error"]

    async def test_rename_needs_a_name_and_a_chat(self, api: Api) -> None:
        chat = await _chat(api)
        url = f"/api/assistant/chats/{chat['id']}"
        assert (await api.client.patch(url, json={"title": "  "})).status == 400
        assert (await api.client.patch(url, json=["x"])).status == 400
        assert (await api.client.patch(url, json={})).status == 400
        bad = await api.client.patch(
            url, data="{nope", headers={"Content-Type": "application/json"}
        )
        assert bad.status == 400
        assert (
            await api.client.patch("/api/assistant/chats/missing", json={"title": "x"})
        ).status == 404

    async def test_deleting_a_missing_chat(self, api: Api) -> None:
        assert (await api.client.delete("/api/assistant/chats/missing")).status == 404


class TestAsk:
    async def test_an_answer_is_saved_titled_and_billed(self, api: Api) -> None:
        chat = await _chat(api)
        api.settings_file.write_text(json.dumps({"model": "gpt-4o", "days": 3}))

        resp = await _ask(api, chat["id"], "  Was anyone at the door?  ")

        assert resp.status == 200
        data = await resp.json()
        assert data["title"] == "Was anyone at the door?"
        user, bot = data["messages"]
        assert user["role"] == "user" and user["content"] == "Was anyone at the door?"
        assert bot["role"] == "assistant" and bot["content"] == "All quiet."
        assert bot["model"] == "gpt-4o"
        assert bot["days"] == 3
        assert (bot["prompt_tokens"], bot["completion_tokens"]) == (100, 20)

        call = api.analyzer.calls[0]  # type: ignore[union-attr]
        assert call["model"] == "gpt-4o"
        assert "last 3 day(s)" in call["system"]

        saved = await api.db.get_assistant_chat(chat["id"])
        assert saved is not None
        assert saved["title"] == "Was anyone at the door?"
        assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]

        usage = await api.db.get_token_usage_stats()
        assert usage["assistant"] == {
            "questions": 1,
            "tokens_prompt": 100,
            "tokens_completion": 20,
            "tokens_total": 120,
        }

    async def test_the_default_model_is_used_when_none_is_chosen(
        self, api: Api
    ) -> None:
        chat = await _chat(api)
        resp = await _ask(api, chat["id"])
        assert (await resp.json())["messages"][1]["model"] == "gpt-4o-mini"
        assert api.analyzer.calls[0]["model"] is None  # type: ignore[union-attr]

    async def test_a_second_question_keeps_the_title_and_sees_the_history(
        self, api: Api
    ) -> None:
        chat = await _chat(api)
        await _ask(api, chat["id"], "first")
        resp = await _ask(api, chat["id"], "second")
        assert (await resp.json())["title"] == "first"
        sent = api.analyzer.calls[1]["messages"]  # type: ignore[union-attr]
        assert [m["content"] for m in sent] == ["first", "All quiet.", "second"]

    async def test_a_renamed_chat_keeps_its_name(self, api: Api) -> None:
        chat = await _chat(api, "Mine")
        resp = await _ask(api, chat["id"], "hello")
        assert (await resp.json())["title"] == "Mine"

    async def test_without_a_provider(self, bare: Api) -> None:
        chat = await _chat(bare)
        resp = await _ask(bare, chat["id"])
        assert resp.status == 409
        assert "not configured" in (await resp.json())["error"]

    async def test_bad_bodies_and_missing_chats(self, api: Api) -> None:
        chat = await _chat(api)
        url = f"/api/assistant/chats/{chat['id']}/ask"
        bad = await api.client.post(
            url, data="{nope", headers={"Content-Type": "application/json"}
        )
        assert bad.status == 400
        assert (await api.client.post(url, json=["x"])).status == 400
        assert (await api.client.post(url, json={})).status == 400
        assert (await _ask(api, "missing")).status == 404

    async def test_an_empty_question_is_a_400(self, api: Api) -> None:
        chat = await _chat(api)
        resp = await _ask(api, chat["id"], "   ")
        assert resp.status == 400
        assert "Ask a question" in (await resp.json())["error"]
        assert api.analyzer.calls == []  # type: ignore[union-attr]

    async def test_a_full_chat_is_refused_before_any_money_is_spent(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(assistant_routes, "MAX_MESSAGES_PER_CHAT", 2)
        chat = await _chat(api)
        assert (await _ask(api, chat["id"])).status == 200
        resp = await _ask(api, chat["id"])
        assert resp.status == 409
        assert "chat is full" in (await resp.json())["error"]
        assert len(api.analyzer.calls) == 1  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (ChatNotSupportedError("No chat here"), 409),
            (ChatError("openai could not answer: Boom"), 502),
            (TimeoutError(), 504),
        ],
    )
    async def test_provider_failures_map_to_statuses(
        self, api: Api, error: Exception, status: int
    ) -> None:
        api.analyzer.reply = error  # type: ignore[union-attr]
        chat = await _chat(api)
        resp = await _ask(api, chat["id"])
        assert resp.status == status
        assert (await resp.json())["error"]
        saved = await api.db.get_assistant_chat(chat["id"])
        assert saved is not None and saved["messages"] == []
        assert (await api.db.get_token_usage_stats())["assistant"]["questions"] == 0

    async def test_an_unexpected_failure_does_not_leak_its_text(self, api: Api) -> None:
        api.analyzer.reply = RuntimeError("secret-key-sk-123")  # type: ignore[union-attr]
        chat = await _chat(api)
        resp = await _ask(api, chat["id"])
        assert resp.status == 500
        assert "secret" not in await resp.text()

    async def test_a_slow_answer_times_out(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(assistant_routes, "ANSWER_TIMEOUT_SECONDS", 0.05)
        api.analyzer.gate = asyncio.Event()  # type: ignore[union-attr]
        chat = await _chat(api)
        resp = await _ask(api, chat["id"])
        assert resp.status == 504

    async def test_only_one_question_is_answered_at_a_time(self, api: Api) -> None:
        gate = asyncio.Event()
        api.analyzer.gate = gate  # type: ignore[union-attr]
        chat = await _chat(api)
        first = asyncio.ensure_future(_ask(api, chat["id"], "one"))
        for _ in range(200):
            if api.server._assistant_lock.locked():
                break
            await asyncio.sleep(0.01)
        second = await _ask(api, chat["id"], "two")
        assert second.status == 429
        gate.set()
        assert (await first).status == 200
        assert len(api.analyzer.calls) == 1  # type: ignore[union-attr]

    async def test_a_chat_deleted_while_answering_is_a_404_but_the_spend_counts(
        self, api: Api
    ) -> None:
        gate = asyncio.Event()
        api.analyzer.gate = gate  # type: ignore[union-attr]
        chat = await _chat(api)
        pending = asyncio.ensure_future(_ask(api, chat["id"]))
        for _ in range(200):
            if api.server._assistant_lock.locked():
                break
            await asyncio.sleep(0.01)
        await api.db.delete_assistant_chat(chat["id"])
        gate.set()
        assert (await pending).status == 404
        assert (await api.db.get_token_usage_stats())["assistant"]["questions"] == 1


class TestLiveState:
    async def test_the_report_gets_what_only_the_running_addon_knows(
        self, db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        analyzer = FakeAnalyzer()
        queue = MagicMock()
        queue.get_queue_status = AsyncMock(return_value={"pending": 4})
        async for a in _make(
            db,
            tmp_path,
            monkeypatch,
            analyzer,
            analysis_queue=queue,
            face_recognition_enabled=True,
            list_camera_names=lambda: ["Porch"],
            get_sync_module_snapshot=lambda: [
                {"name": "Home", "armed": True, "online": True, "cameras": []}
            ],
        ):
            a.server.extra_status["connected"] = True
            chat = await _chat(a)
            assert (await _ask(a, chat["id"])).status == 200
            system = analyzer.calls[0]["system"]
            assert "Blink account connected: yes" in system
            assert "Cameras Blink reports: Porch" in system
            assert "Sync module Home: armed yes" in system
            assert "pending=4" in system
            assert "Face recognition switched on: yes" in system
            assert "reachable: yes" in system

    async def test_an_unreachable_provider_is_reported_as_offline(
        self, api: Api
    ) -> None:
        api.analyzer.online = RuntimeError("down")  # type: ignore[union-attr]
        live = await api.server._assistant_live_state()
        assert live["analysis"]["online"] is False
        assert live["camera_names"] == [] and live["sync_modules"] == []
        assert "queue" not in live

    async def test_without_a_provider_there_is_no_analysis_state(
        self, bare: Api
    ) -> None:
        live = await bare.server._assistant_live_state()
        assert "analysis" not in live


class TestUsageAndAccess:
    async def test_the_usage_page_prices_the_assistants_share(self, api: Api) -> None:
        await api.db.add_assistant_usage("openai", "gpt-4o", 1_000_000, 100_000)
        await api.db.add_assistant_usage("ollama", "llama3", 500, 50)
        data = await (await api.client.get("/api/ai/usage")).json()

        assert data["assistant"]["questions"] == 2
        assert data["assistant"]["tokens_total"] == 1_000_000 + 100_000 + 550
        # gpt-4o: $2.50 in / $10 out per 1M; llama3 is unpriced.
        assert data["assistant"]["cost"] == pytest.approx(2.5 + 1.0)
        assert data["total_estimated_cost"] == pytest.approx(2.5 + 1.0)
        rows = {r["model"]: r for r in data["by_model"]}
        assert rows["gpt-4o"]["assistant"] is True
        assert data["total_analyses"] == 0
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        day = next(d for d in data["daily"] if d["day"] == today)
        assert day["analyses"] == 0
        assert day["tokens_prompt"] == 1_000_500

    async def test_assistant_spend_with_only_unpriced_models_has_no_cost(
        self, api: Api
    ) -> None:
        await api.db.add_assistant_usage("ollama", "llama3", 10, 1)
        data = await (await api.client.get("/api/ai/usage")).json()
        assert data["assistant"]["cost"] is None

    async def test_no_assistant_route_is_reachable_with_the_access_token(self) -> None:
        from blink_downloader.media_server.access_control import TOKEN_ROUTES

        assert not any("/api/assistant" in path for _method, path in TOKEN_ROUTES)
