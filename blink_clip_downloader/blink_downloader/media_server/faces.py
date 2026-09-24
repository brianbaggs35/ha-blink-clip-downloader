"""The Biometrics tab's API: finding faces to enroll, and enrolled people.

Enrolling is a pick-from-what-was-found flow. A scan (of a clip, or of an
uploaded photo) runs face detection and returns every usable face as a
thumbnail plus an opaque candidate id; the embedding behind each one stays
server-side in :class:`~blink_downloader.face_enrollment.FaceCandidateStore`,
and enrolling sends back only the ids of the faces the user picked. So the
browser never handles an embedding, and there is no "exactly one face in
this photo" rule to trip over — a photo with three faces simply offers three.

People are managed by name: a person is every enrolled photo sharing one.
The per-photo *approved* flag gates the safety-critical suspicious-flag
bypass — enrolling someone is not the same as approving them for it. Also
the bypass audit trail and the "this match was wrong" reports behind it.
Embeddings are computed locally and never leave the add-on.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import unicodedata
from pathlib import Path
from typing import Any

from aiohttp import web

from ..face_enrollment import (
    CANDIDATE_CAPACITY,
    MIN_CANDIDATE_PROBABILITY,
    describe_match,
    group_candidates,
    match_candidates,
    review_enrollments,
    suppress_near_duplicates,
)
from ..ffmpeg_output import extract_jpeg_frames
from ..vision import is_face_recognition_available
from ..vision.faces import DetectedFace, match_enrollment
from .core import _MediaServerBase
from .support import _CLIP_NOT_FOUND, _json_object

_LOGGER = logging.getLogger(__name__)

# A person's name ends up in notification text and on the Biometrics tab;
# anything longer than this is a paste accident rather than a name.
_MAX_NAME_LENGTH = 60

# How densely a clip is scanned: at most this many frames, never closer
# together than _SCAN_MIN_INTERVAL seconds. Someone facing the camera is
# often a brief moment, so this samples twice as densely as the old picker's
# one-frame-a-second — affordable because the scan now keeps only frames
# with a face in them instead of sending every frame to the browser.
_SCAN_MAX_FRAMES = 40
_SCAN_MIN_INTERVAL = 0.5

# Most faces one enroll or group request may name: everything the store can
# hold. The picker groups every face found so far in one request, so a lower
# cap (it was 500) silently stopped grouping after a few dozen busy clips.
_MAX_CANDIDATES_PER_REQUEST = CANDIDATE_CAPACITY

# The largest id a face_enrollments row can have (an INTEGER column). An id
# beyond it can match nothing, and passing it to asyncpg is a bare 500.
_MAX_ENROLLMENT_ID = 2**31 - 1

_UNAVAILABLE = (
    "Face recognition is not available on this system "
    "(missing dependencies, or a CPU that can't run them)"
)
_MODELS_FAILED = "The face-recognition models could not be loaded — see the add-on logs"
_NAME_RULE = f"Enter a name of up to {_MAX_NAME_LENGTH} characters"


def _person_name(value: Any) -> str | None:
    """A usable new name for a person, or ``None``.

    Control characters are refused outright: a NUL cannot be stored in a
    PostgreSQL text column at all, and a newline in a name would break the
    one-line notifications it is written into.
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _MAX_NAME_LENGTH:
        return None
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        return None
    return name


def _existing_name(body: dict[str, Any]) -> str:
    """The ``name`` a people request refers to, exactly as stored.

    Not normalised the way :func:`_person_name` normalises a new name: this
    has to match rows written by any earlier version verbatim.
    """
    name = body.get("name")
    if not isinstance(name, str) or not name or "\x00" in name:
        raise web.HTTPBadRequest(text="name is required")
    return name


