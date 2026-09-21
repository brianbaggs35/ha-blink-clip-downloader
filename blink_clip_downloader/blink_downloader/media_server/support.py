"""Request plumbing every route module shares, and nothing route-specific.

The security middleware and CSP, JSON body parsing with a consistent error
shape, paging with bounded offsets, and the handful of error strings the
API repeats. Pure functions and constants only — no handler, no state — so
a route module can import from here without importing another route module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

_CLIP_NOT_FOUND = "Clip not found"


_ARCHIVE_NOT_FOUND = "Archive not found"


_INVALID_JSON_BODY = "Invalid JSON body"


_INVALID_REQUEST_BODY = "Invalid request body"


_LIVE_VIEW_NOT_AVAILABLE = "Live View is not available"


_GDRIVE_NOT_AVAILABLE = "Google Drive backup is not available"


_SYNC_MODULES_NOT_AVAILABLE = "Not connected to Blink"


_NOTIFICATIONS_NOT_CONFIGURED = "Notifications not configured"


_FINETUNE_REQUIRES_MOONDREAM_CLOUD = "Fine-tuning requires ai_provider=moondream_cloud"


_CAMERA_CONFIGS_SAVE_ERROR = "Could not save camera configs: %s"


_AI_FEEDBACK_ROUTE = "/api/ai/feedback/{clip_id}"


# Surfaced to the caller (not just logged) wherever a settings/state write to
# disk fails — a prior version of several of these handlers logged the
# OSError but still responded as if the write had succeeded, silently
# discarding the change while the web UI showed a "saved" toast for it.
_SETTINGS_WRITE_FAILED = "Could not save — check the add-on logs"


# Strict allowlist for the Live View HLS file-serving route — the real
# defense against path traversal: this structurally rejects anything with a
# "/", "..", or an unexpected extension before the filesystem is ever
# touched (see _handle_liveview_hls_file). Must match live_view.py's own
# _HLS_PLAYLIST_NAME/_HLS_SEGMENT_PATTERN naming.
_LIVEVIEW_FILENAME_RE = re.compile(r"^(stream\.m3u8|seg_\d{5}\.ts)$")


# Upper bound on how many frames _handle_clip_frames will ever extract from
# one clip, whether derived from duration (the ~1fps default) or requested
# explicitly via ?count=. Bounds ffmpeg's work and the JSON response size
# (each frame is a base64 480px-wide JPEG) for an unusually long clip.
_MAX_CLIP_FRAMES = 60


#: Most pending feedback rows one "Train" press may consume. Each example
#: costs a frame extraction and a paid Moondream API call, so this is a
#: spend ceiling as much as a paging one — press it again for more.
_MAX_FINETUNE_TRAIN_BATCH = 100


_ARCHIVE_CLIPS_PAGE_SIZE = 50


_MAX_ARCHIVE_CLIPS_PAGE_SIZE = 200


# Built by `npm run build` in frontend/ (vite.config.ts writes straight into
# this directory) — the Dockerfile's frontend-builder stage runs that build
# before the image is packaged, so this always exists in a shipped add-on.
# In a bare checkout without a build (e.g. running the Python test suite
# alone) it won't exist; _handle_index reports that clearly instead of
# serving nothing or a confusing 404.
# Anchored on the blink_downloader package rather than on this file:
# media_server became a package, so `parent` alone would point one
# directory too deep and every page would 500 with "run npm run build".
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# Content-Security-Policy restricting everything to same-origin. Video.js
# is bundled into the Vue build's own JS/CSS (see frontend/src/components/
# library/ClipModal.vue) rather than loaded from a CDN, so no third-party
# script/style/font origin needs to be allow-listed here. 'unsafe-inline' on
# script-src covers the
# `__HAROOT__` ingress-path bootstrap snippet in index.html; on style-src it
# covers Vue's runtime `:style` bindings, which render as inline `style="..."`
# attributes rather than a `<style>` element.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    # Live View's HLS playback depends on this. Video.js's VHS engine
    # transmuxes each MPEG-TS segment in a Web Worker it creates from a
    # blob: URL; with no worker-src, the browser falls back to script-src,
    # which has no blob:, and blocks the worker outright. The symptom is
    # not an error — the player attaches its MSE source (allowed by
    # media-src below), fetches one segment, transmuxes nothing, and sits
    # on a black frame forever while the playlist keeps polling. Library
    # clips are unaffected: a plain MP4 plays natively with no worker.
    "worker-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline' data:; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'"
)


def _is_direct_port_kiosk(request: web.Request) -> bool:
    """True for a `?kiosk=1` page fetched over the direct port, not ingress.

    Kiosk mode exists to be embedded in a Home Assistant dashboard's iframe
    card (see the Automations tab's Dashboards builder). Home Assistant runs
    on its own port, so that frame is cross-origin and
    ``X-Frame-Options: SAMEORIGIN`` refuses it outright — the card renders
    blank, with the refusal only visible in the browser console.

    The exemption is deliberately as narrow as it can be:

    * **Ingress requests never qualify.** They carry ``X-Ingress-Path`` and
      keep SAMEORIGIN, so the authenticated panel — the one reachable from
      outside the LAN through Home Assistant's own auth — cannot be framed
      by anyone.
    * **Only the kiosk display mode qualifies**, not the ordinary UI.

    What remains is a narrow clickjacking surface on a port that already
    serves this UI to anyone who can reach it, with no authentication of its
    own. Deleting the two lines above restores SAMEORIGIN everywhere, at the
    cost of the iframe card no longer loading.
    """
    if request.headers.get("X-Ingress-Path"):
        return False
    return request.query.get("kiosk") == "1"


async def _json_object(
    request: web.Request,
    message: str = _INVALID_JSON_BODY,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read a request body that is expected to be a JSON *object*.

    Every handler that takes a body immediately calls ``body.get(...)`` on
    it. Catching only the parse left a second, equally reachable failure
    open: a body that is perfectly valid JSON but is a list, string or
    number parses fine and then raises ``AttributeError`` on that first
    ``.get`` — a bare 500 for what is plainly a bad request. Both cases
    land here instead.

    *default* is for the handlers that are deliberately lenient about being
    called with no body at all (Live View's stop, say, which is meant to be
    safe to fire on tab unload); they get the default back rather than a
    400.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        return body
    if default is not None:
        return default
    raise web.HTTPBadRequest(text=message)


#: Ceiling on any ``offset`` query parameter. int() happily parses a number
#: far larger than PostgreSQL's bigint, which then fails inside asyncpg as a
#: bare 500 rather than an empty page — and no real library is a million
#: clips deep, so clamping costs nothing a caller would notice.
_MAX_OFFSET = 1_000_000


def _paging(
    query: Any, default_limit: int, max_limit: int, min_limit: int = 0
) -> tuple[int, int]:
    """Read ``limit``/``offset`` from *query*, clamped into usable bounds.

    A negative LIMIT means "no limit" to some engines and a negative OFFSET
    is invalid, so both are floored; both are also capped, so a crafted
    query string can neither dump the whole table nor overflow the column
    type. Anything unparseable falls back to the defaults rather than
    erroring — this is a listing, and a caller who sends nonsense is better
    served the first page than a stack trace.
    """
    try:
        limit = max(min_limit, min(int(query.get("limit", default_limit)), max_limit))
        offset = max(0, min(int(query.get("offset", 0)), _MAX_OFFSET))
    except (TypeError, ValueError):
        return default_limit, 0
    return limit, offset


async def _optional_clip_id(request: web.Request) -> str | None:
    """Read an optional ``clip_id`` from a request body that may not exist.

    Shared by the retry and clear handlers, whose bodies are entirely
    optional (unlike most POST handlers here): no body, an empty body and
    invalid JSON all mean "every failed upload", not a 400.
    """
    try:
        body = await request.json()
        raw = body.get("clip_id")
    except Exception:  # noqa: BLE001
        return None
    return str(raw) if raw else None


@web.middleware
async def _security_middleware(
    request: web.Request, handler: Callable
) -> web.StreamResponse:
    """Reject requests Postgres cannot answer, and attach security headers.

    A NUL byte is the one character PostgreSQL's text type cannot hold at
    all, so any request carrying one in its path or query string — a clip
    id, a camera name, a search term — fails inside asyncpg as an unhandled
    encoding error and surfaces as a bare 500. Rejecting it once here, for
    every route at once, is both the honest answer (a URL containing a NUL
    is malformed, not a server fault) and the only version of this fix that
    keeps covering routes added later.
    """
    # rel_url is the yarl-decoded view; request.path_qs keeps the raw
    # percent-encoded form, where a "%00" would sail straight past this.
    url = request.rel_url
    if "\x00" in url.path or any("\x00" in v for v in url.query.values()):
        raise web.HTTPBadRequest(text="Request contains an invalid null byte")
    response = await handler(request)
    if not response.prepared:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if not _is_direct_port_kiosk(request):
            response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if response.content_type == "text/html":
            response.headers.setdefault("Content-Security-Policy", _CSP)
    return response
