"""Tests for the shared ffmpeg-output helpers.

These used to live twice over — once against the analyzer's copy and
once against ``media_server/``'s identical one. One implementation now
means one set of tests.
"""

from __future__ import annotations

from blink_downloader.ffmpeg_output import format_ffmpeg_error, split_jpeg_frames

_FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xd9"
_TWO_JPEGS = _FAKE_JPEG + _FAKE_JPEG


# ------------------------------------------------------------------
# split_jpeg_frames
# ------------------------------------------------------------------


def test_split_jpeg_frames_single() -> None:
    assert len(split_jpeg_frames(_FAKE_JPEG)) == 1


def test_split_jpeg_frames_multiple() -> None:
    assert len(split_jpeg_frames(_TWO_JPEGS)) == 2


def test_split_jpeg_frames_empty() -> None:
    assert split_jpeg_frames(b"") == []


def test_split_jpeg_frames_garbage() -> None:
    assert split_jpeg_frames(b"\x00\x01\x02") == []


def test_split_jpeg_frames_no_eoi() -> None:
    """SOI found but no EOI — should return no frames."""
    assert split_jpeg_frames(b"\xff\xd8\xff\xe0") == []


def test_split_jpeg_frames_ignores_truncated_trailing_data() -> None:
    """A complete frame followed by a partial one yields only the complete
    frame, never a truncated image the caller would have to detect."""
    assert split_jpeg_frames(_FAKE_JPEG + b"\xff\xd8\xff\xe0") == [_FAKE_JPEG]


# ------------------------------------------------------------------
# format_ffmpeg_error
# ------------------------------------------------------------------


def test_format_ffmpeg_error_handles_no_stderr() -> None:
    """A failing ffmpeg that wrote nothing to stderr must not blow up the
    log call it feeds."""
    assert format_ffmpeg_error(None) == ""
    assert format_ffmpeg_error(b"") == ""


def test_format_ffmpeg_error_collapses_newlines_into_one_record() -> None:
    assert format_ffmpeg_error(b"line one\nline two\n") == "line one line two"


def test_format_ffmpeg_error_keeps_the_tail_not_the_head() -> None:
    """ffmpeg's conclusive "Error ...: <reason>" line comes last, so
    truncating from the front is what would drop the answer."""
    noise = b"decode complaint. " * 40
    out = format_ffmpeg_error(noise + b"Error opening output files: Invalid argument")
    assert out.endswith("Error opening output files: Invalid argument")
    assert len(out) == 200
