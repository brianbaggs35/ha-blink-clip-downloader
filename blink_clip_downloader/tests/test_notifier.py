"""Tests for blink_downloader.notifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp

from blink_downloader.notifier import HANotifier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_resp(status: int = 200):
    resp = AsyncMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_mock_session(resp):
    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    session.closed = False
    return session


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


async def test_notify_success():
    notifier = HANotifier("tok", enabled=True, title="Test")
    resp = _make_mock_resp(200)
    notifier._session = _make_mock_session(resp)

    result = await notifier.notify("Hello world")

    assert result is True
    notifier._session.post.assert_called_once()
    url = notifier._session.post.call_args[0][0]
    assert "persistent_notification" in url


async def test_notify_disabled_returns_false():
    notifier = HANotifier("tok", enabled=False, title="T")
    result = await notifier.notify("msg")
    assert result is False


async def test_notify_no_token_returns_false():
    notifier = HANotifier("", enabled=True, title="T")
    result = await notifier.notify("msg")
    assert result is False


async def test_notify_http_error_returns_false():
    notifier = HANotifier("tok", enabled=True, title="T")
    resp = _make_mock_resp(500)
    notifier._session = _make_mock_session(resp)
    result = await notifier.notify("msg")
    assert result is False


async def test_notify_client_error_returns_false():
    notifier = HANotifier("tok", enabled=True, title="T")
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("refused"))
    session.closed = False
    notifier._session = session
    result = await notifier.notify("msg")
    assert result is False


async def test_notify_uses_custom_title():
    notifier = HANotifier("tok", enabled=True, title="Default")
    resp = _make_mock_resp(200)
    session = _make_mock_session(resp)
    notifier._session = session

    await notifier.notify("msg", title="Override")

    payload = session.post.call_args[1]["json"]
    assert payload["title"] == "Override"


async def test_notify_falls_back_to_default_title():
    notifier = HANotifier("tok", enabled=True, title="Default Title")
    resp = _make_mock_resp(200)
    session = _make_mock_session(resp)
    notifier._session = session

    await notifier.notify("msg")

    payload = session.post.call_args[1]["json"]
    assert payload["title"] == "Default Title"


# ---------------------------------------------------------------------------
# fire_event
# ---------------------------------------------------------------------------


async def test_fire_event_success():
    notifier = HANotifier("tok", enabled=True, title="T")
    resp = _make_mock_resp(200)
    notifier._session = _make_mock_session(resp)

    result = await notifier.fire_event("blink_clip_downloaded", {"id": "123"})

    assert result is True
    url = notifier._session.post.call_args[0][0]
    assert "blink_clip_downloaded" in url


async def test_fire_event_no_token():
    notifier = HANotifier("", enabled=True, title="T")
    result = await notifier.fire_event("event", {})
    assert result is False


# ---------------------------------------------------------------------------
# update_sensor
# ---------------------------------------------------------------------------


async def test_update_sensor_success():
    notifier = HANotifier("tok", enabled=True, title="T")
    resp = _make_mock_resp(201)
    notifier._session = _make_mock_session(resp)

    result = await notifier.update_sensor(
        "sensor.blink_status", "42", {"unit": "clips"}
    )
    assert result is True


# ---------------------------------------------------------------------------
# call_webhook
# ---------------------------------------------------------------------------


async def test_call_webhook_success():
    notifier = HANotifier(
        "tok", enabled=True, title="T", webhook_url="https://hook.test/clip"
    )
    resp = _make_mock_resp(200)
    notifier._session = _make_mock_session(resp)

    result = await notifier.call_webhook({"clip_id": "1", "camera": "Front"})
    assert result is True


async def test_call_webhook_no_url():
    notifier = HANotifier("tok", enabled=True, title="T", webhook_url="")
    result = await notifier.call_webhook({"x": 1})
    assert result is False


async def test_call_webhook_http_error():
    notifier = HANotifier(
        "tok", enabled=True, title="T", webhook_url="https://bad.host/"
    )
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("refused"))
    session.closed = False
    notifier._session = session

    result = await notifier.call_webhook({})
    assert result is False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


async def test_session_reused_across_calls():
    notifier = HANotifier("tok", enabled=True, title="T")
    resp = _make_mock_resp(200)
    notifier._session = _make_mock_session(resp)

    await notifier.notify("a")
    await notifier.notify("b")
    # Both calls used the same session object
    assert notifier._session.post.call_count == 2


async def test_close_closes_session():
    notifier = HANotifier("tok", enabled=True, title="T")
    mock_session = AsyncMock()
    mock_session.closed = False
    notifier._session = mock_session

    await notifier.close()
    mock_session.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Coverage: update_sensor with no token (line 67)
# ---------------------------------------------------------------------------


async def test_update_sensor_no_token():
    notifier = HANotifier("", enabled=True, title="T")
    result = await notifier.update_sensor("sensor.blink_status", "42", {})
    assert result is False


# ---------------------------------------------------------------------------
# Security: the Supervisor token must never be sent to the user-configured
# webhook URL, only to the HA API itself.
# ---------------------------------------------------------------------------


async def test_post_attaches_authorization_header():
    notifier = HANotifier("secret-token", enabled=True, title="T")
    resp = _make_mock_resp(200)
    notifier._session = _make_mock_session(resp)

    await notifier.notify("Hello world")

    headers = notifier._session.post.call_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer secret-token"}


async def test_call_webhook_does_not_attach_authorization_header():
    notifier = HANotifier(
        "secret-token", enabled=True, title="T", webhook_url="https://hook.test/"
    )
    resp = _make_mock_resp(200)
    notifier._session = _make_mock_session(resp)

    await notifier.call_webhook({"data": 1})

    _, kwargs = notifier._session.post.call_args
    assert "headers" not in kwargs


# ---------------------------------------------------------------------------
# Coverage: call_webhook with HTTP 400+ response (lines 86-89)
# ---------------------------------------------------------------------------


async def test_call_webhook_http_400():
    notifier = HANotifier(
        "tok", enabled=True, title="T", webhook_url="https://hook.test/"
    )
    resp = _make_mock_resp(400)
    notifier._session = _make_mock_session(resp)
    result = await notifier.call_webhook({"data": 1})
    assert result is False


async def test_call_webhook_http_500():
    notifier = HANotifier(
        "tok", enabled=True, title="T", webhook_url="https://hook.test/"
    )
    resp = _make_mock_resp(500)
    notifier._session = _make_mock_session(resp)
    result = await notifier.call_webhook({})
    assert result is False


# ---------------------------------------------------------------------------
# Coverage: _get_session creates ClientSession when _session is None (line 100)
# ---------------------------------------------------------------------------


async def test_get_session_creates_session_when_none():
    from unittest.mock import patch

    notifier = HANotifier("mytoken", enabled=True, title="T")
    mock_session = MagicMock()
    with patch(
        "blink_downloader.notifier.aiohttp.ClientSession", return_value=mock_session
    ) as MockCS:
        session = notifier._get_session()
    MockCS.assert_called_once_with()
    assert session is mock_session
