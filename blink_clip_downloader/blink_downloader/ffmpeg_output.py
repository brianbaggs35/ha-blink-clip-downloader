"""Reading what ffmpeg wrote: its concatenated JPEG stdout, and its stderr.

Everything here depends on nothing but the standard library. That is the
point of the module: the ``analyzer`` package, ``vision/`` and
``media_server/`` each shell out to ffmpeg for frames and each need to
make sense of the result, but ``media_server/`` importing ``analyzer``
for a few small helpers would drag ``aiohttp`` and the whole ``security``
package in behind them. Both modules used to carry their own identical
copy to avoid exactly that; a leaf module gives them one implementation
without the heavy import, the same way ``frame_motion.py`` takes
``point_in_polygon`` from ``security/geometry.py`` rather than keeping a
second copy of it. :func:`extract_jpeg_frames` is the one helper that runs
ffmpeg itself: clip analysis extracts its frames through it, and so do the
two places that extract frames for faces, which is what keeps "the same
moments of the clip" true by construction rather than by two copies of one
command staying in step.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

#: Width, in pixels, of the frames clip analysis extracts — and so of the
#: frames face recognition matches against. The Biometrics tab's clip scan
#: extracts at the same width so an enrolled face is the size faces are when
#: they are later matched, which is why it lives here rather than in either.
ANALYSIS_FRAME_WIDTH = 640

# How much of a failing ffmpeg run's stderr to keep in its log line.
FFMPEG_ERROR_CHARS = 200


def format_ffmpeg_error(stderr: bytes | None) -> str:
    """Condense a failing ffmpeg run's stderr into one loggable line.

    Keeps the *tail* rather than the head: ffmpeg's conclusive
    "Error ...: <reason>" line comes last, after any per-frame decode
    complaints, so truncating from the front is what drops the answer.
    Newlines are collapsed so one failure stays one log record.
    """
    return " ".join((stderr or b"").decode(errors="replace").split())[
        -FFMPEG_ERROR_CHARS:
    ]


def split_jpeg_frames(data: bytes) -> list[bytes]:
    """Split concatenated JPEG data into individual frames.

    ffmpeg writing multiple JPEGs to one stdout stream gives no framing of
    its own, so frames are recovered by scanning for each image's start-of-
    image and end-of-image markers. Trailing bytes after the last complete
    frame are discarded rather than returned as a truncated image.
    """
    frames: list[bytes] = []
    soi = b"\xff\xd8"
    eoi = b"\xff\xd9"
    pos = 0
    while pos < len(data):
        start = data.find(soi, pos)
        if start == -1:
            break
        end = data.find(eoi, start + 2)
        if end == -1:
            break
        frames.append(data[start : end + 2])
        pos = end + 2
    return frames


async def extract_jpeg_frames(
    path: str, *, width: int, interval: float, count: int, label: str
) -> list[bytes]:
    """Up to *count* JPEG frames of *path*, *interval* seconds apart, scaled
    to *width* pixels wide.

    :meth:`BaseAnalyzer.extract_frames` extracts through this too, so
    asking for the analyzer's interval and frame count lands on the same
    moments of the clip. ``[]`` on any failure — ffmpeg missing,
    too slow, or exiting non-zero — each logged once as a warning naming
    *label*, with the tail of ffmpeg's stderr.
    """
    cmd = [
        "ffmpeg",
        # Without these, ffmpeg's multi-line banner is the first thing on
        # stderr and the truncated copy logged below is all banner.
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-vf",
        f"fps=1/{interval},scale={width}:-1",
        "-frames:v",
        str(count),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-q:v",
        "2",
        "pipe:1",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        _LOGGER.warning("ffmpeg not available: %s", exc)
        return []
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        _LOGGER.warning("ffmpeg timed out extracting frames for %s", label)
        # A timed-out communicate() leaves the child running; kill and reap it.
        proc.kill()
        await proc.wait()
        return []
    if proc.returncode != 0:
        _LOGGER.warning(
            "ffmpeg exited %d extracting frames for %s: %s",
            proc.returncode,
            label,
            format_ffmpeg_error(stderr),
        )
        return []
    return split_jpeg_frames(stdout or b"")
