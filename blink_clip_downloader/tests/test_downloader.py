"""Tests for blink_downloader.downloader."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blinkpy.auth import BlinkTwoFARequiredError, UnauthorizedError

from blink_downloader.downloader import (
    AuthenticationError,
    BlinkDownloader,
    TwoFARequired,
)
from blink_downloader.storage import StorageManager
from blink_downloader.tracker import ClipTracker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    dl._config.retry_attempts = 1
    dl._config.retry_delay = 0.0

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("boom"))
    mock_session.closed = False
    dl._session = mock_session
    dl._blink = MagicMock()
    dl._blink.auth.header = {}

    # Create partial file to simulate incomplete prior download
    dest.write_bytes(b"partial")
    result = await dl._stream_to_file("https://host/clip.mp4", dest)
    assert result is None
    assert not dest.exists()


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


async def test_download_new_clips_no_clips(dl):
    dl._blink = MagicMock()
    with patch.object(dl, "_fetch_clip_list", AsyncMock(return_value=[])):
        results = await dl.download_new_clips()
    assert results == []


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
    result = await dl._download_clip(clip, sem)

    assert result is not None
    assert result["duration"] == 0
    assert result["network_id"] == 0
    assert result["source"] == ""


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

    with patch("blink_downloader.downloader.TWO_FA_FILE", missing_file):
        with pytest.raises(TwoFARequired):
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

    with patch("blink_downloader.downloader.TWO_FA_FILE", missing_file):
        with pytest.raises(TwoFARequired):
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

    with patch("blink_downloader.downloader.TWO_FA_FILE", missing_file):
        with pytest.raises(TwoFARequired):
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

    with patch("blink_downloader.downloader.TWO_FA_FILE", missing_file):
        with pytest.raises(TwoFARequired):
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
    ):
        with pytest.raises(AuthenticationError) as excinfo:
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
    ):
        with pytest.raises(AuthenticationError) as excinfo:
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
        result = await dl._fetch_clip_list(datetime.now(timezone.utc))

    assert len(result) == 25


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
        result = await dl._fetch_clip_list(datetime.now(timezone.utc))

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
    session = await dl._get_session()
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


async def test_download_local_storage_skips_already_tracked(dl):
    """Clips already in the tracker are not re-downloaded."""
    mock_item = MagicMock()
    mock_item.id = 7777
    mock_item.name = "Garage"
    mock_item.created_at = datetime(2024, 6, 1, tzinfo=timezone.utc)
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


async def test_download_local_storage_downloads_new_clip(dl, tmp_path):
    """Successfully downloads a new clip from USB local storage."""
    from pathlib import Path as _Path

    mock_item = MagicMock()
    mock_item.id = 5555
    mock_item.name = "Front Door"
    mock_item.created_at = datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)
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


async def test_download_local_storage_download_failure_skipped(dl, tmp_path):
    """A failed download_video call is logged and skipped, not raised."""

    mock_item = MagicMock()
    mock_item.id = 6666
    mock_item.name = "Backyard"
    mock_item.created_at = datetime(2024, 6, 2, tzinfo=timezone.utc)
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
