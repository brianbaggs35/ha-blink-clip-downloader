from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import pytest
from PIL import Image

from blink_downloader.e2e_fixtures import (
    CAMERAS,
    SCRATCH_CAMERA,
    SECURITY_FEED_NO_SNAPSHOT_CAMERA,
    E2EFaceEmbedder,
    E2EFixtures,
)


async def test_face_embedder_returns_stable_faces_for_readable_images() -> None:
    embedder = E2EFaceEmbedder()
    buffer = BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="JPEG")

    assert await embedder.ensure_ready()
    faces_with_thumbnails = await embedder.detect(buffer.getvalue(), thumbnails=True)
    faces_without_thumbnails = await embedder.detect(buffer.getvalue())

    assert faces_with_thumbnails is not None
    assert len(faces_with_thumbnails) == 3
    assert all(face.thumbnail for face in faces_with_thumbnails)
    assert faces_without_thumbnails is not None
    assert all(not face.thumbnail for face in faces_without_thumbnails)


async def test_face_embedder_returns_none_for_unreadable_images() -> None:
    assert await E2EFaceEmbedder().detect(b"not an image") is None


async def test_fake_auth_supports_success_retry_and_error_states() -> None:
    auth = E2EFixtures().auth
    assert auth.status()["state"] == "connected"
    assert auth.submit_two_fa("000000") == 1
    assert auth.status()["state"] == "needs_2fa"
    assert auth.submit_two_fa("123456") == 2
    assert auth.status()["two_fa_result_ok"] is True
    assert auth.submit_two_fa("999999") == 3
    assert auth.status()["state"] == "error"
    assert auth.status()["two_fa_result_ok"] is False


async def test_fake_sync_module_updates_module_and_camera_arming() -> None:
    sync = E2EFixtures().sync_module
    assert sync.snapshot()[0]["armed"] is True
    assert await sync.arm_module("Home", False) is True
    assert sync.snapshot()[0]["armed"] is False
    assert await sync.arm_module("Unknown", True) is None
    assert await sync.arm_camera("Front Door", False) is True
    assert sync.snapshot()[0]["cameras"][0]["armed"] is False
    assert await sync.arm_camera("Unknown", True) is None


async def test_fixture_camera_and_snapshot_coverage() -> None:
    fixtures = E2EFixtures()
    assert fixtures.list_camera_names() == [*CAMERAS, SCRATCH_CAMERA]
    assert fixtures.live_view.list_cameras() == list(CAMERAS)
    assert fixtures._get_live_view_camera("Front Door") is not None
    assert fixtures._get_live_view_camera("Unknown") is None
    assert await fixtures.get_camera_snapshot(SECURITY_FEED_NO_SNAPSHOT_CAMERA) is None
    snapshot = await fixtures.get_camera_snapshot("Front Door")
    assert snapshot is not None
    with Image.open(BytesIO(snapshot)) as image:
        assert image.size == (4, 3)
        image.verify()

    camera = fixtures._get_live_view_camera("Front Door")
    assert camera is not None
    with pytest.raises(NotImplementedError, match="no live view support"):
        await camera.init_livestream()


def test_fixture_enables_face_recognition_for_its_media_server(monkeypatch) -> None:
    from blink_downloader.media_server import MediaServer
    from blink_downloader.media_server import faces as faces_routes

    available = faces_routes.is_face_recognition_available
    monkeypatch.setattr(faces_routes, "is_face_recognition_available", available)
    fixtures = E2EFixtures()
    server = MediaServer(db=MagicMock(), port=0)

    fixtures.enable_face_recognition(server)

    assert faces_routes.is_face_recognition_available()
    assert server._face_embedder is fixtures.face_embedder
