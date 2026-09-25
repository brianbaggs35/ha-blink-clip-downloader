"""Direct-port sign-in: media_server/access_control.py and access.py.

The rules being held here, most important first: with Direct Access
Sign-In on, nothing but ingress, a signed-in browser, or the access token
(for its few endpoints) reaches the API; a signed-in browser's writes must
come from the add-on's own page; and with it off, nothing changes at all.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader.media_server import MediaServer, access_control
from blink_downloader.media_server import access as access_module
from blink_downloader.media_server.access_control import (
    LOCKOUT_SECONDS,
    MAX_FAILED_SIGN_INS,
    SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    TOKEN_ROUTES,
    AccessControl,
)

# ---------------------------------------------------------------------------
# AccessControl: secrets
# ---------------------------------------------------------------------------


def test_secrets_are_created_once_and_kept_private() -> None:
    first = AccessControl(enabled=True)
    assert first.load_or_create() is True
    assert first.load_or_create() is False  # already loaded in memory
    path = access_control.ACCESS_FILE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    second = AccessControl(enabled=True)
    assert second.load_or_create() is False  # read back from disk
    assert second.access_token == first.access_token
    assert second.session_user(first.issue_session("brian")) == "brian"


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"session_secret": "", "access_token": "x"}', "{}"],
)
def test_unusable_secrets_file_is_replaced(content: str) -> None:
    access_control.ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    access_control.ACCESS_FILE.write_text(content)
    control = AccessControl(enabled=True)
    assert control.load_or_create() is True
    stored = json.loads(access_control.ACCESS_FILE.read_text())
    assert stored["access_token"] == control.access_token


def test_secrets_survive_in_memory_when_the_file_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(access_control, "ACCESS_FILE", blocker / "web_access.json")
    control = AccessControl(enabled=True)
    assert control.load_or_create() is True
    assert control.access_token
    assert "will not survive a restart" in caplog.text


def test_regenerating_the_token_invalidates_the_old_one() -> None:
    control = AccessControl(enabled=True)
    old = control.access_token
    new = control.regenerate_access_token()
    assert new != old
    assert control.token_matches(new)
    assert not control.token_matches(old)
    assert AccessControl(enabled=True).access_token == new  # persisted


def test_token_matches_rejects_empty() -> None:
    assert not AccessControl(enabled=True).token_matches("")


# ---------------------------------------------------------------------------
# AccessControl: sessions
# ---------------------------------------------------------------------------


def test_session_round_trip_and_expiry() -> None:
    control = AccessControl(enabled=True)
    cookie = control.issue_session("brian", now=1000.0)
    assert control.session_user(cookie, now=1000.0) == "brian"
    assert control.session_user(cookie, now=1000.0 + SESSION_LIFETIME_SECONDS) is None


def test_session_names_with_dots_and_unicode_survive() -> None:
    control = AccessControl(enabled=True)
    assert control.session_user(control.issue_session("a.b-ç")) == "a.b-ç"


def test_tampered_sessions_are_rejected() -> None:
    control = AccessControl(enabled=True)
    user, expires, signature = control.issue_session("brian").split(".")
    forged_user = access_control._b64(b"admin")
    assert control.session_user(f"{forged_user}.{expires}.{signature}") is None
    assert control.session_user(f"{user}.99999999999.{signature}") is None
    assert control.session_user("only.two") is None
    assert control.session_user("garbage") is None
    # Not ASCII: refused, rather than compare_digest raising a 500.
    assert control.session_user(f"{user}.{expires}.\u00e9{signature[1:]}") is None


def test_well_signed_but_malformed_sessions_are_rejected() -> None:
    """A payload the signature covers can still fail to parse."""
    control = AccessControl(enabled=True)
    for payload in ("!!!.123", f"{access_control._b64(b'x')}.soon"):
        cookie = f"{payload}.{control._signature(payload)}"
        assert control.session_user(cookie) is None
    bad_utf8 = f"{access_control._b64(bytes([0xFF]))}.99999999999"
    assert control.session_user(f"{bad_utf8}.{control._signature(bad_utf8)}") is None


def test_sessions_do_not_survive_a_new_secret() -> None:
    cookie = AccessControl(enabled=True).issue_session("brian")
    access_control.ACCESS_FILE.unlink()
    assert AccessControl(enabled=True).session_user(cookie) is None


# ---------------------------------------------------------------------------
# AccessControl: lockout
# ---------------------------------------------------------------------------


def test_lockout_after_repeated_failures_then_ages_out() -> None:
    control = AccessControl(enabled=True)
    for i in range(MAX_FAILED_SIGN_INS - 1):
        control.record_failure("10.0.0.5", now=100.0 + i)
    assert control.locked_out("10.0.0.5", now=110.0) == 0
    control.record_failure("10.0.0.5", now=110.0)
    wait = control.locked_out("10.0.0.5", now=110.0)
    assert 0 < wait <= LOCKOUT_SECONDS
    assert control.locked_out("10.0.0.6", now=110.0) == 0  # per address
    assert control.locked_out("10.0.0.5", now=100.0 + LOCKOUT_SECONDS + 20) == 0
    assert "10.0.0.5" not in control._failures


def test_success_clears_failures() -> None:
    control = AccessControl(enabled=True)
    for _ in range(MAX_FAILED_SIGN_INS):
        control.record_failure("10.0.0.5")
    assert control.locked_out("10.0.0.5")
    control.record_success("10.0.0.5")
    assert control.locked_out("10.0.0.5") == 0


# ---------------------------------------------------------------------------
# AccessControl: Supervisor's /auth
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[Callable[[int], Awaitable[None]]]:
    """A stand-in for Supervisor's /auth answering with a chosen status."""
    seen: list[dict[str, Any]] = []
    status = {"code": 200}

    async def handler(request: web.Request) -> web.Response:
        seen.append(
            {
                "body": await request.json(),
                "auth": request.headers.get("Authorization"),
            }
        )
        return web.Response(status=status["code"])

    app = web.Application()
    app.router.add_post("/auth", handler)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(
        access_control, "_SUPERVISOR_AUTH_URL", str(server.make_url("/auth"))
    )

    async def answer(code: int) -> None:
        status["code"] = code

    answer.seen = seen  # type: ignore[attr-defined]
    yield answer
    await server.close()