def _enrollment_id(request: web.Request) -> int:
    """The ``{id}`` path segment as a usable enrollment id, or a 400."""
    try:
        enrollment_id = int(request.match_info["id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid enrollment id")
    if not 0 < enrollment_id <= _MAX_ENROLLMENT_ID:
        raise web.HTTPBadRequest(text="Invalid enrollment id")
    return enrollment_id


def _candidate_ids(body: dict[str, Any]) -> list[str]:
    """The ``candidate_ids`` list from a request body, or a 400."""
    ids = body.get("candidate_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > _MAX_CANDIDATES_PER_REQUEST
        or not all(isinstance(i, str) for i in ids)
    ):
        raise web.HTTPBadRequest(text="candidate_ids must list the faces to use")
    return list(dict.fromkeys(ids))


def _data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


class FaceRoutesMixin(_MediaServerBase):
    """Finding faces, enrolling them, managing people, and bypass auditing."""

    _FACE_FEEDBACK_TYPES = frozenset({"false_positive", "false_negative"})

    def _register_faces_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""

        # Local-only face-recognition enrollment (see vision/faces.py)
        app.router.add_get("/api/ai/faces", self._handle_faces_list)
        app.router.add_post("/api/ai/faces", self._handle_faces_enroll)
        app.router.add_delete("/api/ai/faces/{id}", self._handle_faces_delete)
        app.router.add_get("/api/ai/faces/thumbs/{id}", self._handle_face_thumbnail)
        app.router.add_patch("/api/ai/faces/people", self._handle_people_patch)
        app.router.add_delete("/api/ai/faces/people", self._handle_people_delete)
        app.router.add_get("/api/ai/faces/scan/{clip_id}", self._handle_faces_scan)
        app.router.add_post("/api/ai/faces/detect", self._handle_faces_detect)
        app.router.add_post("/api/ai/faces/group", self._handle_faces_group)
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
        """Every enrolled photo, plus whether recognition can and does run.

        ``recognition_enabled`` and ``analysis_enabled`` are separate
        because they need different fixes: face recognition only ever runs
        inside clip analysis, so enrolling people achieves nothing until
        both the option and an AI provider are switched on. ``frame_width``
        is the width recognition matches at, which a photo's own
        ``frame_width`` (the clip frame it was captured from, ``null`` for
        an uploaded photo or one enrolled before 6.0.7) should equal — see
        ``FACE_RESOLUTION_WIDTHS`` for why. ``warning``
        flags a photo that resembles none of the person's other photos, or
        that would be recognized as someone else — see
        :func:`~blink_downloader.face_enrollment.review_enrollments`.
        """
        enrollments = await self._db.list_face_enrollments()
        warnings = review_enrollments(enrollments)
        return web.json_response(
            {
                "available": is_face_recognition_available(),
                "recognition_enabled": self._face_recognition_enabled,
                "analysis_enabled": self._analyzer is not None,
                "frame_width": self._face_frame_width,
                "faces": [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "created_at": e["created_at"],
                        "approved": bool(e["approved"]),
                        "has_thumbnail": bool(e.get("has_thumbnail")),
                        "frame_width": e.get("frame_width"),
                        "camera": e.get("camera"),
                        "warning": warnings.get(e["id"]),
                    }
                    for e in enrollments
                ],
            }
        )

    async def _handle_face_thumbnail(self, request: web.Request) -> web.Response:
        thumbnail = await self._db.get_face_enrollment_thumbnail(
            _enrollment_id(request)
        )
        if not thumbnail:
            raise web.HTTPNotFound(text="No photo stored for this enrollment")
        # An enrollment's photo never changes, but it is a face: cacheable
        # by this browser only, never by anything in between.
        return web.Response(
            body=thumbnail,
            content_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    async def _detection_ready(self) -> str | None:
        """Why faces cannot be detected right now, or ``None`` if they can."""
        if not is_face_recognition_available():
            return _UNAVAILABLE
        if not await self._face_embedder.ensure_ready():
            return _MODELS_FAILED
        return None

    def _offer(
        self,
        face: DetectedFace,
        enrollments: list[dict[str, Any]],
        time: float | None = None,
        frame_width: int | None = None,
        camera: str | None = None,
    ) -> dict[str, Any]:
        """Hold *face* as a candidate and describe it for the picker.

        ``match`` is who clip analysis would recognize this face as today
        (same threshold, same tie-break), so the picker can say "already
        recognized as Brian" — a face that is *not* yet recognized is the
        more valuable one to enroll. *frame_width* and *camera* describe the
        clip frame it came from, both ``None`` for an uploaded photo.
        """
        return {
            "id": self._face_candidates.add(
                face.embedding, face.thumbnail, face.quality, frame_width, camera
            ),
            "thumbnail": _data_url(face.thumbnail),
            "quality": face.quality,
            "width": face.width,
            "time": time,
            "match": describe_match(match_enrollment(face.embedding, enrollments)),
        }

    async def _handle_faces_detect(self, request: web.Request) -> web.Response:
        """Find the faces in an uploaded photo.

        Body: ``{"image_base64": str}``, a data-URL prefix allowed. Returns
        ``{"faces": [...]}`` best first; a photo with several faces offers
        each one, rather than being refused. "Nothing usable found" and "this
        photo could not be read" are 200s with an ``error`` field — expected
        outcomes of a normal attempt, which the UI reports itself, unlike
        the malformed-request 400s.
        """
        body = await _json_object(request)
        image_b64 = body.get("image_base64")
        if not isinstance(image_b64, str) or not image_b64:
            return web.json_response({"error": "image_base64 is required"}, status=400)
        if image_b64.startswith("data:") and "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]
        try:
            image = base64.b64decode(image_b64, validate=True)
        except ValueError:  # binascii.Error is a ValueError
            return web.json_response(
                {"error": "image_base64 is not valid base64"}, status=400
            )

        problem = await self._detection_ready()
        if problem == _UNAVAILABLE:
            return web.json_response({"error": problem}, status=400)
        if problem:
            return web.json_response({"error": problem, "faces": []})

        faces = await self._face_embedder.detect(image, thumbnails=True)
        if faces is None:
            return web.json_response(
                {
                    "error": "This photo couldn't be read — JPEG and PNG work; "
                    "iPhone HEIC photos need converting first",
                    "faces": [],
                }
            )
        usable = sorted(
            (f for f in faces if f.probability >= MIN_CANDIDATE_PROBABILITY),
            key=lambda f: -f.quality,
        )
        enrollments = await self._db.list_face_enrollments()
        return web.json_response(
            {"faces": [self._offer(face, enrollments) for face in usable]}
        )

    async def _handle_faces_scan(self, request: web.Request) -> web.Response:
        """Find every usable face in one clip.

        Samples up to ``_SCAN_MAX_FRAMES`` frames across the clip at the
        width face recognition matches at (``ai_face_recognition_resolution``),
        so an enrolled face looks the way faces look when they are later
        matched — the old picker's 480px frames against recognition's 640
        cost a typical doorstep face most of its matches (2 of 30 against
        20 of 30, measured). Near-identical shots of the
        same face are collapsed (``duplicates_hidden`` says how many), and
        each remaining face is returned with its time in the clip. Returns
        ``{"clip_id", "frames_scanned", "duplicates_hidden", "faces"}``.
        """
        clip_id = request.match_info["clip_id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        problem = await self._detection_ready()
        if problem == _UNAVAILABLE:
            return web.json_response({"error": problem}, status=400)
        empty: dict[str, Any] = {
            "clip_id": clip_id,
            "frames_scanned": 0,
            "duplicates_hidden": 0,
            "faces": [],
        }
        if problem:
            return web.json_response({**empty, "error": problem})
        if not await asyncio.to_thread(Path(clip["file_path"]).is_file):
            return web.json_response(
                {**empty, "error": "This clip's video file is no longer on disk"}
            )

        duration = float(clip.get("duration") or 0) or 10.0
        count = max(1, min(math.ceil(duration / _SCAN_MIN_INTERVAL), _SCAN_MAX_FRAMES))
        interval = max(duration / count, _SCAN_MIN_INTERVAL)
        frames = await extract_jpeg_frames(
            clip["file_path"],
            width=self._face_frame_width,
            interval=interval,
            count=count,
            label=clip_id,
        )
        if not frames:
            return web.json_response(
                {**empty, "error": "No frames could be extracted from this clip"}
            )

        found: list[tuple[DetectedFace, float]] = []
        for index, frame in enumerate(frames):
            for face in await self._face_embedder.detect(frame, thumbnails=True) or []:
                if face.probability >= MIN_CANDIDATE_PROBABILITY:
                    found.append((face, round(index * interval, 1)))
        keep = suppress_near_duplicates(
            [face.embedding for face, _ in found], [face.quality for face, _ in found]
        )
        enrollments = await self._db.list_face_enrollments()
        return web.json_response(
            {
                **empty,
                "frames_scanned": len(frames),
                "duplicates_hidden": len(found) - len(keep),
                "faces": [
                    self._offer(
                        found[i][0],
                        enrollments,
                        found[i][1],
                        self._face_frame_width,
                        clip["camera"],
                    )
                    for i in keep
                ],
            }
        )

    async def _handle_faces_group(self, request: web.Request) -> web.Response:
        """Group candidates by apparent person, across every scan so far.

        Body: ``{"candidate_ids": [...]}``. Returns ``{"groups": [[id, ...],
        ...], "expired": [id, ...], "matches": {id: match}}`` — ``expired``
        listing ids no longer held (too old, or already enrolled), so the
        picker can drop them, and ``matches`` who each held face would be
        recognized as against the people enrolled *now* (see
        :func:`~blink_downloader.face_enrollment.match_candidates`).
        """
        ids = _candidate_ids(await _json_object(request))
        held = {i: self._face_candidates.get(i) for i in ids}
        live = [c for c in held.values() if c]
        enrollments = await self._db.list_face_enrollments()
        # In a worker thread: even vectorized, a full store is real numpy
        # work, and it must not stall every other request while it runs.
        groups, matches = await asyncio.to_thread(
            lambda: (group_candidates(live), match_candidates(live, enrollments))
        )
        return web.json_response(
            {
                "groups": groups,
                "expired": [i for i, c in held.items() if c is None],
                "matches": matches,
            }
        )

    async def _handle_faces_enroll(self, request: web.Request) -> web.Response:
        """Enroll the picked faces under one person's name.

        Body: ``{"name": str, "candidate_ids": [str, ...], "approved"?:
        bool}``. *approved* (default ``True``) applies to a new person only:
        photos added to someone already enrolled take that person's current
        approval (see ``get_person_approval``), so adding photos can never
        quietly leave a person half-approved. Returns ``{"name", "enrolled",
        "expired", "approved", "existing"}``; if every picked face has
        expired, a 200 with an ``error`` field instead.
        """
        body = await _json_object(request)
        name = _person_name(body.get("name"))
        if name is None:
            return web.json_response({"error": _NAME_RULE}, status=400)
        ids = _candidate_ids(body)

        existing = await self._db.get_person_approval(name)
        approved = (
            existing if existing is not None else bool(body.get("approved", True))
        )
        enrolled = 0
        for candidate_id in ids:
            # Taken before the insert is awaited, not after: a double-clicked
            # Enroll sends two of these at once, and a face merely looked up
            # here was still there for the second request to enroll again.
            candidate = self._face_candidates.take(candidate_id)
            if candidate is None:
                continue
            await self._db.add_face_enrollment(
                name,
                candidate.embedding,
                approved=approved,
                thumbnail=candidate.thumbnail or None,
                frame_width=candidate.frame_width,
                camera=candidate.camera,
            )
            enrolled += 1

        expired = len(ids) - enrolled
        if not enrolled:
            return web.json_response(
                {
                    "error": "Those faces are no longer available — scan again "
                    "to pick them",
                    "enrolled": 0,
                    "expired": expired,
                }
            )
        return web.json_response(
            {
                "name": name,
                "enrolled": enrolled,
                "expired": expired,
                "approved": approved,
                "existing": existing is not None,
            }
        )

    async def _handle_faces_delete(self, request: web.Request) -> web.Response:
        """Remove one enrolled photo, leaving the person's others."""
        await self._db.delete_face_enrollment(_enrollment_id(request))
        return web.json_response({"deleted": True})

    async def _handle_people_patch(self, request: web.Request) -> web.Response:
        """Approve or rename a person — every photo enrolled under a name.

        Body: ``{"name": str, "approved"?: bool, "new_name"?: str}``. The
        name travels in the body rather than the path because Home
        Assistant's ingress proxy decodes a path before forwarding it: a
        person called "Mom/Dad" became two path segments and 404'd, so they
        could be enrolled but never approved, renamed or removed. Renaming
        onto a name already in use merges the two.
        """
        body = await _json_object(request)
        name = _existing_name(body)
        if "approved" not in body and "new_name" not in body:
            return web.json_response(
                {"error": "approved and/or new_name is required"}, status=400
            )
        new_name = None
        if "new_name" in body:
            new_name = _person_name(body["new_name"])
            if new_name is None:
                return web.json_response({"error": _NAME_RULE}, status=400)

        # Validated in full before anything is written: a rename refused
        # after the approval had already been applied used to leave half of
        # a request done behind a 400.
        if "approved" in body:
            await self._db.set_face_enrollments_approved_by_name(
                name, bool(body["approved"])
            )
        if new_name is not None:
            await self._db.rename_face_enrollments_by_name(name, new_name)
        return web.json_response({"updated": True})

    async def _handle_people_delete(self, request: web.Request) -> web.Response:
        """Remove a person: every photo enrolled under ``{"name": str}``."""
        name = _existing_name(await _json_object(request))
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

        body = await _json_object(request)
        report_type = str(body.get("report_type", ""))
        note = str(body.get("note", "") or "")
        person_name = str(body.get("person_name", "") or "")

        if report_type not in self._FACE_FEEDBACK_TYPES:
            raise web.HTTPBadRequest(
                text="report_type must be 'false_positive' or 'false_negative'"
            )
        # A NUL cannot be stored in a PostgreSQL text column; left to reach
        # asyncpg it was a bare 500 rather than a bad request.
        if "\x00" in note or "\x00" in person_name:
            raise web.HTTPBadRequest(text="Request contains an invalid null byte")

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
