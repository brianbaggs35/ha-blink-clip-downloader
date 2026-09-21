"""The Biometrics tab's API: enrolled household members.

Enrollment, renaming, removal, and the per-enrollment *approved* flag that
gates the safety-critical suspicious-flag bypass — enrolling someone is
not the same as approving them for it. Also the bypass audit trail and the
"this match was wrong" reports behind it. Embeddings are computed locally
and never leave the add-on.
"""

from __future__ import annotations

import base64
import logging

from aiohttp import web

from ..vision import is_face_recognition_available
from .core import _MediaServerBase
from .support import (
    _CLIP_NOT_FOUND,
    _INVALID_JSON_BODY,
    _json_object,
)

_LOGGER = logging.getLogger(__name__)


class FaceRoutesMixin(_MediaServerBase):
    """Face enrollments, approval, and bypass auditing."""

    _FACE_FEEDBACK_TYPES = frozenset({"false_positive", "false_negative"})

    def _register_faces_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""

        # Local-only face-recognition enrollment (see vision/faces.py)
        app.router.add_get("/api/ai/faces", self._handle_faces_list)
        app.router.add_post("/api/ai/faces", self._handle_faces_enroll)
        app.router.add_delete("/api/ai/faces/{id}", self._handle_faces_delete)
        app.router.add_patch("/api/ai/faces/{id}", self._handle_faces_patch)
        app.router.add_patch(
            "/api/ai/faces/by-name/{name}", self._handle_faces_patch_by_name
        )
        app.router.add_delete(
            "/api/ai/faces/by-name/{name}", self._handle_faces_delete_by_name
        )
        app.router.add_get(
            "/api/ai/faces/bypass-stats", self._handle_faces_bypass_stats
        )
        app.router.add_get(
            "/api/ai/faces/feedback", self._handle_face_recognition_feedback_list
        )
        app.router.add_post(
            "/api/ai/faces/feedback/{clip_id}",
            self._handle_face_recognition_feedback_submit,
        )

    async def _handle_faces_list(self, _request: web.Request) -> web.Response:
        enrollments = await self._db.list_face_enrollments()
        return web.json_response(
            {
                "available": is_face_recognition_available(),
                "faces": [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "created_at": e["created_at"],
                        "approved": bool(e["approved"]),
                    }
                    for e in enrollments
                ],
            }
        )

    async def _handle_faces_enroll(self, request: web.Request) -> web.Response:
        """Enroll a household member from a single reference photo.

        Body: ``{"name": str, "image_base64": str, "approved"?: bool}`` — a
        data-URL prefix (e.g. ``data:image/jpeg;base64,``) on
        ``image_base64`` is stripped automatically if present. Requires
        exactly one face to be detected in the photo, to avoid an ambiguous
        enrollment. ``approved`` defaults to ``True`` (bypass trust granted
        immediately) — pass ``False`` to enroll someone for recognition
        labeling only, without granting suspicious-flag bypass trust.

        "No face detected" and "multiple faces detected" come back as HTTP
        200 with an ``error`` field, not 400 — those are expected outcomes
        of a normal attempt (a bad frame), unlike the malformed-request
        cases below which stay 400. Keeping them off 400 avoids the browser
        logging a spurious network error to the console for something the
        UI already reports via a toast.
        """
        try:
            body = await request.json()
            name = str(body.get("name", "") or "").strip()
            image_b64 = str(body.get("image_base64", "") or "")
            approved = bool(body.get("approved", True))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        if not name:
            return web.json_response({"error": "name is required"}, status=400)
        if not image_b64:
            return web.json_response({"error": "image_base64 is required"}, status=400)
        if "," in image_b64 and image_b64.strip().startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]

        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": "image_base64 is not valid base64"}, status=400
            )

        if not is_face_recognition_available():
            return web.json_response(
                {
                    "error": "Face recognition is not available on this system "
                    "(missing dependencies, or a CPU that can't run them)"
                },
                status=400,
            )

        embeddings = await self._face_embedder.embed(image_bytes)
        if not embeddings:
            # Not detecting a face is an expected, recoverable outcome of a
            # normal enrollment attempt (a blurry frame, bad angle, etc.) --
            # not a malformed request. Returning it as an HTTP error status
            # would make every browser log a "POST .../api/ai/faces 400"
            # network error to the console even though the UI already
            # surfaces this via a toast; a 200 with an error field avoids
            # that noise while still letting the frontend distinguish it
            # from success.
            return web.json_response(
                {"error": "No face detected in the provided photo"}
            )
        if len(embeddings) > 1:
            return web.json_response(
                {
                    "error": (
                        f"Detected {len(embeddings)} faces in the provided photo — "
                        "use a photo with only the person being enrolled visible"
                    )
                }
            )

        enrollment_id = await self._db.add_face_enrollment(
            name, embeddings[0], approved=approved
        )
        return web.json_response(
            {"id": enrollment_id, "name": name, "approved": approved}
        )

    async def _handle_faces_delete(self, request: web.Request) -> web.Response:
        try:
            enrollment_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid enrollment id")
        await self._db.delete_face_enrollment(enrollment_id)
        return web.json_response({"deleted": True})

    async def _handle_faces_patch(self, request: web.Request) -> web.Response:
        """Update an enrolled member's ``approved`` flag and/or ``name``.

        Body: ``{"approved"?: bool, "name"?: str}`` — at least one field
        must be present. Lets you flip bypass trust or fix a typo without
        deleting and re-enrolling (which would require a new photo).
        """
        try:
            enrollment_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid enrollment id")
        body = await _json_object(request)

        if "approved" not in body and "name" not in body:
            return web.json_response(
                {"error": "approved and/or name is required"}, status=400
            )

        if "approved" in body:
            await self._db.set_face_enrollment_approved(
                enrollment_id, bool(body["approved"])
            )
        if "name" in body:
            new_name = str(body["name"] or "").strip()
            if not new_name:
                return web.json_response({"error": "name cannot be empty"}, status=400)
            await self._db.rename_face_enrollment(enrollment_id, new_name)

        return web.json_response({"updated": True})

    async def _handle_faces_patch_by_name(self, request: web.Request) -> web.Response:
        """Bulk-update every enrolled photo sharing a name at once.

        Body: ``{"approved"?: bool, "name"?: str}`` — used by the Biometrics
        tab's grouped person view (see the multi-frame enrollment ADVANCED
        FEATURE) so approving/renaming a person affects every photo enrolled
        for them, not just one row.
        """
        name = request.match_info["name"]
        body = await _json_object(request)

        if "approved" not in body and "name" not in body:
            return web.json_response(
                {"error": "approved and/or name is required"}, status=400
            )

        if "approved" in body:
            await self._db.set_face_enrollments_approved_by_name(
                name, bool(body["approved"])
            )
        if "name" in body:
            new_name = str(body["name"] or "").strip()
            if not new_name:
                return web.json_response({"error": "name cannot be empty"}, status=400)
            await self._db.rename_face_enrollments_by_name(name, new_name)

        return web.json_response({"updated": True})

    async def _handle_faces_delete_by_name(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        await self._db.delete_face_enrollments_by_name(name)
        return web.json_response({"deleted": True})

    async def _handle_faces_bypass_stats(self, _request: web.Request) -> web.Response:
        stats = await self._db.get_face_bypass_stats()
        return web.json_response(stats)

    async def _handle_face_recognition_feedback_submit(
        self, request: web.Request
    ) -> web.Response:
        """Record a human report that face recognition got a clip wrong.

        Body: ``{"report_type": "false_positive"|"false_negative", "note": "",
        "person_name": ""}``. Requires the clip to exist, but deliberately
        does not require an analysis result — a false negative (an enrolled
        person present but never recognized) can be reported on any clip,
        not just ones the bypass already fired on. *person_name* is
        optional/free-form on the wire (the frontend sources it from the
        enrolled-faces list when there's more than one person to
        disambiguate) — see add_face_recognition_feedback for why it's
        stored but never fed back into matching automatically.
        """
        clip_id = request.match_info["clip_id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        try:
            body = await request.json()
            report_type = str(body.get("report_type", ""))
            note = str(body.get("note", "") or "")
            person_name = str(body.get("person_name", "") or "")
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        if report_type not in self._FACE_FEEDBACK_TYPES:
            raise web.HTTPBadRequest(
                text="report_type must be 'false_positive' or 'false_negative'"
            )

        await self._db.add_face_recognition_feedback(
            clip_id=clip_id,
            camera=clip["camera"],
            report_type=report_type,
            note=note,
            person_name=person_name,
        )
        return web.json_response({"saved": True})

    async def _handle_face_recognition_feedback_list(
        self, _request: web.Request
    ) -> web.Response:
        feedback = await self._db.get_face_recognition_feedback()
        return web.json_response(feedback)
