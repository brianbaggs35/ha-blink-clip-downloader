"""The Library tab's API: clips, their media, and their metadata.

Listing and filtering, the video stream and thumbnail a player asks for,
starring and tagging, the ZIP export, and deleting a clip — including its
Google Drive copy, which is the one piece of deletion that reaches outside
this container.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import zipfile
from pathlib import Path

import aiofiles
from aiohttp import web

from ..database import ClipFilters
from .storage import StorageRoutesMixin
from .support import (
    _CLIP_NOT_FOUND,
    _INVALID_JSON_BODY,
    _INVALID_REQUEST_BODY,
    _paging,
)

_LOGGER = logging.getLogger(__name__)

# The file app.TRIGGER_FILE polls for between cycles — touched by Download
# Now when no trigger_download callback was wired in. A copy rather than an
# import because app.py imports this package, not the other way round.
_TRIGGER_FILE = Path("/data/trigger_download")


class LibraryRoutesMixin(StorageRoutesMixin):
    """Clips: list, fetch, stream, annotate, export, delete.

    Inherits :class:`StorageRoutesMixin` for ``_delete_gdrive_backup``:
    deleting a clip has to remove its Google Drive copy too, and that is
    Drive behaviour rather than library behaviour, so it lives with the
    rest of the Drive code instead of being duplicated here.
    """

    def _register_library_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/clips", self._handle_list_clips)
        app.router.add_get("/api/clips/{id}", self._handle_get_clip)
        app.router.add_delete("/api/clips/{id}", self._handle_delete_clip)
        app.router.add_put("/api/clips/{id}/star", self._handle_star_clip)
        app.router.add_put("/api/clips/{id}/tags", self._handle_set_tags)
        app.router.add_get("/api/clips/{id}/stream", self._handle_stream)
        app.router.add_get("/api/clips/{id}/thumb", self._handle_thumbnail)
        app.router.add_post("/api/clips/export-zip", self._handle_export_zip)
        app.router.add_post("/api/download-now", self._handle_download_now)

    async def _handle_list_clips(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        limit, offset = _paging(q, default_limit=48, max_limit=200)

        starred_raw = q.get("starred")
        if starred_raw == "1":
            starred = True
        elif starred_raw == "0":
            starred = False
        else:
            starred = None
        notified_only = q.get("notified") == "1"
        recognized_only = q.get("recognized") == "1"
        archived = q.get("archived") == "1"
        min_confidence = (
            self._analysis_queue.min_confidence if self._analysis_queue else 0.0
        )

        clips = await self._db.get_clips(
            ClipFilters(
                camera=q.get("camera") or None,
                since=q.get("since") or None,
                until=q.get("until") or None,
                starred=starred,
                source=q.get("source") or None,
                tag=q.get("tag") or None,
                search=q.get("search") or None,
                archived=archived,
                archive_path=q.get("archive_path") or None,
                notified_only=notified_only,
                recognized_only=recognized_only,
                min_confidence=min_confidence,
            ),
            sort=q.get("sort") or "newest",
            limit=limit,
            offset=offset,
        )
        return web.json_response(clips)

    async def _handle_get_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response(clip)

    async def _handle_delete_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        # Trash the Drive copy first — a Drive failure here must not block
        # the local/DB delete below, which proceeds regardless of whether
        # this succeeded.
        gdrive_deleted = await self._delete_gdrive_backup(clip)

        file_path = Path(clip["file_path"])
        if file_path.exists():
            try:
                file_path.unlink()
                thumb = file_path.with_suffix(".jpg")
                if thumb.exists():
                    thumb.unlink()
            except OSError as exc:
                _LOGGER.warning("Could not delete file %s: %s", file_path, exc)
        await self._db.delete_clip(clip_id)
        return web.json_response({"deleted": True, "gdrive_deleted": gdrive_deleted})

    async def _handle_star_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        try:
            body = await request.json()
            starred = bool(body.get("starred", True))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
        found = await self._db.star_clip(clip_id, starred)
        if not found:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response({"id": clip_id, "starred": starred})

    async def _handle_set_tags(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        try:
            body = await request.json()
            tags = [str(t) for t in body.get("tags", [])]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
        found = await self._db.set_tags(clip_id, tags)
        if not found:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response({"id": clip_id, "tags": tags})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        file_path = Path(clip["file_path"])
        if not file_path.exists():
            raise web.HTTPNotFound(text="Clip file not found on disk")

        # aiohttp's FileResponse uses the OS sendfile() syscall on Linux,
        # bypassing the Python interpreter for the actual byte transfer.
        # It automatically handles Range requests (206 Partial Content),
        # ETag/Last-Modified caching, and correct Accept-Ranges headers —
        # all of which contribute to stutter-free video seeking on the Pi.
        return web.FileResponse(
            file_path,
            chunk_size=262_144,
            headers={
                "Content-Disposition": f'inline; filename="{file_path.name}"',
                # Allow the browser to cache video segments so re-seeking an
                # already-watched section never round-trips to the server.
                "Cache-Control": "public, max-age=3600",
            },
        )

    async def _handle_thumbnail(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound()

        thumb = Path(clip["file_path"]).with_suffix(".jpg")
        if thumb.exists():
            return web.FileResponse(
                thumb,
                headers={"Cache-Control": "public, max-age=3600"},
            )

        raise web.HTTPNotFound(text="Thumbnail not available")

    async def _handle_export_zip(self, request: web.Request) -> web.StreamResponse:
        """Package up to 25 selected clips into a ZIP and return it."""
        try:
            body = await request.json()
            clip_ids = [str(c) for c in body.get("ids", [])][:25]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_REQUEST_BODY)

        if not clip_ids:
            raise web.HTTPBadRequest(text="No clip IDs provided")

        paths: list[Path] = []
        for cid in clip_ids:
            clip = await self._db.get_clip(cid)
            if not clip:
                continue
            fp = Path(clip["file_path"])
            if fp.exists():
                paths.append(fp)

        if not paths:
            raise web.HTTPNotFound(text="No clip files found on disk")

        # Built into a scratch file in a worker thread, then streamed.
        # Twenty-five clips is tens of megabytes; assembling that in a
        # BytesIO and then copying it again into the response body held two
        # full copies in memory at once, on a box where the add-on may only
        # have a few hundred megabytes to itself — and deflating them inline
        # blocked the event loop (and so the whole web UI) for the duration.
        fd, tmp_name = tempfile.mkstemp(prefix="blink-export-", suffix=".zip")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            await asyncio.to_thread(self._write_export_zip, tmp_path, paths)
            response = web.StreamResponse(
                headers={
                    "Content-Disposition": 'attachment; filename="blink-clips.zip"',
                    "Content-Length": str(tmp_path.stat().st_size),
                }
            )
            response.content_type = "application/zip"
            await response.prepare(request)
            async with aiofiles.open(tmp_path, "rb") as fh:
                while chunk := await fh.read(262_144):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def _write_export_zip(zip_path: Path, paths: list[Path]) -> None:
        """Compress *paths* into *zip_path*. Runs in a worker thread."""
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in paths:
                zf.write(fp, fp.name)

    async def _handle_download_now(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._trigger_download:
            self._trigger_download()
            return web.json_response({"triggered": True})
        try:
            _TRIGGER_FILE.touch()
        except OSError:
            pass
        return web.json_response({"triggered": True})
