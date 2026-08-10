"""Tests for blink_downloader.downloader."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from blinkpy.auth import (
    BlinkTwoFARequiredError,
    LoginError,
    TokenRefreshFailed,
    UnauthorizedError,
)
from requests.structures import CaseInsensitiveDict

from blink_downloader.downloader import (
    AuthenticationError,
    BlinkDownloader,
    TwoFARequired,
    probe_clip_duration,
)
from blink_downloader.storage import StorageManager
from blink_downloader.tracker import ClipTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_utc_timezone():
    """time_window_start/end are documented as local HH:MM and _in_time_window
    converts the UTC clip timestamp to local time before comparing — pin the
    process timezone to UTC so those tests are deterministic regardless of
    the machine running them."""
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


@pytest.fixture
def storage(tmp_path):
    s = StorageManager(
        base_path=tmp_path / "clips",
        max_storage_gb=10,
        retention_days=30,
        organize_by_camera=True,
        organize_by_date=True,
        filename_format="{camera}_{timestamp}",
    )
    s.ensure_directory()
    return s


@pytest.fixture
def tracker(tmp_path):
    return ClipTracker(tmp_path / "tracker.json")


@pytest.fixture
def dl(base_config, storage, tracker):
    return BlinkDownloader(base_config, storage, tracker)


# ---------------------------------------------------------------------------
# _apply_filters
# ---------------------------------------------------------------------------


def test_filter_by_camera_whitelist(dl, sample_clip):
    dl._config.camera_filter = ["Front Door"]
    clips = [
        {**sample_clip, "id": 1, "device_name": "Front Door"},
        {**sample_clip, "id": 2, "device_name": "Backyard"},
    ]
    result = dl._apply_filters(clips)
    assert len(result) == 1
    assert result[0]["device_name"] == "Front Door"


def test_filter_camera_case_insensitive(dl, sample_clip):
    dl._config.camera_filter = ["front door"]
    clips = [{**sample_clip, "device_name": "Front Door"}]
    result = dl._apply_filters(clips)
    assert len(result) == 1


def test_no_camera_filter_keeps_all(dl, sample_clip):
    dl._config.camera_filter = []
    clips = [
        {**sample_clip, "id": 1, "device_name": "A"},
        {**sample_clip, "id": 2, "device_name": "B"},
    ]
    assert len(dl._apply_filters(clips)) == 2


def test_motion_only_filter(dl, sample_clip):
    dl._config.motion_only = True
    clips = [
        {**sample_clip, "id": 1, "source": "pir"},
        {**sample_clip, "id": 2, "source": "liveview"},
        {**sample_clip, "id": 3, "source": ""},
    ]
    result = dl._apply_filters(clips)
    assert len(result) == 1
    assert result[0]["id"] == 1


def test_motion_only_false_keeps_all(dl, sample_clip):
    dl._config.motion_only = False
    clips = [
        {**sample_clip, "id": 1, "source": "pir"},
        {**sample_clip, "id": 2, "source": "liveview"},
    ]
    assert len(dl._apply_filters(clips)) == 2


def test_time_window_filter_in_window(dl, sample_clip):
    dl._config.time_window_start = "08:00"
    dl._config.time_window_end = "20:00"
    # sample_clip created_at = 08:30 UTC → inside window
    clips = [sample_clip]
    assert len(dl._apply_filters(clips)) == 1


def test_time_window_filter_outside_window(dl, sample_clip):
    dl._config.time_window_start = "22:00"
    dl._config.time_window_end = "06:00"
    # sample_clip at 08:30 → outside 22:00-06:00 window
    clips = [sample_clip]
    assert len(dl._apply_filters(clips)) == 0


def test_time_window_overnight_window_keeps_night_clips(dl, sample_clip):
    dl._config.time_window_start = "22:00"
    dl._config.time_window_end = "06:00"
    # Overnight window wraps past midnight: clips at 23:00 and 02:00 fall
    # inside the 22:00-06:00 nighttime range and must be kept.
    late_night = {**sample_clip, "id": 2, "created_at": "2024-06-15T23:00:00+00:00"}
    early_morning = {**sample_clip, "id": 3, "created_at": "2024-06-15T02:00:00+00:00"}
    clips = [late_night, early_morning]
    assert len(dl._apply_filters(clips)) == 2


def test_time_window_overnight_window_boundaries_inclusive(dl, sample_clip):
    dl._config.time_window_start = "22:00"
    dl._config.time_window_end = "06:00"
    start_edge = {**sample_clip, "id": 2, "created_at": "2024-06-15T22:00:00+00:00"}
    end_edge = {**sample_clip, "id": 3, "created_at": "2024-06-15T06:00:00+00:00"}
    clips = [start_edge, end_edge]
    assert len(dl._apply_filters(clips)) == 2


def test_time_window_invalid_timestamp_keeps_clip(dl):
    dl._config.time_window_start = "08:00"
    dl._config.time_window_end = "20:00"
    clip = {"id": 1, "device_name": "Cam", "created_at": "not-a-date"}
    result = dl._apply_filters([clip])
    assert len(result) == 1


def test_time_window_start_only_keeps_clips_after_start(dl, sample_clip):
    """With only time_window_start configured (no end), clips at or after
    that time of day are kept regardless of how late they run."""
    dl._config.time_window_start = "08:00"
    dl._config.time_window_end = ""
    before = {**sample_clip, "id": 1, "created_at": "2024-06-15T05:00:00+00:00"}
    after = {**sample_clip, "id": 2, "created_at": "2024-06-15T23:00:00+00:00"}
    result = dl._apply_filters([before, after])
    assert [c["id"] for c in result] == [2]


def test_time_window_end_only_keeps_clips_before_end(dl, sample_clip):
    """With only time_window_end configured (no start), clips at or before
    that time of day are kept."""
    dl._config.time_window_start = ""
    dl._config.time_window_end = "10:00"
    before = {**sample_clip, "id": 1, "created_at": "2024-06-15T05:00:00+00:00"}
    after = {**sample_clip, "id": 2, "created_at": "2024-06-15T23:00:00+00:00"}
    result = dl._apply_filters([before, after])
    assert [c["id"] for c in result] == [1]


def test_min_clip_duration_filter(dl, sample_clip):
    dl._config.min_clip_duration = 10
    clips = [
        {**sample_clip, "id": 1, "duration": 5},
        {**sample_clip, "id": 2, "duration": 15},
    ]
    result = dl._apply_filters(clips)
    assert [c["id"] for c in result] == [2]


def test_in_time_window_returns_true_when_unconfigured(dl, sample_clip):
    """_in_time_window() itself (not just _apply_filters, which skips calling
    it when both bounds are empty) defaults to keeping the clip when neither
    bound is set."""
    dl._config.time_window_start = ""
    dl._config.time_window_end = ""
    assert dl._in_time_window(sample_clip) is True


# ---------------------------------------------------------------------------
# _resolve_url
# ---------------------------------------------------------------------------


def test_resolve_url_absolute_unchanged(dl):
    dl._blink = MagicMock()
    assert (
        dl._resolve_url("https://example.com/clip.mp4")
        == "https://example.com/clip.mp4"
    )


def test_resolve_url_relative_uses_base_url(dl):
    mock_blink = MagicMock()
    mock_blink.urls.base_url = "https://rest-prod.immedia-semi.com"
    dl._blink = mock_blink
    result = dl._resolve_url("/api/v1/clip.mp4")
    assert result == "https://rest-prod.immedia-semi.com/api/v1/clip.mp4"


def test_resolve_url_fallback_when_no_blink(dl):
    dl._blink = None
    result = dl._resolve_url("/clip.mp4")
    assert "immedia-semi.com" in result


def test_resolve_url_falls_back_when_urls_check_raises_attributeerror(dl):
    """getattr(obj, name, default) already swallows an AttributeError raised
    while resolving `urls`/`base_url` themselves — the outer except only
    guards a stranger case where checking truthiness of the resolved value
    itself raises (e.g. a broken __bool__). Contrived, but confirms the
    fallback URL still wins rather than propagating the error."""

    class _WeirdUrls:
        def __bool__(self):
            raise AttributeError("boom")

    class _WeirdBlink:
        urls = _WeirdUrls()

    dl._blink = _WeirdBlink()
    result = dl._resolve_url("/clip.mp4")
    assert "immedia-semi.com" in result


# ---------------------------------------------------------------------------
# _stream_to_file
# ---------------------------------------------------------------------------


async def test_stream_to_file_writes_content(dl, tmp_path):
    dest = tmp_path / "clip.mp4"
    content = b"fake video" * 1000

    async def _iter_chunks(chunk_size):
        yield content

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session

    mock_blink = MagicMock()
    mock_blink.auth.header = {"Authorization": "Bearer tok"}
    dl._blink = mock_blink

    size = await dl._stream_to_file("https://host/clip.mp4", dest)
    assert size == len(content)
    assert dest.read_bytes() == content


async def test_stream_to_file_dest_not_visible_until_write_completes(dl, tmp_path):
    """Regression test: dest must never be visible as a partial/truncated
    file mid-download — _download_clip's dest.exists() pre-check treats any
    file at dest as an already-completed download, so a process kill
    mid-stream (container restart, OOM-kill) must never leave a truncated
    file there. The stream writes to a .tmp file and only os.replace()s it
    over dest after every chunk has been written successfully."""
    dest = tmp_path / "clip.mp4"
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    content = b"fake video" * 1000
    dest_existed_mid_stream = False

    async def _iter_chunks(chunk_size):
        nonlocal dest_existed_mid_stream
        yield content[: len(content) // 2]
        # Halfway through streaming: only the .tmp file should exist so far.
        dest_existed_mid_stream = dest.exists()
        yield content[len(content) // 2 :]

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    size = await dl._stream_to_file("https://host/clip.mp4", dest)

    assert size == len(content)
    assert dest.read_bytes() == content
    assert dest_existed_mid_stream is False
    assert not tmp_dest.exists()


async def test_stream_to_file_non_200_returns_none(dl, tmp_path):
    dest = tmp_path / "clip.mp4"

    mock_resp = AsyncMock()
    mock_resp.status = 403
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    result = await dl._stream_to_file("https://host/clip.mp4", dest)
    assert result is None


async def test_stream_to_file_deletes_partial_on_failure(dl, tmp_path):
    import aiohttp as _aiohttp

    dest = tmp_path / "clip.mp4"
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    dl._config.retry_attempts = 1
    dl._config.retry_delay = 0.0

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("boom"))
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    # Create partial file (at the .tmp write target, not the final dest — see
    # _stream_attempt's atomic-rename-on-success design) to simulate an
    # incomplete prior download attempt.
    tmp_dest.write_bytes(b"partial")
    result = await dl._stream_to_file("https://host/clip.mp4", dest)
    assert result is None
    assert not tmp_dest.exists()
    assert not dest.exists()


async def test_stream_to_file_timeout_error_retries_and_cleans_up(dl, tmp_path):
    """asyncio.TimeoutError from a ClientTimeout(total=...) expiry is not an
    aiohttp.ClientError subclass, so it must be caught explicitly or a
    slow/hung server would skip retries and leave a partial file behind."""
    dest = tmp_path / "clip.mp4"
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    dl._config.retry_attempts = 2
    dl._config.retry_delay = 0.0

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=asyncio.TimeoutError)
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    tmp_dest.write_bytes(b"partial")
    result = await dl._stream_to_file("https://host/clip.mp4", dest)
    assert result is None
    assert not tmp_dest.exists()
    assert not dest.exists()
    assert mock_session.get.call_count == 2


async def test_stream_to_file_oserror_during_write_retries_and_cleans_up(dl, tmp_path):
    """An OSError (e.g. disk full) raised while writing chunks must be caught
    like a ClientError/TimeoutError — otherwise the partial file is left on
    disk and a later poll's `dest.exists()` pre-check in _download_clip
    would treat that truncated file as a completed download permanently."""
    dest = tmp_path / "clip.mp4"
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    dl._config.retry_attempts = 2
    dl._config.retry_delay = 0.0

    async def _iter_chunks(chunk_size):
        yield b"fake video"

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    mock_file = AsyncMock()
    mock_file.write = AsyncMock(side_effect=OSError("No space left on device"))
    mock_file.__aenter__ = AsyncMock(return_value=mock_file)
    mock_file.__aexit__ = AsyncMock(return_value=False)

    tmp_dest.write_bytes(b"partial")
    with patch("blink_downloader.downloader.aiofiles.open", return_value=mock_file):
        result = await dl._stream_to_file("https://host/clip.mp4", dest)

    assert result is None
    assert not tmp_dest.exists()
    assert not dest.exists()
    assert mock_session.get.call_count == 2


async def test_stream_to_file_non_200_retries_then_gives_up(dl, tmp_path):
    """A non-200 response gets the same retry treatment as a ClientError
    instead of giving up on the first attempt."""
    dest = tmp_path / "clip.mp4"
    dl._config.retry_attempts = 3
    dl._config.retry_delay = 0.0

    mock_resp = AsyncMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    result = await dl._stream_to_file("https://host/clip.mp4", dest)
    assert result is None
    assert mock_session.get.call_count == 3


async def test_stream_to_file_non_200_exhausted_cleans_up_stale_tmp_file(dl, tmp_path):
    """A non-200 response never touches tmp_dest itself (unlike a raised
    ClientError, which is cleaned up per-attempt) — so if a stale .tmp file
    from an earlier crashed run was already sitting there, only the final
    loop-exit cleanup removes it once every retry is exhausted."""
    dest = tmp_path / "clip.mp4"
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    dl._config.retry_attempts = 2
    dl._config.retry_delay = 0.0

    mock_resp = AsyncMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    tmp_dest.write_bytes(b"stale leftover from a crashed run")
    result = await dl._stream_to_file("https://host/clip.mp4", dest)

    assert result is None
    assert not tmp_dest.exists()


# ---------------------------------------------------------------------------
# _generate_thumbnail / backfill_thumbnails
# ---------------------------------------------------------------------------


async def test_generate_thumbnail_creates_jpg(dl, tmp_path):
    """ffmpeg exits 0 and writes the thumbnail file."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")
    thumb = tmp_path / "clip.jpg"

    mock_proc = AsyncMock()
    mock_proc.wait = AsyncMock(side_effect=lambda: thumb.write_bytes(b"\xff\xd8\xff"))

    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ) as mock_exec:
        created = await dl._generate_thumbnail(video, thumb)

    assert created is True
    assert thumb.exists()
    assert thumb.stat().st_size > 0

    args = mock_exec.call_args.args
    assert args[0] == "ffmpeg"
    assert str(video) in args
    assert str(thumb) in args


