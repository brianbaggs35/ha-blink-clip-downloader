"""Workarounds for known issues in blinkpy's OAuth v2 login flow.

blinkpy 0.25.0–0.25.5's ``oauth_signin()`` (called from
``Auth._oauth_login_flow()``) only recognised an HTTP ``412`` response as
"2FA required". Blink's backend now returns HTTP ``202 Accepted`` (with a
JSON body describing the available SMS/voice/WhatsApp verification
methods) for many accounts when 2FA is needed. blinkpy treated that 202
as an unconditional login failure -- it logged "Login failed" and returned
``False`` *without* raising ``BlinkTwoFARequiredError`` -- so the add-on
never got a chance to prompt for the code Blink just sent.

This was reported upstream:
https://github.com/fronzbot/blinkpy/issues/1233
https://github.com/fronzbot/blinkpy/issues/1230

blinkpy 0.25.6 (PR #1231) now handles HTTP 202 natively by inspecting the
JSON body for ``tsv_state``, ``tsv_methods``, or ``next_time_in_secs``
before treating it as a 2FA challenge.  blinkpy 0.25.7 additionally
initialises ``response_text = ""`` to prevent an ``UnboundLocalError``
when the error-logging path runs on unexpected status codes.

We keep this patch as a belt-and-suspenders measure, but it now matches
blinkpy's own body-checking logic.  The critical difference from the old
version: a 202 WITHOUT the 2FA indicator fields is **not** treated as
2FA required (it is returned as ``None``/failure instead).  This prevents
spurious 2FA prompts -- which would appear with no corresponding SMS from
Blink -- that could occur on HA restart if a transient network hiccup
causes Blink's signin endpoint to return 202 without a genuine 2FA
challenge body.
"""

from __future__ import annotations

import json
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

        if response.status == 412:
            return "2FA_REQUIRED"

        if response.status == 202:
            # Inspect the body before treating this as a 2FA challenge.
            # Blink can return 202 for reasons other than 2FA; only the
            # presence of tsv_state/tsv_methods/next_time_in_secs indicates
            # that a verification code was actually sent.  Treating every 202
            # as 2FA_REQUIRED would show a prompt with no corresponding SMS
            # from Blink (see module docstring).
            response_text = await response.text()
            try:
                response_json = json.loads(response_text)
            except json.JSONDecodeError:
                response_json = {}
            if (
                response_json.get("tsv_state")
                or response_json.get("tsv_methods")
                or response_json.get("next_time_in_secs")
            ):
                return "2FA_REQUIRED"
            return None

        if response.status in (301, 302, 303, 307, 308):
            return "SUCCESS"

        return None

    setattr(patched_oauth_signin, _PATCH_MARKER, True)
    blink_api.oauth_signin = patched_oauth_signin
    _LOGGER.debug(
        "Patched blinkpy.api.oauth_signin to treat HTTP 202 as 2FA_REQUIRED "
        "(see https://github.com/fronzbot/blinkpy/issues/1233)"
    )
