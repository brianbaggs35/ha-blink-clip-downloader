"""HTTPS on the direct port: media_server/tls.py, the second listener in
``MediaServer.start()``, and sign-in moving to it (access.py).

What is held here, most important first: with HTTPS up, a password is never
read off the plain port; ingress, a signed-in browser and the access token
still work on the plain port untouched; HTTPS failing to start never costs
the add-on its plain listener; and with no certificate nothing changes.

These use a real certificate and real sockets, because "you get an SSL error"
is exactly what a mocked listener cannot show.
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess  # nosec B404 - the tests make a throwaway certificate
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from blink_downloader.media_server import MediaServer, access_control
from blink_downloader.media_server import tls as tls_module
from blink_downloader.media_server.access_control import SESSION_COOKIE
from blink_downloader.media_server.tls import (
    HTTPS_PORT,
    TlsConfigError,
    load_ssl_context,
)

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="needs the openssl command"
)


def _make_pair(directory: Path, name: str) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1/localhost and its key."""
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(  # nosec B603 B607 - fixed arguments, a test fixture
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture(scope="module")
def pairs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    directory = tmp_path_factory.mktemp("certs")
    return {"a": _make_pair(directory, "a"), "b": _make_pair(directory, "b")}


@pytest.fixture
def ssl_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairs: dict[str, tuple[Path, Path]]
) -> Path:
    """A /ssl holding pair "a" as fullchain.pem / privkey.pem."""
    root = tmp_path / "ssl"
    root.mkdir()
    shutil.copy(pairs["a"][0], root / "fullchain.pem")
    shutil.copy(pairs["a"][1], root / "privkey.pem")
    monkeypatch.setattr(tls_module, "SSL_DIR", root)
    return root


# ---------------------------------------------------------------------------
# load_ssl_context
# ---------------------------------------------------------------------------


def test_loads_the_pair_in_ssl(ssl_dir: Path) -> None:
    context = load_ssl_context("fullchain.pem", "privkey.pem")
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_a_missing_file_is_named(ssl_dir: Path) -> None:
    with pytest.raises(TlsConfigError, match=r"nope\.pem does not exist"):
        load_ssl_context("nope.pem", "privkey.pem")
    with pytest.raises(TlsConfigError, match=r"nokey\.pem does not exist"):
        load_ssl_context("fullchain.pem", "nokey.pem")


@pytest.mark.parametrize("name", ["", "   "])
def test_an_empty_name_is_refused(ssl_dir: Path, name: str) -> None:
    with pytest.raises(TlsConfigError, match="certfile is empty"):
        load_ssl_context(name, "privkey.pem")
    with pytest.raises(TlsConfigError, match="keyfile is empty"):
        load_ssl_context("fullchain.pem", name)


@pytest.mark.parametrize("name", ["../outside.pem", "/etc/hostname", "sub/../../x.pem"])
def test_a_path_outside_ssl_is_refused_even_if_it_exists(
    ssl_dir: Path, name: str
) -> None:
    (ssl_dir.parent / "outside.pem").write_text("x")
    with pytest.raises(TlsConfigError, match="inside"):
        load_ssl_context(name, "privkey.pem")


def test_a_symlink_out_of_ssl_is_refused_and_one_inside_is_followed(
    ssl_dir: Path,
) -> None:
    (ssl_dir / "live").mkdir()
    (ssl_dir / "live" / "cert.pem").write_bytes(
        (ssl_dir / "fullchain.pem").read_bytes()
    )
    (ssl_dir / "link.pem").symlink_to(ssl_dir / "live" / "cert.pem")
    assert load_ssl_context("link.pem", "privkey.pem")

    (ssl_dir / "escape.pem").symlink_to(ssl_dir.parent / "elsewhere.pem")
    (ssl_dir.parent / "elsewhere.pem").write_text("x")
    with pytest.raises(TlsConfigError, match="inside"):
        load_ssl_context("escape.pem", "privkey.pem")


def test_a_key_that_does_not_belong_to_the_certificate_is_refused(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]]
) -> None:
    shutil.copy(pairs["b"][1], ssl_dir / "other.key")
    with pytest.raises(TlsConfigError, match="could not be loaded"):
        load_ssl_context("fullchain.pem", "other.key")


def test_a_file_that_is_not_a_certificate_is_refused(ssl_dir: Path) -> None:
    (ssl_dir / "junk.pem").write_text("not a certificate")
    with pytest.raises(TlsConfigError, match="could not be loaded"):
        load_ssl_context("junk.pem", "privkey.pem")


# ---------------------------------------------------------------------------
# The listener and sign-in, over real sockets
# ---------------------------------------------------------------------------


class Running:
    """A started MediaServer, with clients for each of its two ports."""

    def __init__(self, server: MediaServer, trust: Path) -> None:
        self.server = server
        self._trust = trust
        runner = server._runner
        assert runner is not None
        self.http_port: int = runner.addresses[0][1]
        self.https_port: int | None = server._https_port

    def http(self, path: str) -> str:
        return f"http://127.0.0.1:{self.http_port}{path}"

    def https(self, path: str) -> str:
        return f"https://127.0.0.1:{self.https_port}{path}"

    def session(self) -> aiohttp.ClientSession:
        context = ssl.create_default_context(cafile=str(self._trust))
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=context),
            # The default jar refuses cookies set by an IP address.
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )


