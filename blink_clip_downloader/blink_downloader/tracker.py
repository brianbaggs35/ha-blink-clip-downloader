"""Persistent tracker that records which clip IDs have been downloaded."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DEFAULT_TRACKER_FILE = Path("/data/downloaded_clips.json")
# Keep memory bounded; Blink IDs are not reused so pruning old ones is safe.
_MAX_TRACKED_IDS = 100_000


class ClipTracker:
    """Stores downloaded clip IDs in a JSON file so restarts don't re-download."""

    def __init__(self, tracker_file: Path = DEFAULT_TRACKER_FILE) -> None:
        self._file = tracker_file
        # Insertion-ordered so _prune_if_needed() can actually drop the oldest
        # IDs first — a plain set()'s iteration order is a hash-table
        # artifact in CPython, not insertion order, so pruning "from the
        # front" of list(a_set) discards an arbitrary subset instead.
        self._downloaded: dict[str, None] = {}
        self._last_download_time: datetime | None = None
        self._stats: dict = {
            "total_downloaded": 0,
            "total_bytes": 0,
            "session_count": 0,
        }
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_downloaded(self, clip_id: str) -> bool:
        """Return True if *clip_id* has already been downloaded."""
        return clip_id in self._downloaded

    def mark_downloaded(self, clip_id: str, size_bytes: int = 0) -> None:
        """Record that *clip_id* was successfully downloaded."""
        self._downloaded[clip_id] = None
        self._last_download_time = datetime.now(timezone.utc)
        self._stats["total_downloaded"] += 1
        self._stats["total_bytes"] += size_bytes

    def increment_session_count(self) -> None:
        self._stats["session_count"] += 1

    def set_last_download_time(self, when: datetime) -> None:
        """Explicitly (re)set the polling cursor.

        Used by the downloader to hold the cursor back when a poll leaves
        backlog clips undownloaded (e.g. capped by ``max_clips_per_poll``),
        since ``mark_downloaded()`` would otherwise advance it to "now" and
        cause the Blink API's ``since`` filter to permanently skip them.
        """
        self._last_download_time = when

    def save(self) -> None:
        """Persist state to disk."""
        self._prune_if_needed()
        payload = {
            "downloaded_ids": list(self._downloaded),
            "last_download_time": (
                self._last_download_time.isoformat()
                if self._last_download_time
                else None
            ),
            "stats": self._stats,
        }
        # Write to a temp file and rename over the target so a crash mid-write
        # (or a concurrent read) never observes a truncated/corrupt JSON file
        # — os.replace() is atomic on the same filesystem.
        tmp_path = self._file.with_suffix(self._file.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self._file)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def last_download_time(self) -> datetime | None:
        return self._last_download_time

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._downloaded = dict.fromkeys(data.get("downloaded_ids", []))
            raw_time = data.get("last_download_time")
            if raw_time:
                self._last_download_time = datetime.fromisoformat(raw_time)
            self._stats = {**self._stats, **data.get("stats", {})}
        except (ValueError, KeyError) as exc:
            _LOGGER.warning(
                "Tracker file %s is corrupt, starting fresh: %s", self._file, exc
            )

    def _prune_if_needed(self) -> None:
        """Trim the oldest IDs when the tracked set grows too large."""
        if len(self._downloaded) > _MAX_TRACKED_IDS:
            excess = len(self._downloaded) - _MAX_TRACKED_IDS
            # dict preserves insertion order, so the first `excess` keys are
            # genuinely the oldest-recorded IDs (unlike a set(), whose
            # iteration order has no relationship to insertion order).
            for clip_id in list(self._downloaded)[:excess]:
                del self._downloaded[clip_id]
            _LOGGER.debug("Pruned %d old clip IDs from tracker", excess)