async def test_generate_thumbnail_skips_if_thumb_exists(dl, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")
    thumb = tmp_path / "clip.jpg"
    thumb.write_bytes(b"existing thumbnail")

    created = await dl._generate_thumbnail(video, thumb)

    assert created is False
    assert thumb.read_bytes() == b"existing thumbnail"


async def test_generate_thumbnail_skips_if_video_missing(dl, tmp_path):
    video = tmp_path / "missing.mp4"
    thumb = tmp_path / "missing.jpg"

    created = await dl._generate_thumbnail(video, thumb)

    assert created is False
    assert not thumb.exists()


async def test_generate_thumbnail_handles_missing_ffmpeg(dl, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")
    thumb = tmp_path / "clip.jpg"

    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("ffmpeg not found")),
    ):
        created = await dl._generate_thumbnail(video, thumb)

    assert created is False
    assert not thumb.exists()


async def test_generate_thumbnail_handles_ffmpeg_producing_no_output(dl, tmp_path):
    """ffmpeg can exit 0 without writing a file (e.g. a corrupt video)."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not a real video")
    thumb = tmp_path / "clip.jpg"

    mock_proc = AsyncMock()
    mock_proc.wait = AsyncMock(return_value=0)

    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        created = await dl._generate_thumbnail(video, thumb)

    assert created is False
    assert not thumb.exists()


async def test_generate_thumbnail_kills_ffmpeg_process_on_timeout(dl, tmp_path):
    """A hung ffmpeg process must be killed/reaped, not awaited forever."""
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake video data")
    thumb = tmp_path / "clip.jpg"

    mock_proc = AsyncMock()
    # First call (inside wait_for) simulates the timeout expiring; the
    # second call is the kill-then-reap cleanup in the except branch.
    mock_proc.wait = AsyncMock(side_effect=[TimeoutError(), 0])
    mock_proc.kill = MagicMock()

    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        created = await dl._generate_thumbnail(video, thumb)

    assert created is False
    assert not thumb.exists()
    mock_proc.kill.assert_called_once()
    assert mock_proc.wait.await_count == 2


async def test_backfill_thumbnails_disabled_returns_zero(dl):
    dl._config.download_thumbnails = False
    dl._db = AsyncMock()

    assert await dl.backfill_thumbnails(10) == 0
    dl._db.get_all_file_paths.assert_not_called()


async def test_backfill_thumbnails_without_db_returns_zero(dl):
    dl._config.download_thumbnails = True
    dl._db = None

    assert await dl.backfill_thumbnails(10) == 0


async def test_backfill_thumbnails_generates_missing_only(dl, tmp_path):
    dl._config.download_thumbnails = True

    has_thumb = tmp_path / "has_thumb.mp4"
    has_thumb.write_bytes(b"video")
    has_thumb.with_suffix(".jpg").write_bytes(b"existing thumb")

    needs_thumb = tmp_path / "needs_thumb.mp4"
    needs_thumb.write_bytes(b"video")

    missing_video = tmp_path / "missing.mp4"

    dl._db = AsyncMock()
    dl._db.get_all_file_paths = AsyncMock(
        return_value={str(has_thumb), str(needs_thumb), str(missing_video)}
    )

    with patch.object(
        dl, "_generate_thumbnail", AsyncMock(return_value=True)
    ) as mock_gen:
        generated = await dl.backfill_thumbnails(10)

    assert generated == 1
    mock_gen.assert_awaited_once_with(needs_thumb, needs_thumb.with_suffix(".jpg"))


async def test_backfill_thumbnails_respects_limit(dl, tmp_path):
    dl._config.download_thumbnails = True

    videos = []
    for i in range(5):
        video = tmp_path / f"clip{i}.mp4"
        video.write_bytes(b"video")
        videos.append(video)

    dl._db = AsyncMock()
    dl._db.get_all_file_paths = AsyncMock(return_value={str(v) for v in videos})

    with patch.object(
        dl, "_generate_thumbnail", AsyncMock(return_value=True)
    ) as mock_gen:
        generated = await dl.backfill_thumbnails(2)

    assert generated == 2
    assert mock_gen.await_count == 2


# ---------------------------------------------------------------------------
# download_new_clips
# ---------------------------------------------------------------------------


async def test_download_new_clips_skips_already_tracked(dl, tracker, sample_clip):
    tracker.mark_downloaded(str(sample_clip["id"]))
    dl._tracker = tracker

    dl._blink = MagicMock()
    with patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=[sample_clip])):
        results = await dl.download_new_clips()

    assert results == []


async def test_download_new_clips_respects_max_clips(dl, sample_clip):
    dl._config.max_clips_per_poll = 2
    dl._blink = MagicMock()
    # 5 new clips, but limit is 2
    clips = [{**sample_clip, "id": i} for i in range(1, 6)]

    downloaded = []

    async def _fake_download(clip, sem):
        downloaded.append(clip["id"])
        return {"id": str(clip["id"]), "camera": "Cam", "path": "/x", "timestamp": "t"}

    with (
        patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=clips)),
        patch.object(dl, "_download_clip", side_effect=_fake_download),
    ):
        await dl.download_new_clips()

    assert len(downloaded) == 2


async def test_download_new_clips_uses_six_hour_lookback_on_fresh_tracker(
    dl, sample_clip
):
    """With no last_download_time (fresh tracker — first run, or reconnect
    after a reinstall), the Blink API since= filter must be a short 6-hour
    lookback, not a wider window that could pull in — and then, before the
    burst-analysis cap, would have auto-queued for AI analysis — a much
    larger backlog than a fresh reconnect should reasonably cost tokens on.
    """
    assert dl._tracker.last_download_time is None
    dl._blink = MagicMock()

    with patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=[])) as mock_fetch:
        await dl.download_new_clips()

    since_arg = mock_fetch.call_args.args[0]
    expected = datetime.now(UTC) - timedelta(hours=6)
    # Generous tolerance for test execution time, not for the window itself.
    assert abs((since_arg - expected).total_seconds()) < 5
    assert since_arg > datetime.now(UTC) - timedelta(hours=7)


async def test_download_new_clips_holds_cursor_when_backlog_remains(dl, sample_clip):
    """When max_clips_per_poll truncates the backlog, the tracker cursor must
    not advance past the clips left undownloaded, or the Blink API's since=
    filter would permanently skip them on the next poll."""
    dl._config.max_clips_per_poll = 2
    dl._blink = MagicMock()
    clips = [{**sample_clip, "id": i} for i in range(1, 6)]  # 5 new clips, limit 2

    original_since = datetime(2024, 1, 1, tzinfo=UTC)
    dl._tracker.set_last_download_time(original_since)

    async def _fake_download(clip, sem):
        dl._tracker.mark_downloaded(str(clip["id"]), 100)
        return {"id": str(clip["id"]), "camera": "Cam", "path": "/x", "timestamp": "t"}

    with (
        patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=clips)),
        patch.object(dl, "_download_clip", side_effect=_fake_download),
    ):
        await dl.download_new_clips()

    # The cursor must be held back at the pre-poll `since`, not advanced to
    # "now" (which is what mark_downloaded() would otherwise leave behind).
    assert dl._tracker.last_download_time == original_since


async def test_download_new_clips_advances_cursor_when_backlog_cleared(dl, sample_clip):
    """When every fetched clip is downloaded (no backlog), the cursor is free
    to advance normally so the next poll's window moves forward."""
    dl._config.max_clips_per_poll = 10
    dl._blink = MagicMock()
    clips = [{**sample_clip, "id": i} for i in range(1, 4)]  # 3 new clips, limit 10

    async def _fake_download(clip, sem):
        dl._tracker.mark_downloaded(str(clip["id"]), 100)
        return {"id": str(clip["id"]), "camera": "Cam", "path": "/x", "timestamp": "t"}

    with (
        patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=clips)),
        patch.object(dl, "_download_clip", side_effect=_fake_download),
    ):
        await dl.download_new_clips()

    assert dl._tracker.last_download_time is not None


