"""The Assets tab's API: the things on each camera's view worth protecting.

Each asset is a named zone on one camera — a front door, a parcel spot, a
bike — stored in ``/data/protected_assets.json`` (see
:mod:`blink_downloader.protected_assets`, which owns the stored form and its
validation) and pushed to the live analyzer on every change, so the very
next clip from that camera is analyzed with it. Each camera also keeps one
reference frame, the one its assets were last drawn on, so the tab never
silently redraws them over a different picture later.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from .. import protected_assets
from ..database import ClipFilters
from .core import _MediaServerBase
from .support import _CLIP_NOT_FOUND, _SETTINGS_WRITE_FAILED, _json_object

_LOGGER = logging.getLogger(__name__)

_ASSET_NOT_FOUND = "Asset not found"


class AssetsRoutesMixin(_MediaServerBase):
    """Marked assets: list, create, edit, remove, and each camera's frame."""

    _PROTECTED_ASSETS_FILE = protected_assets.ASSETS_FILE

    _ASSET_SNAPSHOTS_DIR = Path("/data/asset_snapshots")

    def _register_assets_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/assets", self._handle_assets_get)
        app.router.add_post("/api/assets", self._handle_asset_create)
        app.router.add_get("/api/assets/activity", self._handle_asset_activity)
        app.router.add_get(
            "/api/assets/snapshot/{camera}", self._handle_asset_snapshot_get
        )
        app.router.add_put("/api/assets/{asset_id}", self._handle_asset_update)
        app.router.add_delete("/api/assets/{asset_id}", self._handle_asset_delete)

    # -- reading ---------------------------------------------------------

    async def _handle_assets_get(self, _request: web.Request) -> web.Response:
        """Every marked asset, plus what the tab needs to explain its effect.

        ``analysis_enabled``/``detection_enabled`` say how much of the
        protection is actually running: with no AI provider nothing reads
        the assets at all, and with object detection off the prompt still
        names them but the per-asset checks (someone entering an asset's
        area, handling it, the asset looking different afterwards) cannot
        run. The tab says so rather than letting someone believe otherwise.
        """
        async with self._camera_configs_lock:
            assets = protected_assets.read_assets(self._PROTECTED_ASSETS_FILE)
        analyzer = self._analyzer
        return web.json_response(
            {
                "assets": assets,
                "limits": {
                    "per_camera": protected_assets.MAX_ASSETS_PER_CAMERA,
                    "name": protected_assets.MAX_NAME_LENGTH,
                    "description": protected_assets.MAX_DESCRIPTION_LENGTH,
                },
                "analysis_enabled": analyzer is not None,
                "detection_enabled": bool(
                    analyzer is not None and analyzer.object_detection_enabled
                ),
            }
        )

    async def _handle_asset_activity(self, request: web.Request) -> web.Response:
        """How often something happened at each marked asset lately."""
        try:
            days = int(request.query.get("days", "7"))
        except ValueError:
            raise web.HTTPBadRequest(text="days must be a whole number")
        days = max(1, min(days, 90))
        return web.json_response(
            {"days": days, "activity": await self._db.get_asset_activity(days)}
        )

    async def _handle_asset_snapshot_get(
        self, request: web.Request
    ) -> web.StreamResponse:
        """The frame this camera's assets were drawn on.

        Falls back to the camera's newest clip thumbnail when none has been
        saved yet — which is also what the editor draws a first asset on.
        Blink cameras do not move, so a zone drawn on one frame fits every
        other one from the same camera.
        """
        camera = request.match_info["camera"]
        snapshot = self._asset_snapshot_path(camera)
        if snapshot.exists():
            return web.FileResponse(snapshot, headers={"Cache-Control": "no-cache"})
        clips = await self._db.get_clips(
            ClipFilters(camera=camera), limit=1, sort="newest"
        )
        fallback = Path(clips[0]["file_path"]).with_suffix(".jpg") if clips else None
        if fallback is None or not fallback.exists():
            raise web.HTTPNotFound(text="No frame available for this camera yet")
        return web.FileResponse(fallback, headers={"Cache-Control": "no-cache"})

    # -- writing ---------------------------------------------------------

    async def _handle_asset_create(self, request: web.Request) -> web.Response:
        """Mark a new asset on one camera.

        Drawn on one of its clips' frames (``clip_id``), or — once the
        camera has assets and so a reference frame — on that frame, which
        keeps every asset on a camera drawn over the same picture.
        """
        body = await _json_object(request)
        camera = str(body.get("camera") or "").strip()
        if not camera:
            raise web.HTTPBadRequest(text="Missing camera")
        fields = self._asset_fields(body, require_all=True)
        clip_id = str(body.get("clip_id") or "")
        frame = await self._asset_frame(camera, clip_id) if clip_id else None
        if frame is None and not self._asset_snapshot_path(camera).exists():
            raise web.HTTPBadRequest(text="Missing clip_id")

        now = datetime.now(UTC).isoformat()
        asset = {
            "id": protected_assets.new_asset_id(),
            "camera": camera,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        async with self._camera_configs_lock:
            assets = protected_assets.read_assets(self._PROTECTED_ASSETS_FILE)
            self._refuse_duplicate_name(assets, camera, fields["name"])
            on_camera = sum(1 for a in assets if a["camera"].lower() == camera.lower())
            if on_camera >= protected_assets.MAX_ASSETS_PER_CAMERA:
                raise web.HTTPConflict(
                    text=(
                        "This camera already has "
                        f"{protected_assets.MAX_ASSETS_PER_CAMERA} assets, the most "
                        "one camera can hold"
                    )
                )
            assets.append(asset)
            if frame is not None:
                self._save_asset_snapshot(camera, frame)
            self._store_assets(assets)
        return web.json_response({"asset": asset})

    async def _handle_asset_update(self, request: web.Request) -> web.Response:
        """Rename, retype, redraw, or switch one asset on or off.

        Every field is optional; only those present change. An asset never
        moves camera — its zone only means anything on its own camera's
        frame. A ``clip_id`` replaces the camera's reference frame with that
        clip's, which is what a redraw on a newer frame needs.
        """
        asset_id = request.match_info["asset_id"]
        body = await _json_object(request)
        fields = self._asset_fields(body, require_all=False)
        if "enabled" in body:
            fields["enabled"] = body["enabled"] is not False
        clip_id = str(body.get("clip_id") or "")

        async with self._camera_configs_lock:
            assets = protected_assets.read_assets(self._PROTECTED_ASSETS_FILE)
            asset = next((a for a in assets if a["id"] == asset_id), None)
            if asset is None:
                raise web.HTTPNotFound(text=_ASSET_NOT_FOUND)
            if "name" in fields:
                self._refuse_duplicate_name(
                    assets, asset["camera"], fields["name"], asset_id
                )
            frame = (
                await self._asset_frame(asset["camera"], clip_id) if clip_id else None
            )
            asset.update(fields, updated_at=datetime.now(UTC).isoformat())
            if frame is not None:
                self._save_asset_snapshot(asset["camera"], frame)
            self._store_assets(assets)
        return web.json_response({"asset": asset})

    async def _handle_asset_delete(self, request: web.Request) -> web.Response:
        """Remove one asset, and its camera's frame once nothing is left on it."""
        asset_id = request.match_info["asset_id"]
        async with self._camera_configs_lock:
            assets = protected_assets.read_assets(self._PROTECTED_ASSETS_FILE)
            asset = next((a for a in assets if a["id"] == asset_id), None)
            if asset is None:
                raise web.HTTPNotFound(text=_ASSET_NOT_FOUND)
            remaining = [a for a in assets if a["id"] != asset_id]
            self._store_assets(remaining)
            camera = asset["camera"].lower()
            if not any(a["camera"].lower() == camera for a in remaining):
                self._asset_snapshot_path(asset["camera"]).unlink(missing_ok=True)
        return web.json_response({"deleted": True})

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _asset_fields(body: dict[str, Any], require_all: bool) -> dict[str, Any]:
        """The editable fields present in *body*, validated.

        *require_all* is creation, where name, type and zone must all be
        there; an update takes whichever it is given.
        """
        fields: dict[str, Any] = {}
        if require_all or "name" in body:
            name = protected_assets.clean_text(
                body.get("name"), protected_assets.MAX_NAME_LENGTH
            )
            if not name:
                raise web.HTTPBadRequest(text="Give the asset a name")
            fields["name"] = name
        if require_all or "asset_type" in body:
            asset_type = body.get("asset_type")
            if asset_type not in protected_assets.MARKABLE_TYPES:
                raise web.HTTPBadRequest(text="Unknown asset type")
            fields["asset_type"] = asset_type
        if require_all or "description" in body:
            fields["description"] = protected_assets.clean_text(
                body.get("description"), protected_assets.MAX_DESCRIPTION_LENGTH
            )
        if require_all or "zone" in body:
            zone = protected_assets.normalize_zone(body.get("zone"))
            if zone is None:
                raise web.HTTPBadRequest(text="Invalid or missing zone")
            fields["zone"] = zone
        return fields

    @staticmethod
    def _refuse_duplicate_name(
        assets: list[dict[str, Any]], camera: str, name: str, own_id: str = ""
    ) -> None:
        """Refuse a second asset with the same name on one camera.

        The name is how an asset is referred to everywhere after it is
        saved — in the analysis prompt, in each security event's detail and
        ``asset_name``, and in the tab's activity counts — so two called
        "Door" on one camera would make every one of those ambiguous.
        Case-insensitive, like camera names. The same name on another camera
        is fine: events always carry their camera too.
        """
        wanted = name.lower()
        for other in assets:
            if (
                other["id"] != own_id
                and other["camera"].lower() == camera.lower()
                and other["name"].lower() == wanted
            ):
                raise web.HTTPConflict(
                    text=f"This camera already has an asset called “{other['name']}”"
                )

    async def _asset_frame(self, camera: str, clip_id: str) -> bytes:
        """The thumbnail of *clip_id*, which must be one of *camera*'s clips.

        A zone drawn on another camera's frame would be saved against a
        picture of somewhere else entirely, so that is refused rather than
        stored.
        """
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        if str(clip.get("camera", "")).lower() != camera.lower():
            raise web.HTTPBadRequest(text="That clip is from a different camera")
        thumb = Path(clip["file_path"]).with_suffix(".jpg")
        try:
            return thumb.read_bytes()
        except OSError:
            raise web.HTTPNotFound(text="Thumbnail not available for that clip")

    def _save_asset_snapshot(self, camera: str, frame: bytes) -> None:
        """Keep *frame* as *camera*'s reference frame; a failure is logged.

        Losing the reference frame costs the tab its fixed picture — it falls
        back to the newest clip's — not the asset, so it must not fail the
        save.
        """
        try:
            self._ASSET_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            # The path is slugified to [a-z0-9-] plus a hash, so nothing the
            # camera name contains can reach the filesystem as a separator.
            self._asset_snapshot_path(camera).write_bytes(frame)  # NOSONAR
        except OSError as exc:
            _LOGGER.warning("Could not save the asset frame for %s: %s", camera, exc)

    def _store_assets(self, assets: list[dict[str, Any]]) -> None:
        """Apply *assets* to the live analyzer, then persist them.

        Applied first, as every settings save here is: a failed write should
        not also cost the change its immediate effect this session.
        """
        if self._analyzer is not None:
            self._analyzer.update_protected_assets(assets)
        try:
            protected_assets.write_assets(assets, self._PROTECTED_ASSETS_FILE)
        except OSError as exc:
            _LOGGER.warning("Could not save protected assets: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc

    @classmethod
    def _asset_snapshot_path(cls, camera: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", camera.lower()).strip("-") or "camera"
        suffix = hashlib.sha256(camera.lower().encode("utf-8")).hexdigest()[:10]
        return cls._ASSET_SNAPSHOTS_DIR / f"{slug}-{suffix}.jpg"

    def _migrate_protected_assets(self, old_name: str, new_name: str) -> None:
        """Carry every asset, and the camera's frame, across a rename.

        Called from ``MediaServer.rename_camera`` with the camera-config lock
        already held. Raises ``OSError`` if the new list cannot be written,
        like the other migrations there, so the rename is retried.
        """
        assets = protected_assets.read_assets(self._PROTECTED_ASSETS_FILE)
        renamed = protected_assets.rename_camera(assets, old_name, new_name)
        if renamed is not None:
            protected_assets.write_assets(renamed, self._PROTECTED_ASSETS_FILE)
            if self._analyzer is not None:
                self._analyzer.update_protected_assets(renamed)
        old_snapshot = self._asset_snapshot_path(old_name)
        new_snapshot = self._asset_snapshot_path(new_name)
        if not old_snapshot.exists() or old_snapshot == new_snapshot:
            return
        if new_snapshot.exists():
            old_snapshot.unlink()
        else:
            old_snapshot.replace(new_snapshot)
