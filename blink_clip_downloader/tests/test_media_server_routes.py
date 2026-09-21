"""The route table as a whole: no shadowing, and no orphaned registrar.

``MediaServer`` became a package whose sixteen route modules each register
their own endpoints, so ``_build_app`` no longer lists the API in one
place. Two things could go wrong that no individual endpoint's test would
notice, and both are checked here:

* **Shadowing.** A concrete path must not be swallowed by another module's
  ``{placeholder}`` pattern. aiohttp indexes plain paths ahead of dynamic
  ones, so registration order does not currently decide this — which is
  exactly why it needs asserting rather than assuming, since the day that
  stops being true the symptom is one endpoint quietly answering for
  another with no error anywhere.
* **A registrar nobody calls.** A route module that defines
  ``_register_*_routes`` but is missing from ``_build_app`` contributes no
  endpoints at all, and every test for that area would fail in a way that
  points at the handlers rather than at the omission.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from blink_downloader.media_server import MediaServer


def _app() -> web.Application:
    return MediaServer(db=MagicMock(), port=0)._build_app()


def _registered() -> list[tuple[str, str, str]]:
    """(method, path pattern, handler name) for every non-static route."""
    rows: list[tuple[str, str, str]] = []
    for resource in _app().router.resources():
        info = resource.get_info()
        path = info.get("path") or info.get("formatter")
        if not path:  # the static /assets mount has a directory, not a path
            continue
        for route in resource:
            if route.method in ("HEAD", "OPTIONS"):
                continue
            rows.append((route.method, path, getattr(route.handler, "__name__", "?")))
    return rows


def _concrete(path: str) -> str:
    """A sample URL for a pattern: each ``{placeholder}`` filled in."""
    while "{" in path:
        start, end = path.index("{"), path.index("}")
        path = f"{path[:start]}sample{path[end + 1 :]}"
    return path


@pytest.mark.parametrize(("method", "path", "handler"), _registered())
async def test_every_registered_route_resolves_to_its_own_handler(
    method: str, path: str, handler: str
) -> None:
    """No endpoint may be shadowed by another module's dynamic pattern."""
    match = await _app().router.resolve(make_mocked_request(method, _concrete(path)))
    resolved = getattr(match.handler, "__name__", None)
    assert resolved == handler, (
        f"{method} {path} is served by {resolved}, not {handler} — some other "
        f"route module registers a pattern that swallows it."
    )


def test_every_route_registrar_is_called_by_build_app() -> None:
    """A route module that registers nothing would silently lose its tab."""
    registrars = {
        name
        for name, _ in inspect.getmembers(MediaServer, inspect.isfunction)
        if name.startswith("_register_") and name.endswith("_routes")
    }
    called = inspect.getsource(MediaServer._build_app)
    missing = sorted(n for n in registrars if f"self.{n}(app)" not in called)
    assert not missing, f"_build_app never calls: {missing}"


def test_route_registrars_cover_every_handler() -> None:
    """Every ``_handle_*`` method must be reachable through some route."""
    handlers = {
        name
        for name, _ in inspect.getmembers(MediaServer, inspect.isfunction)
        if name.startswith("_handle_")
    }
    # _handle_arm_request is a shared implementation behind two routes, not
    # a route target of its own.
    handlers.discard("_handle_arm_request")
    wired = {h for _, _, h in _registered()}
    assert not handlers - wired, f"handlers with no route: {sorted(handlers - wired)}"
