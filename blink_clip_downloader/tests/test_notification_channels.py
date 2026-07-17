"""Tests for NotificationDispatcher."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from blink_downloader.analyzer import AnalysisResult
from blink_downloader.notification_channels import NotificationDispatcher


def _mock_session(**overrides: Any) -> MagicMock:
    s = MagicMock()
    s.closed = False
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_result(suspicious: bool = True, **kwargs: Any) -> AnalysisResult:
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


async def test_send_mobile_attaches_authorization_header() -> None:
    """Legitimate HA API calls still get the Supervisor token (security fix)."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="secret-token",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    post = MagicMock(return_value=mock_resp)
    dispatcher._session = _mock_session(post=post)

    await dispatcher.send_mobile("Alert", "Test message")

    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-token"}


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


async def test_send_test_mobile_ignores_mobile_app_enabled_false() -> None:
    """send_test_mobile works even when mobile_app_enabled is off, unlike
    send_mobile — same rationale as send_test_email."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=False,
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    ok, message = await dispatcher.send_test_mobile()
    assert ok is True
    assert "mobile_app_phone" in message


async def test_send_test_mobile_no_target() -> None:
    dispatcher = NotificationDispatcher(supervisor_token="tok", mobile_app_target="")
    ok, message = await dispatcher.send_test_mobile()
    assert ok is False
    assert "not configured" in message


async def test_send_test_mobile_no_token() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="", mobile_app_target="mobile_app_phone"
    )
    ok, message = await dispatcher.send_test_mobile()
    assert ok is False
    assert "Supervisor token" in message


async def test_send_test_mobile_failure_message() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="tok", mobile_app_target="mobile_app_phone"
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    ok, message = await dispatcher.send_test_mobile()
    assert ok is False
    assert "Failed" in message


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


async def test_send_email_port_465_uses_implicit_tls() -> None:
    """Port 465 is implicit TLS; start_tls must not be sent to that server."""
    dispatcher = NotificationDispatcher(
        smtp_enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_user="user@example.com",
        smtp_password="pass",
        smtp_recipients=["admin@example.com"],
        smtp_sender="noreply@example.com",
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        result = await dispatcher.send_email("Alert", "Body text")

    assert result is True
    call_kwargs = mock_send.call_args
    assert call_kwargs.kwargs["port"] == 465
    assert call_kwargs.kwargs["use_tls"] is True
    assert call_kwargs.kwargs["start_tls"] is False


async def test_send_email_port_587_uses_starttls() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_recipients=["admin@example.com"],
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await dispatcher.send_email("Alert", "Body text")

    call_kwargs = mock_send.call_args
    assert call_kwargs.kwargs["use_tls"] is False
    assert call_kwargs.kwargs["start_tls"] is True


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
# Test email (bypasses smtp_enabled)
# ------------------------------------------------------------------


async def test_send_test_email_ignores_smtp_enabled_false() -> None:
    """send_test_email works even when smtp_enabled is off, unlike send_email."""
    dispatcher = NotificationDispatcher(
        smtp_enabled=False,
        smtp_host="smtp.example.com",
        smtp_recipients=["admin@example.com"],
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        ok, message = await dispatcher.send_test_email()
    assert ok is True
    assert "admin@example.com" in message
    mock_send.assert_awaited_once()


async def test_send_test_email_no_host() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True, smtp_host="", smtp_recipients=["a@b.com"]
    )
    ok, message = await dispatcher.send_test_email()
    assert ok is False
    assert "host" in message.lower()


async def test_send_test_email_no_recipients() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True, smtp_host="smtp.example.com", smtp_recipients=[]
    )
    ok, message = await dispatcher.send_test_email()
    assert ok is False
    assert "recipient" in message.lower()


async def test_send_test_email_failure_message() -> None:
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
        ok, message = await dispatcher.send_test_email()
    assert ok is False
    assert "log" in message.lower()


def test_smtp_configured_true() -> None:
    dispatcher = NotificationDispatcher(
        smtp_host="smtp.example.com", smtp_recipients=["a@b.com"]
    )
    assert dispatcher.smtp_configured is True


def test_smtp_configured_false_without_recipients() -> None:
    dispatcher = NotificationDispatcher(smtp_host="smtp.example.com")
    assert dispatcher.smtp_configured is False


def test_smtp_configured_false_without_host() -> None:
    dispatcher = NotificationDispatcher(smtp_recipients=["a@b.com"])
    assert dispatcher.smtp_configured is False


# ------------------------------------------------------------------
# Discord Webhook
# ------------------------------------------------------------------


async def test_send_discord_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="secret-token",
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await dispatcher.send_discord("Alert", "Body", "Front Door", 0.85)
    assert result is True

    call_kwargs = dispatcher._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["embeds"][0]["color"] == 0xFF0000  # high confidence = red
    # Security: the Supervisor token must never be sent to a third-party
    # Discord webhook URL.
    assert "headers" not in call_kwargs.kwargs


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


async def test_send_test_discord_ignores_discord_enabled_false() -> None:
    """send_test_discord works even when discord_enabled is off, unlike
    send_discord — same rationale as send_test_email."""
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=False,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    ok, message = await dispatcher.send_test_discord()
    assert ok is True
    assert "Discord" in message


async def test_send_test_discord_no_url() -> None:
    dispatcher = NotificationDispatcher(discord_enabled=True, discord_webhook_url="")
    ok, message = await dispatcher.send_test_discord()
    assert ok is False
    assert "not configured" in message


async def test_send_test_discord_failure_message() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    ok, message = await dispatcher.send_test_discord()
    assert ok is False
    assert "Failed" in message


# ------------------------------------------------------------------
# Home Assistant Notification (suspicious activity only — distinct
# from HANotifier/notify_ha, which covers new-clip-downloaded, the
# digest, and system events)
# ------------------------------------------------------------------


async def test_send_ha_notification_success() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        ha_notify_enabled=True,
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await dispatcher.send_ha_notification("Alert", "Test message")
    assert result is True


async def test_send_ha_notification_attaches_authorization_header() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        supervisor_token="secret-token",
        ha_notify_enabled=True,
    )
    post = MagicMock(return_value=mock_resp)
    dispatcher._session = _mock_session(post=post)

    await dispatcher.send_ha_notification("Alert", "Test message")

    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-token"}


async def test_send_ha_notification_disabled() -> None:
    dispatcher = NotificationDispatcher(supervisor_token="tok", ha_notify_enabled=False)
    assert await dispatcher.send_ha_notification("Alert", "Test") is False


async def test_send_ha_notification_no_token() -> None:
    dispatcher = NotificationDispatcher(supervisor_token="", ha_notify_enabled=True)
    assert await dispatcher.send_ha_notification("Alert", "Test") is False


async def test_send_ha_notification_http_error() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(supervisor_token="tok", ha_notify_enabled=True)
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    assert await dispatcher.send_ha_notification("Alert", "Test") is False


async def test_send_ha_notification_network_error_returns_false() -> None:
    dispatcher = NotificationDispatcher(supervisor_token="tok", ha_notify_enabled=True)
    dispatcher._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientError("down"))
    )
    assert await dispatcher.send_ha_notification("Alert", "Test") is False


async def test_send_test_ha_notification_ignores_ha_notify_enabled_false() -> None:
    """send_test_ha_notification works even when ha_notify_enabled is off,
    unlike send_ha_notification — same rationale as send_test_email."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(supervisor_token="tok", ha_notify_enabled=False)
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    ok, message = await dispatcher.send_test_ha_notification()
    assert ok is True
    assert "Home Assistant" in message