async def test_verify_credentials_asks_supervisor(fake_supervisor: Any) -> None:
    control = AccessControl(enabled=True, supervisor_token="sup-token")
    await fake_supervisor(200)
    assert await control.verify_credentials("brian", "pw") is True
    assert fake_supervisor.seen[0] == {
        "body": {"username": "brian", "password": "pw"},
        "auth": "Bearer sup-token",
    }
    await fake_supervisor(401)
    assert await control.verify_credentials("brian", "wrong") is False
    await fake_supervisor(400)
    assert await control.verify_credentials("brian", "wrong") is False


async def test_verify_credentials_without_an_answer(
    fake_supervisor: Any, caplog: pytest.LogCaptureFixture
) -> None:
    control = AccessControl(enabled=True, supervisor_token="sup-token")
    await fake_supervisor(403)  # auth_api missing from config.yaml
    assert await control.verify_credentials("brian", "pw") is None
    assert "HTTP 403" in caplog.text


async def test_verify_credentials_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        access_control, "_SUPERVISOR_AUTH_URL", "http://127.0.0.1:1/auth"
    )
    control = AccessControl(enabled=True, supervisor_token="sup-token")
    assert await control.verify_credentials("brian", "pw") is None
    assert "sign-in check failed" in caplog.text


async def test_verify_credentials_outside_home_assistant() -> None:
    control = AccessControl(enabled=True)
    assert not control.can_verify
    assert await control.verify_credentials("brian", "pw") is None


# ---------------------------------------------------------------------------
# The gate, through a real server
# ---------------------------------------------------------------------------

_SNAPSHOT = b"\xff\xd8jpeg"


