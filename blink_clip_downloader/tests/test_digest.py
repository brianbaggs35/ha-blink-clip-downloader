"""Tests for DailyDigest."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from blink_downloader.digest import DailyDigest


def _make_digest(
    tmp_path: Path,
    digest_time: str = "08:00",
    enabled: bool = True,
) -> tuple[DailyDigest, MagicMock, MagicMock]:
    notifier = MagicMock()
    notifier.notify = AsyncMock()

    db = MagicMock()
    db.get_stats = AsyncMock(
        return_value={
            "today_count": 5,
            "total_count": 42,
            "total_size_bytes": 100_000_000,
            "starred_count": 3,
        }
    )
    db.get_camera_stats = AsyncMock(
        return_value=[
            {"camera": "Front Door", "today": 3, "this_week": 10},
            {"camera": "Back Yard", "today": 2, "this_week": 8},
        ]
    )

    digest = DailyDigest(
        notifier=notifier,
        db=db,
        digest_time=digest_time,
        enabled=enabled,
        last_digest_file=tmp_path / "last_digest.json",
    )
    return digest, notifier, db


# ------------------------------------------------------------------
# check_and_send logic
# ------------------------------------------------------------------


async def test_check_and_send_disabled_does_nothing(tmp_path: Path) -> None:
    digest, notifier, _ = _make_digest(tmp_path, enabled=False)
    await digest.check_and_send()
    notifier.notify.assert_not_awaited()


async def test_check_and_send_already_sent_today(tmp_path: Path) -> None:
    digest, notifier, _ = _make_digest(tmp_path)
    digest._last_sent = date.today()  # noqa: DTZ011
    await digest.check_and_send()
    notifier.notify.assert_not_awaited()


async def test_check_and_send_not_yet_time(tmp_path: Path) -> None:
    # Use 23:59 so it's never time during tests
    digest, notifier, _ = _make_digest(tmp_path, digest_time="23:59")
    # Patch datetime.now to return midnight
    fake_now = datetime.now().replace(hour=0, minute=0)  # noqa: DTZ005
    with patch("blink_downloader.digest.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.today = datetime.today
        await digest.check_and_send()
    notifier.notify.assert_not_awaited()


async def test_check_and_send_fires_when_due(tmp_path: Path) -> None:
    digest, notifier, _ = _make_digest(tmp_path, digest_time="08:00")
    fake_now = datetime.now().replace(hour=9, minute=0)  # noqa: DTZ005
    with patch("blink_downloader.digest.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.today = datetime.today
        await digest.check_and_send()
    notifier.notify.assert_awaited_once()


async def test_check_and_send_persists_last_sent(tmp_path: Path) -> None:
    state_file = tmp_path / "last_digest.json"
    digest, _notifier, _ = _make_digest(tmp_path, digest_time="08:00")
    fake_now = datetime.now().replace(hour=9, minute=0)  # noqa: DTZ005
    with patch("blink_downloader.digest.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.today = datetime.today
        await digest.check_and_send()
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["last_sent"] == date.today().isoformat()  # noqa: DTZ011


async def test_check_and_send_invalid_time_skips(tmp_path: Path) -> None:
    digest, notifier, _ = _make_digest(tmp_path, digest_time="not-a-time")
    await digest.check_and_send()
    notifier.notify.assert_not_awaited()


# ------------------------------------------------------------------
# send
# ------------------------------------------------------------------


async def test_send_formats_message(tmp_path: Path) -> None:
    digest, notifier, _ = _make_digest(tmp_path)
    await digest.send()

    notifier.notify.assert_awaited_once()
    call_kwargs = notifier.notify.call_args
    message = (
        call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("message", "")
    )
    # Verify key stats appear in the message
    assert "5" in message or "42" in message
    assert "Blink Daily Digest" in (call_kwargs.kwargs.get("title", "") or "")


async def test_send_includes_camera_breakdown(tmp_path: Path) -> None:
    digest, notifier, _ = _make_digest(tmp_path)
    await digest.send()
    message = notifier.notify.call_args[0][0]
    assert "Front Door" in message
    assert "Back Yard" in message


# ------------------------------------------------------------------
# State file persistence
# ------------------------------------------------------------------


def test_load_last_sent_missing_file(tmp_path: Path) -> None:
    digest, _, _ = _make_digest(tmp_path)
    assert digest._last_sent is None


def test_load_last_sent_valid_file(tmp_path: Path) -> None:
    state_file = tmp_path / "last_digest.json"
    state_file.write_text(json.dumps({"last_sent": "2024-06-01"}))
    digest, _, _ = _make_digest(tmp_path)
    assert digest._last_sent == date(2024, 6, 1)


def test_load_last_sent_corrupt_file(tmp_path: Path) -> None:
    state_file = tmp_path / "last_digest.json"
    state_file.write_text("not json!!!")
    digest, _, _ = _make_digest(tmp_path)
    assert digest._last_sent is None


def test_load_last_sent_unreadable_file_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An existing-but-unreadable file must not crash the app at __init__ —
    DailyDigest() is constructed directly in BlinkClipDownloaderApp.__init__
    with no surrounding try/except, so an uncaught OSError here would take
    down the whole add-on at startup."""
    state_file = tmp_path / "last_digest.json"
    state_file.write_text(json.dumps({"last_sent": "2024-06-01"}))
    with (
        patch("pathlib.Path.read_text", side_effect=OSError("permission denied")),
        caplog.at_level(logging.WARNING),
    ):
        digest, _, _ = _make_digest(tmp_path)
    assert digest._last_sent is None
    assert "corrupt or unreadable" in caplog.text


def test_load_last_sent_wrong_shape_json(tmp_path: Path) -> None:
    """Valid JSON that isn't the expected object shape (e.g. a bare list)
    must not raise — it should be treated the same as any other corrupt file."""
    state_file = tmp_path / "last_digest.json"
    state_file.write_text(json.dumps([1, 2, 3]))
    digest, _, _ = _make_digest(tmp_path)
    assert digest._last_sent is None


# ------------------------------------------------------------------
# Coverage gap tests
# ------------------------------------------------------------------


def test_save_last_sent_none_is_noop(tmp_path: Path) -> None:
    """_save_last_sent() returns immediately when _last_sent is None (line 111)."""
    state_file = tmp_path / "last_digest.json"
    digest, _, _ = _make_digest(tmp_path)
    digest._last_sent = None
    digest._save_last_sent()
    assert not state_file.exists()


def test_save_last_sent_oserror_is_logged(tmp_path: Path) -> None:
    """OSError writing state file is caught and logged (lines 115-116)."""
    from datetime import date
    from unittest.mock import patch

    digest, _, _ = _make_digest(tmp_path)
    digest._last_sent = date.today()  # noqa: DTZ011

    with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
        digest._save_last_sent()  # must not raise
