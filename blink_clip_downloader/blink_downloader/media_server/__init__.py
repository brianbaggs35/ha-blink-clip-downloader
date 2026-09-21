"""HTTP media server: REST API + embedded SPA with Video.js media player.

:class:`MediaServer` is one class, exactly as before, assembled here from
one mixin per tab of the web UI. The API is big because the UI is big, and
the split follows the UI rather than the file's old line numbering, so the
module to open is the one named after the tab you are changing:

    app_shell        the SPA, its assets, /health, Blink auth state
    library          clips: list, stream, thumbnail, star, tag, export
    status           cameras, statistics, activity, battery
    liveview         one live session at a time, and its HLS output
    security_feed    the grid of near-live snapshot tiles
    ai               analyzer status, models, queue, results
    usage            token spend, priced and bucketed
    camera_configs   /data/camera_configs.json, shared by AI and Vehicles
    vehicles         protected-vehicle settings, zones, learned signatures
    security_events  the deterministic event timeline
    sync_module      arming and disarming
    feedback         thumbs up/down, and the tuning it drives
    faces            enrollments, approval, bypass auditing
    finetune         Moondream Cloud fine-tuning
    storage          archived clips and Google Drive backup
    automations      writing HA config objects, notification tests

over :mod:`.support` (middleware, JSON parsing, paging, shared strings) and
:mod:`.core` (the state every mixin reads, declared once).

Each mixin registers its own routes, so adding an endpoint is one file
rather than a handler here and a route line far away. Route order is not
load-bearing — no two registered patterns can match the same URL, and
``tests/test_media_server_routes.py`` fails if that ever stops being true.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ..database import ClipDatabase
from ..vision import FaceEmbedder
from .ai import AiRoutesMixin
from .app_shell import AppShellMixin
from .automations import AutomationRoutesMixin
from .camera_configs import CameraConfigsRoutesMixin
from .faces import FaceRoutesMixin
from .feedback import FeedbackRoutesMixin
from .finetune import FineTuneRoutesMixin
from .library import LibraryRoutesMixin
from .liveview import LiveViewRoutesMixin
from .security_events import SecurityEventsRoutesMixin
from .security_feed import SecurityFeedRoutesMixin
from .status import StatusRoutesMixin
from .storage import StorageRoutesMixin
from .support import _security_middleware
from .sync_module import SyncModuleRoutesMixin
from .usage import UsageRoutesMixin
from .vehicles import VehicleRoutesMixin

if TYPE_CHECKING:
    from ..analysis_queue import AnalysisQueue
    from ..analyzer import BaseAnalyzer
    from ..archiver import ClipArchiver
    from ..gdrive_client import GDriveClient
    from ..gdrive_queue import GDriveUploadQueue
    from ..ha_config import HAConfigWriter
    from ..live_view import LiveViewManager
    from ..notification_channels import NotificationDispatcher

_LOGGER = logging.getLogger(__name__)


class MediaServer(
    AppShellMixin,
    LibraryRoutesMixin,
    StatusRoutesMixin,
    LiveViewRoutesMixin,
    SecurityFeedRoutesMixin,
    AiRoutesMixin,
    UsageRoutesMixin,
    VehicleRoutesMixin,
    CameraConfigsRoutesMixin,
    SecurityEventsRoutesMixin,
    SyncModuleRoutesMixin,
    FeedbackRoutesMixin,
    FaceRoutesMixin,
    FineTuneRoutesMixin,
    StorageRoutesMixin,
    AutomationRoutesMixin,
):
    """aiohttp web server: clip library REST API + Video.js browser UI."""

    # Sonar's S107 (too many parameters) is deliberately suppressed here.
    # Every parameter is an independent, optional collaborator this
    # server may be given: a database, a port, and sixteen callables and
    # services that each area's mixin uses if it was handed one.
    # Grouping them into a config object would satisfy the parameter
    # count and make every one of the ~185 call sites worse — they are
    # overwhelmingly `MediaServer(db=db, port=0)`, naming only the two or
    # three collaborators that test needs, and a bag object forces
    # `MediaServer(deps=ServerDeps(db=db), port=0)` for no gain. This is
    # constructor injection working as intended, not an over-long
    # function.
    #
    # The marker sits on `self` rather than on `def` because SonarPython
    # reports S107 against the *parameter list*, which on a multi-line
    # signature begins on the line after `def` — a NOSONAR on the `def`
    # line silently suppresses nothing, which is how this was first
    # written and why the finding survived a round. Do not tidy it up
    # onto the line above.
    def __init__(
        self,  # NOSONAR
        db: ClipDatabase,
        port: int,
        trigger_download: Callable[[], None] | None = None,
        two_fa_callback: Callable[[str], int] | None = None,
        auth_state_getter: Callable[[], dict] | None = None,
        analyzer: BaseAnalyzer | None = None,
        analysis_queue: AnalysisQueue | None = None,
        notification_dispatcher: NotificationDispatcher | None = None,
        ha_config_writer: HAConfigWriter | None = None,
        gdrive_client: GDriveClient | None = None,
        gdrive_queue: GDriveUploadQueue | None = None,
        archiver: ClipArchiver | None = None,
        moondream_api_key: str = "",
        prompt_debug_enabled: bool = False,
        live_view: LiveViewManager | None = None,
        list_camera_names: Callable[[], list[str]] | None = None,
        get_camera_snapshot: Callable[[str], Awaitable[bytes | None]] | None = None,
        update_auto_analysis_cameras: Callable[[set[str]], None] | None = None,
        get_sync_module_snapshot: Callable[[], list[dict[str, Any]]] | None = None,
        arm_sync_module: Callable[[str, bool], Awaitable[bool | None]] | None = None,
        arm_camera: Callable[[str, bool], Awaitable[bool | None]] | None = None,
    ) -> None:
        self._db = db
        self._port = port
        self._trigger_download = trigger_download
        self._two_fa_callback = two_fa_callback
        self._auth_state_getter = auth_state_getter
        self._analyzer = analyzer
        self._analysis_queue = analysis_queue
        self._notification_dispatcher = notification_dispatcher
        self._ha_config_writer = ha_config_writer
        self._gdrive_client = gdrive_client
        self._gdrive_queue = gdrive_queue
        self._archiver = archiver
        self._live_view = live_view
        # Narrow callables from BlinkDownloader (same DI idiom as
        # trigger_download/two_fa_callback above, and as get_camera/
        # list_camera_names passed into LiveViewManager itself) for the
        # Security Feed tab — deliberately not routed through
        # LiveViewManager, so Security Feed works independently of it.
        self._list_camera_names = list_camera_names
        self._get_camera_snapshot = get_camera_snapshot
        self._update_auto_analysis_cameras = update_auto_analysis_cameras
        # Same narrow-callable DI idiom, for the Sync Module tab.
        self._get_sync_module_snapshot = get_sync_module_snapshot
        self._arm_sync_module = arm_sync_module
        self._arm_camera = arm_camera
        # Used only to stand up a MoondreamFineTuneManager for the Fine-Tuning
        # API/panel when provider == "moondream_cloud" — see _handle_finetune_*.
        self._moondream_api_key = moondream_api_key
        # Gates whether /api/ai/status advertises the feature and whether
        # /api/ai/results/{clip_id} ever includes prompt_text — see
        # ai_prompt_debug_enabled. Off means fully hidden, not just
        # unpopulated, even if a prompt happens to be stored from when the
        # feature was previously on.
        self._prompt_debug_enabled = prompt_debug_enabled
        # Independent from any FaceEmbedder the analyzer's VisionPipeline may
        # hold (see vision/runtime.py) — enrollment is a rare, occasional action, so
        # a second lazily-loaded model instance here is simpler than piping
        # a reference to the analyzer's private pipeline through for it.
        self._face_embedder = FaceEmbedder()
        self._runner: web.AppRunner | None = None
        self._camera_configs_lock = asyncio.Lock()
        self.extra_status: dict = {}
        # Holds a strong reference to the background moondream-install task —
        # asyncio only keeps a weak reference internally, so an unreferenced
        # task can be garbage-collected mid-install.
        self._moondream_install_task: asyncio.Task | None = None
        # Same reasoning, for the Google Drive device-flow poll task.
        self._gdrive_connect_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # Binding all interfaces is required: HA ingress reaches this server
        # from outside the container's network namespace.
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)  # nosec B104
        await site.start()
        _LOGGER.info("Media server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    # App factory
    # ------------------------------------------------------------------

    def _build_app(self) -> web.Application:
        """Create the aiohttp application and let each area register itself.

        Order is the nav order, and it does not matter — but not because
        the patterns are disjoint: ``/api/ai/feedback/stats`` and
        ``/api/ai/feedback/{clip_id}`` both match that URL. aiohttp's
        dispatcher indexes plain paths ahead of dynamic ones, so a concrete
        path wins over a placeholder whichever was registered first.
        ``tests/test_media_server_routes.py`` asserts every registered
        pattern still resolves to its own handler, so if that ever stops
        holding it fails here rather than as one endpoint quietly
        answering for another.

        aiohttp's default client_max_size (1 MB) is comfortably exceeded by
        a single base64-encoded face-enrollment photo (see
        _handle_faces_enroll) — a normal phone photo is routinely 2-8 MB
        even before the ~33% base64 overhead, which would otherwise fail
        every real-world enrollment with an opaque 413 before the handler
        ever runs. 10 MB comfortably fits a real photo while still
        bounding request size.
        """
        app = web.Application(
            middlewares=[_security_middleware], client_max_size=10 * 1024 * 1024
        )
        self._register_core_routes(app)
        self._register_library_routes(app)
        self._register_status_routes(app)
        self._register_liveview_routes(app)
        self._register_securityfeed_routes(app)
        self._register_ai_routes(app)
        self._register_usage_routes(app)
        self._register_camera_configs_routes(app)
        self._register_vehicles_routes(app)
        self._register_security_routes(app)
        self._register_syncmodule_routes(app)
        self._register_feedback_routes(app)
        self._register_faces_routes(app)
        self._register_finetune_routes(app)
        self._register_storage_routes(app)
        self._register_automations_routes(app)
        return app

    # A camera rename has to reach three different areas at once — its
    # per-camera config, the Security Feed's selected cameras, and its
    # vehicle-zone snapshot — so it belongs to none of them and lives here.
    async def rename_camera(self, old_name: str, new_name: str) -> None:
        """Migrate persisted camera settings after Blink changes a name."""
        async with self._camera_configs_lock:
            self._migrate_camera_configs(old_name, new_name)
            self._migrate_security_feed_settings(old_name, new_name)
            self._migrate_vehicle_zone_snapshot(old_name, new_name)


__all__ = ["MediaServer"]
