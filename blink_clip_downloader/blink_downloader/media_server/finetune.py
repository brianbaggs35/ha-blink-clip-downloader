"""The AI tab's Fine-Tuning panel: Moondream Cloud only.

Wraps :class:`~blink_downloader.moondream_finetune.MoondreamFineTuneManager`,
which swallows transport failures rather than raising — nothing in clip
analysis depends on any of this, so a fine-tuning outage must never affect
an analysis.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _FINETUNE_REQUIRES_MOONDREAM_CLOUD,
    _INVALID_JSON_BODY,
    _MAX_FINETUNE_TRAIN_BATCH,
    _SETTINGS_WRITE_FAILED,
    _json_object,
)

if TYPE_CHECKING:
    from ..moondream_finetune import MoondreamFineTuneManager

_LOGGER = logging.getLogger(__name__)


class FineTuneRoutesMixin(_MediaServerBase):
    """Moondream Cloud fine-tuning jobs and checkpoints."""

    _FINETUNE_STATE_FILE = Path("/data/finetune_state.json")

    def _register_finetune_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        # Moondream Cloud fine-tuning endpoints
        app.router.add_get("/api/ai/finetune", self._handle_finetune_list)
        app.router.add_post("/api/ai/finetune", self._handle_finetune_create)
        app.router.add_get("/api/ai/finetune/{finetune_id}", self._handle_finetune_get)
        app.router.add_delete(
            "/api/ai/finetune/{finetune_id}", self._handle_finetune_delete
        )
        app.router.add_get(
            "/api/ai/finetune/{finetune_id}/checkpoints",
            self._handle_finetune_checkpoints,
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/activate", self._handle_finetune_activate
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/train", self._handle_finetune_train
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/save-checkpoint",
            self._handle_finetune_save_checkpoint,
        )

    def _get_finetune_manager(self) -> MoondreamFineTuneManager | None:
        """Return a fine-tune manager, or None if not configured for it.

        Only meaningful when the active provider is moondream_cloud — the
        only one of the six providers with a fine-tuning API (see the
        module docstring and CHANGELOG for why OpenAI/Anthropic aren't
        supported here).
        """
        if (
            self._analyzer is None
            or self._analyzer.provider_name != "moondream_cloud"
            or not self._moondream_api_key
        ):
            return None
        from ..moondream_finetune import MoondreamFineTuneManager

        return MoondreamFineTuneManager(api_key=self._moondream_api_key)

    async def _handle_finetune_list(self, _request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response({"enabled": False, "finetunes": []})
        try:
            finetunes = await manager.list_finetunes()
            return web.json_response({"enabled": True, "finetunes": finetunes})
        finally:
            await manager.close()

    async def _handle_finetune_create(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": _FINETUNE_REQUIRES_MOONDREAM_CLOUD},
                status=400,
            )
        try:
            try:
                body = await request.json()
                name = str(body.get("name", "") or "").strip()
                rank = int(body.get("rank", 16))
            except Exception:  # noqa: BLE001
                raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

            if not name:
                return web.json_response({"error": "name is required"}, status=400)

            finetune_id = await manager.create_finetune(name, rank=rank)
            if finetune_id is None:
                return web.json_response(
                    {"error": "Failed to create fine-tune"}, status=500
                )
            return web.json_response({"finetune_id": finetune_id})
        except web.HTTPBadRequest:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Moondream create_finetune failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            await manager.close()

    async def _handle_finetune_get(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning not configured"}, status=400
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            finetune = await manager.get_finetune(finetune_id)
            if finetune is None:
                raise web.HTTPNotFound(text="Fine-tune not found")
            return web.json_response(finetune)
        finally:
            await manager.close()

    async def _handle_finetune_delete(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning not configured"}, status=400
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            deleted = await manager.delete_finetune(finetune_id)
            return web.json_response({"deleted": deleted})
        finally:
            await manager.close()

    async def _handle_finetune_checkpoints(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response({"enabled": False, "checkpoints": []})
        finetune_id = request.match_info["finetune_id"]
        try:
            checkpoints = await manager.list_checkpoints(finetune_id)
            return web.json_response({"enabled": True, "checkpoints": checkpoints})
        finally:
            await manager.close()

    async def _handle_finetune_activate(self, request: web.Request) -> web.Response:
        """Switch live inference to a fine-tuned checkpoint, no restart.

        Body: ``{"step": int}``. Only valid when the active analyzer is a
        MoondreamCloudAnalyzer (checked via _get_finetune_manager's
        provider_name gate, but the hot-swap itself needs the concrete
        analyzer instance, not just the manager).

        Also persists the activated model id to finetune_state.json —
        mirroring the camera_configs.json/vehicle_settings.json pattern —
        so a later add-on restart resumes on this checkpoint instead of
        silently reverting to whatever moondream_finetune_model was last
        saved in options.json (see App._load_finetune_model_from_ui()).
        """
        from ..analyzer import MoondreamCloudAnalyzer
        from ..moondream_finetune import MoondreamFineTuneManager

        if self._analyzer is None or not isinstance(
            self._analyzer, MoondreamCloudAnalyzer
        ):
            return web.json_response(
                {"error": _FINETUNE_REQUIRES_MOONDREAM_CLOUD},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            body = await request.json()
            step = int(body.get("step"))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        model_id = MoondreamFineTuneManager.get_model_id(finetune_id, step)
        self._analyzer.set_finetune_model(model_id)
        try:
            self._FINETUNE_STATE_FILE.write_text(
                json.dumps({"active_model_id": model_id}, indent=2)
            )
        except OSError as exc:
            # The hot-swap above already took effect for this running
            # session regardless — but the whole point of persisting here
            # (see this method's docstring) is surviving a restart, so a
            # write failure must still be reported rather than silently
            # risking a later revert with no record anything went wrong.
            _LOGGER.warning("Could not save fine-tune activation state: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc
        return web.json_response({"activated": True, "model": model_id})

    async def _build_finetune_examples(
        self, feedback_rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[int]]:
        """Pair each feedback row with a representative frame and ground truth.

        Rows whose clip or frame is no longer available are skipped (and
        left untrained, per _handle_finetune_train's docstring).
        """
        assert self._analyzer is not None
        examples: list[dict[str, Any]] = []
        trained_ids: list[int] = []
        for row in feedback_rows:
            clip = await self._db.get_clip(row["clip_id"])
            if not clip or not clip.get("file_path"):
                continue
            frames = await self._analyzer.extract_frames(clip["file_path"])
            if not frames:
                continue

            if row.get("corrected_suspicious") is not None:
                suspicious = bool(row["corrected_suspicious"])
            else:
                suspicious = bool(row["original_suspicious"])
            description = row.get("correction_note") or (
                "Suspicious activity is happening in this clip."
                if suspicious
                else "Nothing suspicious is happening in this clip."
            )
            ground_truth = json.dumps(
                {
                    "suspicious": suspicious,
                    "confidence": row["original_confidence"],
                    "description": description,
                }
            )
            examples.append(
                {
                    "image": frames[len(frames) // 2],
                    "question": self._analyzer.base_prompt_for_camera(row["camera"]),
                    "ground_truth": ground_truth,
                }
            )
            trained_ids.append(int(row["id"]))
        return examples, trained_ids

    async def _handle_finetune_train(self, request: web.Request) -> web.Response:
        """Turn queued human feedback into Moondream SFT training steps.

        Body: ``{"limit": int}`` (default 10) — how many pending feedback
        rows to consume this run. Each row is paired with a representative
        frame re-extracted from its clip and the camera's base prompt (see
        BaseAnalyzer.base_prompt_for_camera), then trained via
        MoondreamFineTuneManager.train_from_examples(). Rows behind a
        successfully-generated rollout are marked trained so a later run
        doesn't repeat them; rows this run skipped (clip/frame gone) are
        left untrained so a future run can retry them.
        """
        manager = self._get_finetune_manager()
        if manager is None:
            # _get_finetune_manager() only returns a manager once it has
            # already confirmed self._analyzer is set, so there's nothing
            # to close here.
            return web.json_response(
                {"error": _FINETUNE_REQUIRES_MOONDREAM_CLOUD},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        body = await _json_object(request, default={})
        # Bounded like every other limit on this server. Unclamped, a
        # negative value reached Postgres as a negative LIMIT (a 500), a
        # non-numeric one raised out of int() (also a 500), and a very large
        # one would extract frames and spend a paid Moondream call for every
        # pending feedback row in the database.
        try:
            limit = int(body.get("limit", 10)) if isinstance(body, dict) else 10
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, _MAX_FINETUNE_TRAIN_BATCH))

        try:
            feedback_rows = await self._db.get_untrained_feedback(limit=limit)
            if not feedback_rows:
                return web.json_response(
                    {"trained": 0, "message": "No new feedback to train on"}
                )

            examples, trained_ids = await self._build_finetune_examples(feedback_rows)

            if not examples:
                return web.json_response(
                    {
                        "trained": 0,
                        "message": "No usable clip frames for pending feedback",
                    }
                )

            result = await manager.train_from_examples(finetune_id, examples)
            successful_ids = [
                trained_ids[i] for i in result.get("successful_indices", [])
            ]
            if successful_ids:
                await self._db.mark_feedback_trained(successful_ids)
            return web.json_response(
                {
                    "trained": result.get("steps_completed", 0),
                    "finetune_id": finetune_id,
                    "examples_attempted": len(examples),
                }
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Moondream train_from_feedback failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            await manager.close()

    async def _handle_finetune_save_checkpoint(
        self, request: web.Request
    ) -> web.Response:
        """Persist the fine-tune's current trained state as an activatable checkpoint.

        Training steps (see _handle_finetune_train) update the fine-tune's
        model weights in place, but only show up under Checkpoints — and
        become selectable via Activate — once explicitly saved.
        """
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": _FINETUNE_REQUIRES_MOONDREAM_CLOUD},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            saved = await manager.save_checkpoint(finetune_id)
            return web.json_response({"saved": saved})
        finally:
            await manager.close()
