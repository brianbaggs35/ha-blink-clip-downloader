"""Persistent tracker that records which clip IDs have been downloaded."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DEFAULT_TRACKER_FILE = Path("/data/downloaded_clips.json")
# Keep memory bounded; Blink IDs are not reused so pruning old ones is safe.
_MAX_TRACKED_IDS = 100_000
# A clip that keeps failing to download is given up on only once it has
# failed in this many separate polls *and* its first failure is at least
# this old. Both, because either alone gives up too soon in a common case:
# fast polling runs every 15 seconds, so five polls can pass in a minute,
# and an add-on that was stopped for a day would otherwise give up on the
# first attempt after it restarts. Until then the clip holds the clip-list
# cursor back (BlinkDownloader.download_new_clips), so this is also the
# bound on how far back one clip that will never download (deleted from
# Blink, or broken media) can pin that cursor.
GIVE_UP_AFTER_ATTEMPTS = 5
GIVE_UP_AFTER = timedelta(hours=6)
# How long after a failure the clip is next tried: this base, doubling with
# each failure, up to the cap. A clip that will never download would
# otherwise be retried on every poll -- every 15 seconds while fast polling
# -- and the downloader waits for every download in a poll before handing
# the new clips on, so each retry would delay the alerts for fresh ones.
RETRY_BACKOFF_BASE = timedelta(minutes=1)
RETRY_BACKOFF_MAX = timedelta(hours=1)
# Failure records are dropped this long after the first failure, given up
# or not, so the file cannot grow without bound. It is also the window the
# Status tab's "given up" count covers.
FAILURE_RECORD_TTL = timedelta(days=7)


@dataclass
class FailedDownload:
    """One clip's run of failed download attempts, one per poll."""

    attempts: int
    first_failed: datetime
    last_failed: datetime
    given_up: bool = False

    @property
    def next_try(self) -> datetime:
        """When the clip is next due to be tried (see RETRY_BACKOFF_BASE)."""
        delay = RETRY_BACKOFF_BASE * 2 ** max(self.attempts - 1, 0)
        return self.last_failed + min(delay, RETRY_BACKOFF_MAX)


