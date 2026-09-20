"""Creating automations, scripts and scenes in Home Assistant directly.

The Automations tab generates YAML; this writes it into Home Assistant so
the user does not have to copy it anywhere. Core exposes a config API for
exactly the three things it stores in its own editable files:

* ``POST /api/config/automation/config/<id>`` -> automations.yaml
* ``POST /api/config/script/config/<id>``     -> scripts.yaml
* ``POST /api/config/scene/config/<id>``      -> scenes.yaml

Nothing else the tab generates can be created this way, and the tab says so
rather than pretending: blueprints, helpers and Lovelace views are
WebSocket-only, and the ``configuration.yaml`` blocks (rest_command, the
template sensors, the Generic Camera platform) have no API at all. Those
keep their copy/download buttons.

Each kind of YAML needs a different unwrapping before Core will take it —
see :func:`normalize_config`. The object id comes from the caller rather
than from the YAML, so pressing the button twice updates the same
automation instead of piling up duplicates.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import yaml

_LOGGER = logging.getLogger(__name__)

# Same internal Supervisor route notifier.py uses — see its comment.
_HA_API = "http://supervisor/core/api"  # NOSONAR
_TIMEOUT = aiohttp.ClientTimeout(total=20)

#: What Core calls each kind in its config API path. Anything absent from
#: here is not creatable through this API at all.
_KINDS: dict[str, str] = {
    "automation": "automation",
    "script": "script",
    "scene": "scene",
}


class HAConfigError(Exception):
    """A create attempt failed, with a message meant for the user."""


def normalize_config(kind: str, text: str) -> dict[str, Any]:
    """Parse generated YAML into the body Core's config API expects.

    Each kind arrives in the shape a user would paste into a file, which is
    not the shape the API takes:

    * an **automation** is already the right shape (a mapping of
      alias/triggers/actions) and passes through;
    * a **script** is written as ``<script_id>:`` with the body nested under
      it, because that is how scripts.yaml looks — the API wants the body
      alone, with the id in the URL;
    * a **scene** is written as a one-item list, because scenes.yaml is a
      list — the API wants the single mapping, without the ``id`` key it
      injects itself.

    Raises :class:`HAConfigError` with something a user can act on, since
    every failure here is visible in the UI.
    """
    if kind not in _KINDS:
        raise HAConfigError(f"{kind!r} cannot be created from here.")
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HAConfigError(f"The generated YAML could not be parsed: {exc}") from exc

    if kind == "scene":
        if not isinstance(parsed, list) or not parsed:
            raise HAConfigError("Expected a scene entry, got something else.")
        parsed = parsed[0]
        if isinstance(parsed, dict):
            # Core writes its own id from the URL; leaving ours in would
            # store a duplicate key.
            parsed = {k: v for k, v in parsed.items() if k != "id"}

    if not isinstance(parsed, dict) or not parsed:
        raise HAConfigError("Expected a single configuration block.")

    if kind == "script":
        # scripts.yaml nests the body under its own id. A block with more
        # than one top-level key is a recipe that also generates something
        # else (a rest_command, a helper) and is not creatable as a unit.
        if len(parsed) != 1:
            raise HAConfigError(
                "This one generates more than a script on its own — copy it instead."
            )
        body = next(iter(parsed.values()))
        if not isinstance(body, dict):
            raise HAConfigError("Expected a script body.")
        return body

    return parsed


class HAConfigWriter:
    """Writes configuration into Home Assistant over its REST config API."""

    def __init__(self, supervisor_token: str) -> None:
        self._token = supervisor_token
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def create(self, kind: str, object_id: str, text: str) -> str:
        """Create (or replace) one automation/script/scene. Returns its id.

        The id is the caller's, not the YAML's, so pressing Create twice
        updates the same object rather than accumulating copies of it.
        """
        body = normalize_config(kind, text)
        if not object_id:
            raise HAConfigError("An id is required to create this.")
        if not self._token:
            raise HAConfigError(
                "This add-on has no Home Assistant API token — it only gets "
                "one when Home Assistant starts it as an add-on."
            )

        domain = _KINDS[kind]
        # No reload call afterwards: Core's own config view runs a
        # post_write_hook that reloads the domain on every successful write
        # (see homeassistant/components/config/{automation,script,scene}.py),
        # so asking for a second one would just reload twice.
        await self._post(f"{_HA_API}/config/{domain}/config/{object_id}", body)
        _LOGGER.info("Created %s.%s in Home Assistant", domain, object_id)
        return f"{domain}.{object_id}"

    async def _post(self, url: str, payload: dict[str, Any]) -> None:
        try:
            session = self._get_session()
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (200, 201):
                    return
                raise HAConfigError(_describe_failure(resp.status))
        except (aiohttp.ClientError, OSError) as exc:
            raise HAConfigError(f"Could not reach Home Assistant: {exc}") from exc


def _describe_failure(status: int) -> str:
    """Turn an HTTP status into something worth showing a user."""
    if status in (401, 403):
        return (
            "Home Assistant refused the request. Its configuration API needs "
            "an administrator, and this add-on's token is not being granted "
            "that."
        )
    if status == 404:
        return (
            "Home Assistant has no configuration API for this. It is only "
            "available when the matching integration is loaded and its file "
            "is editable from the UI."
        )
    if status == 400:
        return "Home Assistant rejected the configuration as invalid."
    return f"Home Assistant returned HTTP {status}."
