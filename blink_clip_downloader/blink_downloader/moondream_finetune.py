"""Moondream Cloud fine-tuning: a thin async wrapper over its REST API.

Not an analyzer. Nothing here looks at a clip, builds a prompt or produces a
verdict — it creates and trains fine-tune jobs, manages their checkpoints,
and reports progress, all on behalf of the AI tab's fine-tuning panel. It
lived in :mod:`blink_downloader.analyzer` only because that is where the
Moondream provider does, and it made that module several hundred lines
longer for something none of the analyzers ever call.

Every method swallows transport failures and returns ``None``/an empty
result rather than raising: this is an optional convenience feature layered
on a paid third-party API, and no part of clip analysis depends on it
working.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Long timeout for the two calls that actually do work on Moondream's side
# (a rollout generation and a training step); short one for the ordinary
# CRUD calls around them. Both mirror analyzer.py's own values.
_API_TIMEOUT = aiohttp.ClientTimeout(total=120)
_HEALTH_TIMEOUT = aiohttp.ClientTimeout(total=10)
_CONTENT_TYPE_JSON = "application/json"


class MoondreamFineTuneManager:
    """HTTP API wrapper for Moondream Cloud fine-tuning operations.

    Fine-tunes train entirely in Moondream Cloud — no local GPU required.
    After training, call :meth:`get_model_id` and pass the result as
    ``finetune_model`` to :class:`MoondreamCloudAnalyzer` to run inference
    with the fine-tuned model.

    Supports both RL (Reinforcement Learning) and SFT (Supervised Fine-tuning)
    training modes across three skills: ``query``, ``point``, and ``detect``.
    """

    _TUNING_BASE_URL = "https://api.moondream.ai/v1/tuning"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": _CONTENT_TYPE_JSON,
        }

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Finetune management
    # ------------------------------------------------------------------

    async def create_finetune(self, name: str, rank: int = 16) -> str | None:
        """Create a new fine-tune and return its finetune_id.

        Args:
            name: Unique identifier (alphanumeric, dots, hyphens, underscores).
            rank: LoRA rank — 8, 16, 24, or 32.  Higher = more capacity but
                  longer training time.

        Returns:
            ``finetune_id`` string on success, ``None`` on error.
        """
        if rank not in (8, 16, 24, 32):
            _LOGGER.error("Moondream create_finetune: rank must be 8, 16, 24, or 32")
            return None
        payload: dict[str, Any] = {"name": name, "rank": rank}
        try:
            session = self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/finetunes",
                json=payload,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Moondream create_finetune returned HTTP %d", resp.status
                    )
                    return None
                data = await resp.json()
                fid = str(data.get("finetune_id", ""))
                return fid or None
        except (aiohttp.ClientError, OSError):
            _LOGGER.exception("Moondream create_finetune failed")
            return None

    async def list_finetunes(
        self, limit: int = 20, cursor: str = ""
    ) -> list[dict[str, Any]]:
        """List all fine-tunes for the current API key."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            session = self._get_session()
            async with session.get(
                f"{self._TUNING_BASE_URL}/finetunes",
                params=params,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return list(data.get("finetunes", []))
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream list_finetunes failed: %s", exc)
            return []

    async def get_finetune(self, finetune_id: str) -> dict[str, Any] | None:
        """Return details for a specific fine-tune, or ``None`` if not found."""
        try:
            session = self._get_session()
            async with session.get(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status in (404, 400):
                    return None
                if resp.status != 200:
                    return None
                return dict(await resp.json())
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream get_finetune failed: %s", exc)
            return None

    async def delete_finetune(self, finetune_id: str) -> bool:
        """Soft-delete a fine-tune and all its checkpoints."""
        try:
            session = self._get_session()
            async with session.delete(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream delete_finetune failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    async def generate_rollouts(
        self,
        finetune_id: str,
        image: bytes,
        question: str,
        num_rollouts: int = 4,
        ground_truth: str | None = None,
        skill: str = "query",
    ) -> dict[str, Any]:
        """Generate multiple model outputs for a single request.

        Args:
            finetune_id: ID of the fine-tune to generate rollouts for.
            image: JPEG frame bytes.
            question: Question to ask the model (skill='query') or object name
                      to locate (skill='point'/'detect').
            num_rollouts: Number of outputs to generate (1–16).
            ground_truth: Expected answer for automatic reward computation.
                          Supported for 'query', 'point', and 'detect' skills.
            skill: One of ``'query'``, ``'point'``, or ``'detect'``.

        Returns:
            Dict with ``'rollouts'`` list and optional ``'rewards'`` list when
            ``ground_truth`` was provided.  Empty dict on error.
        """
        image_b64 = base64.b64encode(image).decode("ascii")
        request: dict[str, Any] = {
            "skill": skill,
            "image_url": f"data:image/jpeg;base64,{image_b64}",
        }
        if skill == "query":
            request["question"] = question
        else:
            request["object"] = question

        payload: dict[str, Any] = {
            "finetune_id": finetune_id,
            "num_rollouts": min(max(1, num_rollouts), 16),
            "request": request,
        }
        if ground_truth is not None:
            payload["ground_truth"] = ground_truth

        try:
            session = self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/rollouts",
                json=payload,
                headers=self._headers(),
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Moondream generate_rollouts returned HTTP %d", resp.status
                    )
                    return {}
                return dict(await resp.json())
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Moondream generate_rollouts failed: %s", exc)
            return {}

    async def train_step(
        self,
        finetune_id: str,
        request: dict[str, Any],
        rollouts: list[str],
        rewards: list[float] | None = None,
        mode: str = "rl",
        learning_rate: float = 2e-4,
    ) -> dict[str, Any]:
        """Execute one RL or SFT training step.

        Args:
            finetune_id: Target fine-tune ID.
            request: The rollout request dict (same structure used in
                     :meth:`generate_rollouts`).
            rollouts: Model outputs from :meth:`generate_rollouts`.
            rewards: Score per rollout (0.0–1.0) for RL mode.  Not used in
                     SFT mode (first rollout is treated as the target).
            mode: ``'rl'`` for reinforcement learning (requires ``rewards``),
                  ``'sft'`` for supervised fine-tuning.
            learning_rate: Optimizer learning rate (default 2e-4).

        Returns:
            Dict with training metrics such as ``kl_divergence`` and
            ``gradient_norm``.  Empty dict on error.
        """
        group: dict[str, Any] = {
            "mode": mode,
            "request": request,
            "rollouts": rollouts,
        }
        if mode == "rl":
            group["rewards"] = rewards or []
        elif mode == "sft" and rollouts:
            group["target"] = rollouts[0]

        payload: dict[str, Any] = {
            "finetune_id": finetune_id,
            "groups": [group],
            "learning_rate": learning_rate,
        }
        try:
            session = self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/train_step",
                json=payload,
                headers=self._headers(),
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Moondream train_step returned HTTP %d", resp.status
                    )
                    return {}
                return dict(await resp.json())
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Moondream train_step failed: %s", exc)
            return {}

    async def train_from_examples(
        self,
        finetune_id: str,
        examples: list[dict[str, Any]],
        mode: str = "sft",
        num_rollouts: int = 4,
    ) -> dict[str, Any]:
        """Run one rollout-then-train_step cycle per example.

        Each item in *examples* is ``{"image": bytes, "question": str,
        "ground_truth": str}`` — typically built from a corrected human
        feedback row (see ``ClipDatabase.get_untrained_feedback``) paired
        with a representative clip frame. In ``mode="sft"`` (the default),
        each rollout batch is generated with ``ground_truth`` set so
        Moondream can score them, then trained with the human-provided
        ``ground_truth`` as the supervised target — the rollouts themselves
        are only used to establish the batch, matching the ``skill="query"``
        request/response shape :meth:`generate_rollouts` and
        :meth:`train_step` already expect. ``mode="rl"`` instead trains on
        Moondream's own reward-scored rollouts, no explicit target.

        Continues past a single example's failure (network error, empty
        rollout) so one bad frame doesn't abandon the rest of the batch.
        Returns ``{"steps_completed": int, "results": [...], "successful_indices":
        [...]}`` — ``successful_indices`` holds the position (within
        *examples*) of each example that actually completed a training
        step, so the caller (see ``MediaServer._handle_finetune_train``) can
        mark only the feedback rows behind successfully-trained examples as
        consumed, not every row it attempted.
        """
        results = []
        successful_indices: list[int] = []
        for i, example in enumerate(examples):
            rollout_resp = await self.generate_rollouts(
                finetune_id,
                example["image"],
                example["question"],
                num_rollouts=num_rollouts,
                ground_truth=example.get("ground_truth"),
                skill="query",
            )
            rollouts = rollout_resp.get("rollouts") or []
            if not rollouts:
                continue
            request: dict[str, Any] = {
                "skill": "query",
                "question": example["question"],
            }
            step_result = await self.train_step(
                finetune_id,
                request,
                rollouts if mode == "rl" else [example["ground_truth"]],
                rewards=rollout_resp.get("rewards"),
                mode=mode,
            )
            if step_result:
                results.append(step_result)
                successful_indices.append(i)
        return {
            "steps_completed": len(results),
            "results": results,
            "successful_indices": successful_indices,
        }

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    async def save_checkpoint(self, finetune_id: str) -> bool:
        """Persist the current model state as a named checkpoint."""
        try:
            session = self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/checkpoints/save",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream save_checkpoint failed: %s", exc)
            return False

    async def list_checkpoints(
        self, finetune_id: str, limit: int = 20, cursor: str = ""
    ) -> list[dict[str, Any]]:
        """List saved checkpoints for a fine-tune."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            session = self._get_session()
            async with session.get(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/checkpoints",
                params=params,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return list(data.get("checkpoints", []))
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream list_checkpoints failed: %s", exc)
            return []

    async def delete_checkpoint(self, finetune_id: str, step: int) -> bool:
        """Delete a specific checkpoint by training step number."""
        try:
            session = self._get_session()
            async with session.delete(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/checkpoints/{step}",
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream delete_checkpoint failed: %s", exc)
            return False

    async def log_metrics(
        self, finetune_id: str, step: int, metrics: dict[str, float]
    ) -> bool:
        """Record custom evaluation metrics for a given training step."""
        payload: dict[str, Any] = {"step": step, "metrics": metrics}
        try:
            session = self._get_session()
            async with session.post(
                f"{self._TUNING_BASE_URL}/finetunes/{finetune_id}/metrics",
                json=payload,
                headers=self._headers(),
                timeout=_HEALTH_TIMEOUT,
            ) as resp:
                return resp.status == 200
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream log_metrics failed: %s", exc)
            return False

    @staticmethod
    def get_model_id(finetune_id: str, step: int) -> str:
        """Return the inference model identifier for a saved checkpoint.

        Pass the returned string as ``finetune_model`` to
        :class:`MoondreamCloudAnalyzer` to run inference with your fine-tuned
        model instead of the base ``moondream3-preview``.

        Example::

            model_id = MoondreamFineTuneManager.get_model_id("abc123", 50)
            # → "moondream3-preview/abc123@50"
        """
        return f"moondream3-preview/{finetune_id}@{step}"