async def test_download_new_clips_no_clips(dl):
    dl._blink = MagicMock()
    with patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=[])):
        results = await dl.download_new_clips()
    assert results == []


async def test_download_new_clips_raises_without_connect(dl):
    """download_new_clips() before connect() must fail loudly, not silently
    return an empty list — callers rely on this to know setup was skipped."""
    dl._blink = None
    with pytest.raises(RuntimeError, match="connect"):
        await dl.download_new_clips()


async def test_download_new_clips_logs_and_skips_failed_clip(dl, sample_clip):
    """One clip's download raising must not abort the whole batch — the
    other clips in the same poll still get downloaded and returned."""
    dl._blink = MagicMock()
    clips = [{**sample_clip, "id": 1}, {**sample_clip, "id": 2}]

    async def _fake_download(clip, sem):
        if clip["id"] == 1:
            raise RuntimeError("network blip")
        return {"id": str(clip["id"]), "camera": "Cam", "path": "/x", "timestamp": "t"}

    with (
        patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=clips)),
        patch.object(dl, "_download_clip", side_effect=_fake_download),
    ):
        results = await dl.download_new_clips()

    assert len(results) == 1
    assert results[0]["id"] == "2"


async def test_download_clip_null_api_fields(dl, tmp_path):
    """_download_clip must not raise TypeError when the Blink API returns null
    for duration, network_id, or source (present key, None value).

    Regression test for:
      int() argument must be a string, a bytes-like object or a real number,
      not 'NoneType'
    """
    clip = {
        "id": 99999,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/99999.mp4",
        "thumbnail": None,
        "created_at": "2024-06-01T08:30:00+00:00",
        "duration": None,  # null in Blink API for some clip types
        "network_id": None,  # null in Blink API for some clip types
        "source": None,  # null in Blink API for some clip types
        "deleted": False,
    }
    content = b"fake video data"

    async def _iter_chunks(chunk_size):
        yield content

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session

    mock_blink = MagicMock()
    mock_blink.auth.header = {}
    mock_blink.urls.base_url = "https://rest-prod.immedia-semi.com"
    dl._blink = mock_blink

    dl._storage = MagicMock()
    dl._storage.is_over_quota.return_value = False
    dl._storage.resolve_path.return_value = tmp_path / "clip.mp4"
    dl._tracker = MagicMock()
    dl._db = None  # skip DB write

    sem = asyncio.Semaphore(1)
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("ffprobe not found")),
    ):
        result = await dl._download_clip(clip, sem)

    assert result is not None
    # duration=None from the API falls back to probe_clip_duration, which
    # also comes back 0 here (ffprobe unavailable in this unit test) —
    # covers the same "must not crash" null-handling this test targets
    # without a real, slow, environment-dependent subprocess call.
    assert result["duration"] == 0
    assert result["network_id"] == 0
    assert result["source"] == ""


async def test_download_clip_falls_back_to_probed_duration(dl, tmp_path):
    """Confirmed against a real account: the Blink API's own duration field
    came back 0 for every single clip despite each one downloading and
    playing fine — _download_clip must probe the file it just downloaded
    in that case rather than storing the API's useless 0."""
    clip = {
        "id": 55555,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/55555.mp4",
        "thumbnail": None,
        "created_at": "2024-06-01T08:30:00+00:00",
        "duration": 0,
        "network_id": 3,
        "source": "pir",
        "deleted": False,
    }
    content = b"fake video data"

    async def _iter_chunks(chunk_size):
        yield content

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session

    mock_blink = MagicMock()
    mock_blink.auth.header = {}
    mock_blink.urls.base_url = "https://rest-prod.immedia-semi.com"
    dl._blink = mock_blink

    dl._storage = MagicMock()
    dl._storage.is_over_quota.return_value = False
    dl._storage.resolve_path.return_value = tmp_path / "clip.mp4"
    dl._tracker = MagicMock()
    dl._db = None

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"23.4\n", b""))
    sem = asyncio.Semaphore(1)
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        result = await dl._download_clip(clip, sem)

    assert result is not None
    assert result["duration"] == 23


async def _download_sample_clip(dl, tmp_path, *, download_thumbnails):
    """Run _download_clip for a basic clip and return (result, dest_path)."""
    clip = {
        "id": 1234,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/1234.mp4",
        "thumbnail": None,
        "created_at": "2024-06-01T08:30:00+00:00",
        "duration": 10,
        "network_id": 10,
        "source": "camera",
        "deleted": False,
    }
    content = b"fake video data"

    async def _iter_chunks(chunk_size):
        yield content

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session

    mock_blink = MagicMock()
    mock_blink.auth.header = {}
    mock_blink.urls.base_url = "https://rest-prod.immedia-semi.com"
    dl._blink = mock_blink

    dest = tmp_path / "clip.mp4"
    dl._config.download_thumbnails = download_thumbnails
    dl._storage = MagicMock()
    dl._storage.is_over_quota.return_value = False
    dl._storage.resolve_path.return_value = dest
    dl._tracker = MagicMock()
    dl._db = None

    sem = asyncio.Semaphore(1)
    result = await dl._download_clip(clip, sem)
    return result, dest


async def test_download_clip_generates_thumbnail_when_enabled(dl, tmp_path):
    with patch.object(
        dl, "_generate_thumbnail", AsyncMock(return_value=True)
    ) as mock_gen:
        result, dest = await _download_sample_clip(
            dl, tmp_path, download_thumbnails=True
        )

    assert result is not None
    mock_gen.assert_awaited_once_with(dest, dest.with_suffix(".jpg"))


async def test_download_clip_skips_thumbnail_when_disabled(dl, tmp_path):
    with patch.object(
        dl, "_generate_thumbnail", AsyncMock(return_value=True)
    ) as mock_gen:
        result, _ = await _download_sample_clip(dl, tmp_path, download_thumbnails=False)

    assert result is not None
    mock_gen.assert_not_called()


async def test_download_clip_no_media_url_returns_none(dl):
    """A clip with no media URL is skipped rather than attempting a download."""
    clip = {"id": 1, "device_name": "Front Door", "media": "", "deleted": False}
    sem = asyncio.Semaphore(1)
    assert await dl._download_clip(clip, sem) is None


async def test_download_clip_over_quota_returns_none(dl, tmp_path):
    """A clip is skipped once storage is over quota, without attempting a
    network download."""
    clip = {
        "id": 2,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/2.mp4",
        "created_at": "2024-06-01T08:30:00+00:00",
        "deleted": False,
    }
    dl._storage.is_over_quota = MagicMock(return_value=True)  # type: ignore[method-assign]
    sem = asyncio.Semaphore(1)
    assert await dl._download_clip(clip, sem) is None


