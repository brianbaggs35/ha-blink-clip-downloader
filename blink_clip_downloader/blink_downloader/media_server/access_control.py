"""Who is making a request over the direct port, and whether to let them.

The add-on answers on two doors. **Ingress** — the Home Assistant sidebar
panel — arrives from Supervisor's proxy, which only forwards a request
after Home Assistant has authenticated the user, so it needs nothing more.
The **direct port** (8099 by default) is reachable by anything on the
network, and until 6.0.8 answered all of it, writes included.

This module holds the state and rules for the second door, with no aiohttp
routing in it (that is :mod:`.access`), so each rule can be tested on its
own:

* a signed-in browser carries a session cookie: a random token with nothing
  in it, which this module looks up in the sessions it keeps in ``/data``
  (as a hash, so the file is not a way back in);
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
#: a header. Fixed by Supervisor, which documents it as the one address an
#: ingress add-on should accept — not a deployment detail to configure.
INGRESS_PROXY_IP = "172.30.32.2"  # NOSONAR

#: The access token and the signed-in sessions, created on first use.
ACCESS_FILE = Path("/data/web_access.json")

SESSION_COOKIE = "blink_session"
#: How long a sign-in lasts: long enough that a wall tablet's kiosk card is
#: not asking every week, short enough that a lost phone expires.
SESSION_LIFETIME_SECONDS = 30 * 24 * 3600
#: Sessions kept at once. Signing in is rare and each one lasts a month, so
#: this is far more than a household uses; it only stops a script that signs
#: in over and over from growing the file without limit. The one closest to
#: expiring is dropped first.
MAX_SESSIONS = 200

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


def _digest(cookie: str) -> str:
    """What is stored in place of a session cookie."""
    return hashlib.sha256(cookie.encode()).hexdigest()


def _stored_sessions(raw: object) -> dict[str, tuple[str, int]]:
    """The sessions read back from the file, leaving out anything malformed."""
    sessions: dict[str, tuple[str, int]] = {}
    if not isinstance(raw, dict):
        return sessions
    now = time.time()
    for key, entry in raw.items():
        if not (isinstance(key, str) and isinstance(entry, dict)):
            continue
        user, expires = entry.get("user"), entry.get("expires")
        if (
            isinstance(user, str)
            and isinstance(expires, int)
            and not isinstance(expires, bool)
            and expires > now
        ):
            sessions[key] = (user, expires)
    return sessions


class AccessControl:
    """Sessions, the access token and sign-in checks for the direct port."""

    def __init__(  # nosec B107 - "" means not running under Supervisor
        self, enabled: bool = False, supervisor_token: str = ""
    ) -> None:
        self.enabled = enabled
        self._supervisor_token = supervisor_token
        self._secrets: dict[str, str] | None = None
        #: Signed-in sessions: ``_digest(cookie)`` -> (username, expiry time).
        self._sessions: dict[str, tuple[str, int]] = {}
        self._failures: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    def load_or_create(self) -> bool:
        """Load the stored token and sessions, creating the token if needed.

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
                and isinstance(stored.get("access_token"), str)
                and stored["access_token"]
            ):
                self._secrets = {"access_token": stored["access_token"]}
                self._sessions = _stored_sessions(stored.get("sessions"))
                return False
        except FileNotFoundError:
            # First start: nothing saved yet, so new secrets are made below.
            pass
        except (OSError, ValueError):
            _LOGGER.warning("%s is unreadable; creating new sign-in secrets", path)
        self._secrets = {"access_token": secrets.token_urlsafe(24)}
        self._sessions = {}
        self._save()
        return True

    def _save(self) -> None:
        """Write the token and sessions atomically, readable by this process only."""
        assert self._secrets is not None
        path = ACCESS_FILE
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(
                    {
                        **self._secrets,
                        "sessions": {
                            key: {"user": user, "expires": expires}
                            for key, (user, expires) in self._sessions.items()
                        },
                    },
                    fh,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            _LOGGER.warning(
                "Could not save sign-in details to %s (%s); sign-ins will "
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

    def _forget_expired(self, now: float) -> None:
        self._sessions = {
            key: session for key, session in self._sessions.items() if session[1] > now
        }

    def issue_session(self, username: str, now: float | None = None) -> str:
        """A new session cookie for *username*, valid for the lifetime.

        The cookie is random: it names nobody, and nothing the browser sent
        is in it. Who it belongs to is kept here, so that it can also be
        taken back (:meth:`revoke_session`).
        """
        self.load_or_create()
        now = time.time() if now is None else now
        self._forget_expired(now)
        while len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[min(self._sessions, key=lambda k: self._sessions[k][1])]
        cookie = secrets.token_urlsafe(32)
        self._sessions[_digest(cookie)] = (
            username,
            int(now + SESSION_LIFETIME_SECONDS),
        )
        self._save()
        return cookie

    def session_user(self, cookie: str, now: float | None = None) -> str | None:
        """The username a live session cookie belongs to, else None."""
        self.load_or_create()
        session = self._sessions.get(_digest(cookie))
        if session is None:
            return None
        username, expires = session
        if expires <= (time.time() if now is None else now):
            return None
        return username

    def revoke_session(self, cookie: str) -> None:
        """End the session *cookie* belongs to, so it opens nothing again."""
        self.load_or_create()
        if self._sessions.pop(_digest(cookie), None) is not None:
            self._save()

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

        The add-on identifies itself with ``X-Supervisor-Token``, not the
        ``Authorization: Bearer`` every other Supervisor call here uses:
        this endpoint takes the *user's* credentials as Basic auth, JSON or a
        form, so an ``Authorization`` header on it is read as those
        credentials and not as the add-on's token. Supervisor's documentation
        gives ``X-Supervisor-Token`` for this endpoint for that reason.
        """
        if not self.can_verify:
            return None
        try:
            async with (
                aiohttp.ClientSession(timeout=_AUTH_TIMEOUT) as session,
                session.post(
                    _SUPERVISOR_AUTH_URL,
                    json={"username": username, "password": password},
                    headers={"X-Supervisor-Token": self._supervisor_token},
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
