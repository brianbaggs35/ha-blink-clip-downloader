"""Deterministic external-service fixtures for browser E2E runs."""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import TYPE_CHECKING, Any

from PIL import Image

from .live_view import LiveViewManager
from .vision import DetectedFace, FaceEmbedder

if TYPE_CHECKING:
    from .media_server import MediaServer

CAMERAS = ("Front Door", "Backyard", "Garage")
SCRATCH_CAMERA = "Test Scratch"
SECURITY_FEED_NO_SNAPSHOT_CAMERA = "Garage"
E2E_FACES = (
    ([0.5, 0.5, 0.5, 0.5], 0.9, (214, 120, 90)),
    ([0.5, -0.5, 0.5, -0.5], 0.5, (90, 150, 214)),
    ([-0.5, 0.5, -0.5, 0.5], 0.1, (130, 130, 130)),
)


def _solid_jpeg(
    colour: tuple[int, int, int], size: tuple[int, int] = (112, 112)
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG")
    return buffer.getvalue()


class E2EFaceEmbedder(FaceEmbedder):
    """Provide stable candidates while retaining the real server enrollment flow."""

    async def ensure_ready(self) -> bool:
        return True

    async def detect(
        self, frame: bytes, *, thumbnails: bool = False
    ) -> list[DetectedFace] | None:
        try:
            Image.open(BytesIO(frame)).verify()
        except Exception:  # noqa: BLE001
            return None
        return [
            DetectedFace(
                embedding=list(embedding),
                probability=0.99,
                width=80,
                quality=quality,
                thumbnail=_solid_jpeg(colour) if thumbnails else b"",
            )
            for embedding, quality, colour in E2E_FACES
        ]


class _FakeBlinkAuth:
    _VALID_CODE = "123456"
    _ERROR_CODE = "999999"

    def __init__(self) -> None:
        self.state = "connected"
        self.message = ""
        self._result_seq = 0
        self._result_ok: bool | None = None

    def status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "message": self.message,
            "two_fa_result_seq": self._result_seq,
            "two_fa_result_ok": self._result_ok,
        }

    def submit_two_fa(self, code: str) -> int:
        self._result_seq += 1
        if code == self._VALID_CODE:
            self.state = "connected"
            self.message = ""
            self._result_ok = True
        elif code == self._ERROR_CODE:
            self.state = "error"
            self.message = "Blink authentication failed (simulated E2E error)."
            self._result_ok = False
        else:
            self.state = "needs_2fa"
            self.message = "Incorrect verification code. Please try again."
            self._result_ok = False
        return self._result_seq


class _FakeSyncModule:
    def __init__(self) -> None:
        self.armed = True
        self.camera_armed: dict[str, bool] = dict.fromkeys(CAMERAS, True)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "Home",
                "network_id": 10,
                "serial": "E2E-SYNC-0001",
                "version": "2.13.30",
                "status": "online",
                "online": True,
                "armed": self.armed,
                "region_id": "e2e",
                "local_storage": False,
                "cameras": [
                    {
                        "name": camera,
                        "armed": self.camera_armed[camera],
                        "online": camera != SECURITY_FEED_NO_SNAPSHOT_CAMERA,
                        "battery_state": "low" if camera == "Backyard" else "ok",
                        "battery_level": 1 if camera == "Backyard" else 3,
                        "wifi_strength": -60,
                        "type": "catalina",
                    }
                    for camera in CAMERAS
                ],
            }
        ]

    async def arm_module(self, name: str, armed: bool) -> bool | None:  # NOSONAR
        if name != "Home":
            return None
        self.armed = armed
        return True

    async def arm_camera(self, name: str, armed: bool) -> bool | None:  # NOSONAR
        if name not in self.camera_armed:
            return None
        self.camera_armed[name] = armed
        return True


class _FakeLiveViewCamera:
    async def init_livestream(self) -> None:
        await asyncio.sleep(0.3)
        raise NotImplementedError("e2e fake camera has no live view support")


class E2EFixtures:
    """External-service substitutes used only by CI's HA browser run."""

    def __init__(self) -> None:
        self.auth = _FakeBlinkAuth()
        self.sync_module = _FakeSyncModule()
        self._live_view_cameras = {name: _FakeLiveViewCamera() for name in CAMERAS}
        self.live_view = LiveViewManager(
            get_camera=self._get_live_view_camera,
            list_camera_names=lambda: list(CAMERAS),
        )
        self.face_embedder = E2EFaceEmbedder()

    @staticmethod
    def list_camera_names() -> list[str]:
        return [*CAMERAS, SCRATCH_CAMERA]

    @staticmethod
    async def get_camera_snapshot(camera: str) -> bytes | None:
        if camera == SECURITY_FEED_NO_SNAPSHOT_CAMERA:
            return None
        return _solid_jpeg((80, 120, 160), size=(4, 3))

    def _get_live_view_camera(self, name: str) -> Any:
        return self._live_view_cameras.get(name)

    def enable_face_recognition(self, server: MediaServer) -> None:
        from .media_server import faces as faces_routes

        faces_routes.is_face_recognition_available = lambda: True
        server._face_embedder = self.face_embedder


__all__ = [
    "CAMERAS",
    "SCRATCH_CAMERA",
    "SECURITY_FEED_NO_SNAPSHOT_CAMERA",
    "E2EFaceEmbedder",
    "E2EFixtures",
]