async def test_download_clip_invalid_created_at_falls_back_to_now(dl, tmp_path):
    """An unparseable created_at must not raise — the clip still downloads,
    filed under the current time instead of a parsed timestamp."""
    clip = {
        "id": 3,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/3.mp4",
        "created_at": "not-a-timestamp",
        "duration": 5,
        "network_id": 1,
        "source": "pir",
        "deleted": False,
    }
    content = b"fake video data"

    async def _iter_chunks(chunk_size):
        yield content

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session

    mock_blink = MagicMock()
    mock_blink.auth.header = {}
    mock_blink.urls.base_url = "https://rest-prod.immedia-semi.com"
    dl._blink = mock_blink
    dl._db = None

    sem = asyncio.Semaphore(1)
    result = await dl._download_clip(clip, sem)

    assert result is not None
    assert result["timestamp"]  # a real ISO timestamp was generated


async def test_download_clip_already_on_disk_marks_downloaded(dl, tmp_path):
    """A clip file already present on disk (e.g. tracker was reset) is not
    re-downloaded — it's marked downloaded from the existing file's size and
    flagged 'skipped'."""
    clip = {
        "id": 4,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/4.mp4",
        "created_at": "2024-06-01T08:30:00+00:00",
        "deleted": False,
    }
    dest = tmp_path / "existing.mp4"
    dest.write_bytes(b"already here")
    dl._storage.resolve_path = MagicMock(return_value=dest)  # type: ignore[method-assign]

    sem = asyncio.Semaphore(1)
    result = await dl._download_clip(clip, sem)

    assert result is not None
    assert result["skipped"] is True
    assert result["size_bytes"] == len(b"already here")
    assert dl._tracker.is_downloaded("4")


async def test_download_clip_stream_failure_returns_none(dl, tmp_path):
    """A failed/aborted stream (all retries exhausted) makes _download_clip
    return None rather than a partial result."""
    clip = {
        "id": 5,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/5.mp4",
        "created_at": "2024-06-01T08:30:00+00:00",
        "deleted": False,
    }
    with patch.object(dl, "_stream_to_file", AsyncMock(return_value=None)):
        sem = asyncio.Semaphore(1)
        result = await dl._download_clip(clip, sem)

    assert result is None


async def test_download_clip_writes_to_db_when_configured(dl, tmp_path):
    """When a ClipDatabase is attached, a successfully downloaded clip is
    persisted to it."""
    clip = {
        "id": 6,
        "device_name": "Front Door",
        "media": "/api/v1/accounts/1/clip/6.mp4",
        "created_at": "2024-06-01T08:30:00+00:00",
        "duration": 5,
        "network_id": 1,
        "source": "pir",
        "deleted": False,
    }
    content = b"fake video data"

    async def _iter_chunks(chunk_size):
        yield content

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content.iter_chunked = _iter_chunks
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.closed = False
    dl._session = mock_session

    mock_blink = MagicMock()
    mock_blink.auth.header = {}
    mock_blink.urls.base_url = "https://rest-prod.immedia-semi.com"
    dl._blink = mock_blink
    dl._db = AsyncMock()

    sem = asyncio.Semaphore(1)
    result = await dl._download_clip(clip, sem)

    assert result is not None
    dl._db.add_clip.assert_awaited_once_with(result)


# ---------------------------------------------------------------------------
# 2FA waiting
# ---------------------------------------------------------------------------


async def test_handle_2fa_reads_code_from_file(dl, tmp_path):
    two_fa_path = tmp_path / "2fa.txt"
    two_fa_path.write_text("123456")
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock()
    dl._config.two_fa_timeout = 30.0

    with patch("blink_downloader.downloader.TWO_FA_FILE", two_fa_path):
        await dl._handle_2fa()

    dl._blink.send_2fa_code.assert_awaited_once_with("123456")
    assert not two_fa_path.exists()


async def test_handle_2fa_times_out(dl, tmp_path):
    missing_file = tmp_path / "no_2fa.txt"
    dl._blink = AsyncMock()
    dl._config.two_fa_timeout = 0.1  # extremely short timeout

    with (
        patch("blink_downloader.downloader.TWO_FA_FILE", missing_file),
        pytest.raises(TwoFARequired),
    ):
        await dl._handle_2fa()


async def test_handle_2fa_web_ui_code(dl, tmp_path):
    """Code submitted via submit_two_fa_code() is picked up by _handle_2fa."""
    missing_file = tmp_path / "no_2fa.txt"
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock()
    dl._config.two_fa_timeout = 30.0

    async def _submit_concurrently():
        # Spin until the event is created inside _handle_2fa, then submit.
        while dl._two_fa_event is None:
            await asyncio.sleep(0)
        dl.submit_two_fa_code("654321")

    with patch("blink_downloader.downloader.TWO_FA_FILE", missing_file):
        await asyncio.gather(dl._handle_2fa(), _submit_concurrently())

    dl._blink.send_2fa_code.assert_awaited_once_with("654321")


def test_submit_two_fa_code_sets_event_and_code(dl):
    """submit_two_fa_code stores the code and fires the event."""
    dl._two_fa_event = asyncio.Event()
    dl.submit_two_fa_code("  999888  ")  # whitespace should be stripped
    assert dl._two_fa_code == "999888"
    assert dl._two_fa_event.is_set()


def test_submit_two_fa_code_without_event_does_not_raise(dl):
    """submit_two_fa_code is safe to call before _handle_2fa initialises the event."""
    assert dl._two_fa_event is None
    dl.submit_two_fa_code("111222")  # must not raise
    assert dl._two_fa_code == "111222"


async def test_handle_2fa_sets_auth_state(dl, tmp_path):
    """_handle_2fa sets auth_state to 'needs_2fa' and resets it on success."""
    two_fa_path = tmp_path / "code.txt"
    two_fa_path.write_text("000000")
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock()
    dl._config.two_fa_timeout = 30.0

    with patch("blink_downloader.downloader.TWO_FA_FILE", two_fa_path):
        await dl._handle_2fa()

    # After a successful _handle_2fa the state is still 'needs_2fa'
    # (connect() is responsible for setting it to 'connected').
    assert dl.auth_state == "needs_2fa"


async def test_handle_2fa_sets_error_state_on_timeout(dl, tmp_path):
    missing_file = tmp_path / "no.txt"
    dl._blink = AsyncMock()
    dl._config.two_fa_timeout = 0.05

    with (
        patch("blink_downloader.downloader.TWO_FA_FILE", missing_file),
        pytest.raises(TwoFARequired),
    ):
        await dl._handle_2fa()

    assert dl.auth_state == "error"


# ---------------------------------------------------------------------------
# 2FA wrong-code handling (v2.6.6)
# ---------------------------------------------------------------------------


def test_submit_two_fa_code_returns_incrementing_seq(dl):
    """submit_two_fa_code returns a sequence number for result correlation."""
    dl._two_fa_event = asyncio.Event()
    assert dl.submit_two_fa_code("111111") == 1
    assert dl.submit_two_fa_code("222222") == 2


async def test_submit_2fa_code_returns_false_on_two_fa_required(dl):
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(side_effect=BlinkTwoFARequiredError("nope"))
    assert await dl._submit_2fa_code("123456") is False


async def test_submit_2fa_code_returns_false_on_generic_exception(dl):
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(side_effect=RuntimeError("boom"))
    assert await dl._submit_2fa_code("123456") is False


async def test_submit_2fa_code_returns_true_on_success(dl):
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(return_value=True)
    assert await dl._submit_2fa_code("123456") is True


async def test_submit_2fa_code_returns_false_when_send_2fa_code_returns_false(dl):
    """blinkpy >= 0.25 returns False (no exception) for a wrong/expired code."""
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(return_value=False)
    assert await dl._submit_2fa_code("123456") is False


async def test_handle_2fa_wrong_code_does_not_raise_and_keeps_waiting(dl, tmp_path):
    """A wrong code must not propagate BlinkTwoFARequiredError out of _handle_2fa.

    Regression test: previously the resulting BlinkTwoFARequiredError escaped
    _handle_2fa entirely, leaving auth_state stuck at "needs_2fa" with no
    way for the web UI to recover.
    """
    missing_file = tmp_path / "no_2fa.txt"
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(side_effect=BlinkTwoFARequiredError("nope"))
    dl._config.two_fa_timeout = 0.15

    async def _submit_concurrently():
        while dl._two_fa_event is None:
            await asyncio.sleep(0)
        dl.submit_two_fa_code("000000")

    with (
        patch("blink_downloader.downloader.TWO_FA_FILE", missing_file),
        pytest.raises(TwoFARequired),
    ):
        await asyncio.gather(dl._handle_2fa(), _submit_concurrently())

    # The wrong-code attempt was recorded …
    assert dl.two_fa_result_ok is False
    assert dl.two_fa_result_seq == 1
    # … but the loop kept running until the (short) timeout expired.
    assert dl.auth_state == "error"


async def test_handle_2fa_wrong_code_returns_false_does_not_raise(dl, tmp_path):
    """blinkpy >= 0.25 returns False (no exception) for a wrong 2FA code.

    _handle_2fa must record the rejection and keep waiting for a retry,
    exactly like the BlinkTwoFARequiredError case above.
    """
    missing_file = tmp_path / "no_2fa.txt"
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(return_value=False)
    dl._config.two_fa_timeout = 0.15

    async def _submit_concurrently():
        while dl._two_fa_event is None:
            await asyncio.sleep(0)
        dl.submit_two_fa_code("000000")

    with (
        patch("blink_downloader.downloader.TWO_FA_FILE", missing_file),
        pytest.raises(TwoFARequired),
    ):
        await asyncio.gather(dl._handle_2fa(), _submit_concurrently())

    assert dl.two_fa_result_ok is False
    assert dl.two_fa_result_seq == 1
    assert dl.auth_state == "error"