class ClipTracker:
    """Stores downloaded clip IDs in a JSON file so restarts don't re-download.

    Also holds the clip-list cursor (the ``since`` of the next request for
    Blink's clip list) and the clips that failed to download, which the
    downloader keeps asking for until they succeed or are given up on.
    """

    def __init__(self, tracker_file: Path | None = None) -> None:
        # None looks the module default up at call time, so tests can redirect it.
        self._file = tracker_file or DEFAULT_TRACKER_FILE
        # Insertion-ordered so _prune_if_needed() can actually drop the oldest
        # IDs first — a plain set()'s iteration order is a hash-table
        # artifact in CPython, not insertion order, so pruning "from the
        # front" of list(a_set) discards an arbitrary subset instead.
        self._downloaded: dict[str, None] = {}
        self._list_cursor: datetime | None = None
        self._failures: dict[str, FailedDownload] = {}
        self._stats: dict = {
            "total_downloaded": 0,
            "total_bytes": 0,
            "session_count": 0,
        }
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def has_history(self) -> bool:
        """True when a tracker file from an earlier run exists on disk."""
        return self._file.exists()

    def is_downloaded(self, clip_id: str) -> bool:
        """Return True if *clip_id* has already been downloaded."""
        return clip_id in self._downloaded

    def mark_downloaded(self, clip_id: str, size_bytes: int = 0) -> None:
        """Record that *clip_id* was successfully downloaded.

        Deliberately leaves the clip-list cursor alone. This used to move it
        to "now", so one successful download (or a Sync Module local-storage
        clip, which never comes from the clip list at all) carried the
        cursor past every clip in the same poll that had failed, been held
        back by ``max_clips_per_poll``, or been listed by Blink while the
        downloads were running, and none of them were ever requested again.
        """
        self._downloaded[clip_id] = None
        self._failures.pop(clip_id, None)
        self._stats["total_downloaded"] += 1
        self._stats["total_bytes"] += size_bytes

    def increment_session_count(self) -> None:
        self._stats["session_count"] += 1

    def set_list_cursor(self, when: datetime) -> None:
        """Set the ``since`` the next request for Blink's clip list uses.

        Only ``BlinkDownloader.download_new_clips`` moves it, once per poll,
        having worked out which clips are still owed a download.
        """
        self._list_cursor = when

    def record_failed_attempt(self, clip_id: str) -> FailedDownload:
        """Count one more poll in which *clip_id* failed to download.

        Returns the clip's record; its ``given_up`` is True once the clip has
        failed in ``GIVE_UP_AFTER_ATTEMPTS`` polls with the first failure at
        least ``GIVE_UP_AFTER`` ago, after which the downloader stops asking
        for it.
        """
        now = datetime.now(UTC)
        record = self._failures.setdefault(
            clip_id, FailedDownload(attempts=0, first_failed=now, last_failed=now)
        )
        record.attempts += 1
        record.last_failed = now
        if (
            record.attempts >= GIVE_UP_AFTER_ATTEMPTS
            and now - record.first_failed >= GIVE_UP_AFTER
        ):
            record.given_up = True
        return record

    def has_given_up(self, clip_id: str) -> bool:
        """Return True if *clip_id* failed for long enough to stop trying."""
        record = self._failures.get(clip_id)
        return record is not None and record.given_up

    def is_retrying(self, clip_id: str) -> bool:
        """Return True if *clip_id* has failed and is still being retried."""
        record = self._failures.get(clip_id)
        return record is not None and not record.given_up

    def retry_due(self, clip_id: str) -> bool:
        """Return True unless *clip_id* failed too recently to try again."""
        record = self._failures.get(clip_id)
        return record is None or datetime.now(UTC) >= record.next_try

    def save(self) -> None:
        """Persist state to disk."""
        self._prune_if_needed()
        self._prune_failures()
        payload = {
            "downloaded_ids": list(self._downloaded),
            # Keeps the key it had when this was the time of the last
            # download, so the cursor survives an upgrade or a downgrade.
            "last_download_time": (
                self._list_cursor.isoformat() if self._list_cursor else None
            ),
            "failed_downloads": {
                clip_id: {
                    "attempts": record.attempts,
                    "first_failed": record.first_failed.isoformat(),
                    "last_failed": record.last_failed.isoformat(),
                    "given_up": record.given_up,
                }
                for clip_id, record in self._failures.items()
            },
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
    def list_cursor(self) -> datetime | None:
        return self._list_cursor

    @property
    def given_up_count(self) -> int:
        """Clips given up on within the last ``FAILURE_RECORD_TTL``.

        Filters by age itself rather than relying on the pruning in save(),
        which runs only after a poll that downloaded something.
        """
        cutoff = datetime.now(UTC) - FAILURE_RECORD_TTL
        return sum(
            1
            for record in self._failures.values()
            if record.given_up and record.first_failed >= cutoff
        )

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
                self._list_cursor = as_utc(datetime.fromisoformat(raw_time))
            self._stats = {**self._stats, **data.get("stats", {})}
        except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            _LOGGER.warning(
                "Tracker file %s is corrupt or unreadable, starting fresh: %s",
                self._file,
                exc,
            )
            return
        self._load_failures(data.get("failed_downloads"))

    def _load_failures(self, raw: object) -> None:
        """Read the failure records, skipping any that are malformed.

        Separate from the rest of ``_load`` so one bad record costs only
        itself, not every downloaded ID in the file.
        """
        if not isinstance(raw, dict):
            return
        for clip_id, entry in raw.items():
            try:
                first_failed = as_utc(datetime.fromisoformat(entry["first_failed"]))
                last_failed = entry.get("last_failed")
                self._failures[str(clip_id)] = FailedDownload(
                    attempts=int(entry["attempts"]),
                    first_failed=first_failed,
                    last_failed=(
                        as_utc(datetime.fromisoformat(last_failed))
                        if last_failed
                        else first_failed
                    ),
                    given_up=bool(entry.get("given_up", False)),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                _LOGGER.debug("Ignoring malformed failure record for %s", clip_id)

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

    def _prune_failures(self) -> None:
        """Drop failure records whose first failure is past the TTL."""
        cutoff = datetime.now(UTC) - FAILURE_RECORD_TTL
        self._failures = {
            clip_id: record
            for clip_id, record in self._failures.items()
            if record.first_failed >= cutoff
        }


def as_utc(when: datetime) -> datetime:
    """Treat a timestamp with no offset as UTC, so it compares with aware ones.

    Blink's timestamps are UTC, and so is everything this module writes.
    """
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)
