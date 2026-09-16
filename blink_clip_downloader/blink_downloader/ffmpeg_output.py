"""Reading what ffmpeg wrote: its concatenated JPEG stdout, and its stderr.

Both functions here are pure and depend on nothing but the standard
library. That is the point of the module: ``analyzer.py`` and
``media_server.py`` each shell out to ffmpeg for frames and each need to
make sense of the result, but ``media_server.py`` importing ``analyzer.py``
for two small helpers would drag ``aiohttp`` and the whole ``security``
package in behind them. Both modules used to carry their own identical
copy to avoid exactly that; a leaf module gives them one implementation
without the heavy import, the same way ``frame_motion.py`` takes
``point_in_polygon`` from ``security/geometry.py`` rather than keeping a
second copy of it.
"""

from __future__ import annotations

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
