"""Workarounds for known issues in blinkpy's OAuth v2 login flow.

blinkpy 0.25.x's ``oauth_signin()`` (called from
``Auth._oauth_login_flow()``) only recognises an HTTP ``412`` response as
"2FA required". Blink's backend now returns HTTP ``202 Accepted`` (with a
JSON body describing the available SMS/voice/WhatsApp verification
methods) for many accounts when 2FA is needed. blinkpy treats that 202 as
an unconditional login failure -- it logs "Login failed" and returns
``False`` *without* raising ``BlinkTwoFARequiredError`` -- so the add-on
never gets a chance to prompt for the code Blink just sent.

This was reported upstream:
https://github.com/fronzbot/blinkpy/issues/1233
https://github.com/fronzbot/blinkpy/issues/1230

blinkpy 0.25.6 (PR #1231) now handles HTTP 202 natively, so this patch
is redundant when running >= 0.25.6. We keep it as a belt-and-suspenders
measure: it is idempotent, replaces the function with identical behaviour
to the upstream fix, and ensures the 2FA flow works even if a future
blinkpy regression reintroduces the problem.
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
