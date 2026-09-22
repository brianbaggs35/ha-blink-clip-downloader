"""Who the household recognizes, and whether that cleared a clip.

Backs the Biometrics tab. ``face_enrollments.approved`` is the gate on the
safety-critical suspicious-flag bypass — enrolling someone is not the same
as approving them for it — and ``get_face_bypass_stats`` is the audit trail
that makes the bypass reviewable rather than something to trust blindly.
The embeddings themselves never leave the add-on, and a recognized name
never reaches an AI provider.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from .core import _DatabaseBase
from .sql import (
    _qm,
)

_LOGGER = logging.getLogger(__name__)


class FaceEnrollmentsMixin(_DatabaseBase):
    """Enrolled faces, their approval state, and bypass auditing."""

    async def get_face_bypass_stats(self, recent_limit: int = 20) -> dict[str, Any]:
        """Auditable summary of face-recognition suspicious-flag bypasses.

        Backs the Biometrics tab's bypass activity card, which exists so a
        household member can confirm the bypass is firing for the right
        people (and catch it if it's ever wrong) instead of trusting it
        blindly — see analyzer/base.py's face-bypass-gating docstrings.
        """
        if self._pool is None:
            return {"total_bypassed": 0, "by_name": [], "recent": []}
        total = (
            await self._pool.fetchval(
                "SELECT COUNT(*) FROM analysis_results WHERE face_bypass_applied"
            )
            or 0
        )
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, face_bypass_names, analyzed_at
                FROM analysis_results
                WHERE face_bypass_applied
                ORDER BY analyzed_at DESC
                LIMIT ?
                """
            ),
            recent_limit,
        )
        recent = [dict(r) for r in rows]

        by_name: dict[str, int] = {}
        for row in recent:
            for name in str(row.get("face_bypass_names", "")).split(", "):
                if name:
                    by_name[name] = by_name.get(name, 0) + 1
        # The per-name breakdown above only covers `recent` (bounded, cheap)
        # rather than a full-table GROUP BY — good enough for "which names
        # are showing up lately", not meant to be an exact lifetime tally.

        return {
            "total_bypassed": total,
            "by_name": [
                {"name": name, "count": count}
                for name, count in sorted(by_name.items(), key=lambda kv: -kv[1])
            ],
            "recent": recent,
        }

    async def add_face_recognition_feedback(
        self,
        clip_id: str,
        camera: str,
        report_type: str,
        note: str = "",
        person_name: str = "",
    ) -> None:
        """Record a human report that face recognition got a clip wrong.

        *report_type* is ``"false_positive"`` (a face was matched/bypassed
        that shouldn't have been) or ``"false_negative"`` (an enrolled
        person was present but not recognized/bypassed). *person_name*
        identifies who the report is about — for a false positive it's
        whoever the (wrong) bypass matched, for a false negative it's
        whichever enrolled person the reporter says was missed, or '' if
        unknown/not selected. Pure audit trail — see
        face_recognition_feedback's own schema comment for why this
        deliberately doesn't feed any automatic adjustment; person_name
        only makes the trail more actionable for a human reviewing it
        (e.g. "re-enroll this person with clearer photos").
        """
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO face_recognition_feedback
                  (clip_id, camera, report_type, note, person_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """
            ),
            clip_id,
            camera,
            report_type,
            note,
            person_name,
            datetime.now(UTC).isoformat(),
        )

    async def get_face_recognition_feedback(
        self, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Most recent face-recognition accuracy reports, for the
        Biometrics tab's activity card — see add_face_recognition_feedback."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT clip_id, camera, report_type, note, person_name, created_at
                FROM face_recognition_feedback
                ORDER BY created_at DESC
                LIMIT ?
                """
            ),
            limit,
        )
        return [dict(r) for r in rows]

    async def add_face_enrollment(
        self,
        name: str,
        embedding: list[float],
        approved: bool = True,
        thumbnail: bytes | None = None,
        frame_width: int | None = None,
    ) -> int:
        """Store one enrolled photo's face embedding. Returns its id.

        *approved* controls whether this person counts toward the
        suspicious-flag bypass (see analyzer/base.py's ``_face_bypass_applies``)
        — defaults to True so the common "add a family member" flow works
        immediately, but can be set False (or flipped later via
        :meth:`set_face_enrollments_approved_by_name`) to enroll someone for
        recognition/labeling without granting them bypass trust.
        *thumbnail* is the small JPEG of the face the Biometrics tab shows;
        *frame_width* the width of the clip frame it was captured from
        (``None`` for an uploaded photo), which the tab compares against the
        width recognition currently matches at.
        """
        if self._pool is None:
            return 0
        new_id = await self._pool.fetchval(
            _qm(
                "INSERT INTO face_enrollments "
                "(name, embedding, created_at, approved, thumbnail, frame_width) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id"
            ),
            name,
            json.dumps(embedding),
            datetime.now(UTC).isoformat(),
            approved,
            thumbnail,
            frame_width,
        )
        return new_id or 0

    async def get_person_approval(self, name: str) -> bool | None:
        """Whether *name* is approved for the bypass, or ``None`` if nobody
        by that name is enrolled.

        A person counts as approved only when every one of their photos is
        — the conservative reading of a mix that an older version could
        leave behind — so a photo added to them inherits exactly what the
        Biometrics tab's switch shows for them.
        """
        if self._pool is None:
            return None
        return await self._pool.fetchval(
            _qm("SELECT BOOL_AND(approved) FROM face_enrollments WHERE name=?"),
            name,
        )

    async def list_face_enrollments(self) -> list[dict[str, Any]]:
        """Return every enrolled photo, with embeddings decoded to lists.

        ``has_thumbnail`` says whether :meth:`get_face_enrollment_thumbnail`
        has anything to return; the bytes themselves are left out, since
        clip analysis reads this list for every clip and never needs them.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT id, name, embedding, created_at, approved, frame_width, "
            "thumbnail IS NOT NULL AS has_thumbnail "
            "FROM face_enrollments ORDER BY name, id"
        )
        results = []
        for r in rows:
            d = dict(r)
            d["embedding"] = json.loads(d["embedding"])
            results.append(d)
        return results

    async def get_face_enrollment_thumbnail(self, enrollment_id: int) -> bytes | None:
        """The stored JPEG of one enrolled face, or ``None``."""
        if self._pool is None:
            return None
        return await self._pool.fetchval(
            _qm("SELECT thumbnail FROM face_enrollments WHERE id=?"), enrollment_id
        )

    async def delete_face_enrollment(self, enrollment_id: int) -> None:
        """Remove one enrolled photo by id."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("DELETE FROM face_enrollments WHERE id=?"), enrollment_id
        )

    async def set_face_enrollments_approved_by_name(
        self, name: str, approved: bool
    ) -> None:
        """Flip bypass-approval for every enrollment sharing *name*."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE face_enrollments SET approved=? WHERE name=?"),
            approved,
            name,
        )

    async def rename_face_enrollments_by_name(
        self, old_name: str, new_name: str
    ) -> None:
        """Rename every enrollment currently sharing *old_name*."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE face_enrollments SET name=? WHERE name=?"),
            new_name,
            old_name,
        )

    async def delete_face_enrollments_by_name(self, name: str) -> None:
        """Remove every enrollment (every enrolled photo) sharing *name*."""
        if self._pool is None:
            return
        await self._pool.execute(_qm("DELETE FROM face_enrollments WHERE name=?"), name)
