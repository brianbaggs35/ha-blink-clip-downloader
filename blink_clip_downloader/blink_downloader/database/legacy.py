"""One-time import of a pre-5.0.0 SQLite library.

5.0.0 replaced SQLite with the bundled PostgreSQL server. This reads the
rows a previous version left behind and inserts them here, ignoring
conflicts so it is safe to run more than once. It is the only code in the
package that knows the old shape, which is why it is quarantined in its
own module rather than sitting among the live queries.
"""

from __future__ import annotations

import logging
from typing import Any

from .core import _DatabaseBase
from .sql import (
    _qm,
)

_LOGGER = logging.getLogger(__name__)


_LEGACY_CLIPS_INSERT = """
    INSERT INTO clips
      (id, camera, file_path, timestamp, size_bytes, duration, source,
       network_id, starred, tags, downloaded_at, archived, archive_path)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (id) DO NOTHING
"""

_LEGACY_ANALYSIS_RESULTS_INSERT = """
    INSERT INTO analysis_results
      (clip_id, camera, model, response_text, is_suspicious, confidence,
       summary, frame_count, analysis_duration, analyzed_at, tokens_prompt,
       tokens_completion, anomaly_score, escalation_model,
       escalation_tokens_prompt, escalation_tokens_completion,
       escalation_provider, prompt_text, face_bypass_applied,
       face_bypass_names, approved_faces_seen)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class LegacyImportMixin(_DatabaseBase):
    """Importing a pre-PostgreSQL SQLite library into this database."""

    async def import_legacy_sqlite_data(
        self,
        clips: list[tuple[Any, ...]],
        analysis_results: list[tuple[Any, ...]],
        ai_usage_reset_at: str | None,
    ) -> int:
        """Merge rows already read from a pre-5.0.0 SQLite clip library
        (see sqlite_migration.py, this method's only caller) into this
        database, in a single transaction.

        Deliberately a merge, not a plain "only if clips is empty" bulk
        insert: by the time this ships, anyone who already upgraded to
        5.0.0 already has a non-empty ``clips`` table, entirely populated
        by library_scanner.import_existing_clips()'s lossy filesystem
        reconstruction (synthetic "import-*" ids, ``source=''``, no
        starred/tags/archived, ``downloaded_at`` stamped at rescan time
        rather than the real download time — see its docstring). Gating on
        emptiness would make this a permanent no-op for exactly the
        installs it exists to fix. Instead, each old row is matched
        against ``clips`` by ``file_path`` (the one thing both a
        reconstructed row and the original row agree on, even though their
        ids differ): a match backfills that existing row in place —
        preserving its current id, and with it any real analysis already
        run against it since upgrading — with the old row's camera,
        source, timestamp, starred, tags, downloaded_at, archived, and
        archive_path (duration/size_bytes/network_id are left alone: the
        reconstruction's own fresh ffprobe/stat already gave those
        possibly-better values than a possibly-stale old one). No match
        means this clip was never reconstructed (e.g. its file no longer
        exists on disk) and is inserted fresh under its original id.

        The whole operation is one transaction so a failure partway
        through leaves the database exactly as it was — safe to just
        retry on the next restart — rather than needing per-row
        idempotency checks against a partially-completed previous attempt.

        *clips* rows must already be in ``(id, camera, file_path,
        timestamp, size_bytes, duration, source, network_id, starred,
        tags, downloaded_at, archived, archive_path)`` order. *analysis_results*
        rows must already be in ``(clip_id, camera, model, response_text,
        is_suspicious, confidence, summary, frame_count, analysis_duration,
        analyzed_at, tokens_prompt, tokens_completion, anomaly_score,
        escalation_model, escalation_tokens_prompt,
        escalation_tokens_completion, escalation_provider, prompt_text,
        face_bypass_applied, face_bypass_names, approved_faces_seen)``
        order, with ``clip_id`` remapped to whichever id the matching
        *clips* row above ended up with — any row whose ``clip_id`` isn't
        among *clips* at all is silently skipped (an orphaned row from the
        old file; SQLite's own FOREIGN KEY there was never enforced by
        default, so this can happen even though it shouldn't).

        Returns the number of clip rows processed, inserted or backfilled
        alike (0 if there was nothing to import).
        """
        if self._pool is None or not clips:
            return 0
        async with self._pool.acquire() as conn, conn.transaction():
            id_map: dict[Any, Any] = {}
            for row in clips:
                old_id, camera, file_path = row[0], row[1], row[2]
                existing_id = await conn.fetchval(
                    _qm("SELECT id FROM clips WHERE file_path = ?"), file_path
                )
                if existing_id is not None:
                    await conn.execute(
                        _qm(
                            """
                            UPDATE clips
                            SET camera=?, timestamp=?, source=?, starred=?,
                                tags=?, downloaded_at=?, archived=?,
                                archive_path=?
                            WHERE id=?
                            """
                        ),
                        camera,
                        row[3],  # timestamp
                        row[6],  # source
                        row[8],  # starred
                        row[9],  # tags
                        row[10],  # downloaded_at
                        row[11],  # archived
                        row[12],  # archive_path
                        existing_id,
                    )
                    id_map[old_id] = existing_id
                else:
                    await conn.execute(_qm(_LEGACY_CLIPS_INSERT), *row)
                    id_map[old_id] = old_id

            for row in analysis_results:
                resolved_id = id_map.get(row[0])
                if resolved_id is None:
                    continue
                await conn.execute(
                    _qm(_LEGACY_ANALYSIS_RESULTS_INSERT), resolved_id, *row[1:]
                )

            if ai_usage_reset_at:
                await conn.execute(
                    _qm(
                        "INSERT INTO ai_usage_reset (id, reset_at) "
                        "VALUES (1, ?) ON CONFLICT (id) DO NOTHING"
                    ),
                    ai_usage_reset_at,
                )
        return len(clips)