async def test_handle_2fa_wrong_code_then_correct_code_succeeds(dl, tmp_path):
    """A wrong code followed by a correct code completes successfully."""
    missing_file = tmp_path / "no_2fa.txt"
    dl._blink = AsyncMock()
    dl._blink.send_2fa_code = AsyncMock(
        side_effect=[BlinkTwoFARequiredError("nope"), True]
    )
    dl._config.two_fa_timeout = 30.0

    async def _submit_concurrently():
        while dl._two_fa_event is None:
            await asyncio.sleep(0)
        dl.submit_two_fa_code("111111")  # wrong code, seq=1

        while dl.two_fa_result_ok is not False:
            await asyncio.sleep(0)
        dl.submit_two_fa_code("222222")  # correct code, seq=2

    with patch("blink_downloader.downloader.TWO_FA_FILE", missing_file):
        await asyncio.gather(dl._handle_2fa(), _submit_concurrently())

    assert dl._blink.send_2fa_code.await_count == 2
    assert dl.two_fa_result_ok is True
    assert dl.two_fa_result_seq == 2
    # _handle_2fa itself doesn't flip auth_state to "connected" — connect() does.
    assert dl.auth_state == "needs_2fa"
    assert "Incorrect" not in dl.auth_message


async def test_connect_sets_auth_state_connected(dl, tmp_path):
    """connect() sets auth_state to 'connected' on success."""
    missing_auth = tmp_path / "no_auth.json"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 7
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth"),
    ):
        await dl.connect()

    assert dl.auth_state == "connected"


async def test_connect_start_raises_two_fa_required_calls_handle_2fa(dl, tmp_path):
    """If blink.start() itself raises BlinkTwoFARequiredError (not just
    send_2fa_code), connect() must run the 2FA flow and still finish
    connecting rather than propagating the exception."""
    missing_auth = tmp_path / "no_auth.json"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock(side_effect=BlinkTwoFARequiredError("2FA required"))
    mock_blink.account_id = 99
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    dl._handle_2fa = AsyncMock()  # type: ignore[method-assign]

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth"),
    ):
        await dl.connect()

    dl._handle_2fa.assert_awaited_once()
    assert dl.auth_state == "connected"


async def test_connect_generic_exception_sets_error_and_deletes_cache(dl, tmp_path):
    """An unexpected exception from blink.start() must set auth_state to
    'error', delete the (now-possibly-invalid) cached auth file, and re-raise
    rather than being silently swallowed."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"token": "cached_token", "host": "rest-us"}))

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch("blink_downloader.downloader.AUTH_FILE", auth_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth"),
        pytest.raises(RuntimeError),
    ):
        await dl.connect()

    assert dl.auth_state == "error"
    assert not auth_file.exists()


# ---------------------------------------------------------------------------
# connect() — happy path (cached credentials)
# ---------------------------------------------------------------------------


async def test_connect_uses_cached_credentials(dl, tmp_path):
    """connect() merges cached auth data into the login_data dict."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"token": "cached_token", "host": "rest-us"}))

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 42
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {"token": "cached_token"}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", auth_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    # Auth was called with merged login_data including the cached token
    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["token"] == "cached_token"


async def test_connect_proceeds_without_cached_file(dl, tmp_path):
    """connect() works fine when no auth cache file exists."""
    missing_auth = tmp_path / "no_auth.json"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 1
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["username"] == "test@example.com"


async def test_connect_falls_back_when_auth_file_is_wrong_shape(dl, tmp_path):
    """A valid-JSON-but-wrong-shape auth cache (e.g. a bare list) must not
    raise out of connect() — it should be treated like any other corrupt file
    and fall back to a fresh login."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps([1, 2, 3]))

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 1
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", auth_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["username"] == "test@example.com"


async def test_connect_cached_credentials_do_not_override_config(dl, tmp_path):
    """Cached username/password must not override the current config.

    blinkpy's persisted login_attributes include the username/password that
    were active when the token was cached. If the user updates their Blink
    credentials in the add-on configuration (e.g. after a forced password
    reset that also invalidated the cached refresh token), the *new*
    credentials must be used — not the stale ones from the cache file.
    """
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "token": "cached_token",
                "host": "rest-us",
                "username": "stale@example.com",
                "password": "old-password",
            }
        )
    )

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 42
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {"token": "cached_token"}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", auth_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["username"] == "test@example.com"
    assert call_kwargs["login_data"]["password"] == "hunter2"
    # Token data from the cache is still merged in.
    assert call_kwargs["login_data"]["token"] == "cached_token"


# ---------------------------------------------------------------------------
# connect() — failed login on a fresh (non-cached) login
# ---------------------------------------------------------------------------


async def test_connect_unauthorized_without_cache_raises_authentication_error(
    dl, tmp_path
):
    """A 401 on a fresh login raises AuthenticationError with a useful message.

    blinkpy's UnauthorizedError carries no message of its own (str(e) == ""),
    which previously produced log lines like "Authentication failed with
    provided credentials: " with nothing useful after the colon.
    """
    missing_auth = tmp_path / "no_auth.json"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock(side_effect=UnauthorizedError)

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth"),
        pytest.raises(AuthenticationError) as excinfo,
    ):
        await dl.connect()

    assert str(excinfo.value)  # non-empty, actionable message
    assert dl.auth_state == "error"


async def test_connect_start_returns_false_without_cache_raises_authentication_error(
    dl, tmp_path
):
    """blinkpy >= 0.25 returns False (no exception) when OAuth login fails.

    A fresh (non-cached) login that fails this way must still raise
    AuthenticationError with a useful message, just like the old
    UnauthorizedError path did.
    """
    missing_auth = tmp_path / "no_auth.json"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock(return_value=False)

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth"),
        pytest.raises(AuthenticationError) as excinfo,
    ):
        await dl.connect()

    assert str(excinfo.value)
    assert dl.auth_state == "error"


async def test_connect_start_returns_false_with_cache_retries_fresh(dl, tmp_path):
    """A stale cached token (start() returns False) triggers one retry with
    fresh credentials, which then succeeds."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"token": "stale_token", "host": "rest-us"}))

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock(side_effect=[False, True])
    mock_blink.account_id = 42
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {"token": "fresh_token"}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", auth_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth"),
        patch("blink_downloader.downloader.asyncio.sleep", AsyncMock()),
    ):
        await dl.connect()

    assert mock_blink.start.await_count == 2
    assert dl.auth_state == "connected"


# ---------------------------------------------------------------------------
# connect() — stable hardware_id across retries/restarts
# ---------------------------------------------------------------------------


async def test_connect_generates_and_persists_hardware_id(dl, tmp_path):
    """A fresh install gets a hardware_id, persisted for future attempts.

    blinkpy generates a random hardware_id for every Auth() instance that
    isn't given one. Without persistence, every retry after a failed login
    would present Blink with a brand new "device" using the same
    credentials -- a pattern that can trigger Blink's fraud detection.
    """
    missing_auth = tmp_path / "no_auth.json"
    hw_file = tmp_path / "hardware_id.txt"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 1
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.HARDWARE_ID_FILE", hw_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    hardware_id = call_kwargs["login_data"]["hardware_id"]
    assert hardware_id
    assert hw_file.read_text(encoding="utf-8").strip() == hardware_id


async def test_connect_reuses_persisted_hardware_id(dl, tmp_path):
    """A retry after a failed login reuses the same persisted hardware_id."""
    missing_auth = tmp_path / "no_auth.json"
    hw_file = tmp_path / "hardware_id.txt"
    hw_file.write_text("EXISTING-HARDWARE-ID", encoding="utf-8")

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 1
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.HARDWARE_ID_FILE", hw_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["hardware_id"] == "EXISTING-HARDWARE-ID"
    assert hw_file.read_text(encoding="utf-8").strip() == "EXISTING-HARDWARE-ID"


