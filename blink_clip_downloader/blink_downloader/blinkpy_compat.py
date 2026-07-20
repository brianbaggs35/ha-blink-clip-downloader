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
import uuid
from urllib.parse import urlencode

from blinkpy import api as blink_api
from blinkpy.auth import Auth

_LOGGER = logging.getLogger(__name__)

_PATCH_MARKER = "_blink_clip_downloader_patched_2fa_status"
_HARDWARE_ID_PATCH_MARKER = "_blink_clip_downloader_patched_hardware_id"
_REQUEST_LOGIN_PATCH_MARKER = "_blink_clip_downloader_patched_request_login"
_REFRESH_LOGGING_PATCH_MARKER = "_blink_clip_downloader_patched_refresh_logging"


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


def patch_auth_hardware_id_validation() -> None:
    """Make ``blinkpy.auth.Auth`` regenerate an invalid ``hardware_id``.

    ``Auth.__init__`` only regenerated ``hardware_id`` when the value from
    ``login_data`` was falsy (``None``/``""``) -- a *non-empty* but
    non-UUID value (e.g. a string like ``"Home Assistant"`` carried over
    from an old migrated config) passed through unchanged. Blink's OAuth
    endpoints now reject non-UUID ``hardware_id`` values with HTTP 406.

    This add-on always supplies a valid UUID via
    ``BlinkDownloader._get_or_create_hardware_id()`` (see downloader.py),
    so in practice this patch is belt-and-suspenders for stale/imported
    state rather than the fix that restores a broken login --  see
    ``patch_request_login_hardware_id()`` below for that.

    Reported upstream: https://github.com/fronzbot/blinkpy/pull/1268

    Idempotent: safe to call multiple times, including across repeated
    imports within the same process.
    """
    if getattr(Auth.__init__, _HARDWARE_ID_PATCH_MARKER, False):
        return

    _original_init = Auth.__init__

    def patched_init(self, login_data=None, *args, **kwargs):
        _original_init(self, login_data, *args, **kwargs)
        try:
            uuid.UUID(self.hardware_id)
        except (TypeError, ValueError):
            self.hardware_id = str(uuid.uuid4()).upper()

    setattr(patched_init, _HARDWARE_ID_PATCH_MARKER, True)
    Auth.__init__ = patched_init
    _LOGGER.debug(
        "Patched blinkpy.auth.Auth.__init__ to regenerate a non-UUID "
        "hardware_id (see https://github.com/fronzbot/blinkpy/pull/1268)"
    )


def patch_request_login_hardware_id() -> None:
    """Make ``blinkpy.api.request_login`` send a valid ``hardware_id`` header.

    ``request_login()`` is called on every mid-session token refresh (via
    ``Auth.query() -> Auth.refresh_tokens() -> Auth.login()``, once the
    ~90-minute access token nears expiry) as well as on full password
    logins. It built its ``hardware_id`` header from
    ``login_data.get("device_id", "Blinkpy")`` -- but neither this add-on
    nor blinkpy's own OAuth v2 login flow ever populate a ``device_id``
    key (only ``hardware_id``), so that lookup always fell through to the
    literal string ``"Blinkpy"``. Blink's OAuth endpoint now rejects
    non-UUID ``hardware_id`` values with HTTP 406, so every refresh after
    the first ~90 minutes of a session failed this way -- surfacing as
    "unable to log in" even though the *initial* login (``Auth.startup()``,
    a separate code path that already uses ``auth.hardware_id`` correctly)
    succeeded fine.

    This also folds in a fix for a second bug in the same function: a
    ``2fa_code`` of ``None`` in ``login_data`` produced a
    ``2fa-code: None`` header, which aiohttp raises a ``TypeError`` trying
    to serialize.

    Reported upstream:
    https://github.com/fronzbot/blinkpy/pull/1268
    https://github.com/fronzbot/blinkpy/pull/1269

    Idempotent: safe to call multiple times, including across repeated
    imports within the same process.
    """
    if getattr(blink_api.request_login, _REQUEST_LOGIN_PATCH_MARKER, False):
        return

    async def patched_request_login(
        auth,
        url,
        login_data,
        is_refresh=False,
        is_retry=False,
    ):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": blink_api.DEFAULT_USER_AGENT,
            "hardware_id": auth.hardware_id,
            "2fa-code": login_data.get("2fa_code") or "",
        }

        form_data = {
            "username": login_data["username"],
            "client_id": blink_api.OAUTH_CLIENT_ID,
            "scope": blink_api.OAUTH_SCOPE,
        }

        if is_refresh:
            form_data["grant_type"] = blink_api.OAUTH_GRANT_TYPE_REFRESH_TOKEN
            form_data["refresh_token"] = auth.refresh_token
        else:
            form_data["grant_type"] = blink_api.OAUTH_GRANT_TYPE_PASSWORD
            form_data["password"] = login_data["password"]

        data = urlencode(form_data)

        return await auth.query(
            url=url,
            headers=headers,
            data=data,
            json_resp=False,
            reqtype="post",
            is_retry=is_retry,
            skip_refresh_check=True,
        )

    setattr(patched_request_login, _REQUEST_LOGIN_PATCH_MARKER, True)
    blink_api.request_login = patched_request_login
    _LOGGER.debug(
        "Patched blinkpy.api.request_login to send auth.hardware_id "
        "(see https://github.com/fronzbot/blinkpy/pull/1269)"
    )


def patch_oauth_refresh_token_logging() -> None:
    """Log the HTTP status when ``blinkpy.api.oauth_refresh_token`` fails.

    ``oauth_refresh_token()`` returned ``None`` on any non-200 response
    with no logging at all, so a rejected OAuth v2 refresh (e.g. an
    expired refresh token, or the HTTP 406 described in
    ``patch_request_login_hardware_id()``) surfaced only as a generic
    downstream login failure with no indication of what actually went
    wrong.

    Reported upstream: https://github.com/fronzbot/blinkpy/pull/1268

    Idempotent: safe to call multiple times, including across repeated
    imports within the same process.
    """
    if getattr(blink_api.oauth_refresh_token, _REFRESH_LOGGING_PATCH_MARKER, False):
        return

    _original_oauth_refresh_token = blink_api.oauth_refresh_token

    async def patched_oauth_refresh_token(auth, refresh_token, hardware_id):
        token_data = await _original_oauth_refresh_token(
            auth, refresh_token, hardware_id
        )
        if token_data is None:
            _LOGGER.warning(
                "Blink OAuth v2 token refresh was rejected (non-200 "
                "response); falling back to full login"
            )
        return token_data

    setattr(patched_oauth_refresh_token, _REFRESH_LOGGING_PATCH_MARKER, True)
    blink_api.oauth_refresh_token = patched_oauth_refresh_token
    _LOGGER.debug(
        "Patched blinkpy.api.oauth_refresh_token to log refresh failures "
        "(see https://github.com/fronzbot/blinkpy/pull/1268)"
    )
