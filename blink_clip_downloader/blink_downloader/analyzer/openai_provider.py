"""The OpenAI Chat Completions vision provider.

Named ``openai_provider`` rather than ``openai`` for the same reason as
``anthropic_provider`` — so neither a reader nor pyright has to wonder
which ``openai`` an ``import openai`` inside it resolves to.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from ..model_catalog import (
    _OPENAI_DATED_SNAPSHOT_RE,
    _OPENAI_FALLBACK_MODELS,
    _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
    _openai_model_rank,
    is_openai_vision_model,
    lookup_model_pricing,
    model_entry,
)
from .base import _VISION_SYSTEM_PROMPT, BaseAnalyzer, SecurityLayerSettings

_LOGGER = logging.getLogger(__name__)


# OpenAI error codes that arrive as an HTTP 429 but mean "this account is out
# of money", not "you are going too fast". The distinction matters in the log:
# the rate-limit wording tells a user to wait, and waiting never fixes this.
_OPENAI_NO_CREDIT_CODES: frozenset[str] = frozenset(
    {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}
)


class OpenAIAnalyzer(BaseAnalyzer):
    """Analyzes clips via the OpenAI Chat Completions API (platform.openai.com).

    Sends JPEG frames as base64 image_url content to a GPT-4o / GPT-4.1 / GPT-5
    model and extracts token usage for cost tracking.  Authentication errors are
    logged clearly so the user knows to check ``openai_api_key`` in the add-on
    settings.

    Two-tier escalation (any provider re-checking a suspicious verdict from
    this one) is handled generically by ``BaseAnalyzer`` — see
    ``create_analyzer()``'s ``escalation_provider``/``escalation_model`` and
    ``BaseAnalyzer._maybe_escalate``. This class only implements the tier-1
    (or tier-2) call itself.
    """

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
        self._model = model or "gpt-4o-mini"
        self._client: Any = None

    _supports_prompt_caching = True

    @property
    def provider_name(self) -> str:
        return "openai"

    def model_name(self) -> str:
        return self._model

    def model_pricing(self) -> tuple[float, float]:
        """Return (input_price, output_price) per 1M tokens for the current model."""
        return lookup_model_pricing(self._model) or (
            2.50,
            10.00,
        )  # gpt-4o level fallback for unknown models

    def _get_client(self) -> Any:
        if self._client is None:
            import openai as _openai  # type: ignore[import-not-found]

            self._client = _openai.AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def health_check(self) -> bool:
        """Return True when the API key is valid and the OpenAI API is reachable.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits OpenAI's real
        API — see the cache helpers on :class:`BaseAnalyzer` for why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("OpenAI: no API key configured")
            return self._store_health_check_result(False)
        try:
            import openai as _openai  # type: ignore[import-not-found]
        except ImportError:
            _LOGGER.error(
                "openai package is not installed. Install it with: pip install openai"
            )
            return self._store_health_check_result(False)
        try:
            client = self._get_client()
            await client.models.list()
            return self._store_health_check_result(True)
        except _openai.AuthenticationError:
            _LOGGER.error(
                "OpenAI: invalid API key (AuthenticationError) — "
                "check your openai_api_key in the add-on settings"
            )
            return self._store_health_check_result(False)
        except _openai.PermissionDeniedError:
            _LOGGER.error(
                "OpenAI: API key does not have permission to access this resource"
            )
            return self._store_health_check_result(False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("OpenAI health check failed: %s", exc)
            return self._store_health_check_result(False)

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Fetch vision-capable models from the OpenAI API; falls back to a hardcoded list.

        Excludes dated snapshot ids (keeping only the bare alias) and sorts
        newest-to-oldest via _openai_model_rank, so every entry's ``name`` is
        exactly the id a user should paste into this add-on's
        ``openai_model`` configuration option — see _OPENAI_MODEL_DISPLAY_ORDER.
        """
        live = await self._fetch_models_from_api()
        if live:
            return live
        return [model_entry(name) for name in _OPENAI_FALLBACK_MODELS]

    async def _fetch_models_from_api(self) -> list[dict[str, Any]]:
        """The live vision-capable model list, newest first; empty on failure.

        Empty rather than ``None``, because unlike Anthropic's list this one
        is filtered down to vision models: an account whose models are all
        text-only legitimately yields nothing, and the fallback list is the
        right answer there too.
        """
        if not self._api_key:
            return []
        try:
            import openai as _openai  # type: ignore[import-not-found]
        except ImportError:
            return []
        try:
            pages = await self._get_client().models.list()
            result = [
                model_entry(m.id)
                for m in pages.data
                if is_openai_vision_model(m.id)
                and not _OPENAI_DATED_SNAPSHOT_RE.search(m.id)
            ]
            return sorted(
                result, key=lambda m: (_openai_model_rank(m["name"]), m["name"])
            )
        except _openai.AuthenticationError:
            _LOGGER.error(
                "OpenAI: invalid API key — "
                "check your openai_api_key in the add-on settings"
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to fetch OpenAI models from API: %s", exc)
        return []

    @staticmethod
    def _resize_frame(frame_bytes: bytes, max_dimension: int = 2048) -> bytes:
        """Resize a JPEG frame so its longest side is at most max_dimension pixels.

        OpenAI resizes images server-side to 2048px; doing it client-side reduces
        upload bandwidth for high-resolution security cameras.
        Returns the original bytes unchanged if the image cannot be opened.
        """
        import io

        from PIL import Image

        try:
            img = Image.open(io.BytesIO(frame_bytes))
            if max(img.width, img.height) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Frame resize failed, sending original: %s", exc)
        return frame_bytes

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        return await self._call_openai_model(frames, prompt, self._model)

    async def _call_openai_model(
        self, frames: list[bytes], prompt: str, model: str
    ) -> str:
        """Send frames to a specific OpenAI Chat Completions model."""
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
            client = self._get_client()
            create_kwargs = self._build_openai_create_kwargs(frames, prompt, model)
            response = await client.chat.completions.create(**create_kwargs)
            return self._extract_openai_response_text(response)
        except Exception as exc:  # noqa: BLE001
            return self._handle_openai_error(_openai, exc, model)

    def _build_openai_create_kwargs(
        self, frames: list[bytes], prompt: str, model: str
    ) -> dict[str, Any]:
        resized = [self._resize_frame(f) for f in frames]
        images: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64.b64encode(frame).decode('ascii')}",
                    "detail": "high",
                },
            }
            for frame in resized
        ]

        # Prompt caching. OpenAI reuses a prompt *prefix* it has seen
        # recently at a large discount on the reused part, so the static,
        # camera-scoped portion of the prompt is emitted ahead of the frames:
        # with the frames first, every request diverged at the first image
        # and nothing beyond the short system message could ever repeat.
        # Same text, same order relative to itself — only its position
        # relative to the frames changes.
        #
        # Where that prefix has to *end* is not up to us. On every model
        # reachable through Chat Completions, OpenAI places the cache
        # breakpoints itself, at fixed intervals measured from the start of
        # its own hidden system content — so a prefix only caches if it is
        # long enough to reach the first one. Measured against a real
        # account on gpt-5.4-nano with this exact request shape (8 frames, a
        # strict output schema, reasoning effort "medium"): a static prefix
        # of 1,832 tokens cached nothing, 1,882 cached 1,792, and the next
        # boundary sat 2,048 tokens further on. This add-on's own prefix is
        # roughly 1,400 tokens including the system message, so it falls
        # short of the first boundary unless the configured ai_prompt is
        # longer than the shipped default — which is why cache activity is
        # reported per analysis rather than assumed (see _note_cache_usage).
        #
        # The explicit ``prompt_cache_breakpoint``/``prompt_cache_options``
        # fields that would let us mark the boundary ourselves are
        # deliberately not sent. They exist on the Chat Completions schema
        # and are validated (a bad mode is rejected with HTTP 400), but they
        # have no effect here: measured on gpt-5.6-luna and gpt-6-astra,
        # every layout — breakpoint inside the user message, in the system
        # message, in a message of its own, with implicit mode and with
        # explicit-only mode, at prefixes up to 8,132 tokens — reported zero
        # tokens read and zero written. Prompt caching for those models is a
        # Responses API feature, and this analyzer speaks Chat Completions.
        # Sending the fields anyway would be inert at best, and
        # ``{"mode": "explicit"}`` actively suppresses the implicit
        # breakpoint, so it would risk turning caching off the moment the
        # rest of it did start working.
        cache_prefix, remainder = self._split_cache_prefix(prompt)
        content: list[dict[str, Any]] = []
        if cache_prefix:
            content.append({"type": "text", "text": cache_prefix})
            content.extend(images)
            content.append({"type": "text", "text": remainder})
        else:
            content.extend(images)
            content.append({"type": "text", "text": prompt})

        # System message keeps role and format rules separate from user
        # content, improving JSON compliance and stopping the model from
        # leaking internal analysis terms into the description field.
        messages_to_send: list[dict[str, Any]] = [
            {"role": "system", "content": _VISION_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]

        model_lower = model.lower()
        is_reasoning_model = model_lower.startswith(
            ("o1", "o3", "o4", "gpt-5", "gpt-6")
        )
        supports_structured_outputs = any(
            prefix in model_lower
            for prefix in ("gpt-4o", "gpt-4.1", "gpt-5", "gpt-6", "o4-mini")
        )
        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages_to_send,
        }
        if cache_prefix:
            create_kwargs["prompt_cache_key"] = self._prompt_cache_key()
        # The o1/o3/o4-mini reasoning models and the gpt-5/gpt-6 families
        # reject the legacy `max_tokens` param (HTTP 400
        # "unsupported_parameter") and require `max_completion_tokens`
        # instead. Their invisible reasoning
        # tokens are billed from the same budget as the visible completion,
        # so give them extra headroom over the 512 used for non-reasoning
        # models to avoid a truncated/empty response on a harder clip.
        if is_reasoning_model:
            create_kwargs["max_completion_tokens"] = 1024
            # Was "low" on the theory that this is a short, well-defined
            # classification task (suspicious yes/no + one-sentence
            # description) rather than multi-step reasoning. Real-account
            # testing didn't bear that out: a nano-tier model at "low"
            # repeatedly flagged completely routine front-door access
            # (a resident reaching for their own handle, checking their own
            # mailbox) as suspicious, describing it with words like
            # "attempting to unlock" or "trying to access" — language that
            # presumes ill intent for an action the prompt's own rules
            # already say is NOT suspicious. Distinguishing "resident using
            # their own door normally" from "stranger tampering with a
            # lock" from a handful of sparse frames, against a long rule
            # list with several competing "favor flagging when in doubt"
            # carve-outs, needs more than the minimum effort tier to apply
            # reliably. "medium" (the API's own default when unspecified)
            # trades a modest amount of latency/cost for that reliability.
            # "-pro" tier reasoning models (e.g. gpt-5.2-pro) reject
            # anything but "high", so they're excluded from this setting
            # entirely rather than sent an unsupported value.
            if not model_lower.endswith("-pro"):
                create_kwargs["reasoning_effort"] = "medium"
        else:
            create_kwargs["max_tokens"] = 512
        if supports_structured_outputs:
            create_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
            }
        elif "gpt-4-turbo" in model_lower:
            create_kwargs["response_format"] = {"type": "json_object"}

        return create_kwargs

    def _extract_openai_response_text(self, response: Any) -> str:
        if response.usage:
            # Unlike Anthropic's, OpenAI's ``prompt_tokens`` already includes
            # any tokens served from the prompt cache, so the total needs no
            # adjustment — the cached count is only broken out for logging, so
            # a real install can confirm caching is actually hitting.
            self._last_prompt_tokens = int(response.usage.prompt_tokens or 0)
            self._last_completion_tokens = int(response.usage.completion_tokens or 0)
            details = getattr(response.usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0)
            # cache_write_tokens only exists on the models that charge for a
            # write (GPT-5.6 and later); on every earlier model a write is
            # free and simply not reported, so its absence is not a sign
            # that nothing was written.
            written = int(getattr(details, "cache_write_tokens", 0) or 0)
            self._note_cache_usage(cached, written)
            if cached or written:
                _LOGGER.debug(
                    "OpenAI prompt cache: %d token(s) reused, %d written",
                    cached,
                    written,
                )

        choice = response.choices[0] if response.choices else None
        if choice and choice.message and choice.message.content:
            return str(choice.message.content)
        return ""

    def _log_openai_rate_limit(self, exc: Exception) -> None:
        """Tell an empty account balance apart from a real rate limit.

        Both arrive as HTTP 429, and the rate-limit wording tells a user to
        wait — which never resolves an unpaid balance. The caller has
        already set ``_last_rate_limited`` either way, and this stays
        *transient*: an empty balance is a permanent state of the account,
        not of the clip, and topping it up must not require hunting down
        every clip that was marked failed while it was empty.
        """
        code = self._api_error_code(exc)
        if code in _OPENAI_NO_CREDIT_CODES:
            _LOGGER.error(
                "OpenAI: the account has no API credit left (%s) — this is a "
                "billing limit rather than a temporary rate limit, so it will "
                "not clear on its own; add credit at "
                "platform.openai.com/settings/organization/billing and queued "
                "clips resume from where they stopped",
                code,
            )
        else:
            _LOGGER.warning(
                "OpenAI: rate limit hit — API quota exceeded; "
                "analysis will resume on the next cycle"
            )

    def _handle_openai_not_found(self, exc: Exception, model: str) -> None:
        """Tell a wrong model id apart from a 404 the API never sent.

        A 404 from Chat Completions is two completely different failures
        wearing one status code. With an error body naming the model it is a
        permanent misconfiguration — retrying it three times only delays
        telling the user which setting is wrong. With no body at all it came
        from in front of the API rather than from it, which real installs do
        see as an occasional one-off that the very next attempt succeeds
        through, so it keeps the default retry treatment and gets a single
        line instead of a stack trace through the SDK.
        """
        message = self._api_error_message(exc)
        if self._api_error_code(exc) == "model_not_found" or "model" in message.lower():
            self._last_transient_error = False
            _LOGGER.error(
                "OpenAI: model %r does not exist or this API key has no access "
                "to it (HTTP 404) — check openai_model in the add-on settings "
                "against the Models tab's list. %s",
                model,
                message,
            )
        else:
            _LOGGER.warning(
                "OpenAI: HTTP 404 with no error detail, which points at a "
                "transient failure reaching the API rather than a rejected "
                "request — this clip will be retried on the next cycle"
            )

    def _handle_openai_error(self, _openai: Any, exc: Exception, model: str) -> str:
        if isinstance(exc, _openai.AuthenticationError):
            self._last_transient_error = False
            _LOGGER.error(
                "OpenAI: invalid API key (AuthenticationError) — "
                "check your openai_api_key in the add-on settings"
            )
        elif isinstance(exc, _openai.PermissionDeniedError):
            self._last_transient_error = False
            _LOGGER.error(
                "OpenAI: permission denied — "
                "check that your API key has access to model '%s'",
                model,
            )
        elif isinstance(exc, _openai.RateLimitError):
            self._last_rate_limited = True
            self._log_openai_rate_limit(exc)
        elif isinstance(exc, _openai.NotFoundError):
            self._handle_openai_not_found(exc, model)
        elif isinstance(exc, _openai.BadRequestError):
            self._last_transient_error = False
            # No stack trace, same reason as the branch below it.
            _LOGGER.error(
                "OpenAI: bad request (HTTP 400) — %s; "
                "check that the selected model supports vision",
                self._api_error_message(exc),
            )
        elif isinstance(exc, _openai.APIStatusError):
            # No stack trace: every frame of it is inside the OpenAI SDK and
            # says nothing about this add-on, while burying the status and
            # message that do.
            _LOGGER.warning(
                "OpenAI API error HTTP %d: %s — this clip will be retried on "
                "the next cycle",
                exc.status_code,  # type: ignore[attr-defined]
                self._api_error_message(exc),
            )
        elif isinstance(exc, _openai.APIConnectionError):
            _LOGGER.warning("OpenAI: connection error — %s", exc)
        elif isinstance(exc, asyncio.TimeoutError):
            _LOGGER.warning("OpenAI request timed out")
        else:
            _LOGGER.warning("OpenAI request failed: %s", exc)
        return ""
