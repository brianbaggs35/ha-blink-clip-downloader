"""The Vehicles tab's API: which car is yours, and where it parks.

The protected-vehicle description, the drawn car zone and the reference
snapshot it was drawn against, plus the colour/position signature each
camera learns over time and the ability to reset it. Zone geometry is
stored normalized (0-1) so it survives a resolution change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from aiohttp import web

from ..database import ClipFilters
from .camera_configs import CameraConfigsRoutesMixin
from .support import (
    _CAMERA_CONFIGS_SAVE_ERROR,
    _CLIP_NOT_FOUND,
    _INVALID_JSON_BODY,
    _SETTINGS_WRITE_FAILED,
)

_LOGGER = logging.getLogger(__name__)


class VehicleRoutesMixin(CameraConfigsRoutesMixin):
    """Protected-vehicle settings, zones and learned signatures.

    Inherits :class:`CameraConfigsRoutesMixin` because a car zone *is* a
    camera-config field: saving one is a read-modify-write of the same
    ``/data/camera_configs.json`` the AI tab edits, so this area needs that
    area's storage layer rather than a second copy of it.
    """

    _VEHICLE_SETTINGS_FILE = Path("/data/vehicle_settings.json")

    _VEHICLE_ZONE_SNAPSHOTS_DIR = Path("/data/vehicle_zone_snapshots")

    def _register_vehicles_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/vehicle/settings", self._handle_vehicle_settings_get)
        app.router.add_put("/api/vehicle/settings", self._handle_vehicle_settings_put)
        app.router.add_put("/api/vehicle/zone/{camera}", self._handle_vehicle_zone_put)
        app.router.add_delete(
            "/api/vehicle/zone/{camera}", self._handle_vehicle_zone_delete
        )
        app.router.add_get(
            "/api/vehicle/signature/{camera}", self._handle_vehicle_signature_get
        )
        app.router.add_delete(
            "/api/vehicle/signature/{camera}", self._handle_vehicle_signature_delete
        )
        app.router.add_get(
            "/api/vehicle/zone-snapshot/{camera}",
            self._handle_vehicle_zone_snapshot_get,
        )

    async def _handle_vehicle_settings_get(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        if self._VEHICLE_SETTINGS_FILE.exists():
            try:
                data = json.loads(self._VEHICLE_SETTINGS_FILE.read_text())
                return web.json_response(
                    {"car_description": str(data.get("car_description", ""))}
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not read vehicle settings file: %s", exc)
        # File doesn't exist (or is unreadable) yet — fall back to whatever
        # the live analyzer was started with (ai_car_description).
        fallback = self._analyzer.car_description if self._analyzer else ""
        return web.json_response({"car_description": fallback})

    async def _handle_vehicle_settings_put(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            car_description = str(body.get("car_description", "") or "").strip()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        # Applied before the write attempt, not after: a failure to persist
        # for next restart shouldn't also cost the user the immediate,
        # in-process effect of the change they just made this session.
        if self._analyzer is not None:
            self._analyzer.update_car_description(car_description)

        try:
            self._VEHICLE_SETTINGS_FILE.write_text(
                json.dumps({"car_description": car_description}, indent=2)
            )
        except OSError as exc:
            _LOGGER.warning("Could not save vehicle settings: %s", exc)
            raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc

        return web.json_response({"saved": True})

    async def _handle_vehicle_zone_put(self, request: web.Request) -> web.Response:
        """Save one camera's protected-vehicle zone, together with a
        persisted snapshot of the exact frame it was drawn on, so the
        picker's reference image never silently changes later just because
        a newer clip came in for that camera.
        """
        camera = request.match_info["camera"]
        try:
            body = await request.json()
            clip_id = str(body.get("clip_id") or "")
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        zone = self._normalize_car_zone(
            body.get("zone") if isinstance(body, dict) else None
        )
        if zone is None:
            raise web.HTTPBadRequest(text="Invalid or missing zone")
        if not clip_id:
            raise web.HTTPBadRequest(text="Missing clip_id")

        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        thumb = Path(clip["file_path"]).with_suffix(".jpg")
        if not thumb.exists():
            raise web.HTTPNotFound(text="Thumbnail not available for that clip")

        async with self._camera_configs_lock:
            configs = self._read_camera_configs()
            entry = next((c for c in configs if c.get("camera") == camera), None)
            if entry is None:
                entry = {
                    "camera": camera,
                    "description": "",
                    "custom_prompt": "",
                    "is_car_camera": True,
                    "car_zone": None,
                    "auto_analyze": True,
                }
                configs.append(entry)
            entry["car_zone"] = zone
            # The picker is only reachable once the "protected vehicle visible
            # from this camera" toggle is on, but that toggle only persists via
            # the Vehicles page's own batch save — without this, saving a zone
            # before ever clicking that batch save would silently have no
            # effect (car-zone rules are gated on is_car_camera).
            entry["is_car_camera"] = True

            try:
                self._VEHICLE_ZONE_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                # SonarCloud flags this path as built from user-controlled data
                # (camera comes straight off the URL) — but
                # _vehicle_zone_snapshot_path slugifies it through
                # re.sub(r"[^a-z0-9]+", "-", ...) first, so the result can only
                # ever contain [a-z0-9-]: no "/" or ".." can reach the
                # filesystem here regardless of what camera actually is.
                self._vehicle_zone_snapshot_path(camera).write_bytes(  # NOSONAR
                    thumb.read_bytes()
                )
            except OSError as exc:
                _LOGGER.warning(
                    "Could not save vehicle zone snapshot for %s: %s", camera, exc
                )

            # Applied before the write attempt, not after: a failure to persist
            # for next restart shouldn't also cost the user the immediate,
            # in-process effect of the change they just made this session.
            self._apply_camera_configs_to_analyzer(configs)

            try:
                self._CAMERA_CONFIGS_FILE.write_text(json.dumps(configs, indent=2))
            except OSError as exc:
                _LOGGER.warning(_CAMERA_CONFIGS_SAVE_ERROR, exc)
                raise web.HTTPInternalServerError(text=_SETTINGS_WRITE_FAILED) from exc

        return web.json_response({"saved": True, "car_zone": zone})

    async def _handle_vehicle_zone_delete(  # NOSONAR
        self, request: web.Request
    ) -> web.Response:
        camera = request.match_info["camera"]
        async with self._camera_configs_lock:
            configs = self._read_camera_configs()
            entry = next((c for c in configs if c.get("camera") == camera), None)
            if entry is not None:
                entry["car_zone"] = None
                # Applied before the write attempt, not after: a failure to
                # persist for next restart shouldn't also cost the user the
                # immediate, in-process effect of the change they just made
                # this session.
                self._apply_camera_configs_to_analyzer(configs)
                try:
                    self._CAMERA_CONFIGS_FILE.write_text(json.dumps(configs, indent=2))
                except OSError as exc:
                    _LOGGER.warning(_CAMERA_CONFIGS_SAVE_ERROR, exc)
                    raise web.HTTPInternalServerError(
                        text=_SETTINGS_WRITE_FAILED
                    ) from exc

        self._vehicle_zone_snapshot_path(camera).unlink(missing_ok=True)
        self._legacy_vehicle_zone_snapshot_path(camera).unlink(missing_ok=True)

        return web.json_response({"saved": True})

    async def _handle_vehicle_zone_snapshot_get(
        self, request: web.Request
    ) -> web.StreamResponse:
        camera = request.match_info["camera"]
        snapshot_path = self._vehicle_zone_snapshot_path(camera)
        if not snapshot_path.exists():
            legacy_snapshot_path = self._legacy_vehicle_zone_snapshot_path(camera)
            if legacy_snapshot_path.exists():
                snapshot_path = legacy_snapshot_path
        if not snapshot_path.exists():
            # A car_zone saved before the persisted-snapshot redesign (see
            # _handle_vehicle_zone_put) has no snapshot file on disk — the
            # picker's preview <img> would 404, which also means its @load
            # handler never fires, containerSize never gets measured, and
            # the saved zone overlay silently never renders (VehicleZonePicker.vue's
            # previewRectStyle/previewPolygonAttr both bail out on
            # !width || !height), leaving "Clear zone" as the only way
            # forward. Fall back to that camera's newest clip thumbnail —
            # matching what the picker always showed before this redesign —
            # and persist it as the real snapshot so this is self-healing
            # after the first view.
            clips = await self._db.get_clips(
                ClipFilters(camera=camera), limit=1, sort="newest"
            )
            fallback = (
                Path(clips[0]["file_path"]).with_suffix(".jpg") if clips else None
            )
            if not fallback or not fallback.exists():
                raise web.HTTPNotFound(text="No snapshot saved for this camera")
            try:
                self._VEHICLE_ZONE_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                snapshot_path = self._vehicle_zone_snapshot_path(camera)
                snapshot_path.write_bytes(fallback.read_bytes())
            except OSError as exc:
                _LOGGER.warning(
                    "Could not persist fallback vehicle zone snapshot for %s: %s",
                    camera,
                    exc,
                )
                return web.FileResponse(fallback, headers={"Cache-Control": "no-cache"})
        # Always revalidate rather than a max-age cache: the filename is
        # stable per camera, so a stale browser cache would otherwise keep
        # showing the previous zone's frame after a new save overwrites it.
        return web.FileResponse(snapshot_path, headers={"Cache-Control": "no-cache"})

    @classmethod
    def _vehicle_zone_snapshot_path(cls, camera: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", camera.lower()).strip("-") or "camera"
        suffix = hashlib.sha256(camera.encode("utf-8")).hexdigest()[:10]
        return cls._VEHICLE_ZONE_SNAPSHOTS_DIR / f"{slug}-{suffix}.jpg"

    @classmethod
    def _legacy_vehicle_zone_snapshot_path(cls, camera: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", camera.lower()).strip("-") or "camera"
        return cls._VEHICLE_ZONE_SNAPSHOTS_DIR / f"{slug}.jpg"

    def _migrate_vehicle_zone_snapshot(self, old_name: str, new_name: str) -> None:
        old_snapshot = self._vehicle_zone_snapshot_path(old_name)
        if not old_snapshot.exists():
            old_snapshot = self._legacy_vehicle_zone_snapshot_path(old_name)
        new_snapshot = self._vehicle_zone_snapshot_path(new_name)
        if not old_snapshot.exists() or old_snapshot == new_snapshot:
            return
        try:
            if new_snapshot.exists():
                old_snapshot.unlink()
            else:
                old_snapshot.replace(new_snapshot)
        except OSError as exc:
            _LOGGER.warning(
                "Could not migrate vehicle zone snapshot %s -> %s: %s",
                old_name,
                new_name,
                exc,
            )
            raise

    async def _handle_vehicle_signature_get(self, request: web.Request) -> web.Response:
        """What this camera has learned about where the protected vehicle sits.

        Surfaced so a user can see whether the learned signature is doing
        anything yet — and, when identification is going wrong, that there
        is something to reset.
        """
        camera = request.match_info["camera"]
        signature = await self._db.get_vehicle_signature(camera)
        if signature is None:
            return web.json_response(
                {"camera": camera, "learned": False, "sample_count": 0}
            )
        return web.json_response(
            {
                "camera": camera,
                "learned": True,
                "established": signature.established,
                "sample_count": signature.sample_count,
                "box": list(signature.box),
            }
        )

    async def _handle_vehicle_signature_delete(
        self, request: web.Request
    ) -> web.Response:
        """Forget the learned signature for one camera.

        Needed whenever the premise changes — a new car, a rearranged
        driveway, or a signature that has plainly latched onto the
        neighbour's vehicle and would otherwise keep confirming its own
        mistake.
        """
        camera = request.match_info["camera"]
        return web.json_response(
            {"reset": await self._db.reset_vehicle_signature(camera)}
        )
