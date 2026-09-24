"""The Assets tab's API (media_server/assets.py) against a real database."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blink_downloader import protected_assets
from blink_downloader.database import ClipDatabase
from blink_downloader.media_server import MediaServer

JPEG = b"\xff\xd8\xff" + b"\x01" * 16
OTHER_JPEG = b"\xff\xd8\xff" + b"\x02" * 16
RECT = {"shape": "rect", "x_min": 0.1, "y_min": 0.2, "x_max": 0.4, "y_max": 0.6}
POLYGON = {"shape": "polygon", "points": [[0.5, 0.5], [0.9, 0.5], [0.7, 0.9]]}


def _clip(
    tmp_path: Path,
    clip_id: str,
    camera: str,
    jpeg: bytes | None = JPEG,
    timestamp: str = "2024-06-01T08:00:00+00:00",
) -> dict[str, Any]:
    video = tmp_path / f"{clip_id}.mp4"
    video.write_bytes(b"vid")
    if jpeg is not None:
        (tmp_path / f"{clip_id}.jpg").write_bytes(jpeg)
    return {
        "id": clip_id,
        "camera": camera,
        "path": str(video),
        "timestamp": timestamp,
        "size_bytes": 1024,
        "duration": 5,
        "source": "pir",
        "network_id": 1,
    }


@dataclass
class _Api:
    client: TestClient
    server: MediaServer
    assets_file: Path
    snapshots: Path
    analyzer: MagicMock | None

    def stored(self) -> list[dict[str, Any]]:
        return json.loads(self.assets_file.read_text())["assets"]


async def _api(
    db: ClipDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    analyzer: MagicMock | None,
) -> AsyncGenerator[_Api]:
    assets_file = tmp_path / "protected_assets.json"
    snapshots = tmp_path / "asset_snapshots"
    monkeypatch.setattr(MediaServer, "_PROTECTED_ASSETS_FILE", assets_file)
    monkeypatch.setattr(MediaServer, "_ASSET_SNAPSHOTS_DIR", snapshots)
    await db.add_clip(_clip(tmp_path, "porch1", "Porch"))
    await db.add_clip(_clip(tmp_path, "porch2", "Porch", jpeg=OTHER_JPEG))
    await db.add_clip(_clip(tmp_path, "yard1", "Yard"))
    await db.add_clip(_clip(tmp_path, "nothumb", "Porch", jpeg=None))
    server = MediaServer(db=db, port=0, analyzer=analyzer)
    client = TestClient(TestServer(server._build_app()))
    await client.start_server()
    try:
        yield _Api(client, server, assets_file, snapshots, analyzer)
    finally:
        await client.close()


@pytest.fixture
async def api(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[_Api]:
    analyzer = MagicMock()
    analyzer.object_detection_enabled = True
    async for a in _api(db, tmp_path, monkeypatch, analyzer):
        yield a


@pytest.fixture
async def bare_api(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[_Api]:
    async for a in _api(db, tmp_path, monkeypatch, None):
        yield a


async def _create(api: _Api, **overrides: Any) -> dict[str, Any]:
    body = {
        "camera": "Porch",
        "name": "Front door",
        "asset_type": "door",
        "description": "Red door",
        "zone": RECT,
        "clip_id": "porch1",
        **overrides,
    }
    resp = await api.client.post("/api/assets", json=body)
    assert resp.status == 200, await resp.text()
    return (await resp.json())["asset"]


# ----------------------------------------------------------------------
# GET /api/assets
# ----------------------------------------------------------------------


async def test_list_without_analysis(bare_api: _Api) -> None:
    resp = await bare_api.client.get("/api/assets")
    assert resp.status == 200
    assert await resp.json() == {
        "assets": [],
        "limits": {"per_camera": 12, "name": 48, "description": 160},
        "analysis_enabled": False,
        "detection_enabled": False,
    }


async def test_list_says_whether_detection_runs(api: _Api) -> None:
    body = await (await api.client.get("/api/assets")).json()
    assert body["analysis_enabled"] is True
    assert body["detection_enabled"] is True
    assert api.analyzer is not None
    api.analyzer.object_detection_enabled = False
    body = await (await api.client.get("/api/assets")).json()
    assert body["detection_enabled"] is False


# ----------------------------------------------------------------------
# POST /api/assets
# ----------------------------------------------------------------------


async def test_create_stores_applies_and_keeps_the_frame(api: _Api) -> None:
    asset = await _create(api, name='  The "front"\n door  ')
    assert len(asset["id"]) == 12
    assert asset["camera"] == "Porch"
    assert asset["name"] == "The 'front' door"
    assert asset["zone"] == RECT
    assert asset["enabled"] is True
    assert asset["created_at"] == asset["updated_at"] != ""
    assert api.stored() == [asset]
    assert MediaServer._asset_snapshot_path("Porch").read_bytes() == JPEG
    assert api.analyzer is not None
    api.analyzer.update_protected_assets.assert_called_with([asset])

    listed = await (await api.client.get("/api/assets")).json()
    assert listed["assets"] == [asset]


async def test_a_second_asset_is_drawn_on_the_cameras_frame(api: _Api) -> None:
    await _create(api, clip_id="porch1")
    bike = await _create(api, clip_id="", name="Bike", asset_type="bicycle")
    assert bike["name"] == "Bike"
    assert MediaServer._asset_snapshot_path("Porch").read_bytes() == JPEG


async def test_create_takes_a_freeform_zone(api: _Api) -> None:
    asset = await _create(api, zone=POLYGON, asset_type="bicycle", name="Bike")
    assert asset["zone"] == POLYGON


@pytest.mark.parametrize(
    ("overrides", "status", "message"),
    [
        ({"camera": "  "}, 400, "Missing camera"),
        ({"name": "   "}, 400, "Give the asset a name"),
        ({"asset_type": "vehicle"}, 400, "Unknown asset type"),
        ({"zone": {"x_min": 0.5}}, 400, "Invalid or missing zone"),
        ({"clip_id": ""}, 400, "Missing clip_id"),
        ({"clip_id": "nope"}, 404, "Clip not found"),
        ({"clip_id": "yard1"}, 400, "different camera"),
        ({"clip_id": "nothumb"}, 404, "Thumbnail not available"),
    ],
)
async def test_create_refuses_what_it_cannot_store(
    api: _Api, overrides: dict[str, Any], status: int, message: str
) -> None:
    body = {
        "camera": "Porch",
        "name": "Front door",
        "asset_type": "door",
        "zone": RECT,
        "clip_id": "porch1",
        **overrides,
    }
    resp = await api.client.post("/api/assets", json=body)
    assert resp.status == status
    assert message in await resp.text()
    assert not api.assets_file.exists()


async def test_create_refuses_a_non_object_body(api: _Api) -> None:
    resp = await api.client.post("/api/assets", json=["not", "an", "object"])
    assert resp.status == 400


async def test_create_is_capped_per_camera(
    api: _Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(protected_assets, "MAX_ASSETS_PER_CAMERA", 2)
    await _create(api)
    await _create(api, name="Bike", asset_type="bicycle")
    resp = await api.client.post(
        "/api/assets",
        json={
            "camera": "porch",
            "name": "Mailbox",
            "asset_type": "mailbox",
            "zone": RECT,
            "clip_id": "porch1",
        },
    )
    assert resp.status == 409
    assert "2 assets" in await resp.text()
    # Other cameras have their own allowance.
    await _create(api, camera="Yard", clip_id="yard1", name="Gate", asset_type="gate")


async def test_names_are_unique_per_camera(api: _Api) -> None:
    """The name is how events and the prompt refer to an asset."""
    door = await _create(api, name="Front door")
    resp = await api.client.post(
        "/api/assets",
        json={
            "camera": "PORCH",
            "name": "front DOOR",
            "asset_type": "door",
            "zone": RECT,
            "clip_id": "porch1",
        },
    )
    assert resp.status == 409
    assert "already has an asset called “Front door”" in await resp.text()
    # The same name on another camera is its own asset.
    await _create(api, camera="Yard", clip_id="yard1", name="Front door")
    # Renaming onto a taken name is refused; keeping one's own is not.
    bike = await _create(api, name="Bike", asset_type="bicycle")
    clash = await api.client.put(
        f"/api/assets/{bike['id']}", json={"name": "Front Door"}
    )
    assert clash.status == 409
    same = await api.client.put(
        f"/api/assets/{door['id']}", json={"name": "Front Door"}
    )
    assert same.status == 200


async def test_a_failed_frame_save_does_not_fail_the_asset(
    api: _Api, caplog: pytest.LogCaptureFixture
) -> None:
    api.snapshots.write_text("a file where the directory should be")
    asset = await _create(api)
    assert api.stored() == [asset]
    assert "Could not save the asset frame" in caplog.text


async def test_a_failed_write_is_an_error_but_still_applies(api: _Api) -> None:
    api.assets_file.mkdir()
    resp = await api.client.post(
        "/api/assets",
        json={
            "camera": "Porch",
            "name": "Front door",
            "asset_type": "door",
            "zone": RECT,
            "clip_id": "porch1",
        },
    )
    assert resp.status == 500
    assert api.analyzer is not None
    api.analyzer.update_protected_assets.assert_called_once()


async def test_create_without_an_analyzer(bare_api: _Api) -> None:
    asset = await _create(bare_api)
    assert bare_api.stored() == [asset]


# ----------------------------------------------------------------------
# PUT /api/assets/{id}
# ----------------------------------------------------------------------


async def test_update_changes_only_what_it_is_given(api: _Api) -> None:
    asset = await _create(api)
    resp = await api.client.put(
        f"/api/assets/{asset['id']}",
        json={"name": "Back door", "enabled": False, "camera": "Yard"},
    )
    assert resp.status == 200
    updated = (await resp.json())["asset"]
    assert updated["name"] == "Back door"
    assert updated["enabled"] is False
    assert updated["camera"] == "Porch"
    assert updated["zone"] == RECT
    assert updated["description"] == "Red door"
    assert updated["updated_at"] >= asset["updated_at"]
    assert api.stored() == [updated]


async def test_update_can_redraw_on_a_newer_frame(api: _Api) -> None:
    asset = await _create(api)
    resp = await api.client.put(
        f"/api/assets/{asset['id']}",
        json={"zone": POLYGON, "asset_type": "window", "clip_id": "porch2"},
    )
    assert resp.status == 200
    updated = (await resp.json())["asset"]
    assert (updated["zone"], updated["asset_type"]) == (POLYGON, "window")
    assert MediaServer._asset_snapshot_path("Porch").read_bytes() == OTHER_JPEG


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"name": ""}, 400),
        ({"asset_type": "spaceship"}, 400),
        ({"zone": None}, 400),
        ({"clip_id": "yard1"}, 400),
    ],
)
async def test_update_refuses_bad_fields(
    api: _Api, body: dict[str, Any], status: int
) -> None:
    asset = await _create(api)
    resp = await api.client.put(f"/api/assets/{asset['id']}", json=body)
    assert resp.status == status
    assert api.stored() == [asset]


async def test_update_of_an_unknown_asset(api: _Api) -> None:
    resp = await api.client.put("/api/assets/doesnotexist", json={"name": "x"})
    assert resp.status == 404


# ----------------------------------------------------------------------
# DELETE /api/assets/{id}
# ----------------------------------------------------------------------


async def test_delete_keeps_the_frame_until_the_last_asset_goes(api: _Api) -> None:
    door = await _create(api)
    bike = await _create(api, name="Bike", asset_type="bicycle")
    frame = MediaServer._asset_snapshot_path("Porch")

    resp = await api.client.delete(f"/api/assets/{door['id']}")
    assert resp.status == 200
    assert await resp.json() == {"deleted": True}
    assert api.stored() == [bike]
    assert frame.exists()

    await api.client.delete(f"/api/assets/{bike['id']}")
    assert api.stored() == []
    assert not frame.exists()
    assert api.analyzer is not None
    api.analyzer.update_protected_assets.assert_called_with([])


async def test_delete_of_an_unknown_asset(api: _Api) -> None:
    resp = await api.client.delete("/api/assets/doesnotexist")
    assert resp.status == 404


# ----------------------------------------------------------------------
# GET /api/assets/snapshot/{camera}
# ----------------------------------------------------------------------


async def test_snapshot_serves_the_saved_frame(api: _Api) -> None:
    await _create(api, clip_id="porch1")
    resp = await api.client.get("/api/assets/snapshot/Porch")
    assert resp.status == 200
    assert await resp.read() == JPEG
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_snapshot_falls_back_to_the_newest_clip(
    api: _Api, db: ClipDatabase, tmp_path: Path
) -> None:
    await db.add_clip(
        _clip(
            tmp_path,
            "yard2",
            "Yard",
            jpeg=OTHER_JPEG,
            timestamp="2025-01-01T00:00:00+00:00",
        )
    )
    resp = await api.client.get("/api/assets/snapshot/Yard")
    assert resp.status == 200
    assert await resp.read() == OTHER_JPEG


async def test_snapshot_with_no_frame_anywhere(
    api: _Api, db: ClipDatabase, tmp_path: Path
) -> None:
    assert (await api.client.get("/api/assets/snapshot/Garage")).status == 404
    await db.add_clip(_clip(tmp_path, "shed1", "Shed", jpeg=None))
    assert (await api.client.get("/api/assets/snapshot/Shed")).status == 404


def test_snapshot_path_is_slugified_and_case_insensitive() -> None:
    path = MediaServer._asset_snapshot_path("Front Door/../x")
    assert path.parent == MediaServer._ASSET_SNAPSHOTS_DIR
    assert path.name.startswith("front-door-x-")
    assert MediaServer._asset_snapshot_path(
        "PORCH"
    ) == MediaServer._asset_snapshot_path("porch")
    assert MediaServer._asset_snapshot_path("!!!").name.startswith("camera-")


# ----------------------------------------------------------------------
# GET /api/assets/activity
# ----------------------------------------------------------------------


async def _event(
    db: ClipDatabase,
    clip_id: str,
    camera: str,
    asset_name: str,
    asset_type: str,
    severity: str,
) -> None:
    assert db._pool is not None
    await db._pool.execute(
        "INSERT INTO security_events (clip_id, camera, event_type, severity, "
        "asset_name, asset_type, created_at) VALUES ($1, $2, 'zone_entered', $3, "
        "$4, $5, '2026-01-01T00:00:00+00:00')",
        clip_id,
        camera,
        severity,
        asset_name,
        asset_type,
    )


async def test_activity_counts_clips_per_marked_asset(
    api: _Api, db: ClipDatabase, tmp_path: Path
) -> None:
    recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    await db.add_clip(_clip(tmp_path, "r1", "Porch", timestamp=recent))
    await db.add_clip(_clip(tmp_path, "r2", "Porch", timestamp=recent))
    await db.add_clip(_clip(tmp_path, "o1", "Porch", timestamp=old))
    await _event(db, "r1", "Porch", "Front door", "door", "noteworthy")
    await _event(db, "r1", "Porch", "Front door", "door", "suspicious")
    await _event(db, "r2", "Porch", "Front door", "door", "routine")
    await _event(db, "r2", "Porch", "Silver Kia", "vehicle", "critical")
    await _event(db, "r2", "Porch", "", "", "routine")
    await _event(db, "o1", "Porch", "Front door", "door", "critical")

    resp = await api.client.get("/api/assets/activity")
    assert resp.status == 200
    body = await resp.json()
    assert body["days"] == 7
    assert body["activity"] == [
        {
            "camera": "Porch",
            "asset_name": "Front door",
            "clips": 2,
            "last_seen": recent,
            "top_severity": "suspicious",
        }
    ]
    long = await (await api.client.get("/api/assets/activity?days=500")).json()
    assert long["days"] == 90
    assert long["activity"][0]["clips"] == 3
    assert long["activity"][0]["top_severity"] == "critical"


async def test_activity_rejects_a_non_number(api: _Api) -> None:
    assert (await api.client.get("/api/assets/activity?days=week")).status == 400


async def test_activity_with_no_database_pool() -> None:
    assert await ClipDatabase("postgresql://unused").get_asset_activity() == []


# ----------------------------------------------------------------------
# Camera renames
# ----------------------------------------------------------------------


async def test_rename_carries_assets_and_their_frame(api: _Api) -> None:
    door = await _create(api)
    await _create(api, camera="Yard", clip_id="yard1", name="Gate", asset_type="gate")
    await api.server.rename_camera("Porch", "Front Porch")
    stored = api.stored()
    assert [a["camera"] for a in stored] == ["Front Porch", "Yard"]
    assert stored[0]["id"] == door["id"]
    assert not MediaServer._asset_snapshot_path("Porch").exists()
    assert MediaServer._asset_snapshot_path("Front Porch").read_bytes() == JPEG
    assert api.analyzer is not None
    api.analyzer.update_protected_assets.assert_called_with(stored)


async def test_rename_onto_a_camera_with_its_own_frame_keeps_that_one(
    api: _Api,
) -> None:
    await _create(api)
    await _create(api, camera="Yard", clip_id="yard1", name="Gate", asset_type="gate")
    MediaServer._asset_snapshot_path("Yard").write_bytes(OTHER_JPEG)
    await api.server.rename_camera("Porch", "Yard")
    assert {a["camera"] for a in api.stored()} == {"Yard"}
    assert not MediaServer._asset_snapshot_path("Porch").exists()
    assert MediaServer._asset_snapshot_path("Yard").read_bytes() == OTHER_JPEG


async def test_rename_of_a_camera_with_nothing_marked_writes_nothing(
    bare_api: _Api,
) -> None:
    await bare_api.server.rename_camera("Porch", "Front Porch")
    assert not bare_api.assets_file.exists()
    # A case-only rename leaves the frame where it is.
    await _create(bare_api)
    await bare_api.server.rename_camera("Porch", "porch")
    assert MediaServer._asset_snapshot_path("porch").exists()
    assert bare_api.stored()[0]["camera"] == "porch"
