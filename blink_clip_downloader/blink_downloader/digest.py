"""Daily activity digest sent via HA notification."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

from .database import ClipDatabase
from .notifier import HANotifier

_LOGGER = logging.getLogger(__name__)

_LAST_DIGEST_FILE = Path("/data/last_digest.json")


class DailyDigest:
    """Checks once per poll cycle whether it's time to send a daily summary."""

    def __init__(
        self,
        notifier: HANotifier,
        db: ClipDatabase,
        digest_time: str,
        enabled: bool,
        last_digest_file: Path = _LAST_DIGEST_FILE,
    ) -> None:
        self._notifier = notifier
        self._db = db
        self._digest_time = digest_time  # "HH:MM"
        self._enabled = enabled
        self._state_file = last_digest_file
        self._last_sent: date | None = self._load_last_sent()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def check_and_send(self) -> None:
        """Send the digest if it's due (called every poll cycle)."""
        if not self._enabled:
            return

        # Digest scheduling deliberately uses naive local wall-clock time —
        # digest_time is documented as local HH:MM, matching ai_schedule_*.
        today = date.today()  # noqa: DTZ011
        if self._last_sent == today:
            return

        try:
            h, m = (int(p) for p in self._digest_time.split(":")[:2])
        except (ValueError, AttributeError):
            _LOGGER.warning("Invalid digest_time %r; skipping", self._digest_time)
            return

        now = datetime.now()  # noqa: DTZ005
        if now.hour * 60 + now.minute < h * 60 + m:
            return  # Not yet time today.

        await self.send()
        self._last_sent = today
        self._save_last_sent()

    async def send(self) -> None:
        """Build and dispatch the daily digest notification."""
        stats = await self._db.get_stats()
        camera_stats = await self._db.get_camera_stats()

        today_count = stats.get("today_count", 0)
        total_count = stats.get("total_count", 0)
        total_mb = stats.get("total_size_bytes", 0) / 1024**2
        starred = stats.get("starred_count", 0)

        lines = [
            f"📅 {date.today().strftime('%A, %B %-d')}",  # noqa: DTZ011
            f"  New clips today   : {today_count}",
            f"  Library total     : {total_count} clips ({total_mb:.0f} MB)",
            f"  Starred           : {starred}",
        ]

        if camera_stats:
            lines.append("\nBy camera (top 5):")
            for cam in camera_stats[:5]:
                lines.append(
                    f"  • {cam['camera']}: {cam['today']} today, "
                    f"{cam['this_week']} this week"
                )

        await self._notifier.notify(
            "\n".join(lines),
            title="Blink Daily Digest",
        )
        _LOGGER.info("Daily digest sent (%d clips today)", today_count)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_last_sent(self) -> date | None:
        if not self._state_file.exists():
            return None
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return date.fromisoformat(data["last_sent"])
        except (OSError, KeyError, ValueError, TypeError) as exc:
            # json.JSONDecodeError is a ValueError subclass, already covered.
            _LOGGER.warning(
                "Digest state file %s is corrupt or unreadable, treating as "
                "never sent: %s",
                self._state_file,
                exc,
            )
            return None

    def _save_last_sent(self) -> None:
        """Save last digest send time to state file."""
        try:
            if self._last_sent is None:
                return
            # Write to a temp file and rename over the target so a crash
            # mid-write can't leave a truncated/corrupt state file behind —
            # os.replace() is atomic on the same filesystem. Mirrors
            # BlinkDownloader._persist_auth() / ClipTracker.save().
            tmp_path = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps({"last_sent": self._last_sent.isoformat()}))
            os.replace(tmp_path, self._state_file)
        except OSError as exc:
            _LOGGER.warning("Could not save digest state: %s", exc)
