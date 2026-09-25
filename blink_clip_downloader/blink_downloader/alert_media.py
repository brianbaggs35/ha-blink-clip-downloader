"""The picture an alert carries: choosing it, extracting it, and storing it.

An alert is judged from a lock screen in a couple of seconds, so its
picture should show whoever or whatever the clip is about — not the first
frame, which is the scene an instant after motion started and very often
still empty. When object detection ran, the frame with the most prominent
person (then vehicle, then anything it found) is used; otherwise the frame
with the most motion in it; the first frame is never preferred.

The companion app only accepts a picture as a URL it can fetch, and a
Home-Assistant-relative ``/media/local/...`` URL is the one it fetches with
the phone's own Home Assistant sign-in. So for phone alerts the frame is
written into Home Assistant's media folder (the add-on maps ``media``), and
that is the only reason this module writes files at all. Discord and email
carry the picture itself and need no file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from . import frame_motion
from .ffmpeg_output import ANALYSIS_FRAME_WIDTH, extract_jpeg_frames

if TYPE_CHECKING:
    from .analyzer import AnalysisResult

_LOGGER = logging.getLogger(__name__)

# Labels ranked by how much an alert's picture should prefer them. A person
# is what most alerts are about; a vehicle next (someone arriving or leaving
# by car); anything else detection found beats a frame chosen by motion.
_SUBJECT_PRIORITY = {
    "person": 0,
    "car": 1,
    "truck": 1,
    "bus": 1,
    "motorcycle": 1,
    "bicycle": 1,
}
_OTHER_SUBJECT = 2
# Below this, a detection is as likely noise as a subject.
_MIN_SUBJECT_CONFIDENCE = 0.4
# Frames compared when no detection says where to look: one a second covers
# a typical Blink clip, and the busiest of them is almost always the subject.
_MOTION_SAMPLE_FRAMES = 10
_EXTRACT_TIMEOUT_SECONDS = 15

# Home Assistant's own media folder, as mapped into this container, and the
# URL prefix its default "local" media source serves that folder under.
_MEDIA_ROOT = Path("/media")
_ALERT_SUBDIR = Path("blink_clip_downloader") / "alerts"
_HA_MEDIA_URL = "/media/local"
# A picture outlives its alert by a week: long enough to scroll back through
# the phone's notification history, short enough never to pile up.
_KEEP_SECONDS = 7 * 24 * 3600
_KEEP_MAX_FILES = 500


def subject_moment(result: AnalysisResult) -> float | None:
    """Seconds into the clip at which its most prominent subject was seen.

    Ranked by label (see ``_SUBJECT_PRIORITY``), then by box area times
    confidence, so a person filling a quarter of the frame beats one far
    down the street. None without detections to go on.
    """
    interval = float(result.detection_interval or 0.0)
    if interval <= 0:
        return None
    best: tuple[int, float] | None = None
    best_index = 0
    for obj in result.detected_objects:
        if obj.confidence < _MIN_SUBJECT_CONFIDENCE:
            continue
        x1, y1, x2, y2 = obj.box
        area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        key = (_SUBJECT_PRIORITY.get(obj.label, _OTHER_SUBJECT), -area * obj.confidence)
        if best is None or key < best:
            best = key
            best_index = obj.frame_index
    if best is None:
        return None
    return best_index * interval


def busiest_frame(frames: list[bytes]) -> bytes:
    """The frame that differs most from the one before it.

    Falls back to the middle frame when the frames cannot be compared (an
    undecodable image), which is still a better guess than the first.
    """
    if len(frames) < 2:
        return frames[0]
    try:
        diffs = frame_motion.frame_motion_diffs(frames)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not compare alert frames: %s", exc)
        return frames[len(frames) // 2]
    peak = max(range(len(diffs)), key=diffs.__getitem__)
    return frames[peak + 1]


async def extract_frame_at(
    path: str, seconds: float, width: int = ANALYSIS_FRAME_WIDTH
) -> bytes | None:
    """One JPEG frame of *path* at *seconds*, scaled to *width*; None on failure."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        # Before -i: a fast keyframe seek, then an exact decode to the moment.
        "-ss",
        f"{max(0.0, seconds):.3f}",
        "-i",
        path,
        "-vf",
        f"scale={width}:-2",
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-q:v",
        "3",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        _LOGGER.warning("ffmpeg not available for the alert picture: %s", exc)
        return None
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_EXTRACT_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        _LOGGER.warning("ffmpeg timed out extracting the alert picture for %s", path)
        return None
    if proc.returncode != 0 or not stdout:
        # A moment past the end of the clip (a detection interval that ran
        # long) exits cleanly with no output; the caller falls back.
        return None
    return stdout


async def key_frame(clip_path: str, result: AnalysisResult) -> bytes | None:
    """The picture an alert about *clip_path* should carry, or None."""
    moment = subject_moment(result)
    if moment is not None:
        frame = await extract_frame_at(clip_path, moment)
        if frame:
            return frame
    frames = await extract_jpeg_frames(
        clip_path,
        width=ANALYSIS_FRAME_WIDTH,
        interval=1.0,
        count=_MOTION_SAMPLE_FRAMES,
        label=f"the alert picture for {result.clip_id}",
    )
    if not frames:
        return None
    return busiest_frame(frames)


def _safe_name(clip_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", clip_id)[:100] or "clip"


class AlertImageStore:
    """Keeps alert pictures where the companion app can fetch them.

    Stores nothing, and returns None, unless Home Assistant's media folder
    is really mounted here: without the mount a file written to ``/media``
    lands in the container's own filesystem, where Home Assistant cannot see
    it, and the phone would show a broken picture instead of none.
    """

    def __init__(self, media_root: Path | None = None) -> None:
        # None looks the module default up at call time, so tests can redirect it.
        self._media_root = media_root

    def _root(self) -> Path:
        return self._media_root if self._media_root is not None else _MEDIA_ROOT

    def available(self) -> bool:
        """True when Home Assistant's media folder is mounted into the add-on."""
        root = self._root()
        return root.is_dir() and os.path.ismount(root)

    def save(self, clip_id: str, image: bytes) -> str | None:
        """Write *image* for *clip_id*; return the URL the companion app fetches."""
        if not self.available():
            return None
        directory = self._root() / _ALERT_SUBDIR
        name = f"{_safe_name(clip_id)}.jpg"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            tmp = directory / f".{name}.tmp"
            tmp.write_bytes(image)
            tmp.replace(directory / name)
        except OSError as exc:
            _LOGGER.warning("Could not store the alert picture: %s", exc)
            return None
        self._prune(directory)
        return f"{_HA_MEDIA_URL}/{quote(_ALERT_SUBDIR.as_posix())}/{quote(name)}"

    @staticmethod
    def _prune(directory: Path) -> None:
        """Drop pictures older than a week, and all but the newest few hundred."""
        try:
            files = sorted(
                (p for p in directory.glob("*.jpg") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            cutoff = time.time() - _KEEP_SECONDS
            for index, path in enumerate(files):
                if index >= _KEEP_MAX_FILES or path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.debug("Could not prune old alert pictures: %s", exc)
