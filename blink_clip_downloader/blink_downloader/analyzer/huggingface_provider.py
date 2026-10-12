"""The Hugging Face Inference Providers vision provider.

Hugging Face's router (``router.huggingface.co/v1``) accepts the OpenAI Chat
Completions dialect and forwards each request to whichever backend (Novita,
Together, DeepInfra, ...) serves the model, billing the cost to the user's
Hugging Face account. So this analyzer is the OpenAI SDK pointed at a
different base URL — the parts that differ are everything the router leaves
to the backend: whether a response schema is honoured, how many images fit
in one request, what a model costs, and what an exhausted balance looks
like.

Named ``huggingface_provider`` rather than ``huggingface`` for the same reason
as every other provider module — so neither a reader nor pyright has to
wonder which ``huggingface`` an import inside it resolves to.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any

import aiohttp

from ..model_catalog import (
    _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
    DEFAULT_HUGGINGFACE_MODEL,
    HUGGINGFACE_ROUTER_URL,
    huggingface_price,
    huggingface_vision_entries,
    model_entry,
    split_huggingface_model,
)
from .base import _VISION_SYSTEM_PROMPT, BaseAnalyzer, SecurityLayerSettings

_LOGGER = logging.getLogger(__name__)

#: Where the token is checked. Not the router: its model listing is public,
#: so a request to it succeeds whether or not the token means anything.
HUGGINGFACE_HUB_URL = "https://huggingface.co"

#: Shown by the Models tab when the router's catalog cannot be fetched. Names,
#: not promises — the catalog changes weekly — so the list is short and the
#: error a stale entry produces is the router's own "model not found".
_FALLBACK_MODELS: tuple[str, ...] = (
    DEFAULT_HUGGINGFACE_MODEL,
    "Qwen/Qwen3.5-9B",
    "google/gemma-3-27b-it",
)

#: How long a fetched catalog is reused. It feeds both the model picker and
#: the usage page's cost estimates, and prices move rarely.
_CATALOG_TTL_SECONDS = 6 * 3600

_CATALOG_TIMEOUT = aiohttp.ClientTimeout(total=20)

#: Generous because the budget is shared with a reasoning model's invisible
#: thinking, which is billed from the same allowance as the visible answer.
_MAX_COMPLETION_TOKENS = 1024

_REQUEST_TIMEOUT_SECONDS = 120.0

_THINKING_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINKING_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)

#: Words in a 400 that say the backend refused ``response_format`` rather than
#: refused the request. Both halves matter: a 400 for any other reason (an
#: image too large, a model with no vision) must not be retried as if
#: dropping the schema would fix it.
_JSON_SCHEMA = "json_schema"

_RESPONSE_FORMAT_REJECTION_MARKERS: tuple[str, ...] = (
    "response_format",
    _JSON_SCHEMA,
    "structured output",
    "structured_output",
    "guided_json",
    "guided decoding",
)


class HuggingFaceAnalyzer(BaseAnalyzer):
    """Analyzes clips through Hugging Face Inference Providers.

    Sends JPEG frames as base64 ``image_url`` content to any vision model the
    router serves and records the token counts it reports. The model id is
    ``org/name``, optionally with a routing suffix — ``:cheapest`` for the
    least expensive backend, ``:fastest`` (the default) for the highest
    throughput, or a backend's name (``:novita``) to pin one.

    The credential is the add-on's ``huggingface_api_key`` — or ``hf_token``,
    the token that already authenticates model downloads, when that is blank —
    and must be allowed to make calls to Inference Providers. Hugging Face bills the account it belongs to, and a
    free account has no monthly credit to bill (see DOCS.md), so an empty
    balance is the first failure a new user is likely to meet; it is reported
    in those terms rather than as a generic API error.

    Two-tier escalation is handled generically by ``BaseAnalyzer`` — see
    ``create_analyzer()``'s ``escalation_provider``/``escalation_model``. This
    class only implements the tier-1 (or tier-2) call itself.
    """

    #: Where requests go. Class attributes rather than constructor arguments
    #: because the constructor already carries the thirteen parameters every
    #: provider shares; they exist so a test can point one instance at a
    #: stand-in for Hugging Face without patching a module.
    router_url: str = HUGGINGFACE_ROUTER_URL
    hub_url: str = HUGGINGFACE_HUB_URL

    def __init__(
        self,
        api_key: str,
        model: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, Any]] | None = None,
        security_settings: SecurityLayerSettings | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
            security_settings=security_settings,
        )
        self._api_key = api_key
        self._model = (model or "").strip() or DEFAULT_HUGGINGFACE_MODEL
        self._client: Any = None
        # Whether to ask for a schema-constrained answer. Starts true because
        # most backends honour it and a constrained answer never needs the
        # keyword fallback in parse_response; turned off for good the first
        # time a backend refuses it (see _create_completion).
        self._send_response_format = True
        self._catalog: list[dict[str, Any]] | None = None
        self._catalog_fetched_at = 0.0

    @property
    def provider_name(self) -> str:
        return "huggingface"

    def model_name(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    # Catalog: model list and prices
    # ------------------------------------------------------------------

    async def _load_catalog(self) -> list[dict[str, Any]]:
        """The router's model catalog, reused for ``_CATALOG_TTL_SECONDS``.

        Public, so fetched without the token. A failed refresh keeps serving
        the previous catalog rather than dropping prices the usage page is
        showing; with none at all it returns an empty list.
        """
        age = time.monotonic() - self._catalog_fetched_at
        if self._catalog is not None and age < _CATALOG_TTL_SECONDS:
            return self._catalog
        try:
            async with (
                aiohttp.ClientSession(timeout=_CATALOG_TIMEOUT) as session,
                session.get(f"{self.router_url.rstrip('/')}/models") as resp,
            ):
                resp.raise_for_status()
                body = await resp.json(content_type=None)
            rows = body.get("data") if isinstance(body, dict) else None
            if isinstance(rows, list):
                self._catalog = [row for row in rows if isinstance(row, dict)]
                self._catalog_fetched_at = time.monotonic()
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            _LOGGER.debug("Hugging Face: could not fetch the model catalog: %s", exc)
        return self._catalog or []

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Vision models the router serves, cheapest first, each with its price.

        Falls back to a short built-in list when the catalog cannot be
        fetched. Every ``name`` is exactly the id to paste into the
        ``huggingface_model`` option.
        """
        entries = huggingface_vision_entries(await self._load_catalog())
        if entries:
            return entries
        return [model_entry(name) for name in _FALLBACK_MODELS]

    async def refresh_pricing(self) -> None:
        await self._load_catalog()

    def price_for_model(self, model: str) -> tuple[float, float] | None:
        """(input, output) $/1M tokens for *model* from the router catalog.

        ``None`` while the catalog has not been fetched, for a model it does
        not list, and for one no backend prices — so the usage page says N/A
        rather than guessing. An estimate even when found: the backend that
        serves a given request is the router's choice (see
        :func:`~blink_downloader.model_catalog.huggingface_price`).
        """
        if self._catalog is None:
            return None
        base, selector = split_huggingface_model(model)
        for row in self._catalog:
            if row.get("id") == base:
                return huggingface_price(row, selector)
        return None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        if self._client is None:
            import openai as _openai  # type: ignore[import-not-found]

            self._client = _openai.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self.router_url,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def health_check(self) -> bool:
        """Return True when the token is accepted by Hugging Face.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` like every provider that
        calls a real API. Asks the Hub who the token belongs to, because the
        router's own listing is public and says nothing about the token.
        That cannot show the token is *allowed to run inference* — only a
        billed request could — so a 403 (a token scoped too narrowly to read
        its own profile) is taken as a valid token, and the first real
        request reports a missing permission if there is one.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning(
                "Hugging Face: no token configured — set huggingface_api_key "
                "(or hf_token) in the add-on settings"
            )
            return self._store_health_check_result(False)
        try:
            async with (
                aiohttp.ClientSession(timeout=_CATALOG_TIMEOUT) as session,
                session.get(
                    f"{self.hub_url.rstrip('/')}/api/whoami-v2",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as resp,
            ):
                status = resp.status
        except (TimeoutError, aiohttp.ClientError) as exc:
            _LOGGER.warning("Hugging Face health check failed: %s", exc)
            return self._store_health_check_result(False)
        if status == 401:
            _LOGGER.error(
                "Hugging Face: the token was rejected (HTTP 401) — check "
                "huggingface_api_key (or hf_token) in the add-on settings; it "
                "may be mistyped, expired or revoked"
            )
            return self._store_health_check_result(False)
        if status >= 500:
            _LOGGER.warning("Hugging Face health check failed: HTTP %d", status)
            return self._store_health_check_result(False)
        await self._warn_if_model_unlisted()
        return self._store_health_check_result(True)

    async def _warn_if_model_unlisted(self) -> None:
        """Say so when the configured model is not in the router's catalog.

        A misspelt id otherwise surfaces only as a failed clip. Silent when
        the catalog could not be fetched: not being able to check is not
        evidence the model is missing.
        """
        catalog = await self._load_catalog()
        base, selector = split_huggingface_model(self._model)
        if not catalog or any(row.get("id") == base for row in catalog):
            return
        _LOGGER.warning(
            "Hugging Face: model %r is not in the Inference Providers catalog"
            "%s — check huggingface_model against the Models list on the AI tab",
            base,
            f" (routing suffix {selector!r} ignored)" if selector else "",
        )

    # ------------------------------------------------------------------
    # Request
    # ------------------------------------------------------------------

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        if not frames:
            return ""
        try:
            import openai as _openai  # type: ignore[import-not-found]
        except ImportError:
            _LOGGER.error(
                "openai package is not installed. Install it with: pip install openai"
            )
            return ""
        try:
            response = await self._create_completion(_openai, frames, prompt)
            return self._extract_response_text(response)
        except Exception as exc:  # noqa: BLE001
            return self._handle_error(_openai, exc)

    async def _create_completion(
        self, _openai: Any, frames: list[bytes], prompt: str
    ) -> Any:
        """Send the request, dropping the response schema if a backend refuses it.

        The router passes ``response_format`` through to whichever backend it
        picked, and backends differ: some enforce the schema, some ignore
        it, some answer HTTP 400. Only the last is a problem, and only when
        the 400 is about the schema — the request is repeated once without
        it, and not asked for again, since the prompt already demands the
        same JSON and ``parse_response`` copes with an answer that drifts.
        """
        client = self._get_client()
        try:
            return await client.chat.completions.create(
                **self._build_create_kwargs(frames, prompt)
            )
        except (_openai.BadRequestError, _openai.UnprocessableEntityError) as exc:
            if not (self._send_response_format and self._rejects_response_format(exc)):
                raise
            self._send_response_format = False
            _LOGGER.info(
                "Hugging Face: the backend serving %s does not accept a response "
                "schema (%s); asking for JSON in the prompt alone from now on",
                self._model,
                self._api_error_message(exc),
            )
            return await client.chat.completions.create(
                **self._build_create_kwargs(frames, prompt)
            )

    @classmethod
    def _rejects_response_format(cls, exc: Exception) -> bool:
        message = cls._api_error_message(exc).lower()
        return any(marker in message for marker in _RESPONSE_FORMAT_REJECTION_MARKERS)

    def _build_create_kwargs(self, frames: list[bytes], prompt: str) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64.b64encode(frame).decode('ascii')}"
                },
            }
            for frame in frames
        ]
        content.append({"type": "text", "text": prompt})
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _VISION_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": _MAX_COMPLETION_TOKENS,
        }
        if self._send_response_format:
            kwargs["response_format"] = {
                "type": _JSON_SCHEMA,
                _JSON_SCHEMA: _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
            }
        return kwargs

    @staticmethod
    def _without_reasoning(text: str) -> str:
        """Drop a model's inline ``<think>`` block from its answer.

        Some backends return a reasoning model's thinking inside ``content``
        rather than in a field of its own, and ``parse_response`` takes the
        first ``{`` to the last ``}`` — which on a thought that mentions JSON
        is not the answer. An unterminated block means the budget ran out
        mid-thought, so nothing after it is an answer either.
        """
        text = _THINKING_BLOCK_RE.sub("", text)
        return _UNCLOSED_THINKING_RE.sub("", text).strip()

    def _extract_response_text(self, response: Any) -> str:
        usage = getattr(response, "usage", None)
        if usage:
            self._last_prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            self._last_completion_tokens = int(
                getattr(usage, "completion_tokens", 0) or 0
            )
        choice = response.choices[0] if response.choices else None
        if choice and choice.message and choice.message.content:
            return self._without_reasoning(str(choice.message.content))
        return ""

    # ------------------------------------------------------------------
    # Failures
    # ------------------------------------------------------------------

    def _handle_error(self, _openai: Any, exc: Exception) -> str:
        if isinstance(exc, _openai.AuthenticationError):
            self._last_transient_error = False
            _LOGGER.error(
                "Hugging Face: the token was rejected (HTTP 401) — check "
                "huggingface_api_key (or hf_token) in the add-on settings"
            )
        elif isinstance(exc, _openai.PermissionDeniedError):
            self._last_transient_error = False
            _LOGGER.error(
                "Hugging Face: permission denied (HTTP 403) — the token needs "
                "'Make calls to Inference Providers' (a fine-grained token) or "
                "to be a read token, and a gated model needs its licence "
                "accepted on the model's page. %s",
                self._api_error_message(exc),
            )
        elif isinstance(exc, _openai.RateLimitError):
            self._last_rate_limited = True
            _LOGGER.warning(
                "Hugging Face: rate limit hit — %s; analysis will resume on "
                "the next cycle",
                self._api_error_message(exc),
            )
        elif isinstance(exc, _openai.NotFoundError):
            self._handle_not_found(exc)
        elif isinstance(
            exc, (_openai.BadRequestError, _openai.UnprocessableEntityError)
        ):
            self._handle_bad_request(exc)
        elif isinstance(exc, _openai.APIStatusError):
            self._handle_status_error(exc)
        elif isinstance(exc, _openai.APIConnectionError):
            _LOGGER.warning("Hugging Face: connection error — %s", exc)
        elif isinstance(exc, asyncio.TimeoutError):
            _LOGGER.warning("Hugging Face request timed out")
        else:
            _LOGGER.warning("Hugging Face request failed: %s", exc)
        return ""

    def _handle_not_found(self, exc: Exception) -> None:
        """Tell a model nothing serves apart from a 404 the router never sent.

        With an error body it is a permanent misconfiguration. With none it
        came from in front of the router, and the next attempt may succeed —
        the same split ``OpenAIAnalyzer`` makes for the same reason.
        """
        message = self._api_error_message(exc)
        if self._api_error_code(exc) == "model_not_found" or "model" in message.lower():
            self._last_transient_error = False
            _LOGGER.error(
                "Hugging Face: model %r is not available through Inference "
                "Providers (HTTP 404) — check huggingface_model against the "
                "Models list on the AI tab. %s",
                self._model,
                message,
            )
        else:
            _LOGGER.warning(
                "Hugging Face: HTTP 404 with no error detail, which points at a "
                "transient failure reaching the router rather than a rejected "
                "request — this clip will be retried on the next cycle"
            )

    def _handle_bad_request(self, exc: Exception) -> None:
        self._last_transient_error = False
        message = self._api_error_message(exc)
        lower = message.lower()
        if "image" in lower and any(
            word in lower for word in ("at most", "too many", "limit", "maximum")
        ):
            advice = (
                "this model accepts fewer images per request than "
                "ai_max_frames sends — set ai_frame_strategy to sequential (one "
                "frame per request), lower ai_max_frames, or pick another model"
            )
        else:
            advice = "check that the selected model accepts images"
        _LOGGER.error("Hugging Face: bad request (HTTP 400) — %s; %s", message, advice)

    def _handle_status_error(self, exc: Exception) -> None:
        status = getattr(exc, "status_code", 0)
        if status == 402:
            # Not the clip's fault and not temporary: stop the batch (every
            # remaining clip would fail the same way) but keep the clips
            # queued, so topping up the balance resumes them.
            self._last_rate_limited = True
            _LOGGER.error(
                "Hugging Face: the account has no credit left (HTTP 402). "
                "Inference Providers bills the account the token belongs to: a "
                "free account has no monthly credit, PRO includes a small one, "
                "and anything beyond it is pay-as-you-go. Add credit at "
                "huggingface.co/settings/billing and queued clips resume from "
                "where they stopped. %s",
                self._api_error_message(exc),
            )
            return
        _LOGGER.warning(
            "Hugging Face API error HTTP %d: %s — this clip will be retried on "
            "the next cycle",
            status,
            self._api_error_message(exc),
        )
