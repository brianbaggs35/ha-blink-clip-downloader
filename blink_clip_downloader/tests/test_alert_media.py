"""Tests for alert_media: choosing, extracting and storing an alert's picture."""

from __future__ import annotations

import asyncio
import io
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from blink_downloader import alert_media
from blink_downloader.alert_media import (
    AlertImageStore,
    busiest_frame,
    extract_frame_at,
    key_frame,
    subject_moment,
)
from blink_downloader.analyzer import AnalysisResult
from blink_downloader.vision import DetectedObject


def _result(objects: list[DetectedObject], interval: float = 2.0) -> AnalysisResult:
    return AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="m",
        response_text="",
        is_suspicious=True,
        confidence=0.9,
        summary="",
        frame_count=3,
        analysis_duration=1.0,
        analyzed_at="2026-09-25T04:00:00+00:00",
        detected_objects=objects,
        detection_interval=interval,
    )


def _obj(label: str, box: tuple[float, float, float, float], frame: int, conf=0.9):
    return DetectedObject(
        label=label, confidence=conf, box=box, track_id=None, frame_index=frame
    )


def _jpeg(shade: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(shade, shade, shade)).save(buf, "JPEG")
    return buf.getvalue()


# ------------------------------------------------------------------
# subject_moment
# ------------------------------------------------------------------


def test_no_moment_without_detections_to_go_on() -> None:
    assert subject_moment(_result([], interval=0.0)) is None
    assert subject_moment(_result([])) is None
    # Below the confidence floor, a box is as likely noise as a subject.
    assert subject_moment(_result([_obj("person", (0, 0, 50, 50), 3, 0.2)])) is None


def test_a_person_beats_a_bigger_vehicle() -> None:
    result = _result(
        [
            _obj("car", (0, 0, 400, 300), frame=1),
            _obj("person", (0, 0, 20, 40), frame=4),
        ]
    )
    assert subject_moment(result) == 8.0


def test_the_most_prominent_of_the_same_kind_wins() -> None:
    result = _result(
        [
            _obj("person", (0, 0, 10, 20), frame=1),
            _obj("person", (0, 0, 100, 200), frame=5),
            _obj("person", (0, 0, 30, 60), frame=2),
        ],
        interval=1.5,
    )
    assert subject_moment(result) == 7.5


def test_a_vehicle_beats_anything_else_detection_found() -> None:
    result = _result(
        [
            _obj("dog", (0, 0, 500, 500), frame=2),
            _obj("truck", (0, 0, 50, 50), frame=6),
        ]
    )
    assert subject_moment(result) == 12.0


def test_an_other_label_still_beats_motion() -> None:
    assert subject_moment(_result([_obj("cat", (0, 0, 5, 5), frame=3)])) == 6.0


# ------------------------------------------------------------------
# busiest_frame
# ------------------------------------------------------------------


def test_a_single_frame_is_the_busiest() -> None:
    assert busiest_frame([b"only"]) == b"only"


def test_the_frame_after_the_largest_change_is_chosen() -> None:
    frames = [_jpeg(10), _jpeg(12), _jpeg(240), _jpeg(242)]
    assert busiest_frame(frames) == frames[2]


def test_undecodable_frames_fall_back_to_the_middle_one() -> None:
    assert busiest_frame([b"a", b"b", b"c"]) == b"b"


# ------------------------------------------------------------------
# extract_frame_at
# ------------------------------------------------------------------


def _proc(stdout: bytes = b"", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    proc.returncode = returncode
    proc.wait = AsyncMock()
    return proc


async def test_extract_frame_at_seeks_to_the_moment() -> None:
    proc = _proc(b"JPEG")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as run:
        assert await extract_frame_at("/c.mp4", 7.25, width=320) == b"JPEG"
    args = run.call_args.args
    assert args[args.index("-ss") + 1] == "7.250"
    assert args[args.index("-i") + 1] == "/c.mp4"
    assert "scale=320:-2" in args


async def test_extract_frame_at_never_seeks_before_the_start() -> None:
    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=_proc(b"J"))
    ) as run:
        await extract_frame_at("/c.mp4", -3)
    args = run.call_args.args
    assert args[args.index("-ss") + 1] == "0.000"


@pytest.mark.parametrize("proc", [_proc(b"", 0), _proc(b"x", 1)], ids=["empty", "fail"])
async def test_extract_frame_at_returns_none_when_ffmpeg_gives_nothing(
    proc: MagicMock,
) -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await extract_frame_at("/c.mp4", 1.0) is None


async def test_extract_frame_at_without_ffmpeg() -> None:
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("no"))):
        assert await extract_frame_at("/c.mp4", 1.0) is None


