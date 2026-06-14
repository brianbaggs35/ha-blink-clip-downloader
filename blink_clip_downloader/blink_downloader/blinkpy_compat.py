"""Workarounds for known bugs in blinkpy's OAuth v2 login flow.

blinkpy 0.25.x's ``oauth_signin()`` (called from
``Auth._oauth_login_flow()``) only recognises an HTTP ``412`` response as
"2FA required". Blink's backend now returns HTTP ``202 Accepted`` (with a
JSON body describing the available SMS/voice/WhatsApp verification
methods) for many accounts when 2FA is needed. blinkpy treats that 202 as
an unconditional login failure -- it logs "Login failed" and returns
``False`` *without* raising ``BlinkTwoFARequiredError`` -- so the add-on
never gets a chance to prompt for the code Blink just sent.

This matches reports upstream:
https://github.com/fronzbot/blinkpy/issues/1233
https://github.com/fronzbot/blinkpy/issues/1230

Both are open and unfixed as of blinkpy 0.25.5 / 0.26.0b0, so we patch
``blinkpy.api.oauth_signin`` at import time to also treat status 202 as
"2FA_REQUIRED". This can be removed once a fixed blinkpy is released and
pinned -- the patched function behaves identically to a future upstream
fix that adds the same status code.
"""

from __future__ import annotations

import logging

from blinkpy import api as blink_api

_LOGGER = logging.getLogger(__name__)

_PATCH_MARKER = "_blink_clip_downloader_patched_2fa_status"


def patch_oauth_signin_2fa_status() -> None:
    """Make ``blinkpy.api.oauth_signin`` treat HTTP 202 as 2FA-required.

    Idempotent: safe to call multiple times, including across repeated
    imports within the same process.
    """
    if getattr(blink_api.oauth_signin, _PATCH_MARKER, False):
        return

    async def patched_oauth_signin(auth, email, password, csrf_token):
        headers = {
            "User-Agent": blink_api.OAUTH_USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://api.oauth.blink.com",
            "Referer": blink_api.OAUTH_SIGNIN_URL,
        }

        data = {
            "username": email,
            "password": password,
            "csrf-token": csrf_token,
        }

        response = await auth.session.post(
            blink_api.OAUTH_SIGNIN_URL,
            headers=headers,
            data=data,
            allow_redirects=False,
        )

        if response.status in (202, 412):
            # 2FA required. See module docstring re: blinkpy issue #1233.
            return "2FA_REQUIRED"
        elif response.status in (301, 302, 303, 307, 308):
            return "SUCCESS"

        return None

    setattr(patched_oauth_signin, _PATCH_MARKER, True)
    blink_api.oauth_signin = patched_oauth_signin
    _LOGGER.debug(
        "Patched blinkpy.api.oauth_signin to treat HTTP 202 as 2FA_REQUIRED "
        "(see https://github.com/fronzbot/blinkpy/issues/1233)"
    )
