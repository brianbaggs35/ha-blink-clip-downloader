"""The state every route module reads, declared in one place.

``_MediaServerBase`` holds nothing but annotations: the dependencies
:class:`~blink_downloader.media_server.MediaServer` is constructed with,
and the few pieces of runtime state it accumulates. Every route mixin
inherits it, which is what lets each one be type-checked on its own
instead of guessing at attributes some other module happens to set.

Nothing is assigned here — the real construction is
``MediaServer.__init__`` — so importing this module costs nothing and
creates no ordering constraint between the route modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..database import ClipDatabase
from ..face_enrollment import FaceCandidateStore
from ..vision import FaceEmbedder

if TYPE_CHECKING:
    from ..analysis_queue import AnalysisQueue
    from ..analyzer import BaseAnalyzer
    from ..archiver import ClipArchiver
    from ..gdrive_client import GDriveClient
    from ..gdrive_queue import GDriveUploadQueue
    from ..ha_config import HAConfigWriter
    from ..live_view import LiveViewManager
    from ..notification_channels import NotificationDispatcher


class _MediaServerBase:
    """Dependencies and runtime state shared by every route mixin."""

    # Injected dependencies.
    _db: ClipDatabase
    _port: int
    _trigger_download: Callable[[], None] | None
    _two_fa_callback: Callable[[str], int] | None
    _auth_state_getter: Callable[[], dict] | None
    _analyzer: BaseAnalyzer | None
    _analysis_queue: AnalysisQueue | None
    _notification_dispatcher: NotificationDispatcher | None
    _ha_config_writer: HAConfigWriter | None
    _gdrive_client: GDriveClient | None
    _gdrive_queue: GDriveUploadQueue | None
    _archiver: ClipArchiver | None
    _live_view: LiveViewManager | None
    _moondream_api_key: str
    _prompt_debug_enabled: bool
    _face_recognition_enabled: bool
    _face_frame_width: int

    # Narrow callables from BlinkDownloader, so no route module has to
    # import blinkpy or reach through LiveViewManager to get at a camera.
    _list_camera_names: Callable[[], list[str]] | None
    _get_camera_snapshot: Callable[[str], Awaitable[bytes | None]] | None
    _update_auto_analysis_cameras: Callable[[set[str]], None] | None
    _get_sync_module_snapshot: Callable[[], list[dict[str, Any]]] | None
    _arm_sync_module: Callable[[str, bool], Awaitable[bool | None]] | None
    _arm_camera: Callable[[str, bool], Awaitable[bool | None]] | None

    # Runtime state.
    _face_embedder: FaceEmbedder
    _face_candidates: FaceCandidateStore
    _runner: web.AppRunner | None
    _camera_configs_lock: asyncio.Lock
    _moondream_install_task: asyncio.Task | None
    _gdrive_connect_task: asyncio.Task | None
    extra_status: dict
