"""Everything a suspicious-activity alert carries beyond its text.

The key frame, where to open the clip, the "Not a threat" button, and the
local time the clip was recorded — gathered once per alert here and handed
to each channel in ``notification_channels.py``, which uses what it can:
Discord and email embed the picture and link to the clip, the companion app
shows the picture, opens the clip on tap and offers the button, and Home
Assistant's persistent notification stays text.

Every part is optional and fails on its own. An alert is the one message
that must go out, so a picture that cannot be extracted, a link that cannot
be resolved or a key that cannot be read each just leave that part out —
nothing here raises into the dispatcher.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import alert_media

if TYPE_CHECKING:
    from .alert_actions import AlertActionSigner
    from .alert_media import AlertImageStore
    from .analyzer import AnalysisResult
    from .ha_links import HALinkResolver

_LOGGER = logging.getLogger(__name__)

# 24-hour and with the zone, so it cannot be misread whoever gets it.
_LOCAL_TIME_FORMAT = "%a %d %b %Y, %H:%M:%S %Z"


def parse_instant(value: str) -> datetime | None:
    """An ISO timestamp as an aware datetime (naive = UTC); None if unparseable."""
    try:
        instant = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant


def format_local_time(instant: datetime) -> str:
    """*instant* in the add-on's time zone — Home Assistant's, via ``TZ``."""
    return instant.astimezone().strftime(_LOCAL_TIME_FORMAT).strip()


@dataclass(frozen=True)
class AlertExtras:
    """What an alert carries besides its text; every field may be empty."""

    clip_id: str = ""
    # When the clip was recorded, for the alert's text and Discord's timestamp.
    recorded_local: str = ""
    recorded_iso: str = ""
    # The key frame as JPEG bytes (Discord and email attach it)...
    image: bytes | None = None
    # ...and as a Home-Assistant-relative URL the companion app fetches.
    image_url: str | None = None
    # Where the clip opens: relative for the companion app, absolute for
    # channels outside Home Assistant.
    open_path: str | None = None
    open_url: str | None = None
    # The action string behind the phone alert's "Not a threat" button.
    not_a_threat_action: str | None = None


class RichAlertBuilder:
    """Gathers an alert's extras from the clip, its verdict and Home Assistant."""

    def __init__(
        self,
        *,
        links: HALinkResolver | None = None,
        image_store: AlertImageStore | None = None,
        signer: AlertActionSigner | None = None,
        include_image: bool = True,
        key_frame: Callable[[str, AnalysisResult], Awaitable[bytes | None]]
        | None = None,
    ) -> None:
        self._links = links
        self._image_store = image_store
        self._signer = signer
        self._include_image = include_image
        self._key_frame = key_frame or alert_media.key_frame

    async def close(self) -> None:
        if self._links is not None:
            await self._links.close()

    async def build(
        self,
        result: AnalysisResult,
        clip: dict[str, Any],
        *,
        attach_image: bool,
        phone: bool,
        external_link: bool,
    ) -> AlertExtras:
        """Extras for one alert about *clip*.

        *attach_image*: a channel will carry the picture itself (Discord,
        email). *phone*: the companion app will get the picture URL, the tap
        target and the button. *external_link*: a channel outside Home
        Assistant wants an absolute link. Nothing is fetched for a part no
        enabled channel will use.
        """
        clip_id = str(clip.get("id") or result.clip_id or "")
        recorded = parse_instant(str(clip.get("timestamp") or ""))
        extras: dict[str, Any] = {"clip_id": clip_id}
        if recorded is not None:
            extras["recorded_local"] = format_local_time(recorded)
            extras["recorded_iso"] = recorded.isoformat()

        want_picture = self._include_image and (
            attach_image or (phone and self._image_store is not None)
        )
        image = await self._picture(str(clip.get("path") or ""), result, want_picture)
        store = self._image_store
        if image is not None:
            if attach_image:
                extras["image"] = image
            if phone and store is not None:
                extras["image_url"] = await self._guard(
                    "the phone picture", asyncio.to_thread(store.save, clip_id, image)
                )

        if self._links is not None and clip_id:
            if phone:
                extras["open_path"] = await self._guard(
                    "the clip link", self._links.clip_path(clip_id)
                )
            if external_link:
                extras["open_url"] = await self._guard(
                    "the clip link", self._links.clip_url(clip_id)
                )
        if phone and self._signer is not None and clip_id:
            extras["not_a_threat_action"] = self._signer.action_for(clip_id)
        return AlertExtras(**extras)

    async def _picture(
        self, clip_path: str, result: AnalysisResult, wanted: bool
    ) -> bytes | None:
        if not wanted or not clip_path:
            return None
        return await self._guard("the key frame", self._key_frame(clip_path, result))

    @staticmethod
    async def _guard[T](what: str, pending: Awaitable[T | None]) -> T | None:
        """Await *pending*; log and return None instead of raising."""
        try:
            return await pending
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Alert sent without %s: %s", what, exc)
            return None
