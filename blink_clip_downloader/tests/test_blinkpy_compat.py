"""Tests for blink_downloader.blinkpy_compat."""

from __future__ import annotations

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


async def _call_oauth_signin(status: int):
    mock_response = MagicMock()
    mock_response.status = status

    mock_auth = MagicMock()
    mock_auth.session.post = AsyncMock(return_value=mock_response)

    return await blink_api.oauth_signin(mock_auth, "user@example.com", "pw", "csrf")


async def test_status_202_means_2fa_required():
    """Blink's signin endpoint now returns 202 (not 412) when 2FA is needed.

    See https://github.com/fronzbot/blinkpy/issues/1233 -- unpatched
    blinkpy falls through to `return None` for this status, which causes
    "Login failed" without ever prompting for the 2FA code.
    """
    assert await _call_oauth_signin(202) == "2FA_REQUIRED"


async def test_status_412_still_means_2fa_required():
    assert await _call_oauth_signin(412) == "2FA_REQUIRED"


async def test_redirect_status_means_success():
    assert await _call_oauth_signin(302) == "SUCCESS"


async def test_other_status_means_none():
    assert await _call_oauth_signin(200) is None
