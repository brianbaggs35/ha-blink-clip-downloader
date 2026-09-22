"""Tests for the shared ffmpeg-output helpers.

These used to live twice over — once against the analyzer's copy and
once against ``media_server/``'s identical one. One implementation now
means one set of tests.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.ffmpeg_output import (
    extract_jpeg_frames,
    format_ffmpeg_error,
    split_jpeg_frames,
)

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


# ------------------------------------------------------------------
# extract_jpeg_frames
# ------------------------------------------------------------------


def _proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    return proc


async def _extract(**overrides) -> list[bytes]:
    kwargs = {"width": 960, "interval": 0.75, "count": 12, "label": "clip-1"}
    return await extract_jpeg_frames("/clips/a.mp4", **(kwargs | overrides))


async def test_extract_jpeg_frames_samples_at_the_requested_width_and_rate() -> None:
    with patch(
        "asyncio.create_subprocess_exec", return_value=_proc(_TWO_JPEGS)
    ) as exec_:
        frames = await _extract()
    assert frames == [_FAKE_JPEG, _FAKE_JPEG]
    cmd = list(exec_.call_args.args)
    assert cmd[cmd.index("-i") + 1] == "/clips/a.mp4"
    assert cmd[cmd.index("-vf") + 1] == "fps=1/0.75,scale=960:-1"
    assert cmd[cmd.index("-frames:v") + 1] == "12"
    assert "-hide_banner" in cmd
    assert cmd[cmd.index("-loglevel") + 1] == "error"


async def test_extract_jpeg_frames_without_ffmpeg(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("missing")):
        assert await _extract() == []
    assert "ffmpeg not available" in caplog.text


async def test_extract_jpeg_frames_kills_a_hung_ffmpeg() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await _extract() == []
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


async def test_extract_jpeg_frames_logs_the_reason_ffmpeg_gave(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING),
        patch(
            "asyncio.create_subprocess_exec",
            return_value=_proc(stderr=b"noise\nInvalid data found\n", returncode=1),
        ),
    ):
        assert await _extract() == []
    assert "clip-1" in caplog.text
    assert "Invalid data found" in caplog.text
