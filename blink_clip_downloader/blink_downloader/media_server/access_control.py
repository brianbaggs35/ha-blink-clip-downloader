"""Who is making a request over the direct port, and whether to let them.

The add-on answers on two doors. **Ingress** — the Home Assistant sidebar
panel — arrives from Supervisor's proxy, which only forwards a request
after Home Assistant has authenticated the user, so it needs nothing more.
The **direct port** (8099 by default) is reachable by anything on the
network, and until 6.0.8 answered all of it, writes included.

This module holds the state and rules for the second door, with no aiohttp
routing in it (that is :mod:`.access`), so each rule can be tested on its
own:

* a signed-in browser carries a session cookie, signed with a secret that
  never leaves ``/data``;
* Home Assistant's own server-side calls — the Generic Camera snapshot URL
  and the ``rest_command`` scripts the Automations tab generates — carry an
  access token instead, which opens only those endpoints
  (:data:`TOKEN_ROUTES`), so a token copied out of someone's YAML cannot
  read the library or write automations;
* sign-in checks a Home Assistant username and password through
  Supervisor's ``/auth`` endpoint, so there is no second password to
  manage, and repeated failures from one address are made to wait.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

import aiohttp

_LOGGER = logging.getLogger(__name__)

#: Supervisor's ingress proxy. Every request from the sidebar panel comes
#: from this address, and only after Home Assistant has signed the user in.
#: Matched against the TCP peer, never a header, since any client can send
#: a header.
INGRESS_PROXY_IP = "172.30.32.2"

#: The session-signing secret and the access token, created on first use.
ACCESS_FILE = Path("/data/web_access.json")

SESSION_COOKIE = "blink_session"
#: How long a sign-in lasts: long enough that a wall tablet's kiosk card is
#: not asking every week, short enough that a lost phone expires.
SESSION_LIFETIME_SECONDS = 30 * 24 * 3600

#: Failed sign-ins one address may make inside the lockout window before it
#: is refused outright until the oldest failure ages out.
MAX_FAILED_SIGN_INS = 5
LOCKOUT_SECONDS = 15 * 60

#: The only endpoints the access token opens, as (method, route pattern):
#: exactly what the Automations tab's generated YAML has Home Assistant
#: call. Matched on aiohttp's resolved route pattern rather than the raw
#: path, so a camera name with an encoded slash in it cannot slip a
#: different endpoint past the check.
TOKEN_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/security-feed/snapshot/{camera}"),
        ("POST", "/api/download-now"),
        ("POST", "/api/sync-modules/{name}/arm"),
        ("POST", "/api/storage/archive/run-now"),
    }
)

# Internal Supervisor API on the isolated `hassio` network, not TLS — the
# same reasoning as notifier.py's _HA_API.
_SUPERVISOR_AUTH_URL = "http://supervisor/auth"  # NOSONAR
_AUTH_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AccessControl:
    """Sessions, the access token and sign-in checks for the direct port."""

    def __init__(  # nosec B107 - "" means not running under Supervisor
        self, enabled: bool = False, supervisor_token: str = ""
    ) -> None:
        self.enabled = enabled
        self._supervisor_token = supervisor_token
        self._secrets: dict[str, str] | None = None
        self._failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    def load_or_create(self) -> bool:
        """Load the stored secrets, creating them if there are none yet.

        Returns True when they were created by this call — the first start
        with sign-in available, which is when app.py tells an existing
        install what changed. A file that cannot be written leaves the
        secrets in memory only: sign-ins then last until the next restart
        rather than failing outright.
        """
        if self._secrets is not None:
            return False
        path = ACCESS_FILE
        try:
            stored = json.loads(path.read_text())
            if (
                isinstance(stored, dict)
                and isinstance(stored.get("session_secret"), str)
                and isinstance(stored.get("access_token"), str)
                and stored["session_secret"]
                and stored["access_token"]
            ):
                self._secrets = {
                    "session_secret": stored["session_secret"],
                    "access_token": stored["access_token"],
                }
                return False
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            _LOGGER.warning("%s is unreadable; creating new sign-in secrets", path)
        self._secrets = {
            "session_secret": secrets.token_urlsafe(32),
            "access_token": secrets.token_urlsafe(24),
        }
        self._save()
        return True

    def _save(self) -> None:
        """Write the secrets atomically, readable by this process only."""
        assert self._secrets is not None
        path = ACCESS_FILE
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self._secrets, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            _LOGGER.warning(
                "Could not save sign-in secrets to %s (%s); sign-ins will "
                "not survive a restart",
                path,
                exc,
            )

    def _secret(self, key: str) -> str:
        self.load_or_create()
        assert self._secrets is not None
        return self._secrets[key]

    @property
    def access_token(self) -> str:
        """The token Home Assistant's own calls to the direct port carry."""
        return self._secret("access_token")

    def regenerate_access_token(self) -> str:
        """Replace the access token, invalidating every copy of the old one."""
        self.load_or_create()
        assert self._secrets is not None
        self._secrets["access_token"] = secrets.token_urlsafe(24)
        self._save()
        return self._secrets["access_token"]

    def token_matches(self, candidate: str) -> bool:
        """True when *candidate* is the current access token."""
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode(), self.access_token.encode())

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def _signature(self, payload: str) -> str:
        key = self._secret("session_secret").encode()
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def issue_session(self, username: str, now: float | None = None) -> str:
        """A signed cookie value naming *username*, valid for the lifetime."""
        expires = int((time.time() if now is None else now) + SESSION_LIFETIME_SECONDS)
        payload = f"{_b64(username.encode())}.{expires}"
        return f"{payload}.{self._signature(payload)}"

    def session_user(self, cookie: str, now: float | None = None) -> str | None:
        """The username a valid, unexpired session cookie names, else None."""
        parts = cookie.split(".")
        if len(parts) != 3:
            return None
        user_part, expires_part, signature = parts
        payload = f"{user_part}.{expires_part}"
        # Compared as bytes: a cookie is whatever the client sent, and
        # compare_digest raises on a str holding anything but ASCII.
        if not hmac.compare_digest(
            signature.encode(), self._signature(payload).encode()
        ):
            return None
        try:
            expires = int(expires_part)
            username = _unb64(user_part).decode()
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
        if expires <= (time.time() if now is None else now):
            return None
        return username

    # ------------------------------------------------------------------
    # Sign-in
    # ------------------------------------------------------------------

    @property
    def can_verify(self) -> bool:
        """False outside Home Assistant, where there is no Supervisor to ask."""
        return bool(self._supervisor_token)

    def locked_out(self, address: str, now: float | None = None) -> int:
        """Seconds *address* must wait before trying again; 0 when it may."""
        now = time.time() if now is None else now
        recent = [
            t for t in self._failures.get(address, []) if now - t < LOCKOUT_SECONDS
        ]
        if recent:
            self._failures[address] = recent
        else:
            self._failures.pop(address, None)
        if len(recent) < MAX_FAILED_SIGN_INS:
            return 0
        return max(1, int(LOCKOUT_SECONDS - (now - recent[0])))

    def record_failure(self, address: str, now: float | None = None) -> None:
        self._failures.setdefault(address, []).append(
            time.time() if now is None else now
        )

    def record_success(self, address: str) -> None:
        self._failures.pop(address, None)

    async def verify_credentials(self, username: str, password: str) -> bool | None:
        """Ask Supervisor whether this is a Home Assistant user's login.

        True or False is Supervisor's answer; None means there was no
        answer to be had — no Supervisor, unreachable, or refusing the
        add-on (``auth_api`` missing from config.yaml) — which the login
        page reports differently from a wrong password.
        """
        if not self.can_verify:
            return None
        try:
            async with (
                aiohttp.ClientSession(timeout=_AUTH_TIMEOUT) as session,
                session.post(
                    _SUPERVISOR_AUTH_URL,
                    json={"username": username, "password": password},
                    headers={"Authorization": f"Bearer {self._supervisor_token}"},
                ) as resp,
            ):
                if resp.status == 200:
                    return True
                if resp.status in (400, 401):
                    return False
                _LOGGER.warning(
                    "Supervisor sign-in check returned HTTP %d", resp.status
                )
                return None
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Supervisor sign-in check failed: %s", exc)
            return None