def _server(**kwargs: object) -> MediaServer:
    return MediaServer(
        db=MagicMock(),
        port=0,
        https_port=0,
        trigger_download=MagicMock(),
        direct_access_login=True,
        supervisor_token="sup-token",
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
async def start() -> AsyncGenerator[Callable[..., object]]:
    servers: list[MediaServer] = []

    async def go(server: MediaServer, trust: Path) -> Running:
        servers.append(server)
        await server.start()
        return Running(server, trust)

    yield go
    for server in servers:
        await server.stop()


async def test_serves_the_same_app_over_https_and_plain_http(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]], start: Callable
) -> None:
    running = await start(
        _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem")),
        pairs["a"][0],
    )

    assert running.https_port
    assert running.https_port != running.http_port
    async with running.session() as session:
        secure = await session.get(running.https("/health"))
        plain = await session.get(running.http("/health"))
        assert secure.status == plain.status == 200
        assert await secure.text() == await plain.text()


async def test_plain_http_to_the_tls_port_is_not_an_http_server(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]], start: Callable
) -> None:
    """The failure this exists to fix, from the other side: https:// on a
    port that only speaks http:// is an SSL error. Here it is the TLS port
    that refuses plain text, and the plain port that does not know TLS."""
    running = await start(
        _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem")),
        pairs["a"][0],
    )

    tls_to_the_plain_port = running.http("/health").replace("http:", "https:")
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientError):
            await session.get(tls_to_the_plain_port)


async def test_the_certificate_the_listener_presents_is_the_one_in_ssl(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]], start: Callable
) -> None:
    running = await start(
        _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem")),
        pairs["a"][0],
    )

    # Trusting a different certificate must fail verification.
    wrong = Running(running.server, pairs["b"][0])
    url = running.https("/health")
    async with wrong.session() as session:
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await session.get(url)


async def test_without_a_certificate_there_is_no_https_and_nothing_changes(
    start: Callable, pairs: dict[str, tuple[Path, Path]]
) -> None:
    running = await start(_server(), pairs["a"][0])

    assert running.https_port is None
    assert len(running.server._runner.addresses) == 1  # type: ignore[union-attr]
    async with aiohttp.ClientSession() as session:
        resp = await session.get(running.http("/?tab=library"), allow_redirects=False)
        assert resp.status == 303
        assert resp.headers["Location"] == "/login?next=%2F%3Ftab%3Dlibrary"
        login = await session.get(running.http("/login"), allow_redirects=False)
        assert login.status == 200


async def test_signing_in_is_sent_to_https(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]], start: Callable
) -> None:
    running = await start(
        _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem")),
        pairs["a"][0],
    )

    async with aiohttp.ClientSession() as session:
        page = await session.get(running.http("/?tab=library"), allow_redirects=False)
        assert page.status == 303
        assert page.headers["Location"] == (
            f"https://127.0.0.1:{running.https_port}/login?next=%2F%3Ftab%3Dlibrary"
        )
        login = await session.get(running.http("/login"), allow_redirects=False)
        assert login.status == 303
        assert login.headers["Location"] == running.https("/login")
        with_next = await session.get(
            # encoded=True: yarl would otherwise tidy %2F before sending.
            URL(running.http("/login?next=%2Fclips%3Fx%3D1&kiosk=1"), encoded=True),
            allow_redirects=False,
        )
        assert with_next.headers["Location"] == running.https(
            "/login?next=%2Fclips%3Fx%3D1&kiosk=1"
        )
        kiosk = await session.get(
            running.http("/?kiosk=1&tab=securityfeed"), allow_redirects=False
        )
        assert kiosk.headers["Location"].startswith("https://127.0.0.1:")
        assert kiosk.headers["Location"].endswith("&kiosk=1")


async def test_a_password_posted_to_the_plain_port_is_not_read(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]], start: Callable
) -> None:
    server = _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem"))
    verify = AsyncMock(return_value=True)
    server._access.verify_credentials = verify  # type: ignore[method-assign]
    running = await start(server, pairs["a"][0])

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            running.http("/login"),
            data={"username": "brian", "password": "pw", "next": "/clips"},
            allow_redirects=False,
        )

    assert resp.status == 303
    assert resp.headers["Location"] == running.https("/login?next=%2Fclips")
    assert "Set-Cookie" not in resp.headers
    verify.assert_not_awaited()


