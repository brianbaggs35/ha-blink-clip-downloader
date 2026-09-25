"""Tests for rich_alerts: gathering an alert's picture, links, button and time."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from blink_downloader.analyzer import AnalysisResult
from blink_downloader.rich_alerts import (
    AlertExtras,
    RichAlertBuilder,
    format_local_time,
    parse_instant,
)


@pytest.fixture
def chicago(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run as the add-on does in a Home Assistant set to US Central time."""
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _result() -> AnalysisResult:
    return AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="m",
        response_text="",
        is_suspicious=True,
        confidence=0.9,
        summary="Person at the door",
        frame_count=3,
        analysis_duration=1.0,
        analyzed_at="2026-09-25T04:10:00+00:00",
    )


_CLIP = {
    "id": "c1",
    "camera": "Front Door",
    "path": "/share/c1.mp4",
    "timestamp": "2026-09-25T02:03:04+00:00",
}


def _builder(**overrides: Any) -> tuple[RichAlertBuilder, dict[str, Any]]:
    parts: dict[str, Any] = {
        "links": MagicMock(
            clip_path=AsyncMock(return_value="/app/s/clip/c1"),
            clip_url=AsyncMock(return_value="https://ha.example/app/s/clip/c1"),
            close=AsyncMock(),
        ),
        "image_store": MagicMock(save=MagicMock(return_value="/media/local/x/c1.jpg")),
        "signer": MagicMock(
            action_for=MagicMock(return_value="BLINK_NOT_A_THREAT_s_c1")
        ),
        "key_frame": AsyncMock(return_value=b"JPEG"),
    }
    parts.update(overrides)
    return RichAlertBuilder(**parts), parts


def test_parse_instant() -> None:
    assert parse_instant("2026-09-25T02:03:04+00:00") == datetime(
        2026, 9, 25, 2, 3, 4, tzinfo=UTC
    )
    # A naive time (a re-imported clip) is read as UTC, as Blink's are.
    assert parse_instant("2026-09-25T02:03:04") == datetime(
        2026, 9, 25, 2, 3, 4, tzinfo=UTC
    )
    assert parse_instant("") is None
    assert parse_instant("yesterday") is None
    assert parse_instant(None) is None  # type: ignore[arg-type]


@pytest.mark.usefixtures("chicago")
def test_format_local_time_uses_home_assistants_time_zone() -> None:
    instant = datetime(2026, 9, 25, 2, 3, 4, tzinfo=UTC)
    assert format_local_time(instant) == "Thu 24 Sep 2026, 21:03:04 CDT"


@pytest.mark.usefixtures("chicago")
async def test_everything_is_gathered_when_every_channel_wants_it() -> None:
    builder, parts = _builder()

    extras = await builder.build(
        _result(), _CLIP, attach_image=True, phone=True, external_link=True
    )

    assert extras == AlertExtras(
        clip_id="c1",
        recorded_local="Thu 24 Sep 2026, 21:03:04 CDT",
        recorded_iso="2026-09-25T02:03:04+00:00",
        image=b"JPEG",
        image_url="/media/local/x/c1.jpg",
        open_path="/app/s/clip/c1",
        open_url="https://ha.example/app/s/clip/c1",
        not_a_threat_action="BLINK_NOT_A_THREAT_s_c1",
    )
    parts["key_frame"].assert_awaited_once()
    assert parts["key_frame"].call_args.args[0] == "/share/c1.mp4"
    parts["image_store"].save.assert_called_once_with("c1", b"JPEG")


async def test_nothing_is_fetched_for_a_part_no_channel_will_use() -> None:
    builder, parts = _builder()

    extras = await builder.build(
        _result(), _CLIP, attach_image=False, phone=False, external_link=False
    )

    assert extras.image is None and extras.image_url is None
    assert extras.open_path is None and extras.open_url is None
    assert extras.not_a_threat_action is None
    parts["key_frame"].assert_not_awaited()
    parts["links"].clip_path.assert_not_awaited()
    parts["links"].clip_url.assert_not_awaited()


async def test_the_phone_alone_gets_a_stored_picture_but_no_attachment() -> None:
    builder, parts = _builder()

    extras = await builder.build(
        _result(), _CLIP, attach_image=False, phone=True, external_link=False
    )

    assert extras.image is None
    assert extras.image_url == "/media/local/x/c1.jpg"
    assert extras.open_url is None
    parts["links"].clip_url.assert_not_awaited()


