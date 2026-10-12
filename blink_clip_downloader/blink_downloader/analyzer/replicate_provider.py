"""The Replicate vision provider.

Replicate runs models behind a prediction API rather than a chat API: a
request names a model and an ``input`` object, and every model defines its
own input names — Gemini takes ``images``, the GPT models ``image_input``,
Claude a single ``image`` — so there is no one request shape to hard-code.
Instead the model's own published input schema is read once
(:func:`~blink_downloader.model_catalog.replicate_input_plan`) and the clip
is arranged to fit it, which is what lets a model Replicate publishes
tomorrow work without a release.

Only *official* models are supported. They are called by name; a community
model has to be called by version id, which is one more thing for a user to
look up and for a model's author to change under them.

Spoken to directly over HTTP with ``aiohttp`` rather than through Replicate's
Python client: the whole contract this needs (create a prediction, wait for
it, read its output) is three calls, and the client would add a dependency —
with its own transitive pins — to the image for them.

Named ``replicate_provider`` rather than ``replicate`` for the same reason as
every other provider module.
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
    DEFAULT_REPLICATE_MODEL,
    REPLICATE_API_URL,
    REPLICATE_VISION_MODELS,
    ReplicateInputPlan,
    model_entry,
    replicate_input_plan,
)
from .base import (
    _VISION_SYSTEM_PROMPT,
    BaseAnalyzer,
    SecurityLayerSettings,
    _MostAlarming,
)

_LOGGER = logging.getLogger(__name__)

#: A prediction's id as Replicate issues it. The poll URL is built from this
#: and the configured base URL, never taken from the response's own ``urls``:
#: the request carries the account's token, and a URL out of a response body
#: is not somewhere to send it.
_PREDICTION_ID_RE = re.compile(r"[A-Za-z0-9]{1,64}")

#: The prediction object's state field.
_STATUS = "status"

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled", "aborted"})

#: Seconds the create call is asked to hold the connection open for the
#: answer (``Prefer: wait``). Most clips finish inside it; one that does not
#: is polled for.
_WAIT_SECONDS = 60

#: How often an unfinished prediction is checked, and how long it is given.
_POLL_SECONDS = 1.5
_DEADLINE_SECONDS = 180.0

#: A prediction still running after this long is cancelled by Replicate
#: itself, so a request this add-on stops waiting for does not go on billing.
_CANCEL_AFTER = "4m"

#: Shared with reasoning models' invisible thinking, like every provider.
_MAX_OUTPUT_TOKENS = 2048

_TIMEOUT = aiohttp.ClientTimeout(total=_DEADLINE_SECONDS + 60)
_SHORT_TIMEOUT = aiohttp.ClientTimeout(total=20)

#: Frames in flight at once for a model that takes a single image per request.
_FRAME_CONCURRENCY = 3


class _ReplicateError(Exception):
    """A call failed in a way the queue needs to know the kind of.

    *permanent* — retrying cannot help (a bad token, a model that does not
    exist). *rate_limited* — stop the rest of the batch (every clip would
    fail the same way) but keep the clips queued. Neither — transient.
    """

    def __init__(
        self, message: str, *, permanent: bool = False, rate_limited: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        self.permanent = permanent
        self.rate_limited = rate_limited


def _detail(body: Any) -> str:
    """Replicate's own explanation of an error, trimmed to one short line."""
    if isinstance(body, dict):
        for key in ("detail", "error", "title"):
            if body.get(key):
                return str(body[key])[:200]
    return ""


def _status_error(status: int, body: Any) -> _ReplicateError:
    """The failure an HTTP error status means for this add-on."""
    detail = _detail(body)
    suffix = f": {detail}" if detail else ""
    if status == 401:
        return _ReplicateError(
            "Replicate rejected replicate_api_key (HTTP 401) — check it in the "
            "add-on settings",
            permanent=True,
        )
    if status == 402:
        return _ReplicateError(
            "the Replicate account has no credit left (HTTP 402) — add credit "
            f"at replicate.com/account/billing and queued clips resume{suffix}",
            rate_limited=True,
        )
    if status == 404:
        return _ReplicateError(
            "Replicate has no such official model (HTTP 404) — check "
            f"replicate_model against the Models list on the AI tab{suffix}",
            permanent=True,
        )
    if status == 422:
        return _ReplicateError(
            f"Replicate rejected the input (HTTP 422){suffix}", permanent=True
        )
    if status == 429:
        return _ReplicateError(
            f"Replicate rate limit hit (HTTP 429){suffix}", rate_limited=True
        )
    return _ReplicateError(f"Replicate answered HTTP {status}{suffix}")


