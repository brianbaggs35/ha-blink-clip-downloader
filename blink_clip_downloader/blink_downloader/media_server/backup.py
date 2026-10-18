"""The Backup & Restore tab's API.

Making a backup is one request that answers with the ZIP (``POST
/api/backup/create``) or hands it to Google Drive (``POST /api/backup/gdrive``).
Restoring is four, each its own button on the page, because a restore is
something to look over before it is done:

* ``POST /api/backup/uploads`` opens an upload; ``PUT
  /api/backup/uploads/{id}?offset=N`` sends it a piece at a time — in pieces
  because Home Assistant's ingress proxy refuses a request body over 16 MiB;
* ``POST .../inspect`` checks the file end to end and answers with what is in
  it and what restoring it would do, which is a real dry run of the restore;
* ``POST .../restore`` does it, and is refused for a file that has not been
  inspected;
* ``DELETE .../{id}`` throws the upload away.

A backup holds face data and settings, so none of this is reachable with the
access token Home Assistant's own automations carry: see
``access_control.TOKEN_ROUTES``, which deliberately lacks these routes.

Only one backup or restore runs at a time. A second is refused with a 409
rather than queued: it would be working from a library the first is changing.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiofiles
from aiohttp import web

from .. import __version__
from ..backup import (
    CHUNK_BYTES,
    BackupArchive,
    BackupError,
    RestoreHooks,
    StagedUpload,
    UploadError,
    create_backup,
    preview_backup,
    preview_files,
    preview_restore,
    restore_backup,
    summarize,
)
from ..gdrive_client import GDriveClient
from .core import _MediaServerBase
from .support import _GDRIVE_NOT_AVAILABLE, _json_object

_LOGGER = logging.getLogger(__name__)

#: The folder inside the connected Drive folder that backups go in, next to
#: the date folders clips are filed under.
_DRIVE_FOLDER = "Backups"

_BACKUP_BUSY = "Another backup or restore is already running. Try again in a moment."

_DRIVE_NOT_READY = {
    "not_connected": "Google Drive is not connected. Connect it on the Storage tab first.",
    "no_folder": "No Google Drive folder is chosen yet. Choose one on the Storage tab first.",
}

_Handler = Callable[[Any, web.Request], Awaitable[web.StreamResponse]]


def _reports_upload_errors(handler: _Handler) -> _Handler:
    """Answer an :class:`UploadError` as JSON with the status it carries.

    JSON rather than text because a piece sent out of order needs to say
    where the server actually is, and the page reads that to carry on.
    """

    @functools.wraps(handler)
    async def wrapped(self: Any, request: web.Request) -> web.StreamResponse:
        try:
            return await handler(self, request)
        except UploadError as exc:
            return web.json_response(
                {"error": str(exc), "received": exc.received}, status=exc.status
            )

    return wrapped


def _failure(
    exc: Exception, doing: str, *, unchanged: bool = False
) -> web.HTTPException:
    """The response for something that went wrong while *doing* something.

    A :class:`BackupError` already says what is wrong with the file. Anything
    else is logged in full and answered with the part a person can act on:
    the operating system's reason when it was one, else a pointer to the log.
    *unchanged* adds that nothing was changed, for the restore's database
    half, which rolls back.
    """
    if isinstance(exc, BackupError):
        text = str(exc) + (" Nothing was changed." if unchanged else "")
        return web.HTTPUnprocessableEntity(text=text)
    _LOGGER.error("%s failed", doing, exc_info=exc)
    if isinstance(exc, OSError):
        reason = exc.strerror or str(exc)
        if isinstance(exc.filename, str):
            reason += f" ({Path(exc.filename).name})"
        text = f"{doing} failed: {reason}."
    else:
        text = f"{doing} failed. The add-on logs have the reason."
    if unchanged:
        text += " Nothing was changed."
    return web.HTTPInternalServerError(text=text)


def _backup_name() -> str:
    return f"blink-clips-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip"


def _scratch_zip() -> Path:
    handle, name = tempfile.mkstemp(prefix="blink-backup-", suffix=".zip")
    os.close(handle)
    return Path(name)


def _flag(body: dict[str, Any], key: str, default: bool) -> bool:
    """A true/false option from a request body; anything else is a mistake."""
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise web.HTTPBadRequest(text=f"{key} must be true or false")
    return value


def _drive_refusal(client: GDriveClient) -> str:
    """Why Drive did not take an upload, from what the client noted."""
    if client.quota_exceeded:
        return "Google Drive is out of space, so it would not take the backup."
    if client.rate_limited:
        return (
            "Google Drive is limiting requests right now. Try again in a few minutes."
        )
    return (
        "Google Drive did not accept the backup. Check that it is still "
        "connected on the Storage tab."
    )


@dataclass(frozen=True)
class _DriveState:
    """Whether Google Drive can take a backup, and the folder it would go in."""

    state: str
    folder: str = ""


_UPLOAD_ID = "upload_id"


def _upload_id(request: web.Request) -> str:
    return request.match_info[_UPLOAD_ID]


class BackupRoutesMixin(_MediaServerBase):
    """Making a backup, and restoring one."""

    def _register_backup_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/backup/preview", self._handle_backup_preview)
        app.router.add_post("/api/backup/create", self._handle_backup_create)
        app.router.add_post("/api/backup/gdrive", self._handle_backup_gdrive)
        app.router.add_post("/api/backup/uploads", self._handle_upload_start)
        app.router.add_put("/api/backup/uploads/{upload_id}", self._handle_upload_chunk)
        app.router.add_delete(
            "/api/backup/uploads/{upload_id}", self._handle_upload_discard
        )
        app.router.add_post(
            "/api/backup/uploads/{upload_id}/inspect", self._handle_upload_inspect
        )
        app.router.add_post(
            "/api/backup/uploads/{upload_id}/restore", self._handle_upload_restore
        )

    # -- the one-at-a-time rule ----------------------------------------------

    @asynccontextmanager
    async def _backup_slot(self) -> AsyncGenerator[None]:
        """Hold the right to back up or restore, or fail at once with a 409."""
        if self._backup_lock.locked():
            raise web.HTTPConflict(text=_BACKUP_BUSY)
        async with self._backup_lock:
            yield

    # -- what a backup would hold --------------------------------------------

    def _backup_drive_state(self) -> _DriveState:
        """Whether a backup can go to Google Drive right now, and where to."""
        client = self._gdrive_client
        if client is None:
            return _DriveState("unavailable")
        if not client.connected:
            return _DriveState("not_connected")
        if not client.folder_id:
            return _DriveState("no_folder")
        return _DriveState("ready", f"{client.folder_name}/{_DRIVE_FOLDER}")

    async def _handle_backup_preview(self, _request: web.Request) -> web.Response:
        preview = await preview_backup(self._db)
        preview["clip_folder"] = str(self._clip_storage_dir)
        preview["gdrive"] = asdict(self._backup_drive_state())
        return web.json_response(preview)

    # -- making one ----------------------------------------------------------

    async def _wants_faces(self, request: web.Request) -> bool:
        body = await _json_object(request, default={})
        return _flag(body, "include_faces", True)

    async def _build_backup(self, path: Path, include_faces: bool) -> None:
        try:
            await create_backup(
                self._db,
                path,
                addon_version=__version__,
                clip_folder=str(self._clip_storage_dir),
                include_faces=include_faces,
            )
        except Exception as exc:
            raise _failure(exc, "Creating the backup") from exc

    async def _handle_backup_create(self, request: web.Request) -> web.StreamResponse:
        """Make a backup and send it as a download."""
        include_faces = await self._wants_faces(request)
        async with self._backup_slot():
            path = _scratch_zip()
            try:
                await self._build_backup(path, include_faces)
                return await self._send_zip(request, path)
            finally:
                path.unlink(missing_ok=True)

    @staticmethod
    async def _send_zip(request: web.Request, path: Path) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={
                "Content-Disposition": f'attachment; filename="{_backup_name()}"',
                "Content-Length": str(path.stat().st_size),
            }
        )
        response.content_type = "application/zip"
        await response.prepare(request)
        async with aiofiles.open(path, "rb") as handle:
            while chunk := await handle.read(262_144):
                await response.write(chunk)
        await response.write_eof()
        return response

    def _ready_drive_client(self) -> GDriveClient:
        client = self._gdrive_client
        if client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        state = self._backup_drive_state().state
        if state != "ready":
            raise web.HTTPConflict(text=_DRIVE_NOT_READY[state])
        return client

    @staticmethod
    async def _upload_to_drive(client: GDriveClient, path: Path, name: str) -> str:
        try:
            folder_id = await client.get_or_create_folder_path([_DRIVE_FOLDER])
            file_id = (
                None
                if folder_id is None
                else await client.upload_file(
                    path, name, content_type="application/zip", folder_id=folder_id
                )
            )
        except Exception as exc:
            raise _failure(exc, "The upload to Google Drive") from exc
        if folder_id is None:
            raise web.HTTPBadGateway(
                text=f"Could not open the {_DRIVE_FOLDER} folder in Google Drive."
            )
        if not file_id:
            raise web.HTTPBadGateway(text=_drive_refusal(client))
        return file_id

    async def _handle_backup_gdrive(self, request: web.Request) -> web.Response:
        """Make a backup and put it in the Backups folder in Google Drive."""
        client = self._ready_drive_client()
        include_faces = await self._wants_faces(request)
        name = _backup_name()
        async with self._backup_slot():
            path = _scratch_zip()
            try:
                await self._build_backup(path, include_faces)
                size = path.stat().st_size
                file_id = await self._upload_to_drive(client, path, name)
            finally:
                path.unlink(missing_ok=True)
        return web.json_response(
            {
                "name": name,
                "size_bytes": size,
                "folder": self._backup_drive_state().folder,
                "file_id": file_id,
            }
        )

    # -- restoring one -------------------------------------------------------

    @_reports_upload_errors
    async def _handle_upload_start(self, request: web.Request) -> web.Response:
        body = await _json_object(request)
        size = body.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            raise web.HTTPBadRequest(text="size must be a whole number of bytes")
        if self._backup_lock.locked():
            raise web.HTTPConflict(text=_BACKUP_BUSY)
        upload = self._restore_staging.begin(size)
        return web.json_response(
            {_UPLOAD_ID: upload.upload_id, "chunk_bytes": CHUNK_BYTES}
        )

    @_reports_upload_errors
    async def _handle_upload_chunk(self, request: web.Request) -> web.Response:
        try:
            offset = int(request.query["offset"])
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text="offset must be a whole number") from None
        data = await request.read()
        received = await self._restore_staging.append(_upload_id(request), offset, data)
        return web.json_response({"received": received})

    @_reports_upload_errors
    async def _handle_upload_discard(  # NOSONAR
        self, request: web.Request
    ) -> web.Response:
        self._restore_staging.get(_upload_id(request))
        if self._backup_lock.locked():
            raise web.HTTPConflict(text=_BACKUP_BUSY)
        self._restore_staging.discard()
        return web.json_response({"discarded": True})

    @_reports_upload_errors
    async def _handle_upload_inspect(self, request: web.Request) -> web.Response:
        """Check the uploaded file and say what is in it and what restoring does."""
        upload = self._restore_staging.get(_upload_id(request))
        if upload.received != upload.size:
            raise web.HTTPConflict(text="The upload has not finished arriving yet.")
        async with self._backup_slot():
            summary = await self._inspect(upload)
        return web.json_response({**summary, _UPLOAD_ID: upload.upload_id})

    async def _inspect(self, upload: StagedUpload) -> dict[str, Any]:
        try:
            archive = await asyncio.to_thread(BackupArchive.open, upload.path)
            database = await preview_restore(self._db, archive)
            summary = summarize(archive, database, preview_files(archive))
        except BackupError as exc:
            # The file is no use to anyone; do not leave it taking space.
            self._restore_staging.discard()
            raise _failure(exc, "Reading the backup") from exc
        except Exception as exc:
            raise _failure(exc, "Reading the backup") from exc
        upload.archive = archive
        return summary

    @_reports_upload_errors
    async def _handle_upload_restore(self, request: web.Request) -> web.Response:
        """Restore the inspected file. Refused for one nobody has looked over."""
        upload = self._restore_staging.get(_upload_id(request))
        if upload.archive is None:
            raise web.HTTPConflict(
                text="Look over the summary first: this backup has not been checked yet."
            )
        body = await _json_object(request, default={})
        replace = _flag(body, "replace_settings", False)
        async with self._backup_slot():
            try:
                result = await restore_backup(
                    self._db,
                    upload.archive,
                    replace_settings=replace,
                    hooks=RestoreHooks(self._merge_downloaded_clip_ids),
                )
            except Exception as exc:
                raise _failure(exc, "The restore", unchanged=True) from exc
            self._restore_staging.discard()
        return web.json_response(result.to_json())
