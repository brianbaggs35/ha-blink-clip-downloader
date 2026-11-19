"""``loggable`` keeps outside text on the one log line it belongs to."""

from __future__ import annotations

import logging

import pytest

from blink_downloader.log_text import loggable


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Front Door", "Front Door"),
        ("a\nb", "a\\nb"),
        ("a\r\nb", "a\\r\\nb"),
        ("x\ry", "x\\ry"),
        (12, "12"),
        (ValueError("bad\nthing"), "bad\\nthing"),
    ],
)
def test_line_breaks_are_written_out(value: object, expected: str) -> None:
    assert loggable(value) == expected


def test_a_forged_line_cannot_be_started_through_a_camera_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    log = logging.getLogger("blink_downloader.test_log_text")
    with caplog.at_level("INFO", logger=log.name):
        log.info("Created %r", loggable("porch\nERROR forged entry"))
    assert len(caplog.text.strip().splitlines()) == 1