async def test_email_or_discord_alone_gets_the_picture_and_absolute_link() -> None:
    builder, parts = _builder()

    extras = await builder.build(
        _result(), _CLIP, attach_image=True, phone=False, external_link=True
    )

    assert extras.image == b"JPEG"
    assert extras.image_url is None
    assert extras.open_path is None
    assert extras.not_a_threat_action is None
    parts["image_store"].save.assert_not_called()


async def test_images_can_be_turned_off_leaving_links_and_the_button() -> None:
    builder, parts = _builder(include_image=False)

    extras = await builder.build(
        _result(), _CLIP, attach_image=True, phone=True, external_link=True
    )

    assert extras.image is None and extras.image_url is None
    assert extras.open_url and extras.open_path and extras.not_a_threat_action
    parts["key_frame"].assert_not_awaited()


async def test_a_phone_with_no_image_store_gets_no_picture() -> None:
    builder, parts = _builder(image_store=None)

    extras = await builder.build(
        _result(), _CLIP, attach_image=False, phone=True, external_link=False
    )

    assert extras.image_url is None
    parts["key_frame"].assert_not_awaited()


async def test_each_part_that_fails_is_left_out_on_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    builder, _ = _builder(
        key_frame=AsyncMock(side_effect=RuntimeError("ffmpeg crashed")),
        links=MagicMock(
            clip_path=AsyncMock(side_effect=RuntimeError("supervisor")),
            clip_url=AsyncMock(return_value="https://ha.example/x"),
        ),
    )

    extras = await builder.build(
        _result(), _CLIP, attach_image=True, phone=True, external_link=True
    )

    assert extras.image is None
    assert extras.open_path is None
    assert extras.open_url == "https://ha.example/x"
    assert extras.not_a_threat_action == "BLINK_NOT_A_THREAT_s_c1"
    assert "Alert sent without the key frame" in caplog.text
    assert "Alert sent without the clip link" in caplog.text


async def test_a_picture_that_cannot_be_stored_still_attaches_elsewhere() -> None:
    builder, _ = _builder(
        image_store=MagicMock(save=MagicMock(side_effect=OSError("full")))
    )

    extras = await builder.build(
        _result(), _CLIP, attach_image=True, phone=True, external_link=False
    )

    assert extras.image == b"JPEG"
    assert extras.image_url is None


async def test_a_clip_with_no_file_or_time_gets_no_picture_or_time() -> None:
    builder, parts = _builder()

    extras = await builder.build(
        _result(), {"id": "c1"}, attach_image=True, phone=False, external_link=False
    )

    assert extras.image is None
    assert extras.recorded_local == "" and extras.recorded_iso == ""
    parts["key_frame"].assert_not_awaited()


async def test_the_result_names_the_clip_when_the_clip_record_does_not() -> None:
    builder, parts = _builder()

    extras = await builder.build(
        _result(), {}, attach_image=False, phone=True, external_link=False
    )

    assert extras.clip_id == "c1"
    parts["signer"].action_for.assert_called_once_with("c1")


async def test_no_links_or_button_without_a_clip_id() -> None:
    builder, parts = _builder()
    result = _result()
    result.clip_id = ""

    extras = await builder.build(
        result, {}, attach_image=False, phone=True, external_link=True
    )

    assert extras.open_path is None and extras.open_url is None
    assert extras.not_a_threat_action is None
    parts["links"].clip_path.assert_not_awaited()


async def test_without_links_or_signer_the_rest_still_builds() -> None:
    builder, _ = _builder(links=None, signer=None)

    extras = await builder.build(
        _result(), _CLIP, attach_image=True, phone=True, external_link=True
    )

    assert extras.image == b"JPEG"
    assert extras.open_path is None and extras.not_a_threat_action is None


async def test_close_closes_the_link_resolver() -> None:
    builder, parts = _builder()
    await builder.close()
    parts["links"].close.assert_awaited_once()
    await RichAlertBuilder().close()  # nothing to close: a no-op


def test_the_default_key_frame_is_alert_medias() -> None:
    from blink_downloader import alert_media

    assert RichAlertBuilder()._key_frame is alert_media.key_frame