def _server(enabled: bool = True) -> MediaServer:
    return MediaServer(
        db=MagicMock(),
        port=0,
        trigger_download=MagicMock(),
        get_camera_snapshot=AsyncMock(return_value=_SNAPSHOT),
        direct_access_login=enabled,
        supervisor_token="sup-token",
    )


@pytest.fixture
async def make_client() -> AsyncGenerator[Callable[..., Awaitable[TestClient]]]:
    clients: list[TestClient] = []

    async def make(server: MediaServer | None = None) -> TestClient:
        tc = TestClient(TestServer((server or _server())._build_app()))
        await tc.start_server()
        clients.append(tc)
        return tc

    yield make
    for tc in clients:
        await tc.close()


async def _sign_in(client: TestClient, server_access: AccessControl) -> None:
    client.session.cookie_jar.update_cookies(
        {SESSION_COOKIE: server_access.issue_session("brian")}
    )


async def test_with_sign_in_off_everything_stays_open(make_client: Any) -> None:
    client = await make_client(_server(enabled=False))
    resp = await client.post("/api/download-now")
    assert resp.status == 200
    body = await (await client.get("/api/access")).json()
    assert body == {
        "login_enabled": False,
        "via": "open",
        "user": None,
        "access_token": "",
    }
    assert not access_control.ACCESS_FILE.exists()


async def test_anonymous_api_calls_get_a_401(make_client: Any) -> None:
    client = await make_client()
    for method, path in (("GET", "/api/access"), ("POST", "/api/download-now")):
        resp = await client.request(method, path)
        assert resp.status == 401
        assert (await resp.json())["login_required"] is True


