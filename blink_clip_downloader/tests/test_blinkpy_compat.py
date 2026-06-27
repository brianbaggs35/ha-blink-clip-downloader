"""Tests for blink_downloader.blinkpy_compat."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from blinkpy import api as blink_api

from blink_downloader.blinkpy_compat import (
    _PATCH_MARKER,
    patch_oauth_signin_2fa_status,
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
