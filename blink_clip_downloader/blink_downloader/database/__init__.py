"""PostgreSQL-backed clip library with metadata, starring, tagging, and stats.

Runs against a PostgreSQL 17 server bundled in this add-on's own container
(see the Dockerfile and rootfs/etc/services.d/postgresql) — not a
user-configured external database. Connects over a local Unix domain socket
with no password (trust auth is scoped to that socket only, never exposed
outside the container), matching the zero-configuration experience SQLite
used to provide while gaining real concurrent access, native types, and
richer query support.

:class:`ClipDatabase` is one class, exactly as before, assembled here from
one mixin per part of the app rather than 3,500 lines in one file. Each
mixin owns the tables for a single job and can be read on its own:

    core        connecting, applying the schema, shutting down
    clips       the clip library and its archive lifecycle
    analysis    what the AI concluded, and what the tokens cost
    detections  the boxes, security events and vehicle signatures behind it
    faces       enrolled household members and the bypass audit trail
    learning    user feedback, activity baselines, scene drift
    queues      the analysis and Google Drive upload work queues
    cameras     battery history, and carrying a rename across every table
    legacy      the one-time import of a pre-5.0.0 SQLite library

plus :mod:`.schema` (the DDL) and :mod:`.sql` (pure text/row helpers).

Mixins rather than sub-objects (``db.clips.get(...)``) deliberately: the
class's public surface stays identical — all 104 methods, same names,
same signatures — so this is a change to how the source is organised and
not to how the database is used. (The module-scope helpers are a different
matter: only the handful re-exported below are reachable from here; the
rest live in :mod:`.sql` and :mod:`.schema`.) Adding a column still means editing :mod:`.schema` — and adding an
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` to ``_MIGRATIONS``, since
``CREATE TABLE IF NOT EXISTS`` is a no-op on a database that already
exists.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .analysis import AnalysisResultsMixin
from .cameras import CameraStateMixin
from .clips import ClipFilters, ClipLibraryMixin
from .core import DEFAULT_DSN, ConnectionMixin
from .detections import DetectionsMixin
from .faces import FaceEnrollmentsMixin
from .learning import AdaptiveLearningMixin
from .legacy import LegacyImportMixin
from .queues import QueuesMixin
from .schema import _MIGRATIONS, _SCHEMA
from .sql import (
    SUSPICIOUS_PERIODS,
    _affected,
    _local_day_bounds,
    _qm,
    _row_to_dict,
    _severities_at_or_above,
)

if TYPE_CHECKING:
    from ..analyzer import AnalysisResult

_LOGGER = logging.getLogger(__name__)


class ClipDatabase(
    ConnectionMixin,
    ClipLibraryMixin,
    AnalysisResultsMixin,
    DetectionsMixin,
    FaceEnrollmentsMixin,
    AdaptiveLearningMixin,
    QueuesMixin,
    CameraStateMixin,
    LegacyImportMixin,
):
    """Async wrapper around the PostgreSQL clip library."""

    # The one write that spans two domains, so it belongs to neither: a
    # completed analysis is a verdict row (analysis) plus the detections and
    # security events it was derived from (detections). Keeping it here also
    # keeps each mixin independently type-checkable, since nothing inside one
    # has to reach for a method defined in another.
    async def save_analysis(self, result: AnalysisResult) -> None:
        """Persist one analysis run in full.

        The verdict row, its object detections, and its security events are
        three tables that belong together — a clip whose security events are
        left over from a previous run reads as a different, older event in
        the timeline than the verdict beside it. Every caller goes through
        here rather than remembering all three.

        Each of the three writes is individually atomic, but they are not one
        transaction: a process killed between them leaves a new verdict
        beside the previous run's events until the clip is analyzed again.
        Widening the transaction would mean threading a connection through
        three otherwise-independent public methods, which is a poor trade for
        a window this small and self-healing — noted here so the next reader
        does not have to work out whether it was considered.

        A run that examined *no* frames is the one exception. Frame
        extraction failing (the file moved, archived, or still being
        written) produces a real result row saying so — but it looked at
        nothing, so it has no evidence of its own and must not replace the
        evidence of a run that did. Without this, re-analyzing a clip whose
        file has since gone erases its detections and its whole entry from
        the Security tab's timeline.
        """
        await self.add_analysis_result(result.to_dict())
        if result.frame_count <= 0:
            return
        await self.save_detected_objects(
            result.clip_id,
            result.detected_objects,
            interval=result.detection_interval,
            frame_size=result.detection_frame_size,
        )
        await self.save_security_events(
            result.clip_id,
            result.camera,
            result.security_events,
            risk_score=result.risk_score,
            evidence_quality=result.evidence_quality,
        )


__all__ = [
    "DEFAULT_DSN",
    "SUSPICIOUS_PERIODS",
    "_MIGRATIONS",
    "_SCHEMA",
    "ClipDatabase",
    "ClipFilters",
    "_affected",
    "_local_day_bounds",
    "_qm",
    "_row_to_dict",
    "_severities_at_or_above",
]