async def test_anonymous_page_loads_go_to_the_login_page(make_client: Any) -> None:
    client = await make_client()
    resp = await client.get("/?tab=library", allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"] == "/login?next=%2F%3Ftab%3Dlibrary"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_kiosk_page_loads_keep_kiosk_on_the_login_page(make_client: Any) -> None:
    client = await make_client()
    resp = await client.get("/?kiosk=1&tab=securityfeed", allow_redirects=False)
    assert resp.headers["Location"].endswith("&kiosk=1")
    page = await client.get(resp.headers["Location"])
    # Frameable, so a dashboard's iframe card can show it and sign in.
    assert "X-Frame-Options" not in page.headers
    assert 'action="/login?kiosk=1"' in await page.text()


async def test_public_paths_need_no_sign_in(make_client: Any) -> None:
    client = await make_client()
    assert (await client.get("/health")).status == 200
    resp = await client.get("/login")
    assert resp.status == 200
    text = await resp.text()
    assert "Sign in with your Home Assistant account" in text
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "Content-Security-Policy" in resp.headers


async def test_ingress_is_never_asked(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_module, "INGRESS_PROXY_IP", "127.0.0.1")
    client = await make_client()
    body = await (await client.get("/api/access")).json()
    assert body["via"] == "ingress"
    assert body["access_token"]
    # Even a cross-site-looking write: Home Assistant already vouched.
    resp = await client.post("/api/download-now", headers={"Origin": "http://evil"})
    assert resp.status == 200
    login = await client.get("/login?next=/x", allow_redirects=False)
    assert login.headers["Location"] == "/x"
    submit = await client.post("/login", data={}, allow_redirects=False)
    assert submit.headers["Location"] == "/"


async def test_a_signed_in_browser_gets_in(make_client: Any) -> None:
    server = _server()
    client = await make_client(server)
    await _sign_in(client, server._access)
    body = await (await client.get("/api/access")).json()
    assert body == {
        "login_enabled": True,
        "via": "session",
        "user": "brian",
        "access_token": server._access.access_token,
    }
    same_origin = f"http://{client.host}:{client.port}"
    resp = await client.post(
        "/api/download-now",
        headers={"Origin": same_origin, "Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status == 200
    # And the login page sends it straight on.
    login = await client.get("/login?next=/?tab=ai", allow_redirects=False)
    assert login.headers["Location"] == "/?tab=ai"


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
    ],
)
async def test_cross_site_writes_are_refused_even_when_signed_in(
    make_client: Any, headers: dict[str, str]
) -> None:
    server = _server()
    client = await make_client(server)
    await _sign_in(client, server._access)
    resp = await client.post("/api/download-now", headers=headers)
    assert resp.status == 403
    server._trigger_download.assert_not_called()  # type: ignore[union-attr]
    # Reads are not affected: a cross-site read cannot see the response.
    assert (await client.get("/api/access", headers=headers)).status == 200


async def test_a_forged_or_expired_cookie_is_anonymous(make_client: Any) -> None:
    server = _server()
    client = await make_client(server)
    client.session.cookie_jar.update_cookies(
        {SESSION_COOKIE: server._access.issue_session("brian", now=0)}
    )
    assert (await client.get("/api/access")).status == 401


@pytest.mark.parametrize("via", ["header", "query"])
async def test_the_token_opens_only_its_endpoints(make_client: Any, via: str) -> None:
    server = _server()
    client = await make_client(server)
    token = server._access.access_token

    def kwargs(path: str) -> tuple[str, dict[str, Any]]:
        if via == "header":
            return path, {"headers": {"Authorization": f"Bearer {token}"}}
        joiner = "&" if "?" in path else "?"
        return f"{path}{joiner}token={token}", {}

    path, extra = kwargs("/api/security-feed/snapshot/Front%20Door")
    resp = await client.get(path, **extra)
    assert resp.status == 200
    assert await resp.read() == _SNAPSHOT
    assert (await client.head(path, **extra)).status == 200

    path, extra = kwargs("/api/download-now")
    assert (await client.post(path, **extra)).status == 200

    for method, other in (
        ("GET", "/api/access"),
        ("GET", "/api/clips"),
        ("POST", "/api/access/token/regenerate"),
        ("POST", "/api/sync-modules/cameras/Front/arm"),
        ("GET", "/api/does-not-exist"),
    ):
        path, extra = kwargs(other)
        resp = await client.request(method, path, **extra)
        assert resp.status == 403, other
        assert "access token only opens" in (await resp.json())["error"]


async def test_a_wrong_token_is_anonymous(make_client: Any) -> None:
    client = await make_client()
    resp = await client.post(
        "/api/download-now", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status == 401


def test_token_routes_are_all_real_routes() -> None:
    """A renamed endpoint would silently break Home Assistant's calls."""
    app = _server()._build_app()
    registered = {
        (route.method, route.resource.canonical)
        for route in app.router.routes()
        if route.resource is not None
    }
    assert registered >= TOKEN_ROUTES


# ---------------------------------------------------------------------------
# The login form
# ---------------------------------------------------------------------------


async def test_signing_in_sets_a_session_and_returns_to_next(
    make_client: Any,
) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post(
        "/login",
        data={"username": " brian ", "password": "pw", "next": "/?tab=ai"},
        allow_redirects=False,
    )
    assert resp.status == 303
    assert resp.headers["Location"] == "/?tab=ai"
    cookie = resp.cookies[SESSION_COOKIE]
    assert cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    assert int(cookie["max-age"]) == SESSION_LIFETIME_SECONDS
    server._access.verify_credentials.assert_awaited_once_with("brian", "pw")
    assert (await (await client.get("/api/access")).json())["user"] == "brian"


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example/x",
        "https://evil.example",
        "x",
        "/\\evil",
        "/\t/evil.example",
        "/ /evil.example",
        "/caf\u00e9",
        "/ok",
    ],
)
async def test_next_can_only_be_a_local_path(make_client: Any, target: str) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post(
        "/login",
        data={"username": "brian", "password": "pw", "next": target},
        allow_redirects=False,
    )
    assert resp.headers["Location"] == (target if target == "/ok" else "/")


