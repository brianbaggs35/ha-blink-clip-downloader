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

# ffprobe timing out or misbehaving on one oddball file must not stall the
# whole reconciliation scan — mirrors the timeout used for the equivalent
# ffmpeg thumbnail-generation subprocess call in downloader.py.
_PROBE_TIMEOUT = 15

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
        record["duration"] = await _probe_duration(file_path)
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


async def _probe_duration(video_path: Path) -> int:
    """Best-effort clip duration in whole seconds, via ffprobe.

    Unlike the normal download path (downloader.py reads `duration`
    straight off the Blink API's own clip metadata), a reconciled file has
    no API response to draw from — only the file itself. Duration is
    embedded in the file's own container metadata though, so it's
    genuinely recoverable here, unlike `source` (a Blink-side "how was
    this clip triggered" fact with no equivalent anywhere in the video
    file, which stays "" for reconciled clips). Returns 0 (matching the
    Library UI's existing "—" placeholder for a clip with no known
    duration) on any failure — a probe going wrong must never block
    reconciling the rest of the library.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        _LOGGER.warning("Could not run ffprobe on %s: %s", video_path, exc)
        return 0

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        _LOGGER.warning("ffprobe timed out probing duration for %s", video_path)
        proc.kill()
        await proc.wait()
        return 0

    try:
        return max(0, int(float(stdout.decode().strip())))
    except (ValueError, UnicodeDecodeError):
        _LOGGER.warning("ffprobe gave no usable duration for %s", video_path)
        return 0


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

    clip_id = "import-" + hashlib.sha1(str(file_path).encode()).hexdigest()[:16]

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
