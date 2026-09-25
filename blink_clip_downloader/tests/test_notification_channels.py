"""Tests for NotificationDispatcher."""

from __future__ import annotations

import email
import email.policy
import json
from email.message import EmailMessage
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from blink_downloader.analyzer import AnalysisResult
from blink_downloader.notification_channels import NotificationDispatcher
from blink_downloader.rich_alerts import AlertExtras


def _sent_email(send: AsyncMock) -> EmailMessage:
    """Parse the message a mocked aiosmtplib.send was handed."""
    raw = bytes(send.call_args.args[0])
    return cast(
        EmailMessage, email.message_from_bytes(raw, policy=email.policy.default)
    )


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
        risk_score=float(kwargs.get("risk_score", 0.0)),
        severity=str(kwargs.get("severity", "routine")),
        risk_override_applied=bool(kwargs.get("risk_override_applied", False)),
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


async def test_discord_alert_carries_the_deterministic_assessment() -> None:
    """The mobile/email/HA bodies already say this; an alert reporting only
    the model's own confidence hides half of why it fired — and in the
    override case, the half that fired it."""
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    await dispatcher.dispatch(
        _make_result(risk_score=88.0, severity="critical", risk_override_applied=True),
        {"id": "c1", "camera": "Driveway", "path": "/clips/c1.mp4"},
    )
    payload = dispatcher._session.post.call_args.kwargs["json"]
    fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
    assert fields["Risk"] == "88/100 (critical)"
    assert "not by the AI model" in fields["Why"]


async def test_discord_alert_reports_a_risk_score_with_no_severity_name() -> None:
    """A clip can carry a score without a severity label (an older row, or
    an assessment that scored but never crossed a named band) -- the number
    is still worth showing, without an empty "()" after it."""
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    await dispatcher.dispatch(
        _make_result(risk_score=42.0, severity=""),
        {"id": "c1", "camera": "Driveway", "path": "/clips/c1.mp4"},
    )
    payload = dispatcher._session.post.call_args.kwargs["json"]
    fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
    assert fields["Risk"] == "42/100"


async def test_discord_alert_omits_risk_when_there_is_none() -> None:
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    await dispatcher.dispatch(
        _make_result(), {"id": "c1", "camera": "Driveway", "path": "/clips/c1.mp4"}
    )
    payload = dispatcher._session.post.call_args.kwargs["json"]
    names = {f["name"] for f in payload["embeds"][0]["fields"]}
    assert "Risk" not in names
    assert "Why" not in names


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


async def test_dispatch_skips_discord_when_disabled() -> None:
    """Coverage: every other test above either enables Discord or leaves
    every channel at its all-disabled default -- none exercises the
    specifically-disabled branch with the other channels on."""
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_enabled=True,
        mobile_app_target="phone",
        discord_enabled=False,
        ha_notify_enabled=True,
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)
    dispatcher.send_discord = AsyncMock()
    dispatcher.send_ha_notification = AsyncMock(return_value=True)

    result = _make_result(suspicious=True)
    await dispatcher.dispatch(result, {"id": "c1", "camera": "A"})

    dispatcher.send_discord.assert_not_awaited()
    dispatcher.send_mobile.assert_awaited_once()
    dispatcher.send_ha_notification.assert_awaited_once()


# ------------------------------------------------------------------
# Dispatch battery alert
# ------------------------------------------------------------------


async def test_dispatch_battery_alert_calls_all_enabled_channels() -> None:
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

    await dispatcher.dispatch_battery_alert("Front Door", "low", 105)

    dispatcher.send_mobile.assert_awaited_once()
    dispatcher.send_email.assert_awaited_once()
    dispatcher.send_discord.assert_awaited_once()
    dispatcher.send_ha_notification.assert_awaited_once()
    # Discord's "Confidence" field doesn't apply to a battery event.
    assert dispatcher.send_discord.call_args.kwargs.get("confidence") is None