async def test_extract_frame_at_kills_a_hung_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _proc()

    async def hang() -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        raise AssertionError("cancelled by the timeout before this")

    proc.communicate = hang
    monkeypatch.setattr(alert_media, "_EXTRACT_TIMEOUT_SECONDS", 0.01)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        assert await extract_frame_at("/c.mp4", 1.0) is None
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


# ------------------------------------------------------------------
# key_frame
# ------------------------------------------------------------------


async def test_key_frame_uses_the_subjects_moment() -> None:
    result = _result([_obj("person", (0, 0, 10, 10), frame=2)])
    with (
        patch.object(alert_media, "extract_frame_at", AsyncMock(return_value=b"AT")),
        patch.object(alert_media, "extract_jpeg_frames", AsyncMock()) as sample,
    ):
        assert await key_frame("/c.mp4", result) == b"AT"
    sample.assert_not_awaited()


async def test_key_frame_falls_back_to_the_busiest_frame() -> None:
    result = _result([_obj("person", (0, 0, 10, 10), frame=40)])
    frames = [_jpeg(0), _jpeg(255)]
    with (
        patch.object(alert_media, "extract_frame_at", AsyncMock(return_value=None)),
        patch.object(
            alert_media, "extract_jpeg_frames", AsyncMock(return_value=frames)
        ) as sample,
    ):
        assert await key_frame("/c.mp4", result) == frames[1]
    assert sample.call_args.kwargs["interval"] == 1.0


async def test_key_frame_without_detection_or_frames_is_none() -> None:
    with patch.object(alert_media, "extract_jpeg_frames", AsyncMock(return_value=[])):
        assert await key_frame("/c.mp4", _result([], interval=0.0)) is None


# ------------------------------------------------------------------
# AlertImageStore
# ------------------------------------------------------------------


@pytest.fixture
def mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat any directory as a mount point, as /media is under Supervisor."""
    monkeypatch.setattr(alert_media.os.path, "ismount", lambda _p: True)


def test_nothing_is_stored_unless_the_media_folder_is_mounted(tmp_path: Path) -> None:
    store = AlertImageStore(tmp_path)
    assert store.available() is False
    assert store.save("c1", b"JPEG") is None
    assert not any(tmp_path.iterdir())


def test_the_default_root_is_home_assistants_media_folder() -> None:
    assert AlertImageStore()._root() == Path("/media")


@pytest.mark.usefixtures("mounted")
def test_a_picture_is_stored_where_the_companion_app_can_fetch_it(
    tmp_path: Path,
) -> None:
    store = AlertImageStore(tmp_path)

    url = store.save("local_1/x y", b"JPEG")

    assert url == "/media/local/blink_clip_downloader/alerts/local_1_x_y.jpg"
    stored = tmp_path / "blink_clip_downloader" / "alerts" / "local_1_x_y.jpg"
    assert stored.read_bytes() == b"JPEG"
    assert [p.name for p in stored.parent.iterdir()] == ["local_1_x_y.jpg"]


@pytest.mark.usefixtures("mounted")
def test_an_empty_clip_id_still_gets_a_file_name(tmp_path: Path) -> None:
    assert AlertImageStore(tmp_path).save("", b"J") == (
        "/media/local/blink_clip_downloader/alerts/clip.jpg"
    )


@pytest.mark.usefixtures("mounted")
def test_old_pictures_are_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "blink_clip_downloader" / "alerts"
    folder.mkdir(parents=True)
    week_old = folder / "old.jpg"
    week_old.write_bytes(b"x")
    stale = time.time() - 8 * 24 * 3600
    os.utime(week_old, (stale, stale))
    for i in range(3):
        recent = folder / f"recent{i}.jpg"
        recent.write_bytes(b"x")
        # Hours old, oldest first, so which one goes is not left to a tie.
        hours_ago = time.time() - (3 - i) * 3600
        os.utime(recent, (hours_ago, hours_ago))
    monkeypatch.setattr(alert_media, "_KEEP_MAX_FILES", 3)

    AlertImageStore(tmp_path).save("new", b"J")

    remaining = sorted(p.name for p in folder.iterdir())
    assert remaining == ["new.jpg", "recent1.jpg", "recent2.jpg"]


@pytest.mark.usefixtures("mounted")
def test_a_failed_write_stores_nothing(tmp_path: Path) -> None:
    (tmp_path / "blink_clip_downloader").write_text("a file where a folder goes")
    assert AlertImageStore(tmp_path).save("c1", b"J") is None


@pytest.mark.usefixtures("mounted")
def test_a_failed_prune_does_not_lose_the_picture(tmp_path: Path) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise OSError("read-only")

    with patch.object(Path, "glob", refuse):
        url = AlertImageStore(tmp_path).save("c1", b"J")
    assert url is not None