async def test_wrong_password_is_said_plainly_and_counted(make_client: Any) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=False)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post(
        "/login", data={"username": "brian", "password": "nope", "next": "/"}
    )
    assert resp.status == 401
    text = await resp.text()
    assert "didn&#x27;t match a Home Assistant user" in text
    assert 'value="brian"' in text  # kept, with the password field focused
    assert SESSION_COOKIE not in resp.cookies


async def test_too_many_failures_lock_the_address_out(make_client: Any) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=False)  # type: ignore[method-assign]
    client = await make_client(server)
    for _ in range(MAX_FAILED_SIGN_INS):
        await client.post("/login", data={"username": "a", "password": "b"})
    resp = await client.post("/login", data={"username": "a", "password": "b"})
    assert resp.status == 429
    assert "Too many attempts. Try again in 15 minutes." in await resp.text()
    assert server._access.verify_credentials.await_count == MAX_FAILED_SIGN_INS


async def test_lockout_message_uses_the_singular(make_client: Any) -> None:
    server = _server()
    server._access.locked_out = MagicMock(return_value=30)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post("/login", data={"username": "a", "password": "b"})
    assert "Try again in 1 minute." in await resp.text()


async def test_missing_fields_are_asked_for(make_client: Any) -> None:
    client = await make_client()
    resp = await client.post("/login", data={"username": "brian"})
    assert resp.status == 400
    assert "Enter your username and password." in await resp.text()


async def test_supervisor_unreachable_is_not_a_wrong_password(
    make_client: Any,
) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=None)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post("/login", data={"username": "a", "password": "b"})
    assert resp.status == 503
    assert "couldn&#x27;t be reached" in await resp.text()
    assert server._access.locked_out("127.0.0.1") == 0  # not counted


async def test_outside_home_assistant_the_page_says_what_to_do(
    make_client: Any,
) -> None:
    server = MediaServer(db=MagicMock(), port=0, direct_access_login=True)
    client = await make_client(server)
    resp = await client.post("/login", data={"username": "a", "password": "b"})
    assert resp.status == 503
    assert "Turn off Direct access sign-in" in await resp.text()


async def test_a_cross_site_login_post_is_refused(make_client: Any) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post(
        "/login",
        data={"username": "a", "password": "b"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status == 403
    server._access.verify_credentials.assert_not_awaited()


async def test_the_login_page_escapes_what_it_echoes(make_client: Any) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=False)  # type: ignore[method-assign]
    client = await make_client(server)
    resp = await client.post(
        "/login",
        data={"username": '"><script>x</script>', "password": "b", "next": "/\"'>"},
    )
    text = await resp.text()
    assert "<script>x" not in text
    assert "&quot;&gt;&lt;script&gt;" in text


async def test_signing_out_clears_the_cookie(make_client: Any) -> None:
    server = _server()
    server._access.verify_credentials = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client = await make_client(server)
    await client.post("/login", data={"username": "brian", "password": "pw"})
    assert (await client.get("/api/access")).status == 200
    resp = await client.post("/logout")
    assert await resp.json() == {"signed_out": True}
    assert resp.cookies[SESSION_COOKIE].value == ""
    assert (await client.get("/api/access")).status == 401


# ---------------------------------------------------------------------------
# The token endpoint and prepare_access
# ---------------------------------------------------------------------------


async def test_regenerating_the_token_over_the_api(make_client: Any) -> None:
    server = _server()
    client = await make_client(server)
    await _sign_in(client, server._access)
    old = server._access.access_token
    resp = await client.post("/api/access/token/regenerate")
    new = (await resp.json())["access_token"]
    assert new != old
    assert server._access.token_matches(new)


async def test_no_token_to_regenerate_with_sign_in_off(make_client: Any) -> None:
    client = await make_client(_server(enabled=False))
    resp = await client.post("/api/access/token/regenerate")
    assert resp.status == 409


def test_prepare_access_reports_first_creation_only() -> None:
    assert _server(enabled=False).prepare_access() is False
    assert not access_control.ACCESS_FILE.exists()
    assert _server().prepare_access() is True
    assert _server().prepare_access() is False
