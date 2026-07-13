"""Local file-system storage management."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_CLIP_GLOB = "*.mp4"
_THUMB_GLOB = "*.jpg"


class StorageManager:
    """Manages the download directory: path resolution, quotas, and retention."""

    def __init__(
        self,
        base_path: Path,
        max_storage_gb: float,
        retention_days: int,
        organize_by_camera: bool,
        organize_by_date: bool,
        filename_format: str,
    ) -> None:
        self._base = base_path
        # 0 means unlimited
        self._max_bytes = int(max_storage_gb * 1024**3) if max_storage_gb > 0 else 0
        self._retention_days = retention_days
        self._organize_by_camera = organize_by_camera
        self._organize_by_date = organize_by_date
        self._filename_format = filename_format

    # ------------------------------------------------------------------
    # Directory management
    # ------------------------------------------------------------------

    def ensure_directory(self) -> None:
        """Create the base download directory if it doesn't exist."""
        self._base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve_path(
        self,
        camera_name: str,
        timestamp: datetime,
        clip_id: str,
        extension: str = ".mp4",
    ) -> Path:
        """Return the destination :class:`Path` for a given clip."""
        safe_cam = _safe_name(camera_name)
        safe_id = _safe_name(clip_id)
        date_str = timestamp.strftime("%Y-%m-%d")
        time_str = timestamp.strftime("%H%M%S")
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")

        filename = (
            self._filename_format.format(
                camera=safe_cam,
                timestamp=ts_str,
                date=date_str,
                time=time_str,
                id=safe_id,
            )
            + extension
        )

        parts: list[Path | str] = [self._base]
        if self._organize_by_camera:
            parts.append(safe_cam)
        if self._organize_by_date:
            parts.append(date_str)
        parts.append(filename)

        return Path(*parts)

    # ------------------------------------------------------------------
    # Quota
    # ------------------------------------------------------------------

    def used_bytes(self) -> int:
        """Return total bytes consumed under the base directory."""
        total = 0
        if not self._base.exists():
            return 0
        for path in self._base.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def is_over_quota(self) -> bool:
        """Return True if the quota is set and has been exceeded."""
        if self._max_bytes == 0:
            return False
        return self.used_bytes() >= self._max_bytes

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def apply_retention_policy_paths(self) -> list[Path]:
        """Delete clips (and thumbnails) older than *retention_days*.

        Returns the deleted clip (``.mp4``) paths so the caller can remove
        the matching rows from the ClipDatabase — this class has no DB
        reference of its own, so callers that run with ``enable_library_db``
        must reconcile the two themselves or the DB accumulates orphaned
        rows for files that no longer exist on disk.
        """
        _, deleted_clips = self._apply_retention_policy()
        return deleted_clips

    def _apply_retention_policy(self) -> tuple[int, list[Path]]:
        if self._retention_days == 0 or not self._base.exists():
            return 0, []

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        cutoff_ts = cutoff.timestamp()
        deleted_clips: list[Path] = []
        deleted_count = 0

        for pattern in (_CLIP_GLOB, _THUMB_GLOB):
            for f in self._base.rglob(pattern):
                if not self._delete_if_expired(f, cutoff_ts):
                    continue
                deleted_count += 1
                if pattern == _CLIP_GLOB:
                    deleted_clips.append(f)

        _cleanup_empty_dirs(self._base)

        if deleted_count:
            _LOGGER.info("Retention policy removed %d file(s)", deleted_count)
        return deleted_count, deleted_clips

    @staticmethod
    def _delete_if_expired(f: Path, cutoff_ts: float) -> bool:
        """Delete *f* if its mtime predates *cutoff_ts*; return whether it was deleted."""
        try:
            if f.stat().st_mtime < cutoff_ts:
                f.unlink()
                return True
        except OSError as exc:
            _LOGGER.warning("Could not delete %s: %s", f, exc)
        return False

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def disk_stats(self) -> dict:
        """Return a dict with storage metrics for use in HA sensors."""
        used = self.used_bytes()
        try:
            total_disk, _, free_disk = shutil.disk_usage(str(self._base.parent))
        except OSError:
            total_disk = free_disk = 0

        return {
            "used_bytes": used,
            "used_mb": round(used / 1024**2, 1),
            "free_bytes": free_disk,
            "free_gb": round(free_disk / 1024**3, 2),
            "total_bytes": total_disk,
            "total_gb": round(total_disk / 1024**3, 2),
            "quota_bytes": self._max_bytes,
            "quota_gb": round(self._max_bytes / 1024**3, 2) if self._max_bytes else 0,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """Convert *name* to a filesystem-safe string (spaces become underscores).

    A result made up entirely of dots (e.g. ``"."`` or ``".."``) is rejected
    and replaced with ``"unknown"`` — sanitization otherwise leaves those
    characters untouched, and passing ``".."`` through as a path component
    (see :meth:`StorageManager.resolve_path`) would let a malicious or
    malformed camera/clip name escape the configured base directory.
    """
    result = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name).strip(
        "_"
    )
    if not result or set(result) <= {"."}:
        return "unknown"
    return result


def _cleanup_empty_dirs(root: Path) -> None:
    """Remove empty sub-directories left behind after retention deletes."""
    for dirpath in sorted(root.rglob("*"), reverse=True):
        if dirpath.is_dir() and dirpath != root:
            try:
                dirpath.rmdir()
            except OSError:
                pass
