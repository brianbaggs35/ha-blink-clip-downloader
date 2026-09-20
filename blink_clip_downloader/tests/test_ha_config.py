"""Tests for blink_downloader.ha_config."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from blink_downloader.ha_config import (
    HAConfigError,
    HAConfigWriter,
    created_name,
    normalize_config,
)

# ---------------------------------------------------------------------------
# normalize_config
# ---------------------------------------------------------------------------

AUTOMATION_YAML = """alias: "Blink – test"
mode: single
triggers:
  - trigger: event
    event_type: blink_clip_analyzed
conditions: []
actions:
  - action: notify.notify
    data:
      title: "hi"
"""

SCRIPT_YAML = """# scripts.yaml
blink_show_cameras:
  alias: Blink – show cameras
  mode: restart
  sequence:
    - action: cast.show_lovelace_view
"""

SCENE_YAML = """# scenes.yaml
- id: blink_security_alert
  name: Blink security alert
  icon: mdi:alarm-light
  entities:
    light.porch:
      state: "on"
"""


def test_an_automation_passes_through_as_the_api_wants_it() -> None:
    body = normalize_config("automation", AUTOMATION_YAML)
    assert body["alias"] == "Blink – test"
    assert body["triggers"][0]["event_type"] == "blink_clip_analyzed"


def test_a_script_is_unwrapped_from_its_own_id() -> None:
    """scripts.yaml nests the body under the script id; the API wants the
    body alone, with the id in the URL."""
    body = normalize_config("script", SCRIPT_YAML)
    assert body["alias"] == "Blink – show cameras"
    assert "blink_show_cameras" not in body


def test_a_scene_is_unwrapped_from_its_list_and_loses_its_id() -> None:
    """scenes.yaml is a list, and Core writes its own id from the URL —
    leaving ours in would store the key twice."""
    body = normalize_config("scene", SCENE_YAML)
    assert body["name"] == "Blink security alert"
    assert "id" not in body
    assert body["entities"]["light.porch"]["state"] == "on"


def test_a_recipe_that_generates_more_than_a_script_is_refused() -> None:
    """The sync-now recipe emits a rest_command as well, which the config
    API cannot create — copying it is the only way."""
    combined = """rest_command:
  blink_sync_now:
    url: "http://x/api/download-now"
blink_sync_now:
  alias: Blink – sync
  sequence: []
