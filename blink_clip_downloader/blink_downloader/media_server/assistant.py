"""The Security Assistant tab's API: saved chats, the model it uses, and asking.

A chat is a titled list of messages kept in the database (see
``assistant_chats``), so it survives a reload and shows on every device. A
question is answered by the *base* analyzer, never the escalation one, from a
text report of the system's own records (see :mod:`blink_downloader.assistant`
for what it holds and the rules that keep faces and names out of it). The one
setting, which of that provider's models to ask, lives in
``/data/assistant_settings.json`` beside the other per-tab settings files
rather than in the add-on's configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from ..analyzer.chat import ChatError, ChatNotSupportedError
from ..assistant import (
    DAYS_CHOICES,
    DEFAULT_DAYS,
    LOCAL_PROVIDERS,
    AssistantInputError,
    ask,
    clamp_days,
    one_line,
)
from .core import _MediaServerBase
from .support import _INVALID_JSON_BODY, _INVALID_REQUEST_BODY, _SETTINGS_WRITE_FAILED

_LOGGER = logging.getLogger(__name__)

DEFAULT_CHAT_TITLE = "New chat"
MAX_TITLE_CHARS = 60
# What a chat may hold, and how many there may be: a runaway client, or a
# person who never deletes anything, should not grow the database without end.
MAX_CHATS = 50
MAX_MESSAGES_PER_CHAT = 200
# Longest a question may take to answer, across every provider's own retries.
ANSWER_TIMEOUT_SECONDS = 180

_NOT_CONFIGURED = "AI analysis is not configured, so the assistant has no model to ask"
_NO_SUCH_CHAT = "Chat not found"


def _title(value: object) -> str:
    return one_line(value, MAX_TITLE_CHARS)


class AssistantRoutesMixin(_MediaServerBase):
    """Chats, settings and questions for the Security Assistant tab."""

    _ASSISTANT_SETTINGS_FILE = Path("/data/assistant_settings.json")

    def _register_assistant_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/assistant/status", self._handle_assistant_status)
        app.router.add_get("/api/assistant/models", self._handle_assistant_models)
        app.router.add_put(
            "/api/assistant/settings", self._handle_assistant_settings_put
        )
        app.router.add_get("/api/assistant/chats", self._handle_assistant_chats_list)
        app.router.add_post("/api/assistant/chats", self._handle_assistant_chat_create)
        app.router.add_get(
            "/api/assistant/chats/{chat_id}", self._handle_assistant_chat_get
        )
        app.router.add_patch(
            "/api/assistant/chats/{chat_id}", self._handle_assistant_chat_rename
        )
        app.router.add_delete(
            "/api/assistant/chats/{chat_id}", self._handle_assistant_chat_delete
        )
        app.router.add_post(
            "/api/assistant/chats/{chat_id}/ask", self._handle_assistant_ask
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _read_assistant_settings(self) -> dict[str, Any]:
        """The saved settings; a missing or damaged file means the defaults."""
        data: Any = {}
        try:
            data = json.loads(self._ASSISTANT_SETTINGS_FILE.read_text())
        except (OSError, ValueError):
            pass
        if not isinstance(data, dict):
            data = {}
        model = data.get("model")
        return {
            "model": model.strip() if isinstance(model, str) else "",
            "days": clamp_days(data.get("days"), DEFAULT_DAYS),
        }

    async def _assistant_model_names(self) -> list[str]:
        """Models the base provider offers; empty when it cannot list them."""
        if self._analyzer is None:
            return []
        try:
            models = await self._analyzer.fetch_models()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Assistant could not list models: %s", exc)
            return []
        names = [
            str(m["name"]) for m in models if isinstance(m, dict) and m.get("name")
        ]
        return list(dict.fromkeys(names))

    async def _handle_assistant_status(self, _request: web.Request) -> web.Response:
        analyzer = self._analyzer
        data: dict[str, Any] = {
            "enabled": analyzer is not None,
            "settings": self._read_assistant_settings(),
            "days_choices": list(DAYS_CHOICES),
            "chat_limit": MAX_CHATS,
            "message_limit": MAX_MESSAGES_PER_CHAT,
        }
        if analyzer is not None:
            data["provider"] = analyzer.provider_name
            data["default_model"] = analyzer.model_name()
            data["supported"] = analyzer.supports_chat
            # Whether what the report holds (descriptions of what the cameras
            # saw, never pictures or names) goes to a service.
            data["sends_off_device"] = analyzer.provider_name not in LOCAL_PROVIDERS
        return web.json_response(data)

    async def _handle_assistant_models(self, _request: web.Request) -> web.Response:
        if self._analyzer is None:
            return web.json_response({"enabled": False, "models": [], "default": ""})
        names = await self._assistant_model_names()
        selected = self._read_assistant_settings()["model"]
        # A model chosen earlier that the provider no longer lists stays
        # selectable, so the picker never shows an empty choice.
        if selected and selected not in names:
            names.append(selected)
        return web.json_response(
            {
                "enabled": True,
                "models": names,
                "default": self._analyzer.model_name(),
                "provider": self._analyzer.provider_name,
            }
        )

    async def _handle_assistant_settings_put(
        self, request: web.Request
    ) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text=_INVALID_REQUEST_BODY)
        model = body.get("model", "")
        if not isinstance(model, str):
            raise web.HTTPBadRequest(text="model must be text")
        model = model.strip()
        days = body.get("days", DEFAULT_DAYS)
        if (
            isinstance(days, bool)
            or not isinstance(days, int)
            or days not in DAYS_CHOICES
        ):
            raise web.HTTPBadRequest(
                text="days must be one of " + ", ".join(str(d) for d in DAYS_CHOICES)
            )
        # A model is only ever picked from the provider's own list, so a typo
        # cannot be saved. If the list cannot be loaded, only "use the default"
        # can be saved, and that is not a typo.
        if model and model not in await self._assistant_model_names():
            current = self._read_assistant_settings()["model"]
            if model != current:
                raise web.HTTPBadRequest(
                    text="That model is not in the provider's list. Pick one from the list."
                )
        settings = {"model": model, "days": days}
        try:
            self._ASSISTANT_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
        except OSError as exc:
            _LOGGER.warning("Could not save assistant settings: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc
        return web.json_response({"saved": True, "settings": settings})

    # ------------------------------------------------------------------
    # Chats
    # ------------------------------------------------------------------

    async def _handle_assistant_chats_list(self, _request: web.Request) -> web.Response:
        return web.json_response(await self._db.list_assistant_chats())

    async def _handle_assistant_chat_create(self, request: web.Request) -> web.Response:
        title = DEFAULT_CHAT_TITLE
        if request.can_read_body:
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
            if isinstance(body, dict) and body.get("title") is not None:
                title = _title(body["title"]) or DEFAULT_CHAT_TITLE
        if len(await self._db.list_assistant_chats()) >= MAX_CHATS:
            return web.json_response(
                {
                    "error": f"You can keep {MAX_CHATS} chats. Delete one to start another."
                },
                status=409,
            )
        chat = await self._db.create_assistant_chat(uuid.uuid4().hex, title)
        return web.json_response(chat, status=201)

    async def _handle_assistant_chat_get(self, request: web.Request) -> web.Response:
        chat = await self._db.get_assistant_chat(request.match_info["chat_id"])
        if chat is None:
            raise web.HTTPNotFound(text=_NO_SUCH_CHAT)
        return web.json_response(chat)

    async def _handle_assistant_chat_rename(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
        title = _title(body.get("title")) if isinstance(body, dict) else ""
        if not title:
            raise web.HTTPBadRequest(text="Give the chat a name")
        if not await self._db.rename_assistant_chat(
            request.match_info["chat_id"], title
        ):
            raise web.HTTPNotFound(text=_NO_SUCH_CHAT)
        return web.json_response({"title": title})

    async def _handle_assistant_chat_delete(self, request: web.Request) -> web.Response:
        if not await self._db.delete_assistant_chat(request.match_info["chat_id"]):
            raise web.HTTPNotFound(text=_NO_SUCH_CHAT)
        return web.json_response({"deleted": True})

    # ------------------------------------------------------------------
    # Asking
    # ------------------------------------------------------------------

    async def _assistant_live_state(self) -> dict[str, Any]:
        """What only a running add-on knows, handed to the report builder."""
        analyzer = self._analyzer
        live: dict[str, Any] = {
            "status": dict(self.extra_status),
            "face_recognition_enabled": self._face_recognition_enabled,
            "camera_names": list(self._list_camera_names())
            if self._list_camera_names
            else [],
            "sync_modules": (
                self._get_sync_module_snapshot()
                if self._get_sync_module_snapshot
                else []
            ),
        }
        if analyzer is not None:
            try:
                online = await analyzer.health_check()
            except Exception:  # noqa: BLE001
                online = False
            live["analysis"] = {
                "provider": analyzer.provider_name,
                "model": analyzer.model_name(),
                "online": online,
            }
        if self._analysis_queue is not None:
            live["queue"] = await self._analysis_queue.get_queue_status()
        return live

    def _answer_error(self, exc: Exception) -> web.Response:
        if isinstance(exc, AssistantInputError):
            return web.json_response({"error": str(exc)}, status=400)
        if isinstance(exc, ChatNotSupportedError):
            return web.json_response({"error": str(exc)}, status=409)
        if isinstance(exc, TimeoutError):
            return web.json_response(
                {"error": "The model took too long to answer. Try again."}, status=504
            )
        return web.json_response({"error": str(exc)}, status=502)

    async def _handle_assistant_ask(self, request: web.Request) -> web.Response:
        analyzer = self._analyzer
        if analyzer is None:
            return web.json_response({"error": _NOT_CONFIGURED}, status=409)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text=_INVALID_REQUEST_BODY)
        chat_id = request.match_info["chat_id"]
        chat = await self._db.get_assistant_chat(chat_id)
        if chat is None:
            raise web.HTTPNotFound(text=_NO_SUCH_CHAT)
        if chat["message_count"] >= MAX_MESSAGES_PER_CHAT:
            return web.json_response(
                {"error": "This chat is full. Start a new chat to keep asking."},
                status=409,
            )
        # One question at a time across every tab and device: each one is a
        # paid call that reads the whole report, and a double-click should
        # cost one answer, not two.
        if self._assistant_lock.locked():
            return web.json_response(
                {"error": "Still answering the last question. One moment."}, status=429
            )
        settings = self._read_assistant_settings()
        async with self._assistant_lock:
            try:
                answer = await asyncio.wait_for(
                    ask(
                        analyzer,
                        self._db,
                        body.get("question"),
                        chat["messages"],
                        days=settings["days"],
                        model=settings["model"] or None,
                        live=await self._assistant_live_state(),
                    ),
                    ANSWER_TIMEOUT_SECONDS,
                )
            except (AssistantInputError, ChatError, TimeoutError) as exc:
                return self._answer_error(exc)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Assistant question failed: %s", exc)
                return web.json_response(
                    {"error": "The assistant could not answer. Check the add-on logs."},
                    status=500,
                )
        # Spent whether or not the chat survives to be written to.
        await self._db.add_assistant_usage(
            analyzer.provider_name,
            settings["model"] or analyzer.model_name(),
            answer.prompt_tokens,
            answer.completion_tokens,
        )
        now = datetime.now(UTC).isoformat()
        question = str(body["question"]).strip()
        added: list[dict[str, Any]] = [
            {"role": "user", "content": question, "at": now},
            {
                "role": "assistant",
                "content": answer.text,
                "at": now,
                "clips": answer.clips,
                "model": settings["model"] or analyzer.model_name(),
                "days": answer.days,
                "prompt_tokens": answer.prompt_tokens,
                "completion_tokens": answer.completion_tokens,
            },
        ]
        if not await self._db.append_assistant_messages(chat_id, added):
            raise web.HTTPNotFound(text=_NO_SUCH_CHAT)
        title = chat["title"]
        if chat["message_count"] == 0 and title == DEFAULT_CHAT_TITLE:
            title = _title(question) or DEFAULT_CHAT_TITLE
            await self._db.rename_assistant_chat(chat_id, title)
        return web.json_response({"title": title, "messages": added})