async def test_connect_adopts_hardware_id_from_auth_cache(dl, tmp_path):
    """A hardware_id cached from a prior successful login is reused and
    backfilled into HARDWARE_ID_FILE so it survives AUTH_FILE being deleted."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps({"token": "cached_token", "hardware_id": "CACHED-ID"})
    )
    hw_file = tmp_path / "hardware_id.txt"  # does not exist yet

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 42
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {"token": "cached_token"}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", auth_file),
        patch("blink_downloader.downloader.HARDWARE_ID_FILE", hw_file),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["hardware_id"] == "CACHED-ID"
    assert hw_file.read_text(encoding="utf-8").strip() == "CACHED-ID"


# ---------------------------------------------------------------------------
# connect() — passwords containing symbols
# ---------------------------------------------------------------------------


async def test_connect_passes_through_password_with_symbols(dl, tmp_path):
    """A password containing special characters reaches login_data unchanged."""
    missing_auth = tmp_path / "no_auth.json"
    dl._config.password = "p@ss!w0rd#123$%^&*()"

    mock_blink = AsyncMock()
    mock_blink.start = AsyncMock()
    mock_blink.account_id = 1
    mock_blink.auth = MagicMock()
    mock_blink.auth.login_attributes = {}

    with (
        patch("blink_downloader.downloader.AUTH_FILE", missing_auth),
        patch("blink_downloader.downloader.HARDWARE_ID_FILE", tmp_path / "hw.txt"),
        patch("blink_downloader.downloader.Blink", return_value=mock_blink),
        patch("blink_downloader.downloader.Auth") as MockAuth,
    ):
        await dl.connect()

    call_kwargs = MockAuth.call_args[1]
    assert call_kwargs["login_data"]["password"] == "p@ss!w0rd#123$%^&*()"


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------


async def test_disconnect_does_not_revoke_token_server_side(dl):
    """disconnect() must not call Blink's logout endpoint.

    Doing so on every add-on restart would invalidate the persisted auth
    token this add-on relies on for a quiet reconnect (see connect()), and
    a resulting full username/password re-login can knock other clients on
    the same Blink account — e.g. Home Assistant's own Blink integration —
    into a temporarily unauthenticated state.
    """
    dl._blink = MagicMock()
    dl._session = AsyncMock()
    dl._session.closed = False

    with patch("blink_downloader.downloader.blink_api.request_logout") as mock_logout:
        await dl.disconnect()

    mock_logout.assert_not_called()
    dl._session.close.assert_awaited_once()


async def test_disconnect_closes_session_without_blink_instance(dl):
    """disconnect() tolerates never having connected (no self._blink)."""
    dl._blink = None
    dl._session = AsyncMock()
    dl._session.closed = False

    await dl.disconnect()

    dl._session.close.assert_awaited_once()


async def test_disconnect_skips_closed_session(dl):
    """disconnect() does not try to close an already-closed session."""
    dl._blink = MagicMock()
    dl._session = AsyncMock()
    dl._session.closed = True

    await dl.disconnect()

    dl._session.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# _fetch_clip_list — pagination
# ---------------------------------------------------------------------------


async def test_fetch_clip_list_paginates(dl, sample_clip):
    """_fetch_clip_list fetches page 1 when page 0 is full (_PAGE_SIZE=25 items).

    blinkpy returns the parsed JSON dict directly, not an aiohttp response.
    """
    full_page = [{**sample_clip, "id": i} for i in range(25)]

    dl._blink = MagicMock()
    dl._blink.account_id = 1

    with patch(
        "blink_downloader.downloader.blink_api.request_videos",
        side_effect=[{"media": full_page}, {"media": []}],
    ):
        result = await dl._fetch_clip_list(datetime.now(UTC))

    assert len(result) == 25


async def test_fetch_clip_list_stops_on_partial_page(dl, sample_clip):
    """A single page with fewer than _PAGE_SIZE items means there is no next
    page — pagination must stop after that one page instead of requesting
    page 1 unnecessarily."""
    partial_page = [{**sample_clip, "id": i} for i in range(10)]

    dl._blink = MagicMock()
    dl._blink.account_id = 1

    with patch(
        "blink_downloader.downloader.blink_api.request_videos",
        side_effect=[{"media": partial_page}],
    ) as mock_request:
        result = await dl._fetch_clip_list(datetime.now(UTC))

    assert len(result) == 10
    assert mock_request.await_count == 1


async def test_fetch_clip_list_stops_at_max_pages_safety_cap(dl, sample_clip, caplog):
    """Regression test: a pathological API response that never returns a
    partial/empty page must not loop forever — pagination stops at a
    defensive page cap (_MAX_PAGES=400) and logs a warning rather than
    growing memory and making requests indefinitely."""
    full_page = [{**sample_clip, "id": i} for i in range(25)]

    dl._blink = MagicMock()
    dl._blink.account_id = 1

    async def _always_full_page(*_args, **_kwargs):
        return {"media": full_page}

    with (
        patch(
            "blink_downloader.downloader.blink_api.request_videos",
            side_effect=_always_full_page,
        ) as mock_request,
        caplog.at_level("WARNING"),
    ):
        result = await dl._fetch_clip_list(datetime.now(UTC))

    assert mock_request.await_count == 400
    assert len(result) == 400 * 25
    assert "safety cap" in caplog.text


async def test_fetch_clip_list_handles_unexpected_response_type(dl):
    """request_videos returning something other than a dict (e.g. blinkpy
    changing its return type in a future version) must stop pagination
    cleanly instead of crashing on `.get()`."""
    dl._blink = MagicMock()
    dl._blink.account_id = 1

    with patch(
        "blink_downloader.downloader.blink_api.request_videos",
        return_value=None,
    ):
        result = await dl._fetch_clip_list(datetime.now(UTC))

    assert result == []


async def test_fetch_clip_list_handles_api_error(dl):
    """Returns an empty list when blinkpy raises an exception for a failed request.

    In blinkpy >= 0.22, non-200 responses raise exceptions (UnauthorizedError,
    ClientConnectionError, BlinkBadResponse) rather than returning an error
    response object with a .status attribute.
    """
    dl._blink = MagicMock()
    with patch(
        "blink_downloader.downloader.blink_api.request_videos",
        side_effect=Exception("HTTP 500"),
    ):
        result = await dl._fetch_clip_list(datetime.now(UTC))

    assert result == []


# ---------------------------------------------------------------------------
# _fetch_clip_list — AUTH_FATAL_EXCEPTIONS propagation (v5.0.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        TokenRefreshFailed("refresh failed"),
        LoginError("login failed"),
        UnauthorizedError("unauthorized"),
    ],
    ids=["TokenRefreshFailed", "LoginError", "UnauthorizedError"],
)
async def test_fetch_clip_list_reraises_auth_fatal_exceptions(dl, caplog, exc):
    """Unlike a generic/transient failure, an AUTH_FATAL_EXCEPTIONS error means
    the whole session is broken -- every call this cycle and every future
    cycle would fail the same way. It must propagate (not be swallowed) so
    app.py's poll loop can reconnect, instead of silently retrying the same
    broken token refresh forever (see CHANGELOG 5.0.5)."""
    dl._blink = MagicMock()
    with (
        patch(
            "blink_downloader.downloader.blink_api.request_videos",
            side_effect=exc,
        ),
        caplog.at_level("WARNING"),
        pytest.raises(type(exc)),
    ):
        await dl._fetch_clip_list(datetime.now(UTC))

    # Still logged for operator visibility, same as any other failed page.
    assert "request_videos failed (page 0)" in caplog.text


async def test_fetch_clip_list_still_swallows_non_auth_fatal_exceptions(dl):
    """Regression guard: only AUTH_FATAL_EXCEPTIONS should propagate --
    everything else (network blips, bad responses, etc.) must keep being
    swallowed so a single bad page doesn't kill the whole poll cycle."""
    dl._blink = MagicMock()
    with patch(
        "blink_downloader.downloader.blink_api.request_videos",
        side_effect=ConnectionResetError("connection reset"),
    ):
        result = await dl._fetch_clip_list(datetime.now(UTC))

    assert result == []


# ---------------------------------------------------------------------------
# _persist_auth
# ---------------------------------------------------------------------------


def test_persist_auth_writes_file(dl, tmp_path):
    auth_path = tmp_path / "auth.json"
    mock_blink = MagicMock()
    mock_blink.auth.login_attributes = {"token": "tok123", "host": "prod"}
    dl._blink = mock_blink

    with patch("blink_downloader.downloader.AUTH_FILE", auth_path):
        dl._persist_auth()

    data = json.loads(auth_path.read_text())
    assert data["token"] == "tok123"


def test_persist_auth_does_not_leave_tmp_file_behind(dl, tmp_path):
    auth_path = tmp_path / "auth.json"
    mock_blink = MagicMock()
    mock_blink.auth.login_attributes = {"token": "tok123", "host": "prod"}
    dl._blink = mock_blink

    with patch("blink_downloader.downloader.AUTH_FILE", auth_path):
        dl._persist_auth()

    assert auth_path.exists()
    assert not (tmp_path / "auth.json.tmp").exists()


def test_persist_auth_handles_exception(dl):
    """_persist_auth should not raise even if writing fails."""
    dl._blink = MagicMock()
    dl._blink.auth.login_attributes = None  # will cause json.dumps to fail

    with patch(
        "blink_downloader.downloader.AUTH_FILE", Path("/nonexistent/deep/auth.json")
    ):
        dl._persist_auth()  # no exception


# ---------------------------------------------------------------------------
# _get_session
# ---------------------------------------------------------------------------


async def test_get_session_uses_unsafe_cookie_jar(dl):
    """The session used for blinkpy's OAuth flow must use an unsafe cookie jar.

    blinkpy's OAuth v2 / PKCE login chains several requests to
    api.oauth.blink.com, relying on cookies from earlier steps (authorize,
    signin page) being sent back on the signin POST. aiohttp's default
    ("safe") CookieJar can drop those cookies, making blinkpy log "Login
    failed" even with correct credentials (fronzbot/blinkpy#1229).
    """
    session = dl._get_session()
    try:
        assert session.cookie_jar._unsafe is True
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# download_local_storage_clips (v2.5.5)
# ---------------------------------------------------------------------------


async def test_download_local_storage_no_blink_returns_empty(dl):
    """Returns empty list when blink is not connected yet."""
    dl._blink = None
    assert await dl.download_local_storage_clips() == []


async def test_download_local_storage_skips_no_usb(dl):
    """Sync modules without active local storage are silently skipped."""
    mock_sync = MagicMock()
    mock_sync.local_storage = False
    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    assert await dl.download_local_storage_clips() == []


async def test_download_local_storage_handles_manifest_error(dl):
    """Manifest refresh failures are caught and do not propagate."""
    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock(
        side_effect=RuntimeError("network timeout")
    )
    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    assert await dl.download_local_storage_clips() == []


@pytest.mark.parametrize(
    "exc",
    [
        TokenRefreshFailed("refresh failed"),
        LoginError("login failed"),
        UnauthorizedError("unauthorized"),
    ],
    ids=["TokenRefreshFailed", "LoginError", "UnauthorizedError"],
)
async def test_download_local_storage_reraises_auth_fatal_exceptions(dl, exc):
    """AUTH_FATAL_EXCEPTIONS from a sync module's manifest refresh must
    propagate (not be caught, unlike test_download_local_storage_handles_manifest_error
    above) so app.py's poll loop can reconnect -- see CHANGELOG 5.0.5."""
    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock(side_effect=exc)
    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}

    with pytest.raises(type(exc)):
        await dl.download_local_storage_clips()


async def test_download_local_storage_skips_already_tracked(dl):
    """Clips already in the tracker are not re-downloaded."""
    mock_item = MagicMock()
    mock_item.id = 7777
    mock_item.name = "Garage"
    mock_item.created_at = datetime(2024, 6, 1, tzinfo=UTC)
    mock_item.size = 1024

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m1"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._tracker.mark_downloaded("local_7777")

    results = await dl.download_local_storage_clips()
    assert results == []