"""
    with pytest.raises(HAConfigError, match="more than a script"):
        normalize_config("script", combined)


@pytest.mark.parametrize(
    ("kind", "text", "match"),
    [
        ("blueprint", AUTOMATION_YAML, "cannot be created"),
        ("automation", "alias: [unclosed", "could not be parsed"),
        ("automation", "just a string", "single configuration block"),
        ("automation", "", "single configuration block"),
        ("scene", AUTOMATION_YAML, "Expected a scene entry"),
        ("scene", "[]", "Expected a scene entry"),
        ("script", "blink_x: not-a-mapping", "Expected a script body"),
    ],
)
def test_unusable_input_is_refused_with_a_reason(
    kind: str, text: str, match: str
) -> None:
    with pytest.raises(HAConfigError, match=match):
        normalize_config(kind, text)


def test_a_scene_list_entry_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(HAConfigError, match="single configuration block"):
        normalize_config("scene", "- just a string\n")


# ---------------------------------------------------------------------------
# HAConfigWriter
# ---------------------------------------------------------------------------


def _mock_session(status: int = 200) -> tuple[MagicMock, MagicMock]:
    resp = AsyncMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=resp)
    session.closed = False
    return session, resp


async def test_create_posts_the_config_and_leaves_reloading_to_core() -> None:
    """Core's config view reloads the domain itself via its post_write_hook,
    so a reload call from here would only ever reload it twice."""
    writer = HAConfigWriter("token")
    session, _ = _mock_session()
    writer._session = session

    name = await writer.create("automation", "blink_test", AUTOMATION_YAML)

    # The alias, not "automation.blink_test": Home Assistant builds an
    # automation's entity id from its alias, so the object id is not it.
    assert name == "Blink – test"
    urls = [call.args[0] for call in session.post.call_args_list]
    assert urls == [
        "http://supervisor/core/api/config/automation/config/blink_test",
    ]
    # The body is the parsed config, not the YAML text.
    assert session.post.call_args_list[0].kwargs["json"]["alias"] == "Blink – test"


async def test_create_uses_the_callers_id_so_pressing_it_twice_updates() -> None:
    writer = HAConfigWriter("token")
    session, _ = _mock_session()
    writer._session = session

    await writer.create("scene", "blink_security_alert", SCENE_YAML)
    await writer.create("scene", "blink_security_alert", SCENE_YAML)

    urls = {call.args[0] for call in session.post.call_args_list}
    assert "http://supervisor/core/api/config/scene/config/blink_security_alert" in urls


async def test_create_without_a_token_says_why() -> None:
    writer = HAConfigWriter("")
    with pytest.raises(HAConfigError, match="no Home Assistant API token"):
        await writer.create("automation", "blink_test", AUTOMATION_YAML)


async def test_create_without_an_id_is_refused() -> None:
    writer = HAConfigWriter("token")
    with pytest.raises(HAConfigError, match="An id is required"):
        await writer.create("automation", "", AUTOMATION_YAML)


@pytest.mark.parametrize(
    ("status", "match"),
    [
        (401, "administrator"),
        (403, "administrator"),
        (404, "no configuration API"),
        (400, "rejected the configuration"),
        (500, "HTTP 500"),
    ],
)
async def test_http_failures_are_explained(status: int, match: str) -> None:
    writer = HAConfigWriter("token")
    session, _ = _mock_session(status)
    writer._session = session
    with pytest.raises(HAConfigError, match=match):
        await writer.create("automation", "blink_test", AUTOMATION_YAML)


async def test_a_transport_failure_is_explained() -> None:
    writer = HAConfigWriter("token")
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    session.closed = False
    writer._session = session
    with pytest.raises(HAConfigError, match="Could not reach Home Assistant"):
        await writer.create("automation", "blink_test", AUTOMATION_YAML)


async def test_close_is_safe_to_call_without_a_session() -> None:
    writer = HAConfigWriter("token")
    await writer.close()

    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()
    writer._session = session
    await writer.close()
    session.close.assert_awaited_once()


async def test_the_session_is_created_once_and_reused() -> None:
    writer = HAConfigWriter("token")
    try:
        first = writer._get_session()
        assert writer._get_session() is first
    finally:
        await writer.close()


async def test_a_closed_session_is_replaced_rather_than_reused() -> None:
    writer = HAConfigWriter("token")
    closed = MagicMock()
    closed.closed = True
    writer._session = closed
    try:
        assert writer._get_session() is not closed
    finally:
        await writer.close()


# ---------------------------------------------------------------------------
# The object id is the last segment of the URL this posts to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "object_id",
    [
        # Collapses to /core/api/addons/self/options — a different Supervisor
        # endpoint, called with this add-on's own token.
        "../../../addons/self/options",
        "..%2f..%2fx",
        "a/b",
        "a?b=c",
        "a#fragment",
        "a b",
        "UPPER",
        "has-hyphen",
        "x" * 65,
        "..",
    ],
)
async def test_an_id_that_could_redirect_the_request_is_refused(object_id: str) -> None:
    writer = HAConfigWriter("token")
    posted: list[str] = []

    async def capture(url: str, payload: dict[str, object]) -> None:
        posted.append(url)

    writer._post = capture  # type: ignore[method-assign]
    try:
        with pytest.raises(HAConfigError, match="lowercase letters"):
            await writer.create("automation", object_id, AUTOMATION_YAML)
        # Refused before anything is sent, not after.
        assert posted == []
    finally:
        await writer.close()


@pytest.mark.parametrize(
    "object_id", ["blink_daily_summary", "blink_all_clear", "1710000000000", "a"]
)
async def test_a_real_object_id_is_accepted(object_id: str) -> None:
    writer = HAConfigWriter("token")
    posted: list[str] = []

    async def capture(url: str, payload: dict[str, object]) -> None:
        posted.append(url)

    writer._post = capture  # type: ignore[method-assign]
    try:
        entity = await writer.create("automation", object_id, AUTOMATION_YAML)
    finally:
        await writer.close()
    assert entity == "Blink – test"
    assert posted == [
        f"http://supervisor/core/api/config/automation/config/{object_id}"
    ]


# ---------------------------------------------------------------------------
# created_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # An automation and a script both carry an alias; a scene a name.
        ({"alias": "Blink – daily summary"}, "Blink – daily summary"),
        ({"name": "Blink security alert"}, "Blink security alert"),
        ({"alias": "  padded  "}, "padded"),
        # Neither, or an unusable one: fall back to the id rather than
        # reporting something the user cannot find.
        ({}, "blink_thing"),
        ({"alias": "   "}, "blink_thing"),
        ({"alias": 42}, "blink_thing"),
        ({"name": None}, "blink_thing"),
    ],
)
def test_created_name_reports_what_home_assistant_will_list(
    body: dict[str, object], expected: str
) -> None:
    assert created_name(body, "blink_thing") == expected
