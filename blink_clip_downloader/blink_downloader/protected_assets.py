"""Assets marked on the Assets tab: how they are stored, validated and read.

``/data/protected_assets.json`` is the single source of truth, edited only
through the Assets tab (``media_server/assets.py``) and read once at startup
(``app.py``) — the same web-UI-owned-file convention as
``camera_configs.json`` and ``vehicle_settings.json``. Each entry is one
asset on one camera::

    {"id": "3f9c2a7d1b4e", "camera": "Front Door", "name": "Front door",
     "asset_type": "door", "description": "", "zone": {...},
     "enabled": true, "created_at": "...", "updated_at": "..."}

One camera per asset rather than one asset seen by several cameras: a zone
only means anything on the frame it was drawn on, so the same door watched
from two cameras is two zones either way, and keeping them apart is what
lets each be named, toggled and redrawn on its own.

Everything here is stdlib and pure apart from the file read/write, so the
add-on's startup path and the web API share one validator instead of two
that drift. Validation is deliberately strict because every one of these
strings ends up inside an AI prompt: names are single-line, bounded, and
cannot close the quotes the prompt wraps them in.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any

from .security import MARKABLE_ASSET_TYPES

_LOGGER = logging.getLogger(__name__)

ASSETS_FILE = Path("/data/protected_assets.json")

#: Longest asset name kept. Long enough for "Garage side door (by the bins)",
#: short enough that a list of them stays a small part of the prompt.
MAX_NAME_LENGTH = 48

#: Longest optional description kept — a few words of what the asset looks
#: like, which is what helps a vision model find it.
MAX_DESCRIPTION_LENGTH = 160

#: Most assets one camera may carry. Every enabled asset adds a line to that
#: camera's prompt and a pass of the event rules to every clip it records,
#: and nobody can usefully watch more than a dozen things in one view.
MAX_ASSETS_PER_CAMERA = 12

#: Asset types the Assets tab accepts, as the strings stored in the file.
MARKABLE_TYPES: frozenset[str] = frozenset(str(t) for t in MARKABLE_ASSET_TYPES)

_ID_PATTERN = re.compile(r"^[a-z0-9]{8,32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACES = re.compile(r"\s+")


def new_asset_id() -> str:
    """A fresh, URL-safe asset id."""
    return secrets.token_hex(6)


def clean_text(value: Any, limit: int) -> str:
    """*value* as one bounded line fit to quote inside a prompt.

    Control characters and runs of whitespace collapse to single spaces, and
    double quotes become single ones: the prompt names each asset in double
    quotes, and a name that could close them could append instructions of
    its own after its asset's line.
    """
    if not isinstance(value, str):
        return ""
    text = _SPACES.sub(" ", _CONTROL.sub(" ", value)).replace('"', "'").strip()
    return text[:limit].rstrip()


def normalize_zone(zone: Any) -> dict[str, Any] | None:
    """Validate and coerce a raw zone into ``{shape: "rect", x_min, y_min,
    x_max, y_max}`` or ``{shape: "polygon", points: [[x, y], ...]}``, or
    ``None`` if it's missing or malformed.

    The one validator for every user-drawn zone — the Vehicles tab's
    ``car_zone`` goes through it too (see
    ``CameraConfigsRoutesMixin._normalize_car_zone``). Zones saved before
    the freeform-polygon feature have no ``shape`` key at all; they are
    treated as ``"rect"`` so existing saved data keeps working without a
    migration, and always stamped with an explicit ``shape`` going forward.
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


def normalize_asset(raw: Any) -> dict[str, Any] | None:
    """One stored asset in canonical form, or ``None`` if it is unusable.

    Used on every read, so a hand-edited or partially written file loses
    only the entries that are actually broken rather than the whole list.
    """
    if not isinstance(raw, dict):
        return None
    asset_id = raw.get("id")
    camera = raw.get("camera")
    if not isinstance(asset_id, str) or not _ID_PATTERN.match(asset_id):
        return None
    if not isinstance(camera, str) or not camera.strip():
        return None
    name = clean_text(raw.get("name"), MAX_NAME_LENGTH)
    asset_type = raw.get("asset_type")
    zone = normalize_zone(raw.get("zone"))
    if not name or asset_type not in MARKABLE_TYPES or zone is None:
        return None
    return {
        "id": asset_id,
        "camera": camera,
        "name": name,
        "asset_type": asset_type,
        "description": clean_text(raw.get("description"), MAX_DESCRIPTION_LENGTH),
        "zone": zone,
        "enabled": raw.get("enabled", True) is not False,
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def parse_assets(data: Any) -> list[dict[str, Any]]:
    """Every usable asset in a parsed file, first occurrence of an id winning."""
    entries = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in entries:
        asset = normalize_asset(raw)
        if asset is None or asset["id"] in seen:
            continue
        seen.add(asset["id"])
        assets.append(asset)
    return assets


def read_assets(path: Path | None = None) -> list[dict[str, Any]]:
    """Every usable asset stored at *path* (default :data:`ASSETS_FILE`,
    looked up at call time so it can be redirected); empty when there is no
    file yet.

    An unreadable file is logged and read as empty rather than raised: the
    add-on must still start, and analysis carries on exactly as it would
    with nothing marked.
    """
    path = path or ASSETS_FILE
    if not path.exists():
        return []
    try:
        return parse_assets(json.loads(path.read_text()))
    except (OSError, ValueError) as exc:
        _LOGGER.warning("Could not read %s; no marked assets loaded: %s", path, exc)
        return []


def write_assets(assets: list[dict[str, Any]], path: Path | None = None) -> None:
    """Persist *assets* to *path* (default :data:`ASSETS_FILE`). Raises
    ``OSError`` if the write fails."""
    (path or ASSETS_FILE).write_text(json.dumps({"assets": assets}, indent=2))


def rename_camera(
    assets: list[dict[str, Any]], old_name: str, new_name: str
) -> list[dict[str, Any]] | None:
    """*assets* with every asset on *old_name* moved to *new_name*, or
    ``None`` when nothing was on it. Case-insensitive, like every other
    camera-name migration in the add-on."""
    old = old_name.lower()
    if not any(a["camera"].lower() == old for a in assets):
        return None
    return [
        {**a, "camera": new_name} if a["camera"].lower() == old else a for a in assets
    ]


def assets_by_camera(assets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The enabled assets, grouped by camera — what analysis consumes."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        if asset.get("enabled", True):
            grouped.setdefault(asset["camera"], []).append(asset)
    return grouped