async def test_dispatch_battery_alert_only_enabled_channels() -> None:
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

    await dispatcher.dispatch_battery_alert("Front Door", "low")

    dispatcher.send_mobile.assert_not_awaited()
    dispatcher.send_email.assert_not_awaited()
    dispatcher.send_discord.assert_awaited_once()
    dispatcher.send_ha_notification.assert_not_awaited()


async def test_dispatch_battery_alert_body_includes_voltage_when_present() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok", mobile_app_enabled=True, mobile_app_target="phone"
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)

    await dispatcher.dispatch_battery_alert("Front Door", "low", 105)

    _, body = dispatcher.send_mobile.call_args.args
    assert "1.05V" in body
    assert "Front Door" in body
    assert "Low" in body


async def test_dispatch_battery_alert_body_omits_voltage_when_none() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok", mobile_app_enabled=True, mobile_app_target="phone"
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)

    await dispatcher.dispatch_battery_alert("Front Door", "low", None)

    _, body = dispatcher.send_mobile.call_args.args
    assert "Voltage" not in body


async def test_send_discord_confidence_none_omits_confidence_field_uses_red() -> None:
    """Battery alerts pass confidence=None — the embed should skip the
    meaningless Confidence field while still reading as high-urgency red."""
    mock_resp = AsyncMock()
    mock_resp.status = 204
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    dispatcher = NotificationDispatcher(
        discord_enabled=True, discord_webhook_url="https://discord.com/api/webhooks/1"
    )
    dispatcher._session = _mock_session(post=MagicMock(return_value=mock_resp))

    result = await dispatcher.send_discord(
        "Low Battery", "Front Door's battery is low.", "Front Door", confidence=None
    )
    assert result is True

    call_kwargs = dispatcher._session.post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    embed = payload["embeds"][0]
    assert embed["color"] == 0xFF0000
    field_names = [f["name"] for f in embed["fields"]]
    assert field_names == ["Camera"]


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


async def test_alert_body_includes_the_deterministic_assessment() -> None:
    """Two independent judgements reached this alert; reporting only the
    model's confidence hides half of why it fired."""
    dispatcher = NotificationDispatcher(ha_notify_enabled=True)
    sent: list[tuple[str, str]] = []
    with patch.object(
        NotificationDispatcher,
        "send_ha_notification",
        new=AsyncMock(side_effect=lambda t, b: sent.append((t, b))),
    ):
        await dispatcher.dispatch(
            _make_result(True, risk_score=88.0, severity="critical"),
            {"camera": "Driveway"},
        )
    assert "Risk: 88/100 (critical)" in sent[0][1]
    assert "Flagged on detection evidence" not in sent[0][1]


async def test_alert_body_says_when_the_model_did_not_raise_the_flag() -> None:
    dispatcher = NotificationDispatcher(ha_notify_enabled=True)
    sent: list[tuple[str, str]] = []
    with patch.object(
        NotificationDispatcher,
        "send_ha_notification",
        new=AsyncMock(side_effect=lambda t, b: sent.append((t, b))),
    ):
        await dispatcher.dispatch(
            _make_result(
                True,
                risk_score=88.0,
                severity="critical",
                risk_override_applied=True,
            ),
            {"camera": "Driveway"},
        )
    assert "Flagged on detection evidence, not by the AI model." in sent[0][1]


async def test_alert_body_omits_the_assessment_when_there_is_none() -> None:
    dispatcher = NotificationDispatcher(ha_notify_enabled=True)
    sent: list[tuple[str, str]] = []
    with patch.object(
        NotificationDispatcher,
        "send_ha_notification",
        new=AsyncMock(side_effect=lambda t, b: sent.append((t, b))),
    ):
        await dispatcher.dispatch(_make_result(True), {"camera": "Driveway"})
    assert "Risk:" not in sent[0][1]


# ------------------------------------------------------------------
# Rich alerts: picture, clip link, "Not a threat" and recorded time
# ------------------------------------------------------------------

