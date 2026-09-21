"""The Storage tab's API: cold storage and Google Drive backup.

Archived clips and the archive run itself, plus the whole Google Drive
side — connecting by device flow, folder selection, quota, the upload
queue and its failures. Drive is the one backend here that can be offline
or unauthorized independently of everything else, so nearly every handler
has a "not configured" answer as well as a real one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _ARCHIVE_CLIPS_PAGE_SIZE,
    _ARCHIVE_NOT_FOUND,
    _GDRIVE_NOT_AVAILABLE,
    _MAX_ARCHIVE_CLIPS_PAGE_SIZE,
    _SETTINGS_WRITE_FAILED,
    _json_object,
    _optional_clip_id,
    _paging,
)

_LOGGER = logging.getLogger(__name__)

_gdrive_connect_state: dict = {"phase": "idle"}


class StorageRoutesMixin(_MediaServerBase):
    """Archived clips, archive runs, and Google Drive backup."""

    def _register_storage_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        # Storage tab: archived clips + Google Drive backup
        app.router.add_get("/api/storage/archives", self._handle_storage_archives)
        app.router.add_get(
            "/api/storage/archive-clips", self._handle_storage_archive_clips
        )
        app.router.add_post(
            "/api/storage/archive/run-now", self._handle_archive_run_now
        )
        app.router.add_delete("/api/storage/archive", self._handle_delete_archive)
        app.router.add_get(
            "/api/storage/gdrive/queue/failed", self._handle_gdrive_queue_failed
        )
        app.router.add_post("/api/storage/gdrive/retry", self._handle_gdrive_retry)
        app.router.add_post(
            "/api/storage/gdrive/queue/failed/clear", self._handle_gdrive_clear_failed
        )
        app.router.add_post("/api/storage/gdrive/pause", self._handle_gdrive_pause)
        app.router.add_get(
            "/api/storage/gdrive/settings", self._handle_gdrive_settings_get
        )
        app.router.add_put(
            "/api/storage/gdrive/settings", self._handle_gdrive_settings_put
        )
        app.router.add_get("/api/storage/gdrive/status", self._handle_gdrive_status)
        app.router.add_post("/api/storage/gdrive/connect", self._handle_gdrive_connect)
        app.router.add_get(
            "/api/storage/gdrive/connect-status", self._handle_gdrive_connect_status
        )
        app.router.add_post(
            "/api/storage/gdrive/disconnect", self._handle_gdrive_disconnect
        )
        app.router.add_get("/api/storage/gdrive/quota", self._handle_gdrive_quota)
        app.router.add_get("/api/storage/gdrive/queue", self._handle_gdrive_queue)
        app.router.add_get("/api/storage/gdrive/folders", self._handle_gdrive_folders)
        app.router.add_post(
            "/api/storage/gdrive/folders", self._handle_gdrive_create_folder
        )
        app.router.add_put(
            "/api/storage/gdrive/folder", self._handle_gdrive_select_folder
        )
        app.router.add_post(
            "/api/storage/gdrive/backup-now", self._handle_gdrive_backup_now
        )
        app.router.add_post("/api/storage/gdrive/upload", self._handle_gdrive_upload)

    async def _delete_gdrive_backup(self, clip: dict[str, Any]) -> bool | None:
        """Best-effort delete of one clip's Google Drive backup, if any.

        Shared by _handle_delete_clip and _handle_delete_archive. Returns
        None when there was nothing to delete (no gdrive_file_id, or no
        client configured) — matching the "was any deletion even attempted"
        signal _handle_delete_clip's response already exposes. A Drive
        failure logs a warning and returns False rather than raising,
        matching archiver.py's per-step resilience style
        (log-and-continue): the caller's own local/DB cleanup must proceed
        regardless of whether this succeeded.
        """
        if not clip.get("gdrive_file_id") or not self._gdrive_client:
            return None
        try:
            return await self._gdrive_client.delete_file(clip["gdrive_file_id"])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not remove Google Drive backup for clip %s: %s",
                clip.get("id"),
                exc,
            )
            return False

    async def _handle_storage_archives(self, request: web.Request) -> web.Response:
        """List distinct ZIP archives (Storage tab's Archived Clips list).

        Grouped server-side rather than left to the frontend to derive from
        a flat clip list — a library can have thousands of archived clips
        but only a few dozen distinct monthly ZIPs, so this is both cheaper
        to fetch and the only way to give the frontend an accurate total
        for real numbered pagination (``/api/clips`` has no total-count
        response at all).
        """
        q = request.rel_url.query
        groups = await self._db.get_archive_groups(
            camera=q.get("camera") or None,
            since=q.get("since") or None,
            until=q.get("until") or None,
        )
        return web.json_response(groups)

    async def _handle_storage_archive_clips(self, request: web.Request) -> web.Response:
        """Return one page of clips from one ZIP archive for the Storage tab."""
        q = request.rel_url.query
        limit, offset = _paging(
            q,
            default_limit=_ARCHIVE_CLIPS_PAGE_SIZE,
            max_limit=_MAX_ARCHIVE_CLIPS_PAGE_SIZE,
            min_limit=1,
        )

        archive_path = q.get("archive_path", "")
        if not archive_path:
            raise web.HTTPBadRequest(text="archive_path is required")

        page = await self._db.get_archive_clips(
            archive_path=archive_path,
            camera=q.get("camera") or None,
            since=q.get("since") or None,
            until=q.get("until") or None,
            limit=limit,
            offset=offset,
        )
        return web.json_response(page)

    async def _handle_archive_run_now(self, _request: web.Request) -> web.Response:
        """Sweep everything currently eligible for archiving immediately,
        instead of waiting for the next poll cycle.

        Calls the exact same ClipArchiver.run() the poll loop already
        calls — run()'s own lock means this is safe even if a poll cycle's
        automatic archive run is in progress at the same time.
        """
        if self._archiver is None:
            raise web.HTTPServiceUnavailable(text="Archiving is not available")
        archived = await self._archiver.run()
        return web.json_response({"archived": len(archived)})

    async def _handle_delete_archive(self, request: web.Request) -> web.Response:
        """Delete an entire archive ZIP: every clip record stored in it,
        their Google Drive backups (best-effort), the now-empty Drive
        folders those backups lived in, and the ZIP file itself.

        Shares _handle_delete_clip's per-item Drive-delete resilience via
        _delete_gdrive_backup (a Drive failure logs a warning and continues
        rather than blocking the rest of the deletion), fanned out
        concurrently over every clip sharing one archive_path — an archive
        can hold many clips, and each delete is its own Drive API round
        trip, so running them one at a time would make handler latency
        scale with clip count instead of the slowest single call.
        """
        archive_path = request.rel_url.query.get("archive_path", "")
        if not archive_path:
            raise web.HTTPBadRequest(text="archive_path is required")

        clips = await self._db.get_clips_by_archive_path(archive_path)
        if not clips:
            raise web.HTTPNotFound(text=_ARCHIVE_NOT_FOUND)

        gdrive_deleted = 0
        gdrive_folders_removed = 0
        if self._gdrive_client:
            results = await asyncio.gather(
                *(self._delete_gdrive_backup(clip) for clip in clips)
            )
            gdrive_deleted = sum(1 for result in results if result)
            # Trashing the clips leaves their whole date/camera scaffolding
            # standing in Drive, which looks a great deal like nothing was
            # deleted. The queue owns where backups land, so it owns
            # clearing up after them; it only removes folders Drive itself
            # confirms are empty.
            if self._gdrive_queue is not None:
                gdrive_folders_removed = (
                    await self._gdrive_queue.prune_empty_backup_folders(clips)
                )

        zip_path = Path(archive_path)
        if zip_path.exists():
            try:
                zip_path.unlink()
            except OSError as exc:
                _LOGGER.warning("Could not delete archive file %s: %s", zip_path, exc)

        deleted_clips = await self._db.delete_clips_by_archive_path(archive_path)
        return web.json_response(
            {
                "deleted_clips": deleted_clips,
                "gdrive_deleted": gdrive_deleted,
                "gdrive_folders_removed": gdrive_folders_removed,
            }
        )

    async def _handle_gdrive_settings_get(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._gdrive_client is None:
            # A literal False against the "has_client_secret" key reads to
            # bandit (B105) as a hardcoded credential, and a nosec inside a
            # multi-line dict sprays spurious per-line "unused nosec"
            # warnings — hand the value over as a plain variable instead.
            configured = False
            return web.json_response(
                {
                    "client_id": "",
                    "has_client_secret": configured,
                    "backup_policy": "archived_only",
                }
            )
        return web.json_response(
            {
                "client_id": self._gdrive_client.client_id,
                "has_client_secret": self._gdrive_client.has_client_secret,
                "backup_policy": self._gdrive_client.backup_policy,
            }
        )

    async def _handle_gdrive_settings_put(self, request: web.Request) -> web.Response:
        if self._gdrive_client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        body = await _json_object(request)

        client_id = str(body.get("client_id", "") or "")
        # Omitted/empty client_secret means "keep the previously stored one"
        # — see GDriveClient.set_settings — so re-saving just the backup
        # policy doesn't force re-entering it every time.
        client_secret = body.get("client_secret") or None
        backup_policy = str(
            body.get("backup_policy", "archived_only") or "archived_only"
        )
        try:
            self._gdrive_client.set_settings(
                client_id,
                str(client_secret) if client_secret else None,
                backup_policy,
            )
        except OSError as exc:
            _LOGGER.warning("Could not save Google Drive settings: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc
        return web.json_response({"saved": True})

    async def _handle_gdrive_status(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._gdrive_client is None:
            return web.json_response(
                {
                    "configured": False,
                    "connected": False,
                    "account_email": "",
                    "folder_id": "",
                    "folder_name": "",
                    "uploads_paused": False,
                    "pause_reason": "",
                }
            )
        return web.json_response(
            {
                "configured": self._gdrive_client.is_configured,
                "connected": self._gdrive_client.connected,
                "account_email": self._gdrive_client.account_email,
                "folder_id": self._gdrive_client.folder_id,
                "folder_name": self._gdrive_client.folder_name,
                "uploads_paused": self._gdrive_client.uploads_paused,
                "pause_reason": self._gdrive_client.pause_reason,
            }
        )

    async def _handle_gdrive_connect(self, _request: web.Request) -> web.Response:
        global _gdrive_connect_state
        if self._gdrive_client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        if not self._gdrive_client.is_configured:
            raise web.HTTPBadRequest(
                text="Set a Google OAuth client ID and secret first"
            )
        if _gdrive_connect_state.get("phase") == "pending":
            # Already connecting — return the in-flight code rather than
            # starting a second device flow, which would invalidate the
            # code the user may already be looking at (mirrors
            # _handle_moondream_install's "already installing" branch).
            return web.json_response(_gdrive_connect_state)

        info = await self._gdrive_client.start_device_flow()
        if info is None:
            raise web.HTTPBadGateway(text="Could not start Google sign-in")

        _gdrive_connect_state = {
            "phase": "pending",
            "user_code": info.user_code,
            "verification_url": info.verification_url,
            "expires_in": info.expires_in,
        }

        self._gdrive_connect_task = asyncio.create_task(self._poll_for_token(info))
        return web.json_response(_gdrive_connect_state)

    async def _poll_for_token(self, info: Any) -> None:
        """Poll Google until the user finishes signing in, or time runs out.

        A method rather than a closure inside the handler purely so the
        two can be read — and measured — separately; it still rebinds the
        module-level ``_gdrive_connect_state`` that the status endpoint
        serves, which only works because both live in this module.
        """
        global _gdrive_connect_state
        deadline = time.monotonic() + info.expires_in
        interval = max(1, info.interval)
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            assert self._gdrive_client is not None
            result = await self._gdrive_client.poll_once_for_token(info.device_code)
            if result.status == "slow_down":
                # Google asking us to back off is the one non-terminal
                # answer that changes something, so it is handled here
                # rather than in the state mapping below.
                interval += 5
                continue
            final = self._token_poll_state(result)
            if final is not None:
                _gdrive_connect_state = final
                return
            # "pending" — keep polling until the deadline above.
        _gdrive_connect_state = {"phase": "expired"}

    def _token_poll_state(self, result: Any) -> dict[str, Any] | None:
        """The connect state one poll result ends on, or ``None`` to keep going."""
        if result.status == "success":
            assert self._gdrive_client is not None
            return {
                "phase": "connected",
                "account_email": self._gdrive_client.account_email,
            }
        if result.status == "expired":
            return {"phase": "expired"}
        if result.status == "denied":
            return {"phase": "error", "message": "Sign-in was denied"}
        if result.status == "error":
            return {"phase": "error", "message": result.message or "Sign-in failed"}
        return None

    async def _handle_gdrive_connect_status(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        return web.json_response(_gdrive_connect_state)

    async def _handle_gdrive_disconnect(self, _request: web.Request) -> web.Response:
        global _gdrive_connect_state
        if self._gdrive_client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        await self._gdrive_client.disconnect()
        _gdrive_connect_state = {"phase": "idle"}
        return web.json_response({"disconnected": True})

    async def _handle_gdrive_quota(self, _request: web.Request) -> web.Response:
        if self._gdrive_client is None or not self._gdrive_client.connected:
            return web.json_response({"available": False})
        quota = await self._gdrive_client.get_quota()
        if quota is None:
            return web.json_response({"available": False})
        return web.json_response(
            {
                "available": True,
                "limit": quota.limit,
                "usage": quota.usage,
                "usage_in_drive": quota.usage_in_drive,
            }
        )

    async def _handle_gdrive_queue(self, _request: web.Request) -> web.Response:
        if self._gdrive_queue is None:
            return web.json_response(
                {
                    "connected": False,
                    "uploads_paused": False,
                    "pause_reason": "",
                    "hold_off_reason": "",
                    "hold_off_seconds": 0,
                    "pending": 0,
                    "processing": 0,
                    "completed": 0,
                    "failed": 0,
                }
            )
        return web.json_response(await self._gdrive_queue.get_queue_status())

    async def _handle_gdrive_queue_failed(self, request: web.Request) -> web.Response:
        """One page of failed upload rows with their error message (Storage
        tab's failed-uploads list) — deliberately gated on self._db only,
        not gdrive_client/gdrive_queue: a failed row and its error message
        are meaningful to look at (and retry) even while currently
        disconnected, same as enqueueing before ever connecting.

        Returns ``total`` alongside the page because the list this powers
        needs to say how many there are in all — a spell of Drive being
        unreachable can fail every clip in the library, and the page on
        screen is then a small fraction of the problem.
        """
        limit, offset = _paging(request.rel_url.query, 25, 100, min_limit=1)
        items = await self._db.get_failed_gdrive_uploads(limit=limit, offset=offset)
        counts = await self._db.get_gdrive_queue_counts()
        return web.json_response({"items": items, "total": counts.get("failed", 0)})

    async def _handle_gdrive_clear_failed(self, request: web.Request) -> web.Response:
        """Discard one (or, with no clip_id, every) failed upload row.

        The counterpart to _handle_gdrive_retry, and takes the same
        optional body for the same reason: a failure a user has looked at
        and decided not to act on should not be stuck on their Storage tab
        forever with Retry as the only way to make it go away.
        """
        cleared = await self._db.clear_failed_gdrive_uploads(
            await _optional_clip_id(request)
        )
        return web.json_response({"cleared": cleared})

    async def _handle_gdrive_pause(self, request: web.Request) -> web.Response:
        """Pause or resume Drive uploads without touching the connection.

        Disconnecting was the only way to stop uploading, which throws away
        the OAuth tokens and the chosen backup folder to achieve it.
        Resuming also clears any hold-off the queue put itself into, since
        someone pressing Resume has usually just fixed the thing that
        caused it — and, when the queue paused *itself* because Drive was
        full, resuming is the only signal that there is any point in trying
        again at all.
        """
        if self._gdrive_client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        body = await _json_object(request)
        paused = bool(body.get("paused"))
        try:
            self._gdrive_client.set_uploads_paused(paused)
        except OSError as exc:
            _LOGGER.warning("Could not save the Google Drive pause state: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc
        if not paused and self._gdrive_queue is not None:
            self._gdrive_queue.resume()
        return web.json_response({"paused": paused})

    async def _handle_gdrive_retry(self, request: web.Request) -> web.Response:
        """Reset one (or, with no clip_id, every) failed upload back to
        pending so the queue retries it. Meaningful even while Drive is
        currently disconnected — see _handle_gdrive_queue_failed above.

        The request body is entirely optional (unlike most POST handlers in
        this file) — no body/an empty body/invalid JSON are all treated the
        same as "no clip_id", meaning "retry everything failed", rather
        than a 400.

        Retrying also clears any hold-off the queue is sitting in: the
        usual reason to press Retry is having just freed up space, and
        waiting out the rest of an hour that is no longer true would look
        exactly like the button not working.
        """
        retried = await self._db.retry_failed_gdrive_uploads(
            await _optional_clip_id(request)
        )
        if self._gdrive_queue is not None:
            self._gdrive_queue.resume()
        return web.json_response({"retried": retried})

    async def _handle_gdrive_folders(self, request: web.Request) -> web.Response:
        """List folders directly inside ?parent_id= (default: Drive root).

        Only ever returns folders, never files of any type — the folder
        browser this powers is navigation for choosing a backup destination,
        not a general file browser, so there's nothing here a user could
        interact with beyond picking/creating a folder.
        """
        if self._gdrive_client is None:
            return web.json_response({"folders": []})
        parent_id = request.rel_url.query.get("parent_id") or "root"
        folders = await self._gdrive_client.list_folders(parent_id)
        return web.json_response(
            {
                "folders": [
                    {"id": f.id, "name": f.name, "modified_time": f.modified_time}
                    for f in folders
                ]
            }
        )

    async def _handle_gdrive_create_folder(self, request: web.Request) -> web.Response:
        if self._gdrive_client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        body = await _json_object(request)
        name = str(body.get("name", "") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="Folder name is required")
        parent_id = str(body.get("parent_id") or "root")

        folder = await self._gdrive_client.create_folder(name, parent_id)
        if folder is None:
            raise web.HTTPBadGateway(text="Could not create Google Drive folder")
        return web.json_response(
            {
                "id": folder.id,
                "name": folder.name,
                "modified_time": folder.modified_time,
            }
        )

    async def _handle_gdrive_select_folder(self, request: web.Request) -> web.Response:
        """Set the default folder used for automatic archived/all_clips backups."""
        if self._gdrive_client is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        body = await _json_object(request)
        folder_id = str(body.get("folder_id", "") or "")
        if not folder_id:
            raise web.HTTPBadRequest(text="folder_id is required")
        folder_name = str(body.get("folder_name", "") or "")
        self._gdrive_client.select_folder(folder_id, folder_name)
        return web.json_response({"saved": True})

    async def _handle_gdrive_backup_now(self, _request: web.Request) -> web.Response:
        """Enqueue every not-yet-backed-up eligible clip — including
        retrying any that previously failed, since get_clips_pending_gdrive_backup
        filters only on gdrive_backed_up=FALSE, which a failed upload still is.

        Without this, connecting Drive for the first time would silently
        only cover clips downloaded/archived from that moment forward.
        """
        if self._gdrive_client is None or self._gdrive_queue is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        include_unarchived = self._gdrive_client.backup_policy == "all_clips"
        pending = await self._db.get_clips_pending_gdrive_backup(include_unarchived)
        # enqueue() returns whether it actually (re)queued the clip — a
        # clip already pending/processing/completed is a no-op, so counting
        # len(pending) unconditionally (the old behavior) overstated success
        # for exactly the clips this button most needs to be honest about.
        enqueued = 0
        for clip in pending:
            if await self._gdrive_queue.enqueue(clip):
                enqueued += 1
        return web.json_response({"enqueued": enqueued})

    async def _handle_gdrive_upload(self, request: web.Request) -> web.Response:
        """Manual upload of specific clips (Library's "Upload to Drive" bulk
        action), optionally targeting a folder other than the default."""
        if self._gdrive_client is None or self._gdrive_queue is None:
            raise web.HTTPServiceUnavailable(text=_GDRIVE_NOT_AVAILABLE)
        body = await _json_object(request)
        clip_ids = body.get("clip_ids")
        if not isinstance(clip_ids, list) or not clip_ids:
            raise web.HTTPBadRequest(text="clip_ids must be a non-empty list")
        folder_id = str(body.get("folder_id") or "")

        enqueued = 0
        for clip_id in clip_ids:
            clip = await self._db.get_clip(str(clip_id))
            if clip:
                await self._gdrive_queue.enqueue(clip, folder_id=folder_id)
                enqueued += 1
        return web.json_response({"enqueued": enqueued})