async def test_signing_in_over_https_works_and_the_cookie_is_secure(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]], start: Callable
) -> None:
    server = _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem"))
    verify = AsyncMock(return_value=True)
    server._access.verify_credentials = verify  # type: ignore[method-assign]
    running = await start(server, pairs["a"][0])

    async with running.session() as session:
        page = await session.get(running.https("/login"))
        assert page.status == 200
        resp = await session.post(
            running.https("/login"),
            data={"username": "brian", "password": "pw", "next": "/clips"},
            allow_redirects=False,
        )

        assert resp.status == 303
        assert resp.headers["Location"] == "/clips"
        cookie = resp.headers["Set-Cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        verify.assert_awaited_once_with("brian", "pw")

        # And that session now opens the API over HTTPS.
        api = await session.get(running.https("/api/access"))
        assert (await api.json())["user"] == "brian"


async def test_everything_that_already_passes_the_gate_is_left_on_plain_http(
    ssl_dir: Path,
    pairs: dict[str, tuple[Path, Path]],
    start: Callable,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant's own calls are plain HTTP: the token, and ingress
    (which is only recognised by the proxy's address)."""
    server = _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem"))
    server._arm_sync_module = AsyncMock(return_value=True)
    token = server._access.load_or_create() and server._access.access_token
    running = await start(server, pairs["a"][0])

    async with aiohttp.ClientSession() as session:
        armed = await session.post(
            running.http("/api/sync-modules/Home/arm"),
            json={"armed": True},
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
        )
        assert armed.status == 200

        # Ingress: this test connects from 127.0.0.1, so make that the proxy.
        monkeypatch.setattr(access_control, "INGRESS_PROXY_IP", "127.0.0.1")
        from blink_downloader.media_server import access as access_module

        monkeypatch.setattr(access_module, "INGRESS_PROXY_IP", "127.0.0.1")
        via_ingress = await session.get(running.http("/api/access"))
        assert (await via_ingress.json())["via"] == "ingress"
        page = await session.get(running.http("/login"), allow_redirects=False)
        assert page.status == 303
        assert page.headers["Location"] == "/"  # not moved to https


async def test_https_not_starting_leaves_the_plain_listener_up(
    ssl_dir: Path,
    pairs: dict[str, tuple[Path, Path]],
    start: Callable,
    caplog: pytest.LogCaptureFixture,
) -> None:
    taken = socket.socket()
    taken.bind(("0.0.0.0", 0))  # nosec B104
    taken.listen()
    try:
        server = MediaServer(
            db=MagicMock(),
            port=0,
            https_port=taken.getsockname()[1],
            direct_access_login=True,
            ssl_context=load_ssl_context("fullchain.pem", "privkey.pem"),
        )
        running = await start(server, pairs["a"][0])
    finally:
        taken.close()

    assert "HTTPS could not start" in caplog.text
    assert running.https_port is None
    async with aiohttp.ClientSession() as session:
        assert (await session.get(running.http("/health"))).status == 200
        # With nowhere to send them, people sign in where they are.
        login = await session.get(running.http("/login"), allow_redirects=False)
        assert login.status == 200


async def test_stop_closes_both_listeners(
    ssl_dir: Path, pairs: dict[str, tuple[Path, Path]]
) -> None:
    server = _server(ssl_context=load_ssl_context("fullchain.pem", "privkey.pem"))
    await server.start()
    running = Running(server, pairs["a"][0])
    http, https = running.http_port, running.https_port

    await server.stop()

    assert server._https_port is None
    for port in (http, https):
        with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), 1):
            pass


def test_the_default_https_port_is_the_one_config_yaml_publishes() -> None:
    import yaml

    manifest = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "config.yaml").read_text()
    )
    assert f"{HTTPS_PORT}/tcp" in manifest["ports"]
    assert {"type": "ssl", "read_only": True} in manifest["map"]


# ---------------------------------------------------------------------------
# _on_https
# ---------------------------------------------------------------------------


def test_on_https_leaves_a_request_alone_that_cannot_be_moved() -> None:
    server = _server()
    server._https_port = 8100
    request = SimpleNamespace(url=URL("/relative"), secure=False)
    assert server._on_https(request, "/login") is None  # type: ignore[arg-type]


def test_on_https_handles_ipv6_hosts() -> None:
    server = _server()
    server._https_port = 8100
    request = SimpleNamespace(url=URL("http://[::1]:8099/x"), secure=False)
    assert server._on_https(request, "/login?next=%2F") == (  # type: ignore[arg-type]
        "https://[::1]:8100/login?next=%2F"
    )


@pytest.mark.parametrize("host", ["bad host", "evil.com/x", "a@b", "a?b#c"])
def test_on_https_does_not_fail_on_a_host_that_cannot_be_an_address(host: str) -> None:
    server = _server()
    server._https_port = 8100
    request = SimpleNamespace(url=SimpleNamespace(host=host), secure=False)
    assert server._on_https(request, "/login") is None  # type: ignore[arg-type]


@pytest.mark.parametrize("location", ["//evil.example/x", "evil", "https://evil/x"])
def test_on_https_only_ever_moves_a_path_on_this_server(location: str) -> None:
    server = _server()
    server._https_port = 8100
    request = SimpleNamespace(url=URL("http://ha.local:8099/x"), secure=False)
    assert server._on_https(request, location) is None  # type: ignore[arg-type]


def test_on_https_does_nothing_for_a_request_already_on_https() -> None:
    server = _server()
    server._https_port = 8100
    request = SimpleNamespace(url=URL("https://ha.local:8100/x"), secure=True)
    assert server._on_https(request, "/login") is None  # type: ignore[arg-type]