_EXTRAS = AlertExtras(
    clip_id="c1",
    recorded_local="Thu 24 Sep 2026, 21:03:04 CDT",
    recorded_iso="2026-09-25T02:03:04+00:00",
    image=b"\xff\xd8JPEG\xff\xd9",
    image_url="/media/local/blink_clip_downloader/alerts/c1.jpg",
    open_path="/app/abc_blink/clip/c1",
    open_url="https://ha.example/app/abc_blink/clip/c1",
    not_a_threat_action="BLINK_NOT_A_THREAT_0123456789abcdef_c1",
)


def _status_response(status: int) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _rich_builder(extras: AlertExtras = _EXTRAS) -> MagicMock:
    builder = MagicMock()
    builder.build = AsyncMock(return_value=extras)
    builder.close = AsyncMock()
    return builder


async def test_dispatch_hands_every_channel_the_alerts_extras() -> None:
    builder = _rich_builder()
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
        rich=builder,
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)
    dispatcher.send_email = AsyncMock(return_value=True)
    dispatcher.send_discord = AsyncMock(return_value=True)
    dispatcher.send_ha_notification = AsyncMock(return_value=True)
    clip = {"id": "c1", "camera": "Front Door", "path": "/c1.mp4"}

    await dispatcher.dispatch(_make_result(), clip)

    builder.build.assert_awaited_once()
    assert builder.build.call_args.kwargs == {
        "attach_image": True,
        "phone": True,
        "external_link": True,
    }
    assert dispatcher.send_mobile.call_args.kwargs["extras"] is _EXTRAS
    assert dispatcher.send_email.call_args.kwargs["extras"] is _EXTRAS
    assert dispatcher.send_discord.call_args.kwargs["extras"] is _EXTRAS
    # The persistent notification stays text: no extras reach it.
    assert dispatcher.send_ha_notification.call_args.kwargs == {}
    body = dispatcher.send_mobile.call_args.args[1]
    assert body.endswith("Recorded: Thu 24 Sep 2026, 21:03:04 CDT")
    assert "Time:" not in body


async def test_dispatch_asks_only_for_what_the_enabled_channels_use() -> None:
    builder = _rich_builder()
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_enabled=True,
        mobile_app_target="family_group",  # a notify group, not the app
        rich=builder,
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)

    await dispatcher.dispatch(_make_result(), {"id": "c1"})

    assert builder.build.call_args.kwargs == {
        "attach_image": False,
        "phone": False,
        "external_link": False,
    }


async def test_a_failing_builder_still_sends_the_plain_alert(caplog) -> None:
    builder = _rich_builder()
    builder.build = AsyncMock(side_effect=RuntimeError("boom"))
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_enabled=True,
        mobile_app_target="mobile_app_phone",
        rich=builder,
    )
    dispatcher.send_mobile = AsyncMock(return_value=True)

    await dispatcher.dispatch(_make_result(), {"id": "c1"})

    dispatcher.send_mobile.assert_awaited_once()
    assert dispatcher.send_mobile.call_args.kwargs["extras"] == AlertExtras(
        clip_id="c1"
    )
    assert "its extras failed" in caplog.text


async def test_without_a_recorded_time_the_analysis_time_is_local() -> None:
    dispatcher = NotificationDispatcher(ha_notify_enabled=True, supervisor_token="t")
    dispatcher.send_ha_notification = AsyncMock(return_value=True)

    await dispatcher.dispatch(_make_result(), {"id": "c1"})

    body = dispatcher.send_ha_notification.call_args.args[1]
    assert "\nAnalyzed: " in body
    assert "2024" in body


async def test_an_unparseable_analysis_time_is_shown_as_it_is() -> None:
    dispatcher = NotificationDispatcher(ha_notify_enabled=True, supervisor_token="t")
    dispatcher.send_ha_notification = AsyncMock(return_value=True)
    result = _make_result()
    result.analyzed_at = "sometime"

    await dispatcher.dispatch(result, {"id": "c1"})

    assert dispatcher.send_ha_notification.call_args.args[1].endswith("Time: sometime")


async def test_close_also_closes_the_rich_builder() -> None:
    builder = _rich_builder()
    await NotificationDispatcher(rich=builder).close()
    builder.close.assert_awaited_once()


