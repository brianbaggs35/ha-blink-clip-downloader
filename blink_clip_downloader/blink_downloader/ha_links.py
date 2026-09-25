"""Links into Home Assistant that open one clip in this add-on's panel.

An alert's "open the clip" link has to go through Home Assistant rather
than to the add-on's own port: that is what works away from home (through
whatever remote access Home Assistant already has) and what signs the
person in. Home Assistant 2026.2 moved add-on panels to ``/app/<slug>`` and
began passing whatever follows the slug to the page inside it (the
``home-assistant/properties`` message the frontend's ``useClipDeepLink``
listens for), so from that version a link can name the clip itself. Older
versions serve the panel at ``/hassio/ingress/<slug>`` and treat anything
after the slug as part of the slug, so the link there opens the add-on and
stops.

Nothing here is guessed: the slug comes from Supervisor, the version and the
instance's own URLs from Core, and a link is left out entirely when either
is unavailable — a missing link is harmless, a wrong one sends someone to an
error page from their lock screen.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Internal HA Supervisor API on the isolated `hassio` Docker network — not
# exposed externally and does not terminate TLS, so http:// is correct here.
_SUPERVISOR = "http://supervisor"  # NOSONAR
# Short on purpose: this runs while an alert is waiting to go out.
_TIMEOUT = aiohttp.ClientTimeout(total=5)
# How long a failed lookup is remembered before the next alert tries again.
_RETRY_AFTER_SECONDS = 300.0
# How long a good answer is trusted: long enough that alerts never wait on a
# lookup, short enough that a Home Assistant update (which restarts Core,
# not this add-on) or a changed external URL reaches alerts the same day.
_REFRESH_AFTER_SECONDS = 3600.0
# The first Home Assistant release whose panel accepts a path after the slug.
_APP_PANEL_VERSION = (2026, 2)


@dataclass(frozen=True)
class _HAContext:
    slug: str
    version: tuple[int, int]
    base_url: str


def parse_core_version(version: str) -> tuple[int, int] | None:
    """``"2026.9.1"`` → ``(2026, 9)``; None for anything unparseable."""
    parts = version.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def clip_panel_path(slug: str, version: tuple[int, int], clip_id: str) -> str:
    """Path, relative to Home Assistant, that opens *clip_id* (or the panel).

    Only 2026.2 and later can open the clip itself; see the module docstring.
    """
    if version >= _APP_PANEL_VERSION:
        return f"/app/{quote(slug)}/clip/{quote(clip_id, safe='')}"
    return f"/hassio/ingress/{quote(slug)}"


class HALinkResolver:
    """Builds Home Assistant links to a clip, from facts looked up once."""

    def __init__(self, supervisor_token: str) -> None:
        self._token = supervisor_token
        self._context: _HAContext | None = None
        self._looked_up_at = 0.0
        self._failed_at: float | None = None
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def clip_path(self, clip_id: str) -> str | None:
        """Home-Assistant-relative path to *clip_id*, or None if unknown.

        Relative on purpose for the companion app, which resolves it against
        whichever of the instance's addresses it is currently using.
        """
        context = await self._get_context()
        if context is None:
            return None
        return clip_panel_path(context.slug, context.version, clip_id)

    async def clip_url(self, clip_id: str) -> str | None:
        """Absolute link to *clip_id*, for channels outside Home Assistant.

        Uses the instance's configured external URL, falling back to its
        internal one; None when neither is set, since an email or Discord
        message has nothing to resolve a relative path against.
        """
        context = await self._get_context()
        if context is None or not context.base_url:
            return None
        return context.base_url + clip_panel_path(
            context.slug, context.version, clip_id
        )

    async def _get_context(self) -> _HAContext | None:
        now = time.monotonic()
        if (
            self._context is not None
            and now - self._looked_up_at < _REFRESH_AFTER_SECONDS
        ):
            return self._context
        if not self._token:
            return None
        if self._failed_at is not None and now - self._failed_at < _RETRY_AFTER_SECONDS:
            return self._context
        context = await self._look_up()
        if context is None:
            self._failed_at = time.monotonic()
            # An hour-old answer beats none: slugs and URLs rarely change.
            return self._context
        self._context = context
        self._looked_up_at = time.monotonic()
        self._failed_at = None
        return context

    async def _look_up(self) -> _HAContext | None:
        try:
            addon = await self._get_json(f"{_SUPERVISOR}/addons/self/info")
            config = await self._get_json(f"{_SUPERVISOR}/core/api/config")
        except (
            aiohttp.ClientError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:
            _LOGGER.warning("Could not look up Home Assistant links: %s", exc)
            return None
        slug = str((addon.get("data") or {}).get("slug") or "")
        version = parse_core_version(str(config.get("version") or ""))
        if not slug or version is None:
            _LOGGER.warning(
                "Home Assistant did not report this add-on's slug and version; "
                "alerts will not link to the clip"
            )
            return None
        base_url = str(config.get("external_url") or config.get("internal_url") or "")
        return _HAContext(slug, version, base_url.rstrip("/"))

    async def _get_json(self, url: str) -> dict:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        async with self._session.get(
            url, headers={"Authorization": f"Bearer {self._token}"}, timeout=_TIMEOUT
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"{url} returned HTTP {resp.status}")
            data = await resp.json()
        if not isinstance(data, dict):
            raise TypeError(f"{url} returned {type(data).__name__}, not an object")
        return data