async def test_download_local_storage_empty_manifest_skipped(dl):
    """A sync module with local storage active but an empty manifest is
    skipped cleanly, without raising."""
    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": set(), "last_manifest_id": "m1"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}

    results = await dl.download_local_storage_clips()
    assert results == []


async def test_download_local_storage_parses_string_created_at(dl, tmp_path):
    """blinkpy can report a manifest item's created_at as an ISO string
    rather than a datetime — it must be parsed rather than passed through
    raw to StorageManager.resolve_path()."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 4242
    mock_item.name = "Patio"
    mock_item.created_at = "2024-06-01T08:00:00+00:00"
    mock_item.size = 1024
    mock_item.prepare_download = AsyncMock(return_value=True)

    async def _fake_download(blink, file_name, max_retries=4):
        _Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        _Path(file_name).write_bytes(b"V" * 50)
        return True

    mock_item.download_video = _fake_download

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m2"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()

    assert len(results) == 1
    assert results[0]["timestamp"].startswith("2024-06-01T08:00:00")


async def test_download_local_storage_unparseable_created_at_falls_back_to_now(
    dl, tmp_path
):
    """An unparseable string created_at must not raise — it falls back to
    the current time instead."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 4243
    mock_item.name = "Patio"
    mock_item.created_at = "not-a-timestamp"
    mock_item.size = 1024
    mock_item.prepare_download = AsyncMock(return_value=True)

    async def _fake_download(blink, file_name, max_retries=4):
        _Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        _Path(file_name).write_bytes(b"V" * 50)
        return True

    mock_item.download_video = _fake_download

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m3"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()

    assert len(results) == 1
    assert results[0]["timestamp"]  # a real (fallback) ISO timestamp


async def test_download_local_storage_already_on_disk_marks_downloaded(dl, tmp_path):
    """A local-storage clip file already present on disk is not re-fetched —
    it's marked downloaded straight from the existing file's size."""
    mock_item = MagicMock()
    mock_item.id = 4244
    mock_item.name = "Patio"
    mock_item.created_at = datetime(2024, 6, 1, tzinfo=UTC)
    mock_item.size = 1024

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m4"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    existing = tmp_path / "existing_local.mp4"
    existing.write_bytes(b"already here")
    dl._storage.resolve_path = MagicMock(return_value=existing)  # type: ignore[method-assign]

    results = await dl.download_local_storage_clips()

    assert results == []  # already-on-disk clips aren't added to the result list
    assert dl._tracker.is_downloaded("local_4244")


async def test_download_local_storage_dest_not_visible_until_write_completes(
    dl, tmp_path
):
    """Regression test: a killed process mid-download must never leave a
    truncated file at dest — blinkpy's download_video() opens the path we
    give it in truncate mode, so it must be pointed at a .tmp path that's
    only atomically renamed over dest after the write fully succeeds,
    mirroring _stream_attempt()'s cloud-clip download."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 7321
    mock_item.name = "Side Yard"
    mock_item.created_at = datetime(2024, 6, 3, tzinfo=UTC)
    mock_item.size = 4096
    mock_item.prepare_download = AsyncMock(return_value=True)

    dest = dl._storage.resolve_path(
        mock_item.name, mock_item.created_at, f"local_{mock_item.id}"
    )
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    dest_existed_during_write = False

    async def _fake_download(blink, file_name, max_retries=4):
        nonlocal dest_existed_during_write
        assert _Path(file_name) == tmp_dest
        _Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        _Path(file_name).write_bytes(b"V" * 100)
        dest_existed_during_write = dest.exists()
        return True

    mock_item.download_video = _fake_download

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m7"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()

    assert len(results) == 1
    assert dest_existed_during_write is False
    assert dest.read_bytes() == b"V" * 100
    assert not tmp_dest.exists()


async def test_download_local_storage_prepare_download_exception_cleans_up(
    dl, tmp_path
):
    """An exception from prepare_download()/download_video() is logged and
    skipped, cleaning up any partial file left on disk."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 4245
    mock_item.name = "Patio"
    mock_item.created_at = datetime(2024, 6, 1, tzinfo=UTC)
    mock_item.size = 1024

    async def _failing_prepare(blink):
        # Simulate a partial file being written before the failure — at the
        # .tmp write target, not the final dest (see _download_local_storage_item's
        # atomic-rename-on-success design, mirroring _stream_attempt).
        dest = dl._storage.resolve_path(
            mock_item.name, mock_item.created_at, f"local_{mock_item.id}"
        )
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        _Path(tmp_dest).parent.mkdir(parents=True, exist_ok=True)
        _Path(tmp_dest).write_bytes(b"partial")
        raise RuntimeError("sync module offline")

    mock_item.prepare_download = _failing_prepare

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m5"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()

    assert results == []
    assert not dl._tracker.is_downloaded("local_4245")


async def test_download_local_storage_prepare_download_exception_no_partial_file(
    dl, tmp_path
):
    """Same failure path as the test above, but the exception happens before
    any bytes were ever written (e.g. prepare_download() itself fails) — the
    .tmp cleanup must be skipped gracefully rather than erroring on a path
    that was never created."""
    mock_item = MagicMock()
    mock_item.id = 4246
    mock_item.name = "Patio"
    mock_item.created_at = datetime(2024, 6, 1, tzinfo=UTC)
    mock_item.size = 1024

    async def _failing_prepare(blink):
        raise RuntimeError("sync module offline")

    mock_item.prepare_download = _failing_prepare

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m5"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()

    assert results == []
    assert not dl._tracker.is_downloaded("local_4246")


async def test_download_local_storage_success_but_no_file_written(dl, tmp_path):
    """blinkpy reports download_video() as succeeded (True), but the target
    .tmp file never actually appeared on disk — must be logged and skipped,
    not treated as a completed download (which os.replace() would then
    raise FileNotFoundError trying to finish)."""
    mock_item = MagicMock()
    mock_item.id = 9911
    mock_item.name = "Garage"
    mock_item.created_at = datetime(2024, 6, 4, tzinfo=UTC)
    mock_item.size = 2048
    mock_item.prepare_download = AsyncMock(return_value=True)
    mock_item.download_video = AsyncMock(return_value=True)

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m9"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()

    assert results == []
    assert not dl._tracker.is_downloaded("local_9911")
    dest = dl._storage.resolve_path(
        mock_item.name, mock_item.created_at, f"local_{mock_item.id}"
    )
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


async def test_download_local_storage_writes_to_db_when_configured(dl, tmp_path):
    """When a ClipDatabase is attached, a successfully downloaded
    local-storage clip is persisted to it."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 4246
    mock_item.name = "Patio"
    mock_item.created_at = datetime(2024, 6, 1, tzinfo=UTC)
    mock_item.size = 1024
    mock_item.prepare_download = AsyncMock(return_value=True)

    async def _fake_download(blink, file_name, max_retries=4):
        _Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        _Path(file_name).write_bytes(b"V" * 50)
        return True

    mock_item.download_video = _fake_download

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m6"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = AsyncMock()

    results = await dl.download_local_storage_clips()

    assert len(results) == 1
    dl._db.add_clip.assert_awaited_once_with(results[0])


async def test_download_local_storage_downloads_new_clip(dl, tmp_path):
    """Successfully downloads a new clip from USB local storage."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 5555
    mock_item.name = "Front Door"
    mock_item.created_at = datetime(2024, 6, 1, 8, 0, tzinfo=UTC)
    mock_item.size = 2_000_000
    mock_item.prepare_download = AsyncMock(return_value=True)

    # Simulate download_video writing a file to dest and returning True.
    async def _fake_download(blink, file_name, max_retries=4):
        _Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        _Path(file_name).write_bytes(b"V" * 100)
        return True

    mock_item.download_video = _fake_download

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m99"}

    mock_blink = MagicMock()
    mock_blink.sync = {"Network": mock_sync}
    dl._blink = mock_blink
    dl._db = None  # skip DB write

    results = await dl.download_local_storage_clips()

    assert len(results) == 1
    r = results[0]
    assert r["id"] == "local_5555"
    assert r["camera"] == "Front Door"
    assert r["source"] == "local_storage"
    assert dl._tracker.is_downloaded("local_5555")


