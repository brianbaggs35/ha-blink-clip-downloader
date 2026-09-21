"""Adaptive learning: the user's verdict on the AI's verdict.

Thumbs up or down on a clip, which tunes that camera's effective
confidence threshold and feeds recent corrections back into its prompt.
Deliberately one-directional in effect: feedback may only ever make a
camera less trigger-happy.
"""

from __future__ import annotations

import logging

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _AI_FEEDBACK_ROUTE,
    _INVALID_JSON_BODY,
)

_LOGGER = logging.getLogger(__name__)


class FeedbackRoutesMixin(_MediaServerBase):
    """Per-clip analysis feedback and its statistics."""

    def _register_feedback_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        # Adaptive learning (feedback) endpoints
        app.router.add_get("/api/ai/feedback/stats", self._handle_ai_feedback_stats)
        app.router.add_get(_AI_FEEDBACK_ROUTE, self._handle_ai_feedback_get)
        app.router.add_post(_AI_FEEDBACK_ROUTE, self._handle_ai_feedback_submit)
        app.router.add_delete(_AI_FEEDBACK_ROUTE, self._handle_ai_feedback_delete)
        app.router.add_get(
            "/api/ai/feedback/untrained-count", self._handle_feedback_untrained_count
        )

    async def _handle_ai_feedback_stats(self, request: web.Request) -> web.Response:
        camera = request.rel_url.query.get("camera") or None
        stats = await self._db.get_feedback_stats(camera)
        return web.json_response(stats)

    async def _handle_ai_feedback_get(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["clip_id"]
        feedback = await self._db.get_feedback_for_clip(clip_id)
        return web.json_response(feedback)

    async def _handle_ai_feedback_submit(self, request: web.Request) -> web.Response:
        """Record feedback on a clip's stored AI verdict.

        Body: ``{"correct": bool, "correction_note": str,
        "corrected_suspicious": true|false|null}``. Requires the clip to
        already have a stored analysis result — feedback is a correction on
        an existing verdict, not a substitute for one.
        """
        clip_id = request.match_info["clip_id"]
        try:
            body = await request.json()
            correct = bool(body.get("correct"))
            correction_note = str(body.get("correction_note", "") or "")
            corrected_suspicious = body.get("corrected_suspicious")
            if corrected_suspicious is not None:
                corrected_suspicious = bool(corrected_suspicious)
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        result = await self._db.get_analysis_for_clip(clip_id)
        if not result:
            return web.json_response(
                {"error": "Clip has not been analyzed yet"}, status=400
            )

        # correct=False always means the single is_suspicious boolean was
        # wrong — there is no third option, so the corrected value is fully
        # determined by the original one. Derive it whenever the caller
        # doesn't explicitly override it, rather than leaving it null: the
        # Moondream fine-tuning training-example builder
        # (_handle_finetune_train) falls back to original_suspicious for a
        # null corrected_suspicious, which silently trained toward the
        # *wrong* label for exactly the case this is meant to fix (e.g. a
        # false positive marked incorrect with no explicit correction).
        if not correct and corrected_suspicious is None:
            corrected_suspicious = not result["is_suspicious"]

        # A bare thumbs-down with no typed note carries no reusable signal
        # for get_prompt_corrections (see database.py), which only folds in
        # rows with a non-empty correction_note. Synthesize one from the
        # direction of the correction so every "incorrect" rating still
        # becomes usable few-shot guidance for future clips on this camera.
        if not correct and not correction_note.strip():
            correction_note = (
                "Reviewer marked this as ordinary, routine activity that "
                "was incorrectly flagged suspicious."
                if result["is_suspicious"]
                else "Reviewer marked this as genuinely suspicious activity "
                "that was incorrectly cleared."
            )

        try:
            await self._db.add_feedback(
                clip_id=clip_id,
                camera=result["camera"],
                analysis_result_id=result.get("id"),
                original_suspicious=bool(result["is_suspicious"]),
                original_confidence=float(result["confidence"]),
                correct=correct,
                correction_note=correction_note,
                corrected_suspicious=corrected_suspicious,
            )
            return web.json_response({"saved": True})
        except Exception as exc:  # noqa: BLE001
            # Mirrors _handle_ai_analyze_now's error handling — an unexpected
            # DB failure here must surface as clean JSON, not aiohttp's
            # generic HTML 500 page.
            _LOGGER.warning("Feedback submit failed for clip %s: %s", clip_id, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_ai_feedback_delete(self, request: web.Request) -> web.Response:
        """Fully retract stored feedback for a clip (see ClipDatabase.delete_feedback).

        Distinct from resubmitting corrected feedback: this removes the row
        entirely, taking it out of confidence-threshold auto-tuning, prompt
        corrections, and fine-tuning training examples rather than replacing
        it with a different verdict.
        """
        clip_id = request.match_info["clip_id"]
        deleted = await self._db.delete_feedback(clip_id)
        return web.json_response({"deleted": deleted})

    async def _handle_feedback_untrained_count(
        self, _request: web.Request
    ) -> web.Response:
        """Return how many feedback rows are queued for the next training run."""
        rows = await self._db.get_untrained_feedback(limit=1000)
        return web.json_response({"count": len(rows)})