def _data_uri(frame: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")


def _build_input(
    plan: ReplicateInputPlan, frames: list[bytes], prompt: str
) -> dict[str, Any]:
    """The ``input`` object for one request, under the model's own names."""
    uris = [_data_uri(frame) for frame in frames]
    body: dict[str, Any] = {
        plan.prompt_field: prompt,
        plan.images_field: uris if plan.images_are_list else uris[0],
    }
    if plan.system_field:
        body[plan.system_field] = _VISION_SYSTEM_PROMPT
    if plan.max_tokens_field:
        body[plan.max_tokens_field] = _MAX_OUTPUT_TOKENS
    return body


def _output_text(prediction: dict[str, Any]) -> str:
    """A finished prediction's text.

    Language models stream, so their ``output`` is a list of text chunks to
    join; some return it whole.
    """
    output = prediction.get("output")
    if isinstance(output, str):
        return output.strip()
    if isinstance(output, list):
        return "".join(str(chunk) for chunk in output).strip()
    return ""


def _token_counts(prediction: dict[str, Any]) -> tuple[int, int]:
    """(input, output) tokens the prediction reports, 0 for any it does not.

    Replicate's language models report them under ``metrics``. Anything else
    — no metrics, or a value that is not a count — is 0 rather than an
    estimate, so the usage page shows what was reported and nothing invented.
    """
    metrics = prediction.get("metrics")
    if not isinstance(metrics, dict):
        return 0, 0

    def count(key: str) -> int:
        value = metrics.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    return count("input_token_count"), count("output_token_count")


class ReplicateAnalyzer(BaseAnalyzer):
    """Analyzes clips with one of Replicate's official vision models.

    Frames go out as base64 data URIs — Replicate recommends them for files
    under 1 MB, and an analysis frame is a 640-pixel JPEG. A model that takes
    a list of images gets the whole clip in one request; one that takes a
    single image (Claude) gets one request per frame, and the most alarming
    answer stands, as with the ``sequential`` frame strategy.

    Replicate bills the account the API token belongs to, per second of
    compute or per token depending on the model, and publishes no rate this
    add-on can read — so the usage page shows token counts (when the model
    reports them) and no cost.
    """

    #: Where requests go. A class attribute rather than a constructor argument
    #: because the constructor already carries the thirteen parameters every
    #: provider shares; it exists so a test can point one instance at a
    #: stand-in for Replicate without patching a module.
    api_url: str = REPLICATE_API_URL

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
        self._model = (model or "").strip() or DEFAULT_REPLICATE_MODEL
        self._plan: ReplicateInputPlan | None = None

    @property
    def provider_name(self) -> str:
        return "replicate"

    def model_name(self) -> str:
        return self._model

    async def close(self) -> None:
        """Nothing to release: every call opens and closes its own session."""

    async def fetch_models(self) -> list[dict[str, Any]]:
        """The official vision models, as ids to paste into ``replicate_model``."""
        return [model_entry(name) for name in REPLICATE_VISION_MODELS]

    # ------------------------------------------------------------------
    # Talking to Replicate
    # ------------------------------------------------------------------

    def _base(self) -> str:
        return self.api_url.rstrip("/")

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", **extra}

    def _model_path(self) -> str:
        """``owner/name``, or a permanent failure for anything else.

        A version suffix or a bare name is a community model's spelling, and
        would be sent to an endpoint that only exists for official ones.
        """
        owner, _, name = self._model.partition("/")
        if not owner or not name or "/" in name or ":" in self._model:
            raise _ReplicateError(
                f"replicate_model {self._model!r} must be an official model "
                "written owner/name (for example openai/gpt-4o-mini)",
                permanent=True,
            )
        return f"{owner}/{name}"

    @staticmethod
    async def _read(resp: aiohttp.ClientResponse) -> dict[str, Any]:
        """The JSON body of a response, or the failure its status means."""
        try:
            body = await resp.json(content_type=None)
        except ValueError:
            body = None
        if resp.status >= 400:
            raise _status_error(resp.status, body)
        if not isinstance(body, dict):
            raise _ReplicateError("Replicate returned something other than an object")
        return body

    async def _load_plan(self, session: aiohttp.ClientSession) -> ReplicateInputPlan:
        """How to call the model, from its own input schema (cached).

        This is also the cheapest honest check that a token and a model name
        both work: the same call fails 401 for one and 404 for the other.
        """
        if self._plan is not None:
            return self._plan
        path = self._model_path()
        async with session.get(
            f"{self._base()}/models/{path}", headers=self._headers()
        ) as resp:
            model = await self._read(resp)
        schemas = (
            (model.get("latest_version") or {})
            .get("openapi_schema", {})
            .get("components", {})
            .get("schemas", {})
        )
        properties = (schemas.get("Input") or {}).get("properties") or {}
        plan = replicate_input_plan(properties)
        if plan is None:
            raise _ReplicateError(
                f"Replicate model {path!r} does not take both a prompt and an "
                "image, so it cannot analyze a clip — pick one from the Models "
                "list on the AI tab",
                permanent=True,
            )
        self._plan = plan
        return plan

    async def _create(
        self, session: aiohttp.ClientSession, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        async with session.post(
            f"{self._base()}/models/{path}/predictions",
            json={"input": body},
            headers=self._headers(
                Prefer=f"wait={_WAIT_SECONDS}", **{"Cancel-After": _CANCEL_AFTER}
            ),
        ) as resp:
            return await self._read(resp)

    async def _finish(
        self, session: aiohttp.ClientSession, prediction: dict[str, Any]
    ) -> dict[str, Any]:
        """Poll a prediction that outlived the wait until it reaches an end."""
        prediction_id = str(prediction.get("id", ""))
        if not _PREDICTION_ID_RE.fullmatch(prediction_id):
            raise _ReplicateError("Replicate returned a prediction with no usable id")
        give_up = time.monotonic() + _DEADLINE_SECONDS
        while prediction.get(_STATUS) not in _TERMINAL_STATUSES:
            if time.monotonic() >= give_up:
                raise _ReplicateError(
                    "the Replicate prediction did not finish in time — this "
                    "clip will be retried on the next cycle"
                )
            await asyncio.sleep(_POLL_SECONDS)
            async with session.get(
                f"{self._base()}/predictions/{prediction_id}",
                headers=self._headers(),
                timeout=_SHORT_TIMEOUT,
            ) as resp:
                prediction = await self._read(resp)
        return prediction

    async def _ask(
        self,
        session: aiohttp.ClientSession,
        plan: ReplicateInputPlan,
        frames: list[bytes],
        prompt: str,
    ) -> tuple[str, int, int]:
        """One prediction: (answer, input tokens, output tokens)."""
        prediction = await self._create(
            session, self._model_path(), _build_input(plan, frames, prompt)
        )
        if prediction.get(_STATUS) not in _TERMINAL_STATUSES:
            prediction = await self._finish(session, prediction)
        if prediction.get(_STATUS) != "succeeded":
            reason = str(prediction.get("error") or prediction.get(_STATUS))[:200]
            raise _ReplicateError(f"the Replicate prediction failed: {reason}")
        tokens_in, tokens_out = _token_counts(prediction)
        return _output_text(prediction), tokens_in, tokens_out

    async def _ask_each_frame(
        self,
        session: aiohttp.ClientSession,
        plan: ReplicateInputPlan,
        frames: list[bytes],
        prompt: str,
    ) -> tuple[str, int, int]:
        """For a model that takes one image: ask about every frame, keep the
        most alarming answer, and total the tokens."""
        gate = asyncio.Semaphore(_FRAME_CONCURRENCY)

        async def one(frame: bytes) -> tuple[str, int, int]:
            async with gate:
                return await self._ask(session, plan, [frame], prompt)

        answers = await asyncio.gather(*(one(frame) for frame in frames))
        best = _MostAlarming()
        for text, _, _ in answers:
            suspicious, confidence, description = self._try_parse_json(text)
            if description:
                best.offer(suspicious, confidence, text)
            elif text:
                best.offer_unranked(text)
        return (
            best.response,
            sum(tokens_in for _, tokens_in, _ in answers),
            sum(tokens_out for _, _, tokens_out in answers),
        )

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        if not frames:
            return ""
        self._last_prompt_tokens = 0
        self._last_completion_tokens = 0
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                plan = await self._load_plan(session)
                ask = self._ask if plan.images_are_list else self._ask_each_frame
                text, tokens_in, tokens_out = await ask(session, plan, frames, prompt)
        except _ReplicateError as error:
            self._note_failure(error)
            return ""
        except (TimeoutError, aiohttp.ClientError) as exc:
            _LOGGER.warning("Replicate request failed: %s", exc)
            return ""
        self._last_prompt_tokens = tokens_in
        self._last_completion_tokens = tokens_out
        return text

    @staticmethod
    def _log_failure(error: _ReplicateError) -> None:
        log = (
            _LOGGER.error if error.permanent or error.rate_limited else _LOGGER.warning
        )
        log("Replicate: %s", error.message)

    def _note_failure(self, error: _ReplicateError) -> None:
        """Record what kind of failure the last model call was, and say so.

        Only for calls that analyze a clip: ``rate_limited`` and
        ``transient_error`` describe the most recent ``_call_model``, so a
        health check that fails the same way must not write to them.
        """
        if error.permanent:
            self._last_transient_error = False
        if error.rate_limited:
            self._last_rate_limited = True
        self._log_failure(error)

    async def health_check(self) -> bool:
        """Return True when the token and the model both work.

        Loading the model's schema is the check — see :meth:`_load_plan` —
        and it is cached after the first success, so this costs a request
        only until then (and, otherwise, the 30-second result cache).
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning(
                "Replicate: no API token configured — set replicate_api_key in "
                "the add-on settings"
            )
            return self._store_health_check_result(False)
        try:
            async with aiohttp.ClientSession(timeout=_SHORT_TIMEOUT) as session:
                await self._load_plan(session)
        except _ReplicateError as error:
            self._log_failure(error)
            return self._store_health_check_result(False)
        except (TimeoutError, aiohttp.ClientError) as exc:
            _LOGGER.warning("Replicate health check failed: %s", exc)
            return self._store_health_check_result(False)
        return self._store_health_check_result(True)
