"""Signing in on the direct port: the gate, the login page and the token.

Every request passes through :meth:`AccessRoutesMixin._access_middleware`.
With ``direct_access_login`` off it lets everything through, exactly as
before 6.0.8. With it on:

* **ingress** requests (from Supervisor's proxy address) pass untouched —
  Home Assistant has already signed that user in;
* ``/health``, ``/login`` and the favicon are open to anyone;
* a **signed-in browser** passes, except that a write must come from the
  add-on's own page (checked with ``Sec-Fetch-Site`` and ``Origin``), so a
  web page elsewhere cannot use the cookie to post here;
* a request carrying the **access token** reaches only
  :data:`~.access_control.TOKEN_ROUTES`;
* anything else gets the login page (a page load) or a 401 (an API call).

The state and rules live in :mod:`.access_control`; this module is the
aiohttp side of them.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from aiohttp import web

from .access_control import (
    INGRESS_PROXY_IP,
    SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    TOKEN_ROUTES,
)
from .core import _MediaServerBase

_LOGGER = logging.getLogger(__name__)

#: Reachable without signing in at all.
_PUBLIC_PATHS = frozenset({"/health", "/login", "/favicon.svg"})

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: How a request was let in: "open" (sign-in off), "ingress", "session",
#: "token" or "anonymous" — and who, for a session.
ACCESS_VIA = web.RequestKey("access_via", str)
ACCESS_USER: web.RequestKey[str | None] = web.RequestKey("access_user")

_SIGN_IN_REQUIRED = (
    "Sign in to use the add-on over its direct port, or open it from the "
    "Home Assistant sidebar."
)
_TOKEN_NOT_ALLOWED = (  # nosec B105 - a message, not a credential
    "The access token only opens the endpoints Home Assistant's own "
    "cameras and scripts call. Sign in to use anything else."
)
_CROSS_SITE_WRITE = "Changes can only be made from the add-on's own page."

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _safe_next(target: str) -> str:
    """Where to go after signing in: a path on this server, nothing else.

    A ``next`` of ``//evil.example`` or ``https://...`` would turn the login
    page into an open redirect, so anything that is not a plain local path
    falls back to the home page. That includes whitespace and control
    characters — browsers drop a tab from a URL, so ``/<tab>/evil.example``
    arrives as ``//evil.example`` — and anything outside printable ASCII:
    the gate hands out ``next`` already percent-encoded, so a real one never
    holds either.
    """
    if (
        not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
        or any(not "!" <= ch <= "~" for ch in target)
    ):
        return "/"
    return target


def _redirect(location: str) -> web.Response:
    """A 303 returned rather than raised, so the security middleware still
    adds its headers to it."""
    return web.Response(status=303, headers={"Location": location})


def _bearer_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return request.query.get("token", "")


def _route_pattern(request: web.Request) -> str | None:
    """The pattern the request resolved to, e.g. ``/api/x/{camera}``."""
    resource = request.match_info.route.resource
    return resource.canonical if resource is not None else None


def _is_cross_site(request: web.Request) -> bool:
    """True when a browser says this request came from another page.

    ``Sec-Fetch-Site`` is the direct answer; ``Origin`` is the fallback for
    browsers that predate it. A client sending neither (curl, a script) is
    not a browser being tricked into anything, so it is let through — it
    still needed a valid session cookie to get here.
    """
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("Origin")
    if origin is None:
        return False
    return origin != f"{request.scheme}://{request.host}"


class AccessRoutesMixin(_MediaServerBase):
    """The sign-in gate, the login page and the access token."""

    def _register_access_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/login", self._handle_login_submit)
        app.router.add_post("/logout", self._handle_logout)
        app.router.add_get("/api/access", self._handle_access_status)
        app.router.add_post(
            "/api/access/token/regenerate", self._handle_access_token_regenerate
        )

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------

    def _identify(self, request: web.Request) -> tuple[str, str | None]:
        """How this request may be let in, and who it is if we know."""
        if not self._access.enabled:
            return "open", None
        if request.remote == INGRESS_PROXY_IP:
            return "ingress", None
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie:
            user = self._access.session_user(cookie)
            if user is not None:
                return "session", user
        if self._access.token_matches(_bearer_token(request)):
            return "token", None
        return "anonymous", None

    def _access_middleware(self) -> Callable[..., Awaitable[web.StreamResponse]]:
        """The middleware enforcing the rules in this module's docstring."""

        @web.middleware
        async def access_middleware(
            request: web.Request, handler: _Handler
        ) -> web.StreamResponse:
            via, user = self._identify(request)
            request[ACCESS_VIA] = via
            request[ACCESS_USER] = user
            if via in ("open", "ingress") or request.path in _PUBLIC_PATHS:
                return await handler(request)
            if via == "session":
                if request.method not in _SAFE_METHODS and _is_cross_site(request):
                    return web.json_response({"error": _CROSS_SITE_WRITE}, status=403)
                return await handler(request)
            if via == "token":
                method = "GET" if request.method == "HEAD" else request.method
                if (method, _route_pattern(request)) in TOKEN_ROUTES:
                    return await handler(request)
                return web.json_response({"error": _TOKEN_NOT_ALLOWED}, status=403)
            return self._sign_in_required(request)

        return access_middleware

    def _sign_in_required(self, request: web.Request) -> web.StreamResponse:
        """The login page for a page load, a 401 for anything else."""
        if request.method in ("GET", "HEAD") and not request.path.startswith("/api/"):
            location = "/login?next=" + quote(request.path_qs, safe="")
            if request.query.get("kiosk") == "1":
                location += "&kiosk=1"
            return _redirect(location)
        return web.json_response(
            {"error": _SIGN_IN_REQUIRED, "login_required": True}, status=401
        )

    # ------------------------------------------------------------------
    # Login page
    # ------------------------------------------------------------------

    async def _handle_login_page(self, request: web.Request) -> web.StreamResponse:
        if request[ACCESS_VIA] in ("open", "ingress", "session"):
            return _redirect(_safe_next(request.query.get("next", "/")))
        return self._login_page(request, request.query.get("next", "/"))

    async def _handle_login_submit(self, request: web.Request) -> web.StreamResponse:
        if request[ACCESS_VIA] in ("open", "ingress"):
            return _redirect("/")
        if _is_cross_site(request):
            return web.json_response({"error": _CROSS_SITE_WRITE}, status=403)
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        target = _safe_next(str(form.get("next", "/")))
        address = request.remote or ""

        wait = self._access.locked_out(address)
        if wait:
            minutes = max(1, round(wait / 60))
            return self._login_page(
                request,
                target,
                f"Too many attempts. Try again in {minutes} minute"
                f"{'' if minutes == 1 else 's'}.",
                username,
                status=429,
            )
        if not username or not password:
            return self._login_page(
                request, target, "Enter your username and password.", username, 400
            )

        verdict = await self._access.verify_credentials(username, password)
        if verdict is None:
            message = (
                "Home Assistant couldn't be reached to check your sign-in. "
                "Try again in a moment."
                if self._access.can_verify
                else "Signing in needs Home Assistant's Supervisor, and this "
                "add-on isn't running under it. Turn off Direct access "
                "sign-in to use this port."
            )
            return self._login_page(request, target, message, username, 503)
        if not verdict:
            self._access.record_failure(address)
            _LOGGER.warning(
                "Failed direct-port sign-in for %r from %s", username, address
            )
            return self._login_page(
                request,
                target,
                "That username and password didn't match a Home Assistant user.",
                username,
                401,
            )

        self._access.record_success(address)
        _LOGGER.info("Signed in on the direct port: %s from %s", username, address)
        response = _redirect(target)
        response.set_cookie(
            SESSION_COOKIE,
            self._access.issue_session(username),
            max_age=SESSION_LIFETIME_SECONDS,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=request.secure,
        )
        return response

    async def _handle_logout(self, _request: web.Request) -> web.Response:
        response = web.json_response({"signed_out": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    def _login_page(
        self,
        request: web.Request,
        target: str,
        error: str = "",
        username: str = "",
        status: int = 200,
    ) -> web.Response:
        """The sign-in form: plain HTML, so it works before any script loads."""
        action = "/login"
        if request.query.get("kiosk") == "1" or "kiosk=1" in target:
            action += "?kiosk=1"
        error_html = (
            f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
        )
        page = _LOGIN_PAGE.format(
            action=html.escape(action),
            next=html.escape(_safe_next(target)),
            username=html.escape(username),
            error=error_html,
            autofocus_user="" if username else " autofocus",
            autofocus_password=" autofocus" if username else "",
        )
        return web.Response(text=page, content_type="text/html", status=status)

    # ------------------------------------------------------------------
    # Status and token
    # ------------------------------------------------------------------

    async def _handle_access_status(self, request: web.Request) -> web.Response:
        """How this page was let in, and the token for generated YAML.

        The token is only included while sign-in is on — with it off, no
        request needs one, and the Automations tab leaves it out of the YAML.
        """
        body: dict[str, object] = {
            "login_enabled": self._access.enabled,
            "via": request[ACCESS_VIA],
            "user": request[ACCESS_USER],
            "access_token": self._access.access_token if self._access.enabled else "",
        }
        return web.json_response(body)

    async def _handle_access_token_regenerate(
        self, _request: web.Request
    ) -> web.Response:
        if not self._access.enabled:
            return web.json_response(
                {"error": "Direct access sign-in is off, so there is no token."},
                status=409,
            )
        _LOGGER.info("Direct-port access token regenerated")
        return web.json_response(
            {"access_token": self._access.regenerate_access_token()}
        )


_LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Blink Clips</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  :root {{
    --bg: #f4f5f7; --card: #ffffff; --text: #1d2330; --muted: #5a6275;
    --border: #cfd4de; --accent: #1f6feb; --accent-text: #ffffff;
    --error-bg: #fdecec; --error-text: #9b1c1c;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #111418; --card: #1b1f26; --text: #e6e9ef; --muted: #a3abbb;
      --border: #384050; --accent: #4c8dff; --accent-text: #0b0e13;
      --error-bg: #3a1618; --error-text: #ffb4b4;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 16px; background: var(--bg);
    color: var(--text); font: 16px/1.5 system-ui, -apple-system, "Segoe UI",
    Roboto, sans-serif;
  }}
  main {{
    width: 100%; max-width: 380px; background: var(--card);
    border: 1px solid var(--border); border-radius: 12px; padding: 28px 24px;
  }}
  h1 {{ margin: 0 0 4px; font-size: 1.35rem; }}
  .lede {{ margin: 0 0 20px; color: var(--muted); font-size: .95rem; }}
  label {{ display: block; margin: 14px 0 6px; font-weight: 600; font-size: .9rem; }}
  input {{
    width: 100%; padding: 10px 12px; font: inherit; color: var(--text);
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  }}
  input:focus {{ outline: 2px solid var(--accent); outline-offset: 1px; }}
  button {{
    width: 100%; margin-top: 22px; padding: 11px; font: inherit;
    font-weight: 600; color: var(--accent-text); background: var(--accent);
    border: 0; border-radius: 8px; cursor: pointer;
  }}
  .error {{
    margin: 0 0 8px; padding: 10px 12px; border-radius: 8px;
    background: var(--error-bg); color: var(--error-text); font-size: .92rem;
  }}
  .hint {{ margin: 18px 0 0; color: var(--muted); font-size: .85rem; }}
</style>
</head>
<body>
<main>
  <h1>Blink Clips</h1>
  <p class="lede">Sign in with your Home Assistant account.</p>
  {error}
  <form method="post" action="{action}">
    <input type="hidden" name="next" value="{next}">
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username"
      autocapitalize="none" value="{username}" required{autofocus_user}>
    <label for="password">Password</label>
    <input id="password" name="password" type="password"
      autocomplete="current-password" required{autofocus_password}>
    <button type="submit">Sign in</button>
  </form>
  <p class="hint">Opening Blink Clips from the Home Assistant sidebar never
  asks for this.</p>
</main>
</body>
</html>
"""