async def test_a_companion_app_push_carries_picture_tap_target_and_button() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(
        post=MagicMock(return_value=_status_response(200))
    )

    assert await dispatcher.send_mobile("Alert", "Body", extras=_EXTRAS) is True

    payload = dispatcher._session.post.call_args.kwargs["json"]
    assert payload["title"] == "Alert" and payload["message"] == "Body"
    assert payload["data"] == {
        "image": "/media/local/blink_clip_downloader/alerts/c1.jpg",
        "url": "/app/abc_blink/clip/c1",
        "clickAction": "/app/abc_blink/clip/c1",
        "actions": [
            {
                "action": "BLINK_NOT_A_THREAT_0123456789abcdef_c1",
                "title": "Not a threat",
                "authenticationRequired": True,
            }
        ],
        "tag": "blink-clip-c1",
    }


async def test_a_rejected_rich_push_is_resent_as_plain_text(caplog) -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(
        post=MagicMock(side_effect=[_status_response(400), _status_response(200)])
    )

    assert await dispatcher.send_mobile("Alert", "Body", extras=_EXTRAS) is True

    first, second = dispatcher._session.post.call_args_list
    assert "data" in first.kwargs["json"]
    assert second.kwargs["json"] == {"title": "Alert", "message": "Body"}
    assert "resending it as plain text" in caplog.text


async def test_a_rich_push_that_cannot_be_sent_at_all_is_not_retried() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(
        post=MagicMock(side_effect=aiohttp.ClientError("down"))
    )

    assert await dispatcher.send_mobile("Alert", "Body", extras=_EXTRAS) is False
    assert dispatcher._session.post.call_count == 1


async def test_a_push_to_anything_but_the_companion_app_stays_plain() -> None:
    """Another notify platform may reject a key it does not know, and a
    group would forward it to every member — so they get what they always
    got."""
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="family_phones",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(
        post=MagicMock(return_value=_status_response(200))
    )

    assert await dispatcher.send_mobile("Alert", "Body", extras=_EXTRAS) is True
    assert dispatcher._session.post.call_args.kwargs["json"] == {
        "title": "Alert",
        "message": "Body",
    }


async def test_extras_with_nothing_in_them_send_a_plain_push() -> None:
    dispatcher = NotificationDispatcher(
        supervisor_token="tok",
        mobile_app_target="mobile_app_phone",
        mobile_app_enabled=True,
    )
    dispatcher._session = _mock_session(
        post=MagicMock(return_value=_status_response(200))
    )

    await dispatcher.send_mobile("Alert", "Body", extras=AlertExtras(clip_id="c1"))

    assert "data" not in dispatcher._session.post.call_args.kwargs["json"]


def test_companion_app_data_leaves_out_what_it_lacks() -> None:
    dispatcher = NotificationDispatcher(mobile_app_target="mobile_app_phone")
    only_button = AlertExtras(not_a_threat_action="A")
    assert dispatcher._companion_app_data(only_button) == {
        "actions": [
            {"action": "A", "title": "Not a threat", "authenticationRequired": True}
        ]
    }
    assert dispatcher._companion_app_data(None) == {}


async def test_an_email_with_extras_shows_the_picture_and_links_the_clip() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True,
        smtp_host="smtp.test.com",
        smtp_recipients=["a@b.com"],
        smtp_sender="alerts@test.com",
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        assert await dispatcher.send_email(
            "Alert — Front", "Camera: <Front>", extras=_EXTRAS
        )

    sent = _sent_email(mock_send)
    assert sent["Subject"] == "Alert — Front"
    assert sent["To"] == "a@b.com"
    parts = {part.get_content_type(): part for part in sent.walk()}
    text = parts["text/plain"].get_content()
    assert "Open clip: https://ha.example/app/abc_blink/clip/c1" in text
    page = parts["text/html"].get_content()
    assert "Camera: &lt;Front&gt;" in page
    assert 'href="https://ha.example/app/abc_blink/clip/c1"' in page
    image = parts["image/jpeg"]
    assert image.get_content() == _EXTRAS.image
    assert image["Content-Disposition"].startswith("inline")
    assert f'src="cid:{image["Content-ID"][1:-1]}"' in page


