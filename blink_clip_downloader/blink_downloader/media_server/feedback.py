"""Adaptive learning: the user's verdict on the AI's verdict.

Thumbs up or down on a clip, which tunes that camera's effective
confidence threshold and feeds recent corrections back into its prompt.
Deliberately one-directional in effect: feedback may only ever make a
camera less trigger-happy.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..verdict_feedback import record_verdict_feedback
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

        # The derivations (the corrected label, the synthesized note) live in
        # verdict_feedback.py, shared with the "Not a threat" button on
        # phone alerts so both write exactly the same row.
        try:
            saved = await record_verdict_feedback(
                self._db,
                clip_id,
                correct=correct,
                correction_note=correction_note,
                corrected_suspicious=corrected_suspicious,
            )
        except Exception as exc:  # noqa: BLE001
            # Mirrors _handle_ai_analyze_now's error handling — an unexpected
            # DB failure here must surface as clean JSON, not aiohttp's
            # generic HTML 500 page.
            _LOGGER.warning("Feedback submit failed for clip %s: %s", clip_id, exc)
            return web.json_response({"error": str(exc)}, status=500)
        if not saved:
            return web.json_response(
                {"error": "Clip has not been analyzed yet"}, status=400
            )
        return web.json_response({"saved": True})

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
