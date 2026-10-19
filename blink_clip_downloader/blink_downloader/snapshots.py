"""Deciding which camera picture the Security Feed may show.

blinkpy keeps whatever bytes Blink's thumbnail request returned, and asks for a
thumbnail again only when Blink announces a new one. A picture Blink was still
writing when it was fetched (this add-on polls fastest right after motion, which
is when that is likeliest) therefore stays cached until the camera's next event.
A browser draws a JPEG that ends early without complaint -- Chrome's
``img.decode()`` even succeeds -- and leaves the rows that never arrived
transparent, so the tile shows the black behind it: everything under the top part
of the picture, or the whole tile. Nothing on the browser's side can tell, so
:class:`SnapshotKeeper` decodes every new image in full before it may be served,
keeps the last whole picture to show meanwhile, and asks Blink for the thumbnail
again, a few times and not continually, when the latest one is damaged.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from PIL import Image

_LOGGER = logging.getLogger(__name__)

# How many more times Blink is asked for a damaged thumbnail, and how far apart.
# Judgement calls, not measurements: long enough that a picture Blink was still
# finishing has finished, few enough that a thumbnail Blink never repairs costs a
# handful of small downloads rather than one per tile refresh.
RECOVERY_ATTEMPTS = 4
RECOVERY_INTERVAL_SECONDS = 30.0

_NOT_JUDGED = object()


def image_defect(data: bytes) -> str | None:
    """Why *data* cannot be drawn in full, or None when every part of it decodes.

    Relies on Pillow's default of raising for an image that ends early
    (``ImageFile.LOAD_TRUNCATED_IMAGES`` is off unless something turns it on,
    and nothing here does). Any failure at all means "not a whole picture":
    whatever the cause, it is not something to put in a tile.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


@dataclass
class _Camera:
    """What the keeper knows about one camera's picture."""

    # The cached object last examined, by identity: blinkpy returns the same
    # object until Blink announces a new thumbnail, so each is decoded once.
    judged: object = _NOT_JUDGED
    whole: bool = False
    # The newest whole picture seen, from blinkpy's cache or a fresh download.
    held: bytes | None = None
    tries_left: int = RECOVERY_ATTEMPTS
    next_try_at: float = 0.0
    # One request at a time per camera, so two open tabs share one judgement and
    # one download instead of each making their own.
    busy: asyncio.Lock = field(default_factory=asyncio.Lock)


class SnapshotKeeper:
    """Serves each camera's newest whole picture, and never a damaged one."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cameras: dict[str, _Camera] = {}

    async def latest(
        self,
        name: str,
        cached: bytes | None,
        download: Callable[[], Awaitable[bytes | None]],
    ) -> bytes | None:
        """The picture to show for *name*, or None when no whole one exists.

        *cached* is what blinkpy is holding for the camera. *download* fetches
        the thumbnail Blink currently names, afresh, and returns None rather
        than raising when it cannot. A damaged *cached* image is replaced by the
        last whole picture, or a re-download of the thumbnail if that works.
        """
        camera = self._cameras.setdefault(name.casefold(), _Camera())
        async with camera.busy:
            if cached is not camera.judged:
                await self._judge(name, camera, cached)
            if camera.whole:
                return cached
            await self._recover(name, camera, download)
            return camera.held

    async def _judge(self, name: str, camera: _Camera, cached: bytes | None) -> None:
        """Decode a cached image blinkpy has not shown us before."""
        defect = (
            await asyncio.to_thread(image_defect, cached)
            if cached
            else "nothing is cached yet"
        )
        # Recorded only now: a request cancelled during the decode above must not
        # leave an image marked as examined that never was.
        camera.judged = cached
        camera.whole = defect is None
        camera.tries_left = RECOVERY_ATTEMPTS
        camera.next_try_at = 0.0
        if camera.whole:
            camera.held = cached
        elif cached:
            _LOGGER.warning(
                "Security Feed: the latest snapshot Blink gave for %r is not a whole "
                "picture (%s, %d bytes), so it will not be shown",
                name,
                defect,
                len(cached),
            )

    async def _recover(
        self,
        name: str,
        camera: _Camera,
        download: Callable[[], Awaitable[bytes | None]],
    ) -> None:
        """Ask Blink for the thumbnail again, when it is time and tries remain."""
        now = self._clock()
        if camera.tries_left == 0 or now < camera.next_try_at:
            return
        camera.tries_left -= 1
        camera.next_try_at = now + RECOVERY_INTERVAL_SECONDS
        fresh = await download()
        defect = (
            await asyncio.to_thread(image_defect, fresh)
            if fresh
            else "Blink sent no picture"
        )
        if defect is None:
            camera.held = fresh
            camera.tries_left = 0
            _LOGGER.info(
                "Security Feed: asked Blink again and got a whole snapshot for %r", name
            )
        elif camera.tries_left == 0:
            _LOGGER.warning(
                "Security Feed: Blink still has no whole snapshot for %r after %d "
                "tries (%s); %s until the camera records something new",
                name,
                RECOVERY_ATTEMPTS,
                defect,
                "keeping the last whole one"
                if camera.held
                else "there is none to show",
            )