async def test_send_test_ha_notification_no_token() -> None:
    dispatcher = NotificationDispatcher(supervisor_token="")
    ok, message = await dispatcher.send_test_ha_notification()
    assert ok is False
    assert "Supervisor token" in message


async def test_send_test_ha_notification_failure_message() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(supervisor_token="tok")
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    ok, message = await dispatcher.send_test_ha_notification()
    assert ok is False
    assert "Failed" in message


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
        ha_notify_enabled=True,
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)
    dispatcher.send_email = AsyncMock(return_value=True)
    dispatcher.send_discord = AsyncMock(return_value=True)
    dispatcher.send_ha_notification = AsyncMock(return_value=True)

    result = _make_result(suspicious=True)
    clip = {"id": "c1", "camera": "Front Door", "path": "/c1.mp4"}

    await dispatcher.dispatch(result, clip)

    dispatcher.send_mobile.assert_awaited_once()
    dispatcher.send_email.assert_awaited_once()
    dispatcher.send_discord.assert_awaited_once()
    dispatcher.send_ha_notification.assert_awaited_once()


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
        ha_notify_enabled=False,
    )
    dispatcher.send_mobile = AsyncMock()
    dispatcher.send_email = AsyncMock()
    dispatcher.send_discord = AsyncMock(return_value=True)
    dispatcher.send_ha_notification = AsyncMock()

    result = _make_result(suspicious=True)
    await dispatcher.dispatch(result, {"id": "c1", "camera": "A"})

    dispatcher.send_mobile.assert_not_awaited()
    dispatcher.send_email.assert_not_awaited()
    dispatcher.send_discord.assert_awaited_once()
    dispatcher.send_ha_notification.assert_not_awaited()


# ------------------------------------------------------------------
# Coverage gap tests
# ------------------------------------------------------------------


async def test_close_closes_open_session() -> None:
    """close() closes the session when one exists and is open (lines 58-59)."""
    dispatcher = NotificationDispatcher()
    mock_session = AsyncMock()
    mock_session.closed = False
    dispatcher._session = mock_session

    await dispatcher.close()
    mock_session.close.assert_awaited_once()


async def test_get_session_creates_when_none() -> None:
    """_get_session() creates a new ClientSession when _session is None (line 63)."""
    dispatcher = NotificationDispatcher(supervisor_token="tok")
    mock_session = MagicMock()
    mock_session.closed = False

    with patch(
        "blink_downloader.notification_channels.aiohttp.ClientSession",
        return_value=mock_session,
    ):
        session = dispatcher._get_session()

    assert session is mock_session


async def test_send_mobile_network_error_returns_false() -> None:
    """aiohttp.ClientError from the HTTP call is caught and returns False (lines 113-115)."""
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientError("timeout"))
    )

    result = await dispatcher.send_mobile("Alert", "msg")
    assert result is False


async def test_send_discord_http_error_returns_false() -> None:
    """HTTP status >= 400 from Discord webhook returns False (lines 197-198)."""
    mock_resp = AsyncMock()
    mock_resp.status = 429
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await dispatcher.send_discord("Alert", "Body")
    assert result is False


async def test_send_discord_network_error_returns_false() -> None:
    """aiohttp.ClientError from Discord webhook call is caught and returns False (lines 199-201)."""
    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientError("connection refused"))
    )

    result = await dispatcher.send_discord("Alert", "Body")
    assert result is False