async def test_an_email_with_only_a_link_has_no_image_part() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True, smtp_host="smtp.test.com", smtp_recipients=["a@b.com"]
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await dispatcher.send_email(
            "Alert", "Body", extras=AlertExtras(open_url="https://ha/x")
        )

    types = [part.get_content_type() for part in _sent_email(mock_send).walk()]
    assert "image/jpeg" not in types
    assert "text/html" in types


async def test_an_email_with_only_a_picture_has_no_link() -> None:
    dispatcher = NotificationDispatcher(
        smtp_enabled=True, smtp_host="smtp.test.com", smtp_recipients=["a@b.com"]
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await dispatcher.send_email(
            "Alert", "Body", extras=AlertExtras(image=b"\xff\xd8J\xff\xd9")
        )

    parts = {part.get_content_type(): part for part in _sent_email(mock_send).walk()}
    assert "Open clip" not in parts["text/plain"].get_content()
    assert "href=" not in parts["text/html"].get_content()
    assert "image/jpeg" in parts


async def test_an_email_with_empty_extras_stays_plain_text() -> None:
    from email.mime.text import MIMEText

    dispatcher = NotificationDispatcher(
        smtp_enabled=True, smtp_host="smtp.test.com", smtp_recipients=["a@b.com"]
    )
    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        await dispatcher.send_email("Alert", "Body", extras=AlertExtras(clip_id="c1"))

    assert isinstance(mock_send.call_args.args[0], MIMEText)


async def test_a_discord_alert_uploads_the_picture_and_links_the_clip() -> None:
    dispatcher = NotificationDispatcher(
        discord_enabled=True, discord_webhook_url="https://discord.com/hook"
    )
    dispatcher._session = _mock_session(
        post=MagicMock(return_value=_status_response(200))
    )

    assert await dispatcher.send_discord(
        "Alert", "Person", "Front Door", 0.9, extras=_EXTRAS
    )

    kwargs = dispatcher._session.post.call_args.kwargs
    assert "json" not in kwargs
    form = kwargs["data"]
    assert isinstance(form, aiohttp.FormData)
    fields = {f[0]["name"]: f for f in form._fields}
    payload = json.loads(fields["payload_json"][2])
    embed = payload["embeds"][0]
    assert embed["image"] == {"url": "attachment://keyframe.jpg"}
    assert embed["url"] == "https://ha.example/app/abc_blink/clip/c1"
    assert embed["timestamp"] == "2026-09-25T02:03:04+00:00"
    assert fields["files[0]"][0]["filename"] == "keyframe.jpg"
    assert fields["files[0]"][2] == _EXTRAS.image


async def test_a_rejected_discord_picture_is_resent_without_it(caplog) -> None:
    dispatcher = NotificationDispatcher(
        discord_enabled=True, discord_webhook_url="https://discord.com/hook"
    )
    dispatcher._session = _mock_session(
        post=MagicMock(side_effect=[_status_response(413), _status_response(204)])
    )

    assert await dispatcher.send_discord("Alert", "Person", "Cam", 0.9, extras=_EXTRAS)

    retry = dispatcher._session.post.call_args_list[1].kwargs
    embed = retry["json"]["embeds"][0]
    assert "image" not in embed
    assert embed["url"] == "https://ha.example/app/abc_blink/clip/c1"
    assert "resending without it" in caplog.text


async def test_a_discord_alert_with_a_link_but_no_picture_posts_json() -> None:
    dispatcher = NotificationDispatcher(
        discord_enabled=True, discord_webhook_url="https://discord.com/hook"
    )
    dispatcher._session = _mock_session(
        post=MagicMock(return_value=_status_response(204))
    )

    await dispatcher.send_discord(
        "Alert", "Person", "Cam", 0.9, extras=AlertExtras(open_url="https://ha/x")
    )

    embed = dispatcher._session.post.call_args.kwargs["json"]["embeds"][0]
    assert embed["url"] == "https://ha/x"
    assert "image" not in embed
