"""Text that came from outside the add-on, made safe to put in a log line.

A camera name, clip id or username arrives from a browser or from Blink, and a
line break inside one would let it write a second, forged-looking log line.
Stdlib only, like ``ffmpeg_output``, so any module can use it.
"""

from __future__ import annotations


def loggable(value: object) -> str:
    """Return ``str(value)`` with carriage returns and line feeds written out."""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")
