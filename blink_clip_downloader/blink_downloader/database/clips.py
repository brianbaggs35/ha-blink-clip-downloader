"""The clip library itself: rows, their metadata, and their lifecycle.

One table's worth of domain — ``clips`` — from the moment the downloader
inserts a row through starring, tagging and duration backfill, into
archiving and cold-storage bookkeeping, and out again at deletion. The
archive half lives here rather than in its own module because it is the
same rows in a later state: ``get_archive_clips`` is ``get_clips`` with an
archive filter, and both read the same columns.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .core import _DatabaseBase
from .sql import (
    _WHERE_CAMERA,
    _WHERE_SINCE,
    _WHERE_UNTIL,
    _affected,
    _local_day_bounds,
    _local_utc_offset_sql,
    _qm,
    _row_to_dict,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClipFilters:
    """Which clips a Library query should match.

    Every field is a condition that narrows the result; all defaulted, so
    ``ClipFilters()`` means "everything that is not archived". Grouped
    because they arrive together — the Library's filter bar sends them as
    one set of query parameters — and because a query taking fifteen
    loose arguments is one positional slip away from filtering by the
    wrong thing.
    """

    camera: str | None = None
    since: str | None = None
    until: str | None = None
    starred: bool | None = None
    source: str | None = None
    tag: str | None = None
    search: str | None = None
    archived: bool = False
    archive_path: str | None = None
    notified_only: bool = False
    recognized_only: bool = False
    #: Confidence at or above which a suspicious verdict counts as
    #: notified — the same gate AnalysisQueue uses to decide whether to
    #: send an alert, so the badge and the alert cannot disagree.
    min_confidence: float = 0.0


class ClipLibraryMixin(_DatabaseBase):
    """Clip rows: create, read, annotate, archive, delete."""

    async def add_clip(self, clip: dict[str, Any]) -> None:
        """Insert or ignore a clip record."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                """
                INSERT INTO clips
                  (id, camera, file_path, timestamp, size_bytes, duration,
                   source, network_id, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            str(clip.get("id") or ""),
            str(clip.get("camera") or "unknown"),
            str(clip.get("path") or ""),
            str(clip.get("timestamp") or ""),
            int(clip.get("size_bytes") or 0),
            # duration / network_id can be None (null) in the Blink API
            # response for live-view and some camera types — use `or 0`
            # so int() never receives NoneType.
            int(clip.get("duration") or 0),
            str(clip.get("source") or ""),
            int(clip.get("network_id") or 0),
            datetime.now(UTC).isoformat(),
        )

    async def star_clip(self, clip_id: str, starred: bool) -> bool:
        """Star or unstar a clip. Returns True if the record was found."""
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE clips SET starred=? WHERE id=?"), starred, clip_id
        )
        return _affected(status) > 0

    async def update_clip_duration(self, clip_id: str, duration: int) -> bool:
        """Backfill a clip's duration (see app.py's startup duration backfill).

        Only ever called with a freshly-probed, positive value — a clip
        already showing a real duration is never re-probed in the first
        place (see get_clips_missing_duration), so this never needs to
        guard against overwriting a good value with a worse one.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE clips SET duration=? WHERE id=?"), duration, clip_id
        )
        return _affected(status) > 0

    async def set_tags(self, clip_id: str, tags: list[str]) -> bool:
        """Replace the tag list for a clip."""
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("UPDATE clips SET tags=? WHERE id=?"), json.dumps(tags), clip_id
        )
        return _affected(status) > 0

    async def mark_archived(self, clip_id: str, archive_path: str) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("UPDATE clips SET archived=TRUE, archive_path=? WHERE id=?"),
            archive_path,
            clip_id,
        )

    async def mark_gdrive_uploaded(self, clip_id: str, file_id: str) -> None:
        if self._pool is None:
            return
        await self._pool.execute(
            _qm(
                "UPDATE clips SET gdrive_backed_up=TRUE, gdrive_file_id=?, "
                "gdrive_uploaded_at=? WHERE id=?"
            ),
            file_id,
            datetime.now(UTC).isoformat(),
            clip_id,
        )

    async def delete_clip(self, clip_id: str) -> bool:
        """Remove a clip record from the database."""
        if self._pool is None:
            return False
        status = await self._pool.execute(_qm("DELETE FROM clips WHERE id=?"), clip_id)
        return _affected(status) > 0

    async def delete_clip_by_path(self, file_path: str) -> bool:
        """Remove a clip record by its file_path.

        Used by retention cleanup, which deletes files directly from the
        filesystem (glob + unlink) rather than by clip ID, so it doesn't
        leave an orphaned DB row (and orphaned analysis_results/
        analysis_queue rows) behind for a file that no longer exists.
        """
        if self._pool is None:
            return False
        status = await self._pool.execute(
            _qm("DELETE FROM clips WHERE file_path=?"), file_path
        )
        return _affected(status) > 0

    async def delete_clips_by_archive_path(self, archive_path: str) -> int:
        """Remove every clip record stored in one archive ZIP.

        Used by the Storage tab's "delete entire archive" action, once the
        ZIP file itself and any Google Drive backups have already been
        handled by the caller (media_server/storage.py's _handle_delete_archive) —
        this is just the bulk DB-side cleanup. Returns the number of rows
        removed.
        """
        if self._pool is None:
            return 0
        status = await self._pool.execute(
            _qm("DELETE FROM clips WHERE archived=TRUE AND archive_path=?"),
            archive_path,
        )
        return _affected(status)

    async def get_clip(self, clip_id: str) -> dict[str, Any] | None:
        """Return a single clip record or None."""
        if self._pool is None:
            return None
        row = await self._pool.fetchrow(_qm("SELECT * FROM clips WHERE id=?"), clip_id)
        return _row_to_dict(row) if row else None

    async def get_clips_missing_duration(self) -> list[dict[str, Any]]:
        """Clips with no known duration, for app.py's startup backfill.

        Covers both a clip whose Blink API response never included a real
        duration (some accounts/clip types consistently don't) and a
        pre-5.0.1 clip downloaded before that fallback existed — either
        way, the file itself is the source of truth once probed. Returns
        just id/file_path, the only two fields the backfill needs.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT id, file_path FROM clips WHERE duration <= 0"
        )
        return [dict(row) for row in rows]

    async def get_clips(
        self,
        filters: ClipFilters | None = None,
        *,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query clips matching *filters*, in *sort* order, one page at a time.

        The twelve match conditions travel together as :class:`ClipFilters`
        while sort/limit/offset stay loose, because they answer different
        questions: one is "which clips", the other "how do I want them
        back", and the Library sends the first from its filter bar and the
        second from its paging controls.

        sort values: "newest" | "oldest" | "camera" | "size" | "duration"

        Every returned clip includes a ``notified`` boolean (True if the
        clip's *most recent* AI analysis result was/would be suspicious at
        *min_confidence* or higher — the same gate ``AnalysisQueue`` uses to
        decide whether to dispatch a notification, including its
        ``risk_override_applied`` exemption: a clip flagged by the
        deterministic risk score rather than by the model skips the
        confidence threshold there, so the badge has to skip it here too or
        the two disagree about a notification that was actually sent). Set
        *notified_only* to restrict results to just those clips. Set *recognized_only* to
        restrict to clips with ``face_recognized`` true (see below) —
        parameter-free, unlike *notified_only*/*min_confidence*, since
        ``approved_faces_seen`` has no equivalent threshold.

        Deliberately scoped to only the latest ``analysis_results`` row per
        clip (matching :meth:`get_analysis_for_clip`'s own "most recent
        wins" semantics) rather than "any row ever" — a clip re-analyzed
        after its first (suspicious) pass, e.g. via the Library's
        "Re-analyze" button or a later fix that changes the verdict, would
        otherwise show the 🔔 notified badge forever even once the current
        verdict is not-suspicious, since `add_analysis_result` always
        inserts a new row rather than replacing the old one.

        Also includes a ``face_recognized`` boolean, True when the clip's
        most recent analysis unambiguously matched only approved,
        locally-enrolled household member(s) with no stranger or
        unrecognized face anywhere in the sampled frames (``analyzer/base.py``'s
        ``approved_faces_seen``, the same all-or-nothing condition
        ``_face_bypass_applies`` itself checks — see its docstring — just
        recorded regardless of whether the clip also happened to need its
        suspicious flag cleared). Deliberately *not* the narrower
        ``face_bypass_applied`` (whether the bypass actually fired): most
        real matches are a household member's own routine, already
        non-suspicious visit, which never reaches the bypass check at all
        (see analyzer/base.py's `if is_suspicious and ...` gating) and would
        otherwise never show any recognition signal at all despite a clear
        match. `face_bypass_applied` itself is intentionally left alone
        for `get_face_bypass_stats`'s narrower audit purpose (confirming
        the *safety* bypass fires for the right people) — same latest-row
        scoping here, so a clip that no longer matches after a later
        re-analysis doesn't keep showing the badge forever either.
        """
        if self._pool is None:
            return []

        f = filters or ClipFilters()
        camera = f.camera
        since = f.since
        until = f.until
        starred = f.starred
        source = f.source
        tag = f.tag
        search = f.search
        archived = f.archived
        archive_path = f.archive_path
        notified_only = f.notified_only
        recognized_only = f.recognized_only
        min_confidence = f.min_confidence

        notified_exists = (
            "EXISTS (SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
            "AND ar.is_suspicious "
            "AND (ar.confidence >= ? OR ar.risk_override_applied) "
            "AND ar.analyzed_at = (SELECT MAX(ar2.analyzed_at) FROM analysis_results ar2 "
            "WHERE ar2.clip_id = clips.id))"
        )
        face_recognized_exists = (
            "EXISTS (SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
            "AND ar.approved_faces_seen "
            "AND ar.analyzed_at = (SELECT MAX(ar2.analyzed_at) FROM analysis_results ar2 "
            "WHERE ar2.clip_id = clips.id))"
        )

        where: list[str] = ["archived = ?"]
        params: list[Any] = [archived]

        if camera and camera != "all":
            where.append(_WHERE_CAMERA)
            params.append(camera)
        if archive_path:
            where.append("archive_path = ?")
            params.append(archive_path)
        if since:
            where.append(_WHERE_SINCE)
            params.append(since)
        if until:
            where.append(_WHERE_UNTIL)
            params.append(until)
        if starred is not None:
            where.append("starred = ?")
            params.append(starred)
        if source:
            where.append("source = ?")
            params.append(source)
        if tag:
            where.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        if search:
            where.append("(LOWER(camera) LIKE LOWER(?) OR id LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if notified_only:
            where.append(notified_exists)
            params.append(min_confidence)
        if recognized_only:
            where.append(face_recognized_exists)

        _sort_map = {
            "newest": "timestamp DESC",
            "oldest": "timestamp ASC",
            "camera": "LOWER(camera) ASC, timestamp DESC",
            "size": "size_bytes DESC",
            "duration": "duration DESC",
        }
        order = _sort_map.get(sort, "timestamp DESC")

        sql = (
            f"SELECT *, {notified_exists} AS notified, "
            f"{face_recognized_exists} AS face_recognized FROM clips "
            f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        params = [min_confidence, *params, limit, offset]

        rows = await self._pool.fetch(_qm(sql), *params)
        return [_row_to_dict(r) for r in rows]

    async def get_archive_groups(
        self,
        camera: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one row per distinct ZIP archive: path, clip count, total
        size, and the most recent clip timestamp in it (for sort order).

        The Storage tab's Archived Clips list is grouped by archive rather
        than shown as a flat per-clip list — a large library can have
        thousands of archived clips but only a few dozen distinct monthly
        ZIPs, so this is cheap to fetch in full (no pagination needed here;
        the frontend paginates over this already-small result) and gives an
        accurate page count, unlike paginating the flat clip list itself.
        *since*/*until* filter individual clips before grouping. This keeps
        archive counts, sizes, and expanded archive pages consistent when a
        monthly ZIP contains clips both inside and outside the selected range.
        """
        if self._pool is None:
            return []

        where = ["archived = TRUE", "archive_path != ''"]
        params: list[Any] = []
        if camera and camera != "all":
            where.append(_WHERE_CAMERA)
            params.append(camera)

        if since:
            where.append(_WHERE_SINCE)
            params.append(since)
        if until:
            where.append(_WHERE_UNTIL)
            params.append(until)

        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    archive_path,
                    COUNT(*) AS clip_count,
                    COALESCE(SUM(size_bytes), 0) AS total_size,
                    MAX(timestamp) AS latest_timestamp
                FROM clips
                WHERE {" AND ".join(where)}
                GROUP BY archive_path
                ORDER BY latest_timestamp DESC
                """
            ),
            *params,
        )
        return [dict(r) for r in rows]

    async def get_archive_clips(
        self,
        archive_path: str,
        camera: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one page of clips from an archive and its total count."""
        if self._pool is None:
            return {"items": [], "total": 0}

        clips = await self.get_clips(
            ClipFilters(
                camera=camera,
                archived=True,
                archive_path=archive_path,
                since=since,
                until=until,
            ),
            sort="newest",
            limit=limit,
            offset=offset,
        )

        where = ["archived = TRUE", "archive_path = ?"]
        params: list[Any] = [archive_path]
        if camera and camera != "all":
            where.append(_WHERE_CAMERA)
            params.append(camera)
        if since:
            where.append(_WHERE_SINCE)
            params.append(since)
        if until:
            where.append(_WHERE_UNTIL)
            params.append(until)
        total = await self._pool.fetchval(
            _qm(f"SELECT COUNT(*) FROM clips WHERE {' AND '.join(where)}"),
            *params,
        )
        return {"items": clips, "total": int(total or 0)}

    async def get_clips_by_archive_path(
        self, archive_path: str
    ) -> list[dict[str, Any]]:
        """Return every clip record stored in one archive ZIP, unpaginated.

        Used when deleting an entire archive — the caller needs each
        clip's ``gdrive_file_id`` to also remove its Google Drive backup,
        which the paginated/filtered :meth:`get_archive_clips` (built for
        the Storage tab's display) isn't a good fit for.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm("SELECT * FROM clips WHERE archived=TRUE AND archive_path=?"),
            archive_path,
        )
        return [_row_to_dict(r) for r in rows]

    async def get_all_file_paths(self) -> set[str]:
        """Return the set of all ``file_path`` values currently indexed."""
        if self._pool is None:
            return set()
        rows = await self._pool.fetch("SELECT file_path FROM clips")
        return {r["file_path"] for r in rows}

    async def get_clips_to_archive(self, older_than_days: int) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat()
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM clips WHERE archived=FALSE AND timestamp < ? "
                "ORDER BY timestamp"
            ),
            cutoff,
        )
        return [_row_to_dict(r) for r in rows]

    async def get_archived_clip_records(self) -> list[dict[str, Any]]:
        """Return ``{id, archive_path, camera, file_path}`` for every clip
        marked archived.

        Used by :meth:`ClipArchiver.prune_orphaned_archives` to find rows
        whose ZIP no longer exists on disk, is unreadable, or (for clips
        archived before a corrupted-ZIP bug was fixed) no longer actually
        contains that clip's member even though the ZIP file itself is
        still present — ``camera``/``file_path`` are what's needed to
        reconstruct the exact archive member name (``_archive_month``'s
        ``f"{camera}/{Path(file_path).name}"``) to check for. ``file_path``
        still holds the pre-archive path even after the source file is
        deleted — ``mark_archived`` never touches that column.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT id, archive_path, camera, file_path FROM clips "
                "WHERE archived=TRUE"
            )
        )
        return [_row_to_dict(r) for r in rows]

    async def get_clips_pending_gdrive_backup(
        self, include_unarchived: bool
    ) -> list[dict[str, Any]]:
        """Clips not yet backed up to Google Drive, for the "Back Up Existing
        Clips Now" action. Always includes archived clips (both backup
        policies cover them); *include_unarchived* additionally includes
        regular clips too, matching the "all_clips" policy.
        """
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                "SELECT * FROM clips WHERE gdrive_backed_up=FALSE "
                "AND (archived=TRUE OR ?=TRUE) ORDER BY timestamp"
            ),
            include_unarchived,
        )
        return [_row_to_dict(r) for r in rows]

    async def get_stats(self) -> dict[str, Any]:
        """Return aggregate statistics for the library."""
        if self._pool is None:
            return {}

        today_start, today_end = _local_day_bounds(0)
        yesterday_start, yesterday_end = _local_day_bounds(1)
        week_start, _week_end = _local_day_bounds(7)

        queries: dict[str, tuple[str, tuple[Any, ...]]] = {
            "total_count": ("SELECT COUNT(*) FROM clips WHERE archived=FALSE", ()),
            "starred_count": ("SELECT COUNT(*) FROM clips WHERE starred=TRUE", ()),
            "archived_count": ("SELECT COUNT(*) FROM clips WHERE archived=TRUE", ()),
            "total_size_bytes": (
                "SELECT COALESCE(SUM(size_bytes),0) FROM clips",
                (),
            ),
            "today_count": (
                "SELECT COUNT(*) FROM clips WHERE timestamp >= ? AND timestamp < ?",
                (today_start, today_end),
            ),
            "yesterday_count": (
                "SELECT COUNT(*) FROM clips WHERE timestamp >= ? AND timestamp < ?",
                (yesterday_start, yesterday_end),
            ),
            "week_count": (
                "SELECT COUNT(*) FROM clips WHERE timestamp >= ?",
                (week_start,),
            ),
            # Same "latest analysis row wins" + approved_faces_seen semantics
            # as get_clips()'s face_recognized column (see its docstring) —
            # a clip that no longer matches after a later re-analysis isn't
            # counted here either.
            "recognized_count": (
                (
                    "SELECT COUNT(*) FROM clips WHERE archived=FALSE AND EXISTS ("
                    "SELECT 1 FROM analysis_results ar WHERE ar.clip_id = clips.id "
                    "AND ar.approved_faces_seen "
                    "AND ar.analyzed_at = (SELECT MAX(ar2.analyzed_at) "
                    "FROM analysis_results ar2 WHERE ar2.clip_id = clips.id))"
                ),
                (),
            ),
        }

        results: dict[str, Any] = {}
        for key, (sql, params) in queries.items():
            results[key] = await self._pool.fetchval(_qm(sql), *params) or 0

        return results

    async def get_camera_stats(self) -> list[dict[str, Any]]:
        """Return per-camera clip counts, sizes, and activity."""
        if self._pool is None:
            return []

        today_start, today_end = _local_day_bounds(0)
        week_start, _week_end = _local_day_bounds(7)

        rows = await self._pool.fetch(
            _qm(
                """
                SELECT
                    MIN(camera) AS camera,
                    COUNT(*) AS total,
                    COALESCE(SUM(size_bytes), 0) AS size_bytes,
                    SUM(CASE WHEN timestamp >= ? AND timestamp < ? THEN 1 ELSE 0 END) AS today,
                    SUM(CASE WHEN timestamp >= ? THEN 1 ELSE 0 END) AS this_week,
                    MAX(timestamp) AS last_seen
                FROM clips
                WHERE archived=FALSE
                GROUP BY LOWER(camera)
                ORDER BY total DESC
                """
            ),
            today_start,
            today_end,
            week_start,
        )
        return [dict(r) for r in rows]

    async def get_distinct_tags(self) -> list[str]:
        """Return all unique tags used across clips (best-effort)."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            "SELECT DISTINCT tags FROM clips WHERE tags != '[]' AND tags != ''"
        )
        all_tags: set[str] = set()
        for r in rows:
            try:
                all_tags.update(json.loads(r["tags"] or "[]"))
            except json.JSONDecodeError:
                pass
        return sorted(all_tags)

    async def get_activity_data(self, days: int = 7) -> list[dict[str, Any]]:
        """Return per-hour clip counts for the last *days* days.

        Each row: ``{"date": "YYYY-MM-DD", "hour": 0-23, "count": n}``, both
        in the household's local calendar day/hour (see
        :func:`_local_utc_offset_sql`) rather than the UTC ones the
        pinned-UTC session (see ``init()``) would otherwise read back —
        without this, evenings in any timezone behind UTC show up as the
        next day. Useful for rendering an activity heat-map in the UI.
        """
        if self._pool is None:
            return []
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        tz = _local_utc_offset_sql()
        rows = await self._pool.fetch(
            _qm(
                f"""
                SELECT
                    to_char(timestamp::timestamptz AT TIME ZONE INTERVAL '{tz}', 'YYYY-MM-DD') AS date,
                    EXTRACT(HOUR FROM timestamp::timestamptz AT TIME ZONE INTERVAL '{tz}')::int AS hour,
                    COUNT(*) AS count
                FROM clips
                WHERE timestamp >= ?
                GROUP BY date, hour
                ORDER BY date, hour
                """
            ),
            cutoff,
        )
        return [dict(r) for r in rows]
