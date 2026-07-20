"""Tests for blink_downloader.blinkpy_compat."""

from __future__ import annotations

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

from blinkpy import api as blink_api
from blinkpy.auth import Auth

from blink_downloader.blinkpy_compat import (
    _HARDWARE_ID_PATCH_MARKER,
    _PATCH_MARKER,
    _REFRESH_LOGGING_PATCH_MARKER,
    _REQUEST_LOGIN_PATCH_MARKER,
    patch_auth_hardware_id_validation,
    patch_oauth_refresh_token_logging,
    patch_oauth_signin_2fa_status,
    patch_request_login_hardware_id,
)


def test_oauth_signin_is_patched():
    patch_oauth_signin_2fa_status()
    assert getattr(blink_api.oauth_signin, _PATCH_MARKER, False) is True


def test_patch_is_idempotent():
    patch_oauth_signin_2fa_status()
    before = blink_api.oauth_signin
    patch_oauth_signin_2fa_status()
    assert blink_api.oauth_signin is before


async def _call_oauth_signin(status: int, body: str = ""):
    mock_response = MagicMock()
    mock_response.status = status
    mock_response.text = AsyncMock(return_value=body)

    mock_auth = MagicMock()
    mock_auth.session.post = AsyncMock(return_value=mock_response)

    return await blink_api.oauth_signin(mock_auth, "user@example.com", "pw", "csrf")


async def test_status_412_means_2fa_required():
    assert await _call_oauth_signin(412) == "2FA_REQUIRED"


async def test_status_202_with_tsv_methods_means_2fa_required():
    """202 with tsv_methods in body is a genuine 2FA challenge — SMS should arrive."""
    body = json.dumps({"tsv_methods": ["sms"], "tsv_state": "required"})
    assert await _call_oauth_signin(202, body) == "2FA_REQUIRED"


async def test_status_202_with_tsv_state_only_means_2fa_required():
    body = json.dumps({"tsv_state": "pending"})
    assert await _call_oauth_signin(202, body) == "2FA_REQUIRED"


async def test_status_202_with_next_time_in_secs_means_2fa_required():
    body = json.dumps({"next_time_in_secs": 30})
    assert await _call_oauth_signin(202, body) == "2FA_REQUIRED"


async def test_status_202_without_2fa_fields_is_not_2fa():
    """202 without 2FA indicator fields must NOT show a prompt.

    Without this check the add-on would display a 2FA overlay with no
    corresponding SMS from Blink — e.g. on HA restart when a transient
    network hiccup causes Blink's signin endpoint to return 202 for a
    non-2FA reason (see blinkpy issue #1233 and blinkpy_compat.py docstring).
    """
    body = json.dumps({"message": "ok"})
    assert await _call_oauth_signin(202, body) is None


async def test_status_202_with_empty_body_is_not_2fa():
    assert await _call_oauth_signin(202, "") is None


async def test_status_202_with_invalid_json_is_not_2fa():
    assert await _call_oauth_signin(202, "not-json") is None


async def test_redirect_status_means_success():
    assert await _call_oauth_signin(302) == "SUCCESS"


async def test_other_status_means_none():
    assert await _call_oauth_signin(200) is None


def test_auth_hardware_id_validation_is_patched():
    patch_auth_hardware_id_validation()
    assert getattr(Auth.__init__, _HARDWARE_ID_PATCH_MARKER, False) is True


def test_auth_hardware_id_validation_patch_is_idempotent():
    patch_auth_hardware_id_validation()
    before = Auth.__init__
    patch_auth_hardware_id_validation()
    assert Auth.__init__ is before


async def test_auth_regenerates_non_uuid_hardware_id():
    """A non-empty but invalid hardware_id (e.g. stale migrated state) is
    replaced, matching blinkpy PR #1268 -- without this, Blink's OAuth
    endpoint 406s every request that carries it."""
    patch_auth_hardware_id_validation()
    auth = Auth(
        {"username": "test@example.com", "hardware_id": "Home Assistant"},
        no_prompt=True,
        session=MagicMock(),
    )
    assert auth.hardware_id != "Home Assistant"
    uuid.UUID(auth.hardware_id)  # does not raise


async def test_auth_keeps_valid_uuid_hardware_id():
    patch_auth_hardware_id_validation()
    hardware_id = "726D586E-6A27-49E4-B61B-1BB070908899"
    auth = Auth(
        {"username": "test@example.com", "hardware_id": hardware_id},
        no_prompt=True,
        session=MagicMock(),
    )
    assert auth.hardware_id == hardware_id


async def test_auth_generates_hardware_id_when_missing():
    patch_auth_hardware_id_validation()
    auth = Auth({"username": "test@example.com"}, no_prompt=True, session=MagicMock())
    uuid.UUID(auth.hardware_id)  # does not raise


def test_request_login_hardware_id_is_patched():
    patch_request_login_hardware_id()
    assert getattr(blink_api.request_login, _REQUEST_LOGIN_PATCH_MARKER, False) is True


def test_request_login_hardware_id_patch_is_idempotent():
    patch_request_login_hardware_id()
    before = blink_api.request_login
    patch_request_login_hardware_id()
    assert blink_api.request_login is before


