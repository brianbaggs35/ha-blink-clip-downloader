"""Tests for ha_links: Home Assistant links that open one clip."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from blink_downloader import ha_links
from blink_downloader.ha_links import (
    HALinkResolver,
    clip_panel_path,
    parse_core_version,
)

_ADDON_INFO = "http://supervisor/addons/self/info"
_CORE_CONFIG = "http://supervisor/core/api/config"


@pytest.mark.parametrize(
    "version,expected",
    [
        ("2026.9.1", (2026, 9)),
        ("2026.10.0b3", (2026, 10)),
        ("2026.2", (2026, 2)),
        ("2026", None),
        ("dev.x", None),
        ("", None),
    ],
)
def test_parse_core_version(version: str, expected: tuple[int, int] | None) -> None:
    assert parse_core_version(version) == expected


def test_new_home_assistant_links_to_the_clip_itself() -> None:
    assert (
        clip_panel_path("abc_blink_clip_downloader", (2026, 2), "local_1/a b")
        == "/app/abc_blink_clip_downloader/clip/local_1%2Fa%20b"
    )


def test_older_home_assistant_links_to_the_panel_alone() -> None:
    """Before 2026.2 anything after the slug is read as part of the slug, so
    a path naming the clip would open an error page instead of the add-on."""
    assert (
        clip_panel_path("abc_blink_clip_downloader", (2026, 1), "123")
        == "/hassio/ingress/abc_blink_clip_downloader"
    )


def _response(status: int, body: Any) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _session(responses: dict[str, Any]) -> MagicMock:
    """A session whose GET answers from *responses* (a response, or an error)."""
    session = MagicMock()
    session.closed = False

    def get(url: str, **_: Any) -> MagicMock:
        answer = responses[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    session.get = MagicMock(side_effect=get)
    session.close = AsyncMock()
    return session


def _resolver(responses: dict[str, Any]) -> HALinkResolver:
    resolver = HALinkResolver("tok")
    resolver._session = _session(responses)
    return resolver


def _mock_get(resolver: HALinkResolver) -> MagicMock:
    """The mocked GET a resolver from _resolver() calls."""
    session = resolver._session
    assert isinstance(session, MagicMock)
    return session.get


def _healthy(**config: Any) -> dict[str, Any]:
    return {
        _ADDON_INFO: _response(200, {"data": {"slug": "abc_blink_clip_downloader"}}),
        _CORE_CONFIG: _response(200, {"version": "2026.9.1", **config}),
    }


async def test_links_use_supervisor_slug_and_core_version_and_url() -> None:
    resolver = _resolver(
        _healthy(external_url="https://home.example.com/", internal_url="http://ha")
    )

    assert await resolver.clip_path("c1") == "/app/abc_blink_clip_downloader/clip/c1"
    assert (
        await resolver.clip_url("c1")
        == "https://home.example.com/app/abc_blink_clip_downloader/clip/c1"
    )
    # Looked up once, then remembered.
    get = _mock_get(resolver)
    assert get.call_count == 2
    headers = get.call_args.kwargs["headers"]
    assert headers == {"Authorization": "Bearer tok"}


async def test_the_internal_url_stands_in_for_a_missing_external_one() -> None:
    resolver = _resolver(
        _healthy(external_url=None, internal_url="http://ha.local:8123")
    )
    assert (
        await resolver.clip_url("c1")
        == "http://ha.local:8123/app/abc_blink_clip_downloader/clip/c1"
    )


async def test_no_absolute_link_without_any_configured_url() -> None:
    resolver = _resolver(_healthy())
    assert await resolver.clip_url("c1") is None
    # The relative one still works: the companion app resolves it itself.
    assert await resolver.clip_path("c1") == "/app/abc_blink_clip_downloader/clip/c1"


async def test_no_links_without_a_supervisor_token() -> None:
    resolver = HALinkResolver("")
    assert await resolver.clip_path("c1") is None
    assert await resolver.clip_url("c1") is None


@pytest.mark.parametrize(
    "responses",
    [
        {_ADDON_INFO: _response(403, {}), _CORE_CONFIG: _response(200, {})},
        {
            _ADDON_INFO: aiohttp.ClientConnectionError("down"),
            _CORE_CONFIG: _response(200, {}),
        },
        {_ADDON_INFO: _response(200, []), _CORE_CONFIG: _response(200, {})},
        {
            _ADDON_INFO: _response(200, {"data": {}}),
            _CORE_CONFIG: _response(200, {"version": "2026.9.1"}),
        },
        {
            _ADDON_INFO: _response(200, {"data": {"slug": "s"}}),
            _CORE_CONFIG: _response(200, {"version": "unknown"}),
        },
    ],
    ids=["http-error", "unreachable", "not-an-object", "no-slug", "no-version"],
)
async def test_no_link_when_the_facts_cannot_be_looked_up(
    responses: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ha_links.time, "monotonic", lambda: clock[0])
    resolver = _resolver(responses)

    assert await resolver.clip_path("c1") is None
    calls = _mock_get(resolver).call_count

    # A failure is not retried on every alert...
    assert await resolver.clip_url("c1") is None
    assert _mock_get(resolver).call_count == calls

    # ...but is once a few minutes have passed.
    clock[0] += 301
    await resolver.clip_path("c1")
    assert _mock_get(resolver).call_count > calls


async def test_a_session_is_opened_on_first_use_and_closed_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = HALinkResolver("tok")
    await resolver.close()  # nothing open yet: a no-op

    created = _session(_healthy())
    monkeypatch.setattr(ha_links.aiohttp, "ClientSession", lambda: created)
    assert await resolver.clip_path("c1") is not None

    await resolver.close()
    created.close.assert_awaited_once()


async def test_the_facts_are_refreshed_hourly_and_kept_when_a_refresh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Home Assistant update restarts Core, not this add-on, so an answer
    looked up at start must not stand forever — but losing Supervisor for a
    moment must not cost alerts the link they had either."""
    clock = [1000.0]
    monkeypatch.setattr(ha_links.time, "monotonic", lambda: clock[0])
    responses = _healthy(external_url="https://old.example")
    responses[_CORE_CONFIG] = _response(200, {"version": "2026.1.3"})
    resolver = _resolver(responses)
    assert await resolver.clip_path("c1") == "/hassio/ingress/abc_blink_clip_downloader"

    # Within the hour nothing is looked up again.
    responses[_CORE_CONFIG] = _response(
        200, {"version": "2026.2.0", "external_url": "https://new.example"}
    )
    calls = _mock_get(resolver).call_count
    clock[0] += 3599
    assert await resolver.clip_path("c1") == "/hassio/ingress/abc_blink_clip_downloader"
    assert _mock_get(resolver).call_count == calls

    # After it, the update and the new URL are picked up.
    clock[0] += 2
    assert (
        await resolver.clip_url("c1")
        == "https://new.example/app/abc_blink_clip_downloader/clip/c1"
    )

    # A refresh that fails keeps the last good answer, and is retried later.
    responses[_ADDON_INFO] = aiohttp.ClientError("supervisor restarting")
    clock[0] += 3601
    assert await resolver.clip_path("c1") == "/app/abc_blink_clip_downloader/clip/c1"
    calls = _mock_get(resolver).call_count
    clock[0] += 60
    assert await resolver.clip_path("c1") == "/app/abc_blink_clip_downloader/clip/c1"
    assert _mock_get(resolver).call_count == calls
    responses[_ADDON_INFO] = _response(
        200, {"data": {"slug": "abc_blink_clip_downloader"}}
    )
    clock[0] += 301
    assert await resolver.clip_path("c2") == "/app/abc_blink_clip_downloader/clip/c2"
    assert _mock_get(resolver).call_count > calls
