"""The Anthropic Claude vision provider.

Named ``anthropic_provider`` rather than ``anthropic`` so that neither a
reader nor pyright has to work out whether ``import anthropic`` inside it
reaches the SDK or the file itself — see :mod:`.moondream_provider` for
the case where getting that wrong actually broke a build.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from ..model_catalog import (
    _ANTHROPIC_DATED_SNAPSHOT_RE,
    _ANTHROPIC_FALLBACK_MODELS,
    _OPENAI_STRUCTURED_OUTPUT_SCHEMA,
    _anthropic_supports_structured_output,
    lookup_model_pricing,
)
from .base import _VISION_SYSTEM_PROMPT, BaseAnalyzer, SecurityLayerSettings

_LOGGER = logging.getLogger(__name__)


class AnthropicAnalyzer(BaseAnalyzer):
    """Analyzes clips via the Anthropic Claude API (claude.ai).

    Sends JPEG frames as base64 image content to a Claude vision model and
    extracts token usage for cost tracking.  Authentication errors are logged
    clearly so the user knows to check ``anthropic_api_key`` in the add-on
    settings.
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
        self._model = model or "claude-haiku-4-5"
        self._client: Any = None

    _supports_prompt_caching = True

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def model_name(self) -> str:
        return self._model

    def model_pricing(self) -> tuple[float, float]:
        """Return (input_price, output_price) per 1M tokens for the current model."""
        return lookup_model_pricing(self._model) or (
            3.00,
            15.00,
        )  # Sonnet-level fallback for unknown models

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic as _anthropic

            self._client = _anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def health_check(self) -> bool:
        """Return True when the API key is valid and the Anthropic API is reachable.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits Anthropic's
        real API — see the cache helpers on :class:`BaseAnalyzer` for why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("Anthropic: no API key configured")
            return self._store_health_check_result(False)
        try:
            import anthropic as _anthropic
        except ImportError:
            _LOGGER.error(
                "anthropic package is not installed. "
                "Install it with: pip install anthropic"
            )
            return self._store_health_check_result(False)
        try:
            client = self._get_client()
            await client.models.list(limit=1)
            return self._store_health_check_result(True)
        except _anthropic.AuthenticationError:
            _LOGGER.error(
                "Anthropic: invalid API key (AuthenticationError) — "
                "check your anthropic_api_key in the add-on settings"
            )
            return self._store_health_check_result(False)
        except _anthropic.PermissionDeniedError:
            _LOGGER.error(
                "Anthropic: API key does not have permission to access this resource"
            )
            return self._store_health_check_result(False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Anthropic health check failed: %s", exc)
            return self._store_health_check_result(False)

    async def fetch_models(self) -> list[dict[str, Any]]:
        """Fetch available models from the Anthropic API; falls back to a hardcoded list.

        Strips a dated-snapshot suffix down to its bare alias (deduping any
        resulting repeats) so every entry's ``name`` is exactly the id a
        user should paste into this add-on's ``anthropic_model``
        configuration option, the same as the OpenAI analyzer's
        fetch_models() — see _ANTHROPIC_DATED_SNAPSHOT_RE.
        """
        if self._api_key:
            try:
                import anthropic as _anthropic
            except ImportError:
                pass
            else:
                try:
                    client = self._get_client()
                    page = await client.models.list()
                    result = []
                    seen: set[str] = set()
                    for m in page.data:
                        name = _ANTHROPIC_DATED_SNAPSHOT_RE.sub("", m.id)
                        if name in seen:
                            continue
                        seen.add(name)
                        result.append(
                            {
                                "name": name,
                                "id": name,
                                "display_name": name,
                                "description": name,
                            }
                        )
                    return result
                except _anthropic.AuthenticationError:
                    _LOGGER.error(
                        "Anthropic: invalid API key — "
                        "check your anthropic_api_key in the add-on settings"
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Failed to fetch Anthropic models from API: %s", exc)

        return [
            {"name": name, "id": name, "display_name": name, "description": name}
            for name in _ANTHROPIC_FALLBACK_MODELS
        ]

    @staticmethod
    def _resize_frame(frame_bytes: bytes, max_dimension: int = 1568) -> bytes:
        """Resize a JPEG frame so its longest side is at most max_dimension pixels.

        Anthropic resizes images server-side to 1568px anyway; doing it client-side
        reduces upload bandwidth for high-resolution security cameras.
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
        """Send frames to Claude via the Anthropic Messages API."""
        if not frames:
            return ""

        try:
            import anthropic as _anthropic
        except ImportError:
            _LOGGER.error(
                "anthropic package is not installed. "
                "Install it with: pip install anthropic"
            )
            return ""

        try:
            client = self._get_client()
            response = await client.messages.create(
                **self._build_anthropic_create_kwargs(frames, prompt)
            )
            return self._extract_anthropic_response_text(response)
        except Exception as exc:  # noqa: BLE001
            return self._handle_anthropic_error(_anthropic, exc)

    def _build_anthropic_create_kwargs(
        self, frames: list[bytes], prompt: str
    ) -> dict[str, Any]:
        resized = [self._resize_frame(f) for f in frames]
        images: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(frame).decode("ascii"),
                },
            }
            for frame in resized
        ]

        # Prompt caching. Anthropic's cache is a *prefix* match: the entry is
        # keyed on the exact bytes of everything rendered before the
        # cache_control breakpoint, not on the marked block alone. The frames
        # are different bytes for every clip, so a breakpoint placed after
        # them (which is where this used to sit) can never be read back — it
        # only ever wrote a fresh entry at the 1.25x write premium and then
        # missed it on the next clip. Emitting the static prefix *before* the
        # images is what makes the cached bytes actually repeat from clip to
        # clip on the same camera.
        #
        # Multiple text blocks in one content array read as simple
        # concatenation, so the model still sees the same prompt text as
        # before; only its position relative to the frames changes — rules
        # and camera context first, then the frames, then this clip's own
        # evidence.
        cache_prefix, remainder = self._split_cache_prefix(prompt)
        content: list[dict[str, Any]] = []
        if cache_prefix:
            content.append(
                {
                    "type": "text",
                    "text": cache_prefix,
                    "cache_control": {"type": "ephemeral"},
                }
            )
            content.extend(images)
            content.append({"type": "text", "text": remainder})
        else:
            content.extend(images)
            content.append({"type": "text", "text": prompt})

        # System prompt keeps the role and output format instructions
        # separate from user content, improving JSON compliance and
        # preventing the model from leaking internal analysis terms.
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 512,
            "system": _VISION_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
        }
        # Schema-constrained structured output (see
        # _anthropic_supports_structured_output) — the same reliability
        # benefit _OPENAI_STRUCTURED_OUTPUT_SCHEMA already gives OpenAI,
        # reusing its exact schema (Anthropic's output_config.format takes
        # just the schema, not OpenAI's separate name/strict fields).
        # parse_response()'s existing JSON parsing stays the shared path
        # either way — this only makes malformed/truncated output far less
        # likely on models that support it, never a required contract.
        if _anthropic_supports_structured_output(self._model):
            kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": _OPENAI_STRUCTURED_OUTPUT_SCHEMA["schema"],
                }
            }
        return kwargs

    def _extract_anthropic_response_text(self, response: Any) -> str:
        if response.usage:
            # ``input_tokens`` is the *uncached remainder* only — tokens served
            # from, or written to, the prompt cache are reported separately and
            # would otherwise vanish from the AI Usage tab entirely (on a cache
            # hit that is the majority of the prompt). Total prompt size is the
            # sum of all three.
            cache_read = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)
            cache_write = int(
                getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            )
            self._last_prompt_tokens = (
                int(response.usage.input_tokens or 0) + cache_read + cache_write
            )
            self._last_completion_tokens = int(response.usage.output_tokens or 0)
            self._note_cache_usage(cache_read, cache_write)
            if cache_read or cache_write:
                _LOGGER.debug(
                    "Anthropic prompt cache: %d token(s) read, %d written",
                    cache_read,
                    cache_write,
                )

        return "\n".join(
            block.text for block in response.content if hasattr(block, "text")
        )

    def _handle_anthropic_not_found(self, exc: Exception) -> None:
        """Tell a wrong model id apart from a 404 the API never sent.

        Same two-failures-one-status-code split as OpenAI's 404, for the
        same reasons — see :meth:`OpenAIAnalyzer._handle_openai_not_found`.
        """
        message = self._api_error_message(exc)
        if "model" in message.lower():
            self._last_transient_error = False
            _LOGGER.error(
                "Anthropic: model %r does not exist or this API key has no "
                "access to it (HTTP 404) — check anthropic_model in the add-on "
                "settings against the Models tab's list. %s",
                self._model,
                message,
            )
        else:
            _LOGGER.warning(
                "Anthropic: HTTP 404 with no error detail, which points at a "
                "transient failure reaching the API rather than a rejected "
                "request — this clip will be retried on the next cycle"
            )

    def _handle_anthropic_bad_request(self, exc: Exception) -> None:
        """Tell an empty account balance apart from a malformed request.

        Anthropic reports an empty balance as a 400 rather than a 429, so
        without this it counts as a permanently malformed request and fails
        every queued clip over it — a status nothing reselects, which would
        leave a silent gap in the library once credit is added. It is the
        same situation as OpenAI's ``insufficient_quota``: an account-level
        state that clears on its own once paid, so it pauses the batch and
        keeps the clip retryable instead.
        """
        message = self._api_error_message(exc)
        if "credit balance" in message.lower():
            self._last_rate_limited = True
            _LOGGER.error(
                "Anthropic: the account's credit balance is too low to use the "
                "API — this is a billing limit rather than a bad request, so it "
                "will not clear on its own; add credit under Plans & Billing "
                "and queued clips resume from where they stopped"
            )
        else:
            self._last_transient_error = False
            _LOGGER.error(
                "Anthropic: bad request (HTTP 400) — %s; "
                "check that the selected model supports vision",
                message,
            )

    def _handle_anthropic_error(self, _anthropic: Any, exc: Exception) -> str:
        if isinstance(exc, _anthropic.AuthenticationError):
            self._last_transient_error = False
            _LOGGER.error(
                "Anthropic: invalid API key (AuthenticationError) — "
                "check your anthropic_api_key in the add-on settings"
            )
        elif isinstance(exc, _anthropic.PermissionDeniedError):
            self._last_transient_error = False
            _LOGGER.error(
                "Anthropic: permission denied — "
                "check that your API key has access to model '%s'",
                self._model,
            )
        elif isinstance(exc, _anthropic.RateLimitError):
            self._last_rate_limited = True
            _LOGGER.warning(
                "Anthropic: rate limit hit — API quota exceeded; "
                "analysis will resume on the next cycle"
            )
        elif isinstance(exc, _anthropic.NotFoundError):
            self._handle_anthropic_not_found(exc)
        elif isinstance(exc, _anthropic.BadRequestError):
            self._handle_anthropic_bad_request(exc)
        elif isinstance(exc, _anthropic.APIStatusError):
            # No stack trace, for the same reason as the OpenAI branch.
            _LOGGER.warning(
                "Anthropic API error HTTP %d: %s — this clip will be retried "
                "on the next cycle",
                exc.status_code,  # type: ignore[attr-defined]
                self._api_error_message(exc),
            )
        elif isinstance(exc, _anthropic.APIConnectionError):
            _LOGGER.warning("Anthropic: connection error — %s", exc)
        elif isinstance(exc, asyncio.TimeoutError):
            _LOGGER.warning("Anthropic request timed out")
        else:
            _LOGGER.warning("Anthropic request failed: %s", exc)
        return ""
