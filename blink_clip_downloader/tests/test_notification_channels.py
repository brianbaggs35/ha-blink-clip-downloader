"""Tests for NotificationDispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from blink_downloader.analyzer import AnalysisResult
from blink_downloader.notification_channels import NotificationDispatcher


def _mock_session(**overrides: object) -> MagicMock:
    s = MagicMock()
    s.closed = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_result(suspicious: bool = True, **kwargs: object) -> AnalysisResult:
    return AnalysisResult(
        clip_id=str(kwargs.get("clip_id", "c1")),
        camera=str(kwargs.get("camera", "Front Door")),
        model="llava",
        response_text="test",
        is_suspicious=suspicious,
        confidence=float(kwargs.get("confidence", 0.9)),
        summary=str(kwargs.get("summary", "Person detected")),
        frame_count=3,
        analysis_duration=2.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )


# ------------------------------------------------------------------
# Mobile App
# ------------------------------------------------------------------


async def test_send_mobile_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await dispatcher.send_mobile("Alert", "Test message")
    assert result is True


async def test_send_mobile_disabled() -> None:
    dispatcher = NotificationDispatcher(mobile_app_enabled=False)
    assert await dispatcher.send_mobile("Alert", "Test") is False


async def test_send_mobile_no_target() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_enabled=True,
        mobile_app_target="",
    )
    assert await dispatcher.send_mobile("Alert", "Test") is False


async def test_send_mobile_http_error() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    assert await dispatcher.send_mobile("Alert", "Test") is False


# ------------------------------------------------------------------
# Email (SMTP)
# ------------------------------------------------------------------


async def test_send_email_success() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="pass",
        smtp_recipients=["admin@example.com"],
        smtp_sender="noreply@example.com",
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        result = await dispatcher.send_email("Alert", "Body text")

    assert result is True
    mock_send.assert_awaited_once()
    call_kwargs = mock_send.call_args
    assert call_kwargs.kwargs["hostname"] == "smtp.example.com"
    assert call_kwargs.kwargs["port"] == 587


async def test_send_email_disabled() -> None:
    dispatcher = NotificationDispatcher(smtp_enabled=False)
    assert await dispatcher.send_email("Alert", "Body") is False


async def test_send_email_no_host() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True, smtp_host="", smtp_recipients=["a@b.com"]
    )
    assert await dispatcher.send_email("Alert", "Body") is False


async def test_send_email_connection_error() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True,
        smtp_host="bad.host",
        smtp_recipients=["a@b.com"],
    )
    with patch(
        "aiosmtplib.send",
        new_callable=AsyncMock,
        side_effect=OSError("Connection refused"),
    ):
        result = await dispatcher.send_email("Alert", "Body")
    assert result is False


# ------------------------------------------------------------------
# Discord Webhook
# ------------------------------------------------------------------


async def test_send_discord_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await dispatcher.send_discord("Alert", "Body", "Front Door", 0.85)
    assert result is True

    call_kwargs = dispatcher._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["embeds"][0]["color"] == 0xFF0000  # high confidence = red


async def test_send_discord_disabled() -> None:
    dispatcher = NotificationDispatcher(discord_enabled=False)
    assert await dispatcher.send_discord("Alert", "Body") is False


async def test_send_discord_no_url() -> None:
    dispatcher = NotificationDispatcher(discord_enabled=True, discord_webhook_url="")
    assert await dispatcher.send_discord("Alert", "Body") is False


async def test_send_discord_low_confidence_color() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    await dispatcher.send_discord("Alert", "Body", "Back", 0.3)
    call_kwargs = dispatcher._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["embeds"][0]["color"] == 0xFF8C00  # low confidence = orange


# ------------------------------------------------------------------
# Dispatch (orchestration)
# ------------------------------------------------------------------


async def test_dispatch_calls_all_enabled_channels() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_enabled=True,
        mobile_app_target="mobile_app_phone",
        smtp_enabled=True,
        smtp_host="smtp.test.com",
        smtp_recipients=["a@b.com"],
        discord_enabled=True,
        discord_webhook_url="https://discord.com/hook",
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)
    dispatcher.send_email = AsyncMock(return_value=True)
    dispatcher.send_discord = AsyncMock(return_value=True)

    result = _make_result(suspicious=True)
    clip = {"id": "c1", "camera": "Front Door", "path": "/c1.mp4"}

    await dispatcher.dispatch(result, clip)

    dispatcher.send_mobile.assert_awaited_once()
    dispatcher.send_email.assert_awaited_once()
    dispatcher.send_discord.assert_awaited_once()


async def test_dispatch_skips_non_suspicious() -> None:
    dispatcher = NotificationDispatcher(
        mobile_app_enabled=True,
        mobile_app_target="phone",
        supervisor_token="tok",
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)

    result = _make_result(suspicious=False)
    clip = {"id": "c1", "camera": "Front Door"}

    await dispatcher.dispatch(result, clip)
    dispatcher.send_mobile.assert_not_awaited()


async def test_dispatch_only_enabled_channels() -> None:
    dispatcher = NotificationDispatcher(
        mobile_app_enabled=False,
        smtp_enabled=False,
        discord_enabled=True,
        discord_webhook_url="https://discord.com/hook",
    )
    dispatcher.send_mobile = AsyncMock()
    dispatcher.send_email = AsyncMock()
    dispatcher.send_discord = AsyncMock(return_value=True)

    result = _make_result(suspicious=True)
    await dispatcher.dispatch(result, {"id": "c1", "camera": "A"})

    dispatcher.send_mobile.assert_not_awaited()
    dispatcher.send_email.assert_not_awaited()
    dispatcher.send_discord.assert_awaited_once()
