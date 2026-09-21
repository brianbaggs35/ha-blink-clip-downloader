"""Per-camera configuration, shared by the AI and Vehicles tabs.

``/data/camera_configs.json`` is the single source of truth for a camera's
description, custom prompt, car-camera flag and car zone — not
``config.yaml``. Every ``PUT`` is a **full-array replace**, so the AI tab
and the Vehicles tab each have to round-trip the fields they do not own or
saving from one would silently discard the other's edits. The read/merge/
canonicalize helpers here are what make that safe, and they live in one
module for the same reason.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from .core import _MediaServerBase
from .support import (
    _CAMERA_CONFIGS_SAVE_ERROR,
    _INVALID_JSON_BODY,
    _SETTINGS_WRITE_FAILED,
)

_LOGGER = logging.getLogger(__name__)


class CameraConfigsRoutesMixin(_MediaServerBase):
    """Reading, merging and persisting per-camera config."""

    _CAMERA_CONFIGS_FILE = Path("/data/camera_configs.json")

    _CAMERA_NAME_ALIASES_FILE = Path("/data/camera_name_aliases.json")

    def _register_camera_configs_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        app.router.add_get("/api/ai/camera-configs", self._handle_ai_camera_configs_get)
        app.router.add_put("/api/ai/camera-configs", self._handle_ai_camera_configs_put)

    async def _handle_ai_camera_configs_get(
        self, _request: web.Request
    ) -> web.Response:
        """Return current per-camera AI configurations."""
        cameras = await self._db.get_camera_stats()
        # Unlike /api/cameras (a clip-browsing surface, where a renamed-away
        # name's history is still real and worth keeping reachable), this
        # endpoint is "configure this camera going forward" -- the AI tab's
        # Camera Configurations and the Vehicles tab. A camera that no
        # longer exists under this name has nothing to configure, no matter
        # how much clip history it has: without this filter here too, a
        # camera renamed *after* it had already produced clips (the common
        # case -- e.g. "Inside" with hundreds of old clips, renamed to
        # "Inside House") would sail straight through this cam_names list
        # forever, since only the second loop below (configured-but-
        # unclipped entries) previously checked the live camera list.
        live_names = self._list_camera_names() if self._list_camera_names else []
        live_names_lower = {str(n).lower() for n in live_names}
        cam_names = [
            c["camera"]
            for c in cameras
            if not live_names_lower or c["camera"].lower() in live_names_lower
        ]
        async with self._camera_configs_lock:
            configs = self._read_camera_configs()
            revision = self._camera_configs_revision(configs)
        # Ensure every known camera has an entry
        configured = {c.get("camera", ""): c for c in configs}
        result = []
        for name in cam_names:
            entry = configured.get(
                name,
                {
                    "camera": name,
                    "description": "",
                    "custom_prompt": "",
                    "is_car_camera": False,
                    "car_zone": None,
                    "auto_analyze": True,
                },
            )
            result.append(
                {
                    "camera": name,
                    "description": str(entry.get("description", "")),
                    "custom_prompt": str(entry.get("custom_prompt", "")),
                    "is_car_camera": bool(entry.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(entry.get("car_zone")),
                    "auto_analyze": entry.get("auto_analyze", True) is not False,
                }
            )
        # Also include configured cameras not in the current clip list (e.g.
        # a battery-dead camera that hasn't produced a clip recently) -- but
        # only if they still exist under this name on the Blink account.
        # A rename this add-on never observed under the old name (renamed
        # before this add-on's rename-tracking ever ran, or before it had
        # ever seen the camera at all) has nothing to migrate this entry
        # away from, so without this check it would linger here forever,
        # looking like a real, selectable camera long after the name is
        # gone. list_camera_names() reflects every camera *registered* to
        # the account regardless of recent activity (unlike cam_names,
        # which only reflects recent clips) so a merely-offline camera is
        # unaffected -- only skip when we have a real, non-empty list to
        # check against, so a startup window before Blink has connected
        # yet (list_camera_names() briefly empty) can't be misread as
        # "every configured camera is gone" and hide them all.
        for name, entry in configured.items():
            if name in cam_names:
                continue
            if live_names_lower and name.lower() not in live_names_lower:
                continue
            result.append(
                {
                    "camera": name,
                    "description": str(entry.get("description", "")),
                    "custom_prompt": str(entry.get("custom_prompt", "")),
                    "is_car_camera": bool(entry.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(entry.get("car_zone")),
                    "auto_analyze": entry.get("auto_analyze", True) is not False,
                }
            )
        # Also include a live camera that has neither clip history nor a
        # saved config entry yet -- e.g. one just added to the account, or
        # one this add-on has only just started seeing clips for but hasn't
        # downloaded any of yet (a fresh per-camera tracker cursor only
        # looks forward, so real pre-existing footage on Blink's side does
        # not retroactively populate local clip history). Without this, a
        # perfectly real, currently-live camera stays invisible on the AI
        # tab's Camera Configurations section, the Vehicles tab, and the AI
        # Analysis Configuration modal until its first clip happens to
        # download -- mirrors _handle_cameras's identical union, which this
        # function's own comments above already assumed existed here too.
        result_names_lower = {str(r["camera"]).lower() for r in result}
        for name in live_names:
            if name.lower() in result_names_lower:
                continue
            result.append(
                {
                    "camera": name,
                    "description": "",
                    "custom_prompt": "",
                    "is_car_camera": False,
                    "car_zone": None,
                    "auto_analyze": True,
                }
            )
            result_names_lower.add(name.lower())
        return web.json_response(
            result,
            headers={
                "ETag": f'"{revision}"',
                "X-Camera-Aliases": json.dumps(self._read_camera_name_aliases()),
            },
        )

    async def _handle_ai_camera_configs_put(self, request: web.Request) -> web.Response:
        """Save per-camera AI configurations and update the live analyzer."""
        try:
            body = await request.json()
            if not isinstance(body, list):
                # A dict (or any other non-list) body is still valid JSON and
                # silently iterates to zero entries below (e.g. `for c in {}`
                # yields nothing, no exception) — without this check that
                # would write an empty array over camera_configs.json,
                # wiping every camera's settings with no error surfaced.
                raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)
            configs = [
                {
                    "camera": str(c["camera"]),
                    "description": str(c.get("description", "")),
                    "custom_prompt": str(c.get("custom_prompt", "")),
                    "is_car_camera": bool(c.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(c.get("car_zone")),
                    "auto_analyze": c.get("auto_analyze", True) is not False,
                }
                for c in body
                if isinstance(c, dict) and c.get("camera")
            ]
            configs = self._canonicalize_camera_configs(configs)
        except web.HTTPBadRequest:
            raise
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text=_INVALID_JSON_BODY)

        async with self._camera_configs_lock:
            current_revision = self._camera_configs_revision(
                self._read_camera_configs()
            )
            current_etag = f'"{current_revision}"'
            expected_revision = request.headers.get("If-Match")
            if expected_revision and expected_revision != current_etag:
                raise web.HTTPConflict(
                    text="Camera settings changed; reload before saving"
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

            revision = self._camera_configs_revision(configs)

        return web.json_response(
            {"saved": True, "count": len(configs)}, headers={"ETag": f'"{revision}"'}
        )

    def _read_camera_configs(self) -> list[dict[str, Any]]:
        if not self._CAMERA_CONFIGS_FILE.exists():
            return []
        try:
            data = json.loads(self._CAMERA_CONFIGS_FILE.read_text())
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _merge_camera_config_fields(
        target: dict[str, Any], source: dict[str, Any]
    ) -> None:
        for field in (
            "description",
            "custom_prompt",
            "is_car_camera",
            "car_zone",
            "auto_analyze",
        ):
            if field not in target or target[field] in ("", None):
                target[field] = source.get(field)

    @staticmethod
    def _normalize_car_zone(zone: Any) -> dict[str, Any] | None:
        """Validate and coerce a raw ``car_zone`` value from stored/incoming
        JSON into either a clean ``{shape: "rect", x_min, y_min, x_max,
        y_max}`` or ``{shape: "polygon", points: [[x, y], ...]}`` dict, or
        ``None`` if it's missing or malformed.

        Zones saved before the freeform-polygon feature have no ``shape``
        key at all — treated as ``"rect"`` here so existing saved data keeps
        working without a migration, and always stamped with an explicit
        ``shape`` going forward.
        """
        if not isinstance(zone, dict):
            return None
        if zone.get("shape") == "polygon":
            points = zone.get("points")
            if not isinstance(points, list) or len(points) < 3:
                return None
            try:
                norm_points = [[float(p[0]), float(p[1])] for p in points]
            except (TypeError, ValueError, IndexError):
                return None
            if not all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in norm_points):
                return None
            return {"shape": "polygon", "points": norm_points}
        try:
            x_min, y_min = float(zone["x_min"]), float(zone["y_min"])
            x_max, y_max = float(zone["x_max"]), float(zone["y_max"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0.0 <= x_min < x_max <= 1.0 and 0.0 <= y_min < y_max <= 1.0):
            return None
        return {
            "shape": "rect",
            "x_min": x_min,
            "y_min": y_min,
            "x_max": x_max,
            "y_max": y_max,
        }

    @staticmethod
    def _camera_configs_revision(configs: list[dict[str, Any]]) -> str:
        """Return a stable revision token for the persisted camera settings."""
        serialized = json.dumps(configs, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    def _apply_camera_configs_to_analyzer(self, configs: list[dict[str, Any]]) -> None:
        """Update the live analyzer without restart.

        Every field is a full replace, not a merge — camera_configs.json is
        the single source of truth for these settings (see CLAUDE.md), so
        clearing a value in the AI tab must stop it from applying
        immediately rather than leaving the last non-empty value in place
        until a restart.
        """
        if self._update_auto_analysis_cameras is not None:
            self._update_auto_analysis_cameras(
                {c["camera"] for c in configs if c.get("auto_analyze", True) is False}
            )
        if self._analyzer is None:
            return
        descriptions = {
            c["camera"]: c["description"] for c in configs if c.get("description")
        }
        self._analyzer.update_camera_descriptions(descriptions)
        prompts = {
            c["camera"]: c["custom_prompt"] for c in configs if c.get("custom_prompt")
        }
        self._analyzer.update_camera_prompts(prompts)
        car_cameras = {c["camera"] for c in configs if c.get("is_car_camera")}
        self._analyzer.update_car_cameras(car_cameras)
        car_zones = {c["camera"]: c["car_zone"] for c in configs if c.get("car_zone")}
        self._analyzer.update_car_zones(car_zones)

    def _canonicalize_camera_configs(
        self, configs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Apply durable camera aliases and collapse stale duplicate entries."""
        aliases = self._read_camera_name_aliases()

        canonical: list[dict[str, Any]] = []
        by_name: dict[str, int] = {}
        for config in configs:
            name = str(config["camera"])
            seen: set[str] = set()
            while name.lower() in aliases and name.lower() not in seen:
                seen.add(name.lower())
                name = aliases[name.lower()]
            config["camera"] = name
            key = name.lower()
            existing = by_name.get(key)
            if existing is None:
                by_name[key] = len(canonical)
                canonical.append(config)
            else:
                canonical[existing] = config
        return canonical

    def _read_camera_name_aliases(self) -> dict[str, str]:
        """Return persisted aliases normalized for case-insensitive lookups."""
        if not self._CAMERA_NAME_ALIASES_FILE.exists():
            return {}
        try:
            data = json.loads(self._CAMERA_NAME_ALIASES_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            _LOGGER.warning("Could not load camera name aliases")
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(alias).lower(): str(target)
            for alias, target in data.items()
            if alias and target
        }

    def _migrate_camera_configs(self, old_name: str, new_name: str) -> None:
        configs = self._read_camera_configs()
        target = next(
            (
                config
                for config in configs
                if str(config.get("camera", "")).lower() == new_name.lower()
            ),
            None,
        )
        migrated: list[dict[str, Any]] = []
        changed = False
        for config in configs:
            if str(config.get("camera", "")).lower() != old_name.lower():
                migrated.append(config)
                continue
            changed = True
            if target is None:
                config["camera"] = new_name
                target = config
                migrated.append(config)
            else:
                # target is always a different object here: it was either
                # found (before this loop ran) among entries already named
                # new_name, or fixed on an earlier iteration of this same
                # loop -- never this iteration's own `config`, since the
                # caller guarantees old_name != new_name and `configs`
                # (freshly parsed JSON) never repeats an object reference.
                self._merge_camera_config_fields(target, config)
        if not changed:
            return
        try:
            self._CAMERA_CONFIGS_FILE.write_text(json.dumps(migrated, indent=2))
        except OSError as exc:
            _LOGGER.warning(_CAMERA_CONFIGS_SAVE_ERROR, exc)
            raise
        self._apply_camera_configs_to_analyzer(migrated)
