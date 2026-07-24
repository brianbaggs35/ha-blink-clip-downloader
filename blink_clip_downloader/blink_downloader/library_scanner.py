"""Re-import pre-existing clip files into the library database.

The add-on's clip library database lives under ``/data``, which Home
Assistant wipes when the add-on is uninstalled. ``download_path`` (typically
under ``/share``) is not wiped, so a reinstall can leave behind ``.mp4``
files that are no longer tracked anywhere. On startup, scan for any such
files and add them to the database so they reappear in the web UI.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .database import ClipDatabase
from .downloader import probe_clip_duration

_LOGGER = logging.getLogger(__name__)

_TIMESTAMP_RE = re.compile(r"(\d{8}_\d{6})")
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ARCHIVE_DIR = "archives"


async def import_existing_clips(db: ClipDatabase, download_path: Path) -> int:
    """Add any untracked ``.mp4`` files under *download_path* to *db*.

    Returns the number of clips added.
    """
    if not download_path.exists():
        return 0

    known = await db.get_all_file_paths()
    added = 0

    # rglob()/stat() are blocking syscalls; a large library means an
    # unbounded number of them, so yield periodically to keep this from
    # starving the event loop (and anything else on it, like the media
    # server) for the whole scan.
    for i, file_path in enumerate(sorted(download_path.rglob("*.mp4"))):
        if i % 25 == 0:
            await asyncio.sleep(0)

        if not file_path.is_file():
            continue
        if _ARCHIVE_DIR in file_path.relative_to(download_path).parts[:-1]:
            continue

        path_str = str(file_path)
        if path_str in known:
            continue

        record = _build_clip_record(download_path, file_path)
        # A reconciled file has no API response to draw from — only the
        # file itself — so duration always comes from probe_clip_duration
        # (see downloader.py) here, unlike the normal download path where
        # it's only a fallback.
        record["duration"] = await probe_clip_duration(file_path)
        await db.add_clip(record)
        known.add(path_str)
        added += 1

    if added:
        _LOGGER.info(
            "Imported %d pre-existing clip(s) from %s into the library",
            added,
            download_path,
        )
    return added


def _build_clip_record(download_path: Path, file_path: Path) -> dict:
    """Best-effort reconstruction of clip metadata from a file's path."""
    rel_dirs = file_path.relative_to(download_path).parts[:-1]
    camera_dirs = [p for p in rel_dirs if not _DATE_DIR_RE.match(p)]
    if camera_dirs:
        camera = camera_dirs[0].replace("_", " ").strip() or "unknown"
    else:
        camera = _camera_from_filename(file_path.stem)

    timestamp = _timestamp_from_filename(file_path.stem)
    if timestamp is None:
        timestamp = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)

    try:
        size = file_path.stat().st_size
    except OSError:
        size = 0

    clip_id = (
        "import-"
        + hashlib.sha1(str(file_path).encode(), usedforsecurity=False).hexdigest()[:16]
    )

    return {
        "id": clip_id,
        "camera": camera,
        "path": str(file_path),
        "timestamp": timestamp.isoformat(),
        "size_bytes": size,
        "duration": 0,
        "source": "",
        "network_id": 0,
    }


def _timestamp_from_filename(stem: str) -> datetime | None:
    match = _TIMESTAMP_RE.search(stem)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _camera_from_filename(stem: str) -> str:
    """Strip a trailing ``_YYYYMMDD_HHMMSS`` timestamp to recover the camera name."""
    name = _TIMESTAMP_RE.sub("", stem).strip("_- ").replace("_", " ").strip()
    return name or "unknown"