async def test_request_login_sends_auth_hardware_id_not_device_id():
    """The header must come from auth.hardware_id, not the never-populated
    login_data["device_id"] -- see blinkpy PR #1269. Sending a non-UUID
    value here is what breaks every token refresh ~90 minutes into a run."""
    patch_request_login_hardware_id()

    mock_auth = MagicMock()
    mock_auth.hardware_id = "726D586E-6A27-49E4-B61B-1BB070908899"
    mock_auth.refresh_token = "a-refresh-token"
    mock_auth.query = AsyncMock(return_value=MagicMock())

    login_data = {"username": "user@example.com", "2fa_code": None}
    await blink_api.request_login(
        mock_auth, "https://example.com/login", login_data, is_refresh=True
    )

    headers = mock_auth.query.call_args.kwargs["headers"]
    assert headers["hardware_id"] == "726D586E-6A27-49E4-B61B-1BB070908899"


async def test_request_login_none_2fa_code_becomes_empty_string():
    """A None 2fa_code must not be sent as a None header value -- aiohttp
    raises TypeError serializing that, per blinkpy PR #1268."""
    patch_request_login_hardware_id()

    mock_auth = MagicMock()
    mock_auth.hardware_id = "726D586E-6A27-49E4-B61B-1BB070908899"
    mock_auth.refresh_token = "a-refresh-token"
    mock_auth.query = AsyncMock(return_value=MagicMock())

    login_data = {"username": "user@example.com", "2fa_code": None}
    await blink_api.request_login(
        mock_auth, "https://example.com/login", login_data, is_refresh=True
    )

    headers = mock_auth.query.call_args.kwargs["headers"]
    assert headers["2fa-code"] == ""


async def test_request_login_sends_real_2fa_code():
    patch_request_login_hardware_id()

    mock_auth = MagicMock()
    mock_auth.hardware_id = "726D586E-6A27-49E4-B61B-1BB070908899"
    mock_auth.refresh_token = "a-refresh-token"
    mock_auth.query = AsyncMock(return_value=MagicMock())

    login_data = {"username": "user@example.com", "2fa_code": "123456"}
    await blink_api.request_login(
        mock_auth, "https://example.com/login", login_data, is_refresh=True
    )

    headers = mock_auth.query.call_args.kwargs["headers"]
    assert headers["2fa-code"] == "123456"


async def test_request_login_refresh_flow_uses_refresh_token():
    patch_request_login_hardware_id()

    mock_auth = MagicMock()
    mock_auth.hardware_id = "726D586E-6A27-49E4-B61B-1BB070908899"
    mock_auth.refresh_token = "a-refresh-token"
    mock_auth.query = AsyncMock(return_value=MagicMock())

    login_data = {"username": "user@example.com"}
    await blink_api.request_login(
        mock_auth, "https://example.com/login", login_data, is_refresh=True
    )

    data = mock_auth.query.call_args.kwargs["data"]
    assert "grant_type=refresh_token" in data
    assert "refresh_token=a-refresh-token" in data


async def test_request_login_password_flow_uses_password():
    patch_request_login_hardware_id()

    mock_auth = MagicMock()
    mock_auth.hardware_id = "726D586E-6A27-49E4-B61B-1BB070908899"
    mock_auth.query = AsyncMock(return_value=MagicMock())

    login_data = {"username": "user@example.com", "password": "hunter2"}
    await blink_api.request_login(
        mock_auth, "https://example.com/login", login_data, is_refresh=False
    )

    data = mock_auth.query.call_args.kwargs["data"]
    assert "grant_type=password" in data
    assert "password=hunter2" in data


def test_oauth_refresh_token_logging_is_patched():
    patch_oauth_refresh_token_logging()
    assert (
        getattr(blink_api.oauth_refresh_token, _REFRESH_LOGGING_PATCH_MARKER, False)
        is True
    )


def test_oauth_refresh_token_logging_patch_is_idempotent():
    patch_oauth_refresh_token_logging()
    before = blink_api.oauth_refresh_token
    patch_oauth_refresh_token_logging()
    assert blink_api.oauth_refresh_token is before


async def test_oauth_refresh_token_logs_on_failure(caplog):
    patch_oauth_refresh_token_logging()

    mock_response = MagicMock()
    mock_response.status = 406
    mock_auth = MagicMock()
    mock_auth.session.post = AsyncMock(return_value=mock_response)

    with caplog.at_level(logging.WARNING):
        result = await blink_api.oauth_refresh_token(
            mock_auth, "a-refresh-token", "a-hardware-id"
        )

    assert result is None
    assert "rejected" in caplog.text.lower()


async def test_oauth_refresh_token_returns_json_on_success():
    patch_oauth_refresh_token_logging()

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"access_token": "abc"})
    mock_auth = MagicMock()
    mock_auth.session.post = AsyncMock(return_value=mock_response)

    result = await blink_api.oauth_refresh_token(
        mock_auth, "a-refresh-token", "a-hardware-id"
    )

    assert result == {"access_token": "abc"}