async def test_download_local_storage_persists_tracker(dl, tmp_path):
    """A crash right after this call must not lose the downloaded-clip record —
    download_local_storage_clips() must flush the tracker to disk itself,
    the same way download_new_clips() does, rather than relying solely on a
    clean-shutdown save() that a container OOM-kill/SIGKILL would skip."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 8888
    mock_item.name = "Driveway"
    mock_item.created_at = datetime(2024, 6, 1, 8, 0, tzinfo=UTC)
    mock_item.size = 2_000_000
    mock_item.prepare_download = AsyncMock(return_value=True)

    async def _fake_download(blink, file_name, max_retries=4):
        _Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        _Path(file_name).write_bytes(b"V" * 100)
        return True

    mock_item.download_video = _fake_download

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m1"}

    mock_blink = MagicMock()
    mock_blink.sync = {"Network": mock_sync}
    dl._blink = mock_blink
    dl._db = None

    await dl.download_local_storage_clips()

    reloaded = ClipTracker(tmp_path / "tracker.json")
    assert reloaded.is_downloaded("local_8888")


async def test_download_local_storage_over_quota_persists_tracker(dl, tmp_path):
    """Stopping early for a quota breach must still flush whatever was
    downloaded before the breach was detected, not just clips downloaded
    to completion — otherwise a later clip in the same manifest hitting
    quota loses the tracker record for an earlier clip in this run."""
    mock_item = MagicMock()
    mock_item.id = 9999
    mock_item.name = "Garage"
    mock_item.created_at = datetime(2024, 6, 1, 8, 0, tzinfo=UTC)
    mock_item.size = 1024

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "m1"}

    mock_blink = MagicMock()
    mock_blink.sync = {"Network": mock_sync}
    dl._blink = mock_blink
    dl._storage.is_over_quota = MagicMock(return_value=True)
    dl._tracker.mark_downloaded("local_1111")

    results = await dl.download_local_storage_clips()
    assert results == []

    reloaded = ClipTracker(tmp_path / "tracker.json")
    assert reloaded.is_downloaded("local_1111")


async def test_download_local_storage_download_failure_skipped(dl, tmp_path):
    """A failed download_video call is logged and skipped, not raised."""

    mock_item = MagicMock()
    mock_item.id = 6666
    mock_item.name = "Backyard"
    mock_item.created_at = datetime(2024, 6, 2, tzinfo=UTC)
    mock_item.size = 500_000
    mock_item.prepare_download = AsyncMock(return_value=True)
    mock_item.download_video = AsyncMock(return_value=False)  # download fails

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "mx"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()
    assert results == []
    assert not dl._tracker.is_downloaded("local_6666")


async def test_download_local_storage_download_failure_cleans_up_partial_file(
    dl, tmp_path
):
    """A failed download_video call must remove any partial file it left
    behind on disk, not just skip marking the clip downloaded."""

    mock_item = MagicMock()
    mock_item.id = 6667
    mock_item.name = "Backyard"
    mock_item.created_at = datetime(2024, 6, 2, tzinfo=UTC)
    mock_item.size = 500_000

    dest = dl._storage.resolve_path(
        mock_item.name, mock_item.created_at, f"local_{mock_item.id}"
    )
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")

    async def _fake_prepare(blink):
        # Simulate a partial file at the .tmp write target (see
        # _download_local_storage_item's atomic-rename-on-success design).
        tmp_dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest.write_bytes(b"partial")

    mock_item.prepare_download = _fake_prepare
    mock_item.download_video = AsyncMock(return_value=False)  # download fails

    mock_sync = MagicMock()
    mock_sync.local_storage = True
    mock_sync.update_local_storage_manifest = AsyncMock()
    mock_sync._local_storage = {"manifest": {mock_item}, "last_manifest_id": "mx2"}

    dl._blink = MagicMock()
    dl._blink.sync = {"Network": mock_sync}
    dl._db = None

    results = await dl.download_local_storage_clips()
    assert results == []
    assert not dl._tracker.is_downloaded("local_6667")
    assert not dest.exists()
    assert not tmp_dest.exists()


# ---------------------------------------------------------------------------
# probe_clip_duration
# ---------------------------------------------------------------------------


async def test_probe_clip_duration_returns_zero_when_ffprobe_missing(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("ffprobe not found")),
    ):
        assert await probe_clip_duration(video) == 0


async def test_probe_clip_duration_returns_zero_on_timeout(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.kill = lambda: None
    mock_proc.wait = AsyncMock()
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        assert await probe_clip_duration(video) == 0


async def test_probe_clip_duration_returns_zero_on_unparseable_output(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"N/A\n", b""))
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        assert await probe_clip_duration(video) == 0


async def test_probe_clip_duration_parses_and_truncates(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"12.7\n", b""))
    with patch(
        "blink_downloader.downloader.asyncio.create_subprocess_exec",
        AsyncMock(return_value=mock_proc),
    ):
        assert await probe_clip_duration(video) == 12


# ---------------------------------------------------------------------------
# get_camera / list_camera_names (Live View)
# ---------------------------------------------------------------------------


def test_get_camera_returns_none_before_connect(dl: BlinkDownloader) -> None:
    assert dl.get_camera("Front Door") is None


def test_list_camera_names_empty_before_connect(dl: BlinkDownloader) -> None:
    assert dl.list_camera_names() == []


def test_get_camera_returns_matching_camera(dl: BlinkDownloader) -> None:
    camera = MagicMock()
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    assert dl.get_camera("Front Door") is camera


def test_get_camera_is_case_insensitive(dl: BlinkDownloader) -> None:
    camera = MagicMock()
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    assert dl.get_camera("front door") is camera
    assert dl.get_camera("FRONT DOOR") is camera


def test_get_camera_returns_none_for_unknown_name(dl: BlinkDownloader) -> None:
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": MagicMock()})
    dl._blink = fake_blink

    assert dl.get_camera("Backyard") is None


def test_list_camera_names_returns_all_connected_cameras(dl: BlinkDownloader) -> None:
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict(
        {"Front Door": MagicMock(), "Backyard": MagicMock()}
    )
    dl._blink = fake_blink

    assert set(dl.list_camera_names()) == {"Front Door", "Backyard"}


# ---------------------------------------------------------------------------
# get_camera_snapshot (Security Feed)
# ---------------------------------------------------------------------------


async def test_get_camera_snapshot_returns_none_for_unknown_camera(
    dl: BlinkDownloader,
) -> None:
    assert await dl.get_camera_snapshot("Front Door") is None


async def test_get_camera_snapshot_prefers_cached_image(dl: BlinkDownloader) -> None:
    """No network call at all when a cached image is already available —
    the whole point of not calling snap_picture()."""
    camera = MagicMock()
    camera.image_from_cache = b"cached-jpeg-bytes"
    camera.get_thumbnail = AsyncMock()
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    result = await dl.get_camera_snapshot("Front Door")

    assert result == b"cached-jpeg-bytes"
    camera.get_thumbnail.assert_not_awaited()


async def test_get_camera_snapshot_falls_back_to_downloading_thumbnail(
    dl: BlinkDownloader,
) -> None:
    camera = MagicMock()
    camera.image_from_cache = None
    response = MagicMock()
    response.status = 200
    response.read = AsyncMock(return_value=b"downloaded-jpeg-bytes")
    camera.get_thumbnail = AsyncMock(return_value=response)
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    result = await dl.get_camera_snapshot("Front Door")

    assert result == b"downloaded-jpeg-bytes"
    camera.get_thumbnail.assert_awaited_once()


async def test_get_camera_snapshot_no_thumbnail_url_available(
    dl: BlinkDownloader,
) -> None:
    """camera.get_thumbnail() itself returns None when self.thumbnail is
    unset (blinkpy logs a warning and returns early)."""
    camera = MagicMock()
    camera.image_from_cache = None
    camera.get_thumbnail = AsyncMock(return_value=None)
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    assert await dl.get_camera_snapshot("Front Door") is None


async def test_get_camera_snapshot_download_http_error(dl: BlinkDownloader) -> None:
    camera = MagicMock()
    camera.image_from_cache = None
    response = MagicMock()
    response.status = 404
    camera.get_thumbnail = AsyncMock(return_value=response)
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    assert await dl.get_camera_snapshot("Front Door") is None


async def test_get_camera_snapshot_download_exception_is_caught(
    dl: BlinkDownloader, caplog: pytest.LogCaptureFixture
) -> None:
    camera = MagicMock()
    camera.image_from_cache = None
    camera.get_thumbnail = AsyncMock(side_effect=RuntimeError("network exploded"))
    fake_blink = MagicMock()
    fake_blink.cameras = CaseInsensitiveDict({"Front Door": camera})
    dl._blink = fake_blink

    with caplog.at_level("WARNING"):
        result = await dl.get_camera_snapshot("Front Door")

    assert result is None
    assert "network exploded" in caplog.text


# ---------------------------------------------------------------------------
# refresh_camera_state
# ---------------------------------------------------------------------------


async def test_refresh_camera_state_returns_false_before_connect(
    dl: BlinkDownloader,
) -> None:
    assert dl._blink is None
    assert await dl.refresh_camera_state() is False


async def test_refresh_camera_state_calls_blink_refresh(dl: BlinkDownloader) -> None:
    fake_blink = MagicMock()
    fake_blink.refresh = AsyncMock(return_value=True)
    dl._blink = fake_blink

    result = await dl.refresh_camera_state()

    assert result is True
    fake_blink.refresh.assert_awaited_once_with()


async def test_refresh_camera_state_returns_false_when_blink_declines(
    dl: BlinkDownloader,
) -> None:
    """blinkpy's own Throttle/refresh_rate can make refresh() a no-op that
    returns False — not an error, just "too soon since the last one"."""
    fake_blink = MagicMock()
    fake_blink.refresh = AsyncMock(return_value=False)
    dl._blink = fake_blink

    assert await dl.refresh_camera_state() is False


async def test_refresh_camera_state_swallows_non_auth_fatal_exceptions(
    dl: BlinkDownloader, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient failure here must not block the rest of the poll cycle
    (clip downloading, archiving, ...) — only report it and move on."""
    fake_blink = MagicMock()
    fake_blink.refresh = AsyncMock(side_effect=ConnectionResetError("reset"))
    dl._blink = fake_blink

    with caplog.at_level("WARNING"):
        result = await dl.refresh_camera_state()

    assert result is False
    assert "reset" in caplog.text


@pytest.mark.parametrize(
    "exc",
    [
        TokenRefreshFailed("refresh failed"),
        LoginError("login failed"),
        UnauthorizedError("unauthorized"),
    ],
    ids=["TokenRefreshFailed", "LoginError", "UnauthorizedError"],
)
async def test_refresh_camera_state_reraises_auth_fatal_exceptions(
    dl: BlinkDownloader, caplog: pytest.LogCaptureFixture, exc: Exception
) -> None:
    """Unlike a generic/transient failure, this means the whole session is
    broken and must propagate so app.py's poll loop can reconnect."""
    fake_blink = MagicMock()
    fake_blink.refresh = AsyncMock(side_effect=exc)
    dl._blink = fake_blink

    with caplog.at_level("WARNING"), pytest.raises(type(exc)):
        await dl.refresh_camera_state()
