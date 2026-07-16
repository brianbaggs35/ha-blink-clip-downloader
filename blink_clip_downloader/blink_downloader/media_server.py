"""HTTP media server: REST API + embedded SPA with Video.js media player."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import platform
import re
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from .database import ClipDatabase
from .vision import FaceEmbedder, is_face_recognition_available, torch_cpu_compatible

if TYPE_CHECKING:
    from .analysis_queue import AnalysisQueue
    from .analyzer import BaseAnalyzer, MoondreamFineTuneManager
    from .notification_channels import NotificationDispatcher

_LOGGER = logging.getLogger(__name__)

_CLIP_NOT_FOUND = "Clip not found"

# Built by `npm run build` in frontend/ (vite.config.ts writes straight into
# this directory) — the Dockerfile's frontend-builder stage runs that build
# before the image is packaged, so this always exists in a shipped add-on.
# In a bare checkout without a build (e.g. running the Python test suite
# alone) it won't exist; _handle_index reports that clearly instead of
# serving nothing or a confusing 404.
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Moondream local install state (persists for the lifetime of the process)
# ---------------------------------------------------------------------------

_MOONDREAM_PACKAGES_DIR = Path("/data/moondream_packages")

# Pinned to the >=1.3,<2 range for the same reason as the Dockerfile's build-time
# install — see the comment there and analyzer.py's
# MoondreamLocalAnalyzer._load_model_sync for the version-drift incident this
# guards against.
_MOONDREAM_PIP_SPEC = "moondream>=1.3,<2"

_moondream_install_state: dict = {"status": "idle", "log": ""}


def _moondream_arch_supported() -> bool:
    """Return True on every architecture the add-on ships for.

    Before 4.1.0 this returned True only on x86_64, since moondream's
    torch/kestrel dependencies had no musllinux (Alpine) wheels for
    aarch64. The add-on's base image switched to Debian (glibc) in 4.1.0
    specifically to support the computer-vision pipeline's own torch
    dependency (see vision.py) — that switch also removed the musllinux
    constraint here, so this is no longer architecture-gated. Local
    ("Photon") inference still requires an NVIDIA CUDA or Apple Silicon
    GPU regardless of architecture; that check happens separately at
    model-load time (see analyzer.py's MoondreamLocalAnalyzer._load_model_sync)
    and reports the provider unavailable there rather than here.
    """
    return True


def _is_moondream_installed() -> bool:
    pkg = str(_MOONDREAM_PACKAGES_DIR)
    if _MOONDREAM_PACKAGES_DIR.exists() and pkg not in sys.path:
        sys.path.insert(0, pkg)
    try:
        import moondream  # noqa: PLC0415, F401  # type: ignore[import-not-found]

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

# Content-Security-Policy restricting everything to same-origin. Video.js
# is bundled into the Vue build's own JS/CSS (see frontend/src/components/
# library/ClipModal.vue) rather than loaded from a CDN, so no third-party
# script/style/font origin needs to be allow-listed here. 'unsafe-inline' on
# script-src covers the
# `__HAROOT__` ingress-path bootstrap snippet in index.html; on style-src it
# covers Vue's runtime `:style` bindings, which render as inline `style="..."`
# attributes rather than a `<style>` element.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' data:; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'"
)


@web.middleware
async def _security_middleware(
    request: web.Request, handler: Callable
) -> web.StreamResponse:
    """Attach security headers to every non-streaming response."""
    response = await handler(request)
    if not response.prepared:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if response.content_type == "text/html":
            response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# ---------------------------------------------------------------------------
# MediaServer
# ---------------------------------------------------------------------------


class MediaServer:
    """aiohttp web server: clip library REST API + Video.js browser UI."""

    def __init__(
        self,
        db: ClipDatabase,
        port: int,
        trigger_download: Callable[[], None] | None = None,
        two_fa_callback: Callable[[str], int] | None = None,
        auth_state_getter: Callable[[], dict] | None = None,
        analyzer: BaseAnalyzer | None = None,
        analysis_queue: AnalysisQueue | None = None,
        notification_dispatcher: NotificationDispatcher | None = None,
        moondream_api_key: str = "",
        prompt_debug_enabled: bool = False,
    ) -> None:
        self._db = db
        self._port = port
        self._trigger_download = trigger_download
        self._two_fa_callback = two_fa_callback
        self._auth_state_getter = auth_state_getter
        self._analyzer = analyzer
        self._analysis_queue = analysis_queue
        self._notification_dispatcher = notification_dispatcher
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
        # hold (see vision.py) — enrollment is a rare, occasional action, so
        # a second lazily-loaded model instance here is simpler than piping
        # a reference to the analyzer's private pipeline through for it.
        self._face_embedder = FaceEmbedder()
        self._runner: web.AppRunner | None = None
        self.extra_status: dict = {}
        # Holds a strong reference to the background moondream-install task —
        # asyncio only keeps a weak reference internally, so an unreferenced
        # task can be garbage-collected mid-install.
        self._moondream_install_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
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
        # aiohttp's default client_max_size (1 MB) is comfortably exceeded by
        # a single base64-encoded face-enrollment photo (see
        # _handle_faces_enroll) — a normal phone photo is routinely 2-8 MB
        # even before the ~33% base64 overhead, which would otherwise fail
        # every real-world enrollment with an opaque 413 before the handler
        # ever runs. 10 MB comfortably fits a real photo while still
        # bounding request size.
        app = web.Application(
            middlewares=[_security_middleware], client_max_size=10 * 1024 * 1024
        )
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/favicon.svg", self._handle_favicon)
        assets_dir = _STATIC_DIR / "assets"
        if assets_dir.is_dir():
            app.router.add_static("/assets", assets_dir)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/clips", self._handle_list_clips)
        app.router.add_get("/api/clips/{id}", self._handle_get_clip)
        app.router.add_delete("/api/clips/{id}", self._handle_delete_clip)
        app.router.add_put("/api/clips/{id}/star", self._handle_star_clip)
        app.router.add_put("/api/clips/{id}/tags", self._handle_set_tags)
        app.router.add_get("/api/clips/{id}/stream", self._handle_stream)
        app.router.add_get("/api/clips/{id}/thumb", self._handle_thumbnail)
        app.router.add_get("/api/clips/{id}/frames", self._handle_clip_frames)
        app.router.add_get("/api/cameras", self._handle_cameras)
        app.router.add_get("/api/stats", self._handle_stats)
        app.router.add_get("/api/activity", self._handle_activity)
        app.router.add_get("/api/tags", self._handle_tags)
        app.router.add_post("/api/clips/export-zip", self._handle_export_zip)
        app.router.add_post("/api/download-now", self._handle_download_now)
        app.router.add_get("/api/auth/status", self._handle_auth_status)
        app.router.add_post("/api/auth/2fa", self._handle_two_fa)
        # AI Analysis endpoints
        app.router.add_get("/api/ai/status", self._handle_ai_status)
        app.router.add_get("/api/ai/usage", self._handle_ai_usage)
        app.router.add_delete("/api/ai/usage", self._handle_ai_usage_clear)
        app.router.add_get("/api/ai/models", self._handle_ai_models)
        app.router.add_get("/api/ai/queue", self._handle_ai_queue)
        app.router.add_get("/api/ai/results/{clip_id}", self._handle_ai_clip_result)
        app.router.add_get("/api/ai/suspicious", self._handle_ai_suspicious)
        app.router.add_post("/api/ai/analyze/{clip_id}", self._handle_ai_analyze_now)
        app.router.add_post("/api/ai/test", self._handle_ai_test)
        app.router.add_get(
            "/api/ai/moondream/install-status", self._handle_moondream_install_status
        )
        app.router.add_post("/api/ai/moondream/install", self._handle_moondream_install)
        app.router.add_get("/api/ai/camera-configs", self._handle_ai_camera_configs_get)
        app.router.add_put("/api/ai/camera-configs", self._handle_ai_camera_configs_put)
        app.router.add_get("/api/vehicle/settings", self._handle_vehicle_settings_get)
        app.router.add_put("/api/vehicle/settings", self._handle_vehicle_settings_put)
        app.router.add_put("/api/vehicle/zone/{camera}", self._handle_vehicle_zone_put)
        app.router.add_delete(
            "/api/vehicle/zone/{camera}", self._handle_vehicle_zone_delete
        )
        app.router.add_get(
            "/api/vehicle/zone-snapshot/{camera}",
            self._handle_vehicle_zone_snapshot_get,
        )
        app.router.add_get(
            "/api/ai/models/escalation", self._handle_ai_models_escalation
        )
        # Adaptive learning (feedback) endpoints
        app.router.add_get("/api/ai/feedback/stats", self._handle_ai_feedback_stats)
        app.router.add_get("/api/ai/feedback/{clip_id}", self._handle_ai_feedback_get)
        app.router.add_post(
            "/api/ai/feedback/{clip_id}", self._handle_ai_feedback_submit
        )
        app.router.add_delete(
            "/api/ai/feedback/{clip_id}", self._handle_ai_feedback_delete
        )

        # Local-only face-recognition enrollment (see vision.py)
        app.router.add_get("/api/ai/faces", self._handle_faces_list)
        app.router.add_post("/api/ai/faces", self._handle_faces_enroll)
        app.router.add_delete("/api/ai/faces/{id}", self._handle_faces_delete)
        app.router.add_patch("/api/ai/faces/{id}", self._handle_faces_patch)
        app.router.add_patch(
            "/api/ai/faces/by-name/{name}", self._handle_faces_patch_by_name
        )
        app.router.add_delete(
            "/api/ai/faces/by-name/{name}", self._handle_faces_delete_by_name
        )
        app.router.add_get(
            "/api/ai/faces/bypass-stats", self._handle_faces_bypass_stats
        )
        app.router.add_get(
            "/api/ai/faces/feedback", self._handle_face_recognition_feedback_list
        )
        app.router.add_post(
            "/api/ai/faces/feedback/{clip_id}",
            self._handle_face_recognition_feedback_submit,
        )

        # Moondream Cloud fine-tuning endpoints
        app.router.add_get("/api/ai/finetune", self._handle_finetune_list)
        app.router.add_post("/api/ai/finetune", self._handle_finetune_create)
        app.router.add_get("/api/ai/finetune/{finetune_id}", self._handle_finetune_get)
        app.router.add_delete(
            "/api/ai/finetune/{finetune_id}", self._handle_finetune_delete
        )
        app.router.add_get(
            "/api/ai/finetune/{finetune_id}/checkpoints",
            self._handle_finetune_checkpoints,
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/activate", self._handle_finetune_activate
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/train", self._handle_finetune_train
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/save-checkpoint",
            self._handle_finetune_save_checkpoint,
        )
        app.router.add_get(
            "/api/ai/feedback/untrained-count", self._handle_feedback_untrained_count
        )
        app.router.add_post("/api/notifications/test-email", self._handle_test_email)
        app.router.add_post(
            "/api/notifications/test-discord", self._handle_test_discord
        )
        app.router.add_post("/api/notifications/test-mobile", self._handle_test_mobile)
        return app

    # ------------------------------------------------------------------
    # Handlers
    #
    # aiohttp always invokes route handlers as `await handler(request)`, so
    # every handler registered with `app.router.add_*` must stay `async def`
    # even when its body happens not to await anything — making one `def`
    # breaks dispatch for that route. A few handlers below (flagged by
    # SonarQube as "async without await") fall in that category; each is
    # marked NOSONAR rather than de-asynced.
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:  # NOSONAR
        # HA ingress sends X-Ingress-Path so the JS can prefix all API calls.
        # For direct port access the header is absent and the prefix is empty.
        # The header value is attacker-controlled on any deployment where a
        # client can set arbitrary request headers, so it must never be
        # interpolated into the page verbatim: json.dumps() produces a
        # properly quote/backslash-escaped JS string literal, and the
        # "</" -> "<\/" swap additionally prevents a value like
        # "</script><script>..." from closing out the surrounding <script>
        # tag early.
        index_file = _STATIC_DIR / "index.html"
        if not index_file.exists():
            raise web.HTTPInternalServerError(
                text=(
                    "Frontend build not found at "
                    f"{index_file}. Run `npm run build` in frontend/ (see "
                    "CONTRIBUTING.md) — the Docker image builds this "
                    "automatically, so this only happens in a bare checkout."
                )
            )
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        safe_literal = json.dumps(ingress_path).replace("</", "<\\/")
        html = index_file.read_text().replace("'__HAROOT__'", safe_literal)
        return web.Response(text=html, content_type="text/html")

    async def _handle_favicon(
        self, _request: web.Request
    ) -> web.StreamResponse:  # NOSONAR
        favicon = _STATIC_DIR / "favicon.svg"
        if not favicon.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(
            favicon, headers={"Cache-Control": "public, max-age=86400"}
        )

    async def _handle_health(self, _request: web.Request) -> web.Response:  # NOSONAR
        return web.json_response({"status": "ok"})

    async def _handle_list_clips(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        try:
            # A negative SQLite LIMIT means "no limit", and a negative OFFSET
            # is invalid - clamp both to non-negative so a crafted query
            # string can't bypass pagination and dump the whole table.
            limit = max(0, min(int(q.get("limit", 48)), 200))
            offset = max(0, int(q.get("offset", 0)))
        except ValueError:
            limit, offset = 48, 0

        starred_raw = q.get("starred")
        starred = True if starred_raw == "1" else False if starred_raw == "0" else None
        notified_only = q.get("notified") == "1"
        recognized_only = q.get("recognized") == "1"
        min_confidence = (
            self._analysis_queue.min_confidence if self._analysis_queue else 0.0
        )

        clips = await self._db.get_clips(
            camera=q.get("camera") or None,
            since=q.get("since") or None,
            until=q.get("until") or None,
            starred=starred,
            source=q.get("source") or None,
            tag=q.get("tag") or None,
            search=q.get("search") or None,
            sort=q.get("sort") or "newest",
            limit=limit,
            offset=offset,
            notified_only=notified_only,
            recognized_only=recognized_only,
            min_confidence=min_confidence,
        )
        return web.json_response(clips)

    async def _handle_get_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response(clip)

    async def _handle_delete_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        file_path = Path(clip["file_path"])
        if file_path.exists():
            try:
                file_path.unlink()
                thumb = file_path.with_suffix(".jpg")
                if thumb.exists():
                    thumb.unlink()
            except OSError as exc:
                _LOGGER.warning("Could not delete file %s: %s", file_path, exc)
        await self._db.delete_clip(clip_id)
        return web.json_response({"deleted": True})

    async def _handle_star_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        try:
            body = await request.json()
            starred = bool(body.get("starred", True))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")
        found = await self._db.star_clip(clip_id, starred)
        if not found:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response({"id": clip_id, "starred": starred})

    async def _handle_set_tags(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        try:
            body = await request.json()
            tags = [str(t) for t in body.get("tags", [])]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")
        found = await self._db.set_tags(clip_id, tags)
        if not found:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response({"id": clip_id, "tags": tags})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        file_path = Path(clip["file_path"])
        if not file_path.exists():
            raise web.HTTPNotFound(text="Clip file not found on disk")

        # aiohttp's FileResponse uses the OS sendfile() syscall on Linux,
        # bypassing the Python interpreter for the actual byte transfer.
        # It automatically handles Range requests (206 Partial Content),
        # ETag/Last-Modified caching, and correct Accept-Ranges headers —
        # all of which contribute to stutter-free video seeking on the Pi.
        return web.FileResponse(
            file_path,
            chunk_size=262_144,
            headers={
                "Content-Disposition": f'inline; filename="{file_path.name}"',
                # Allow the browser to cache video segments so re-seeking an
                # already-watched section never round-trips to the server.
                "Cache-Control": "public, max-age=3600",
            },
        )

    async def _handle_thumbnail(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound()

        thumb = Path(clip["file_path"]).with_suffix(".jpg")
        if thumb.exists():
            return web.FileResponse(
                thumb,
                headers={"Cache-Control": "public, max-age=3600"},
            )

        raise web.HTTPNotFound(text="Thumbnail not available")

    async def _handle_clip_frames(self, request: web.Request) -> web.Response:
        """Extract several evenly-spaced frames from one clip's video, for
        the Biometrics tab's "enroll from a clip" flow (ADVANCED FEATURE).

        Motion often starts recording before someone's face is framed well
        (e.g. a front door camera catching the moment a door opens) — a
        single thumbnail frequently isn't a usable enrollment photo. This
        lets the user browse several frames from a clip they choose and pick
        out the ones that show a face clearly, across as many
        angles/lighting conditions as they like, which is what actually
        makes recognition robust enough to reduce false positives on an
        access-point camera watched by the same few people every day.

        Query: ``count`` (default 8, clamped 1-16). Returns
        ``{"frames": ["data:image/jpeg;base64,...", ...]}`` — capped and
        scaled down (480px wide) since this is a manual, occasional action,
        not a hot path.
        """
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound()

        try:
            count = max(1, min(int(request.rel_url.query.get("count", 8)), 16))
        except ValueError:
            count = 8

        duration = float(clip.get("duration") or 0) or 10.0
        interval = max(duration / count, 0.5)
        cmd = [
            "ffmpeg",
            "-i",
            clip["file_path"],
            "-vf",
            f"fps=1/{interval},scale=480:-1",
            "-frames:v",
            str(count),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "3",
            "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            _LOGGER.warning("ffmpeg not available: %s", exc)
            return web.json_response({"frames": []})

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            _LOGGER.warning("ffmpeg timed out extracting frames for %s", clip_id)
            proc.kill()
            await proc.wait()
            return web.json_response({"frames": []})

        if proc.returncode != 0:
            _LOGGER.warning(
                "ffmpeg exited %d extracting frames for %s: %s",
                proc.returncode,
                clip_id,
                (stderr or b"").decode(errors="replace")[:200],
            )
            return web.json_response({"frames": []})

        frames = self._split_jpeg_frames(stdout or b"")
        encoded = [
            "data:image/jpeg;base64," + base64.b64encode(f).decode("ascii")
            for f in frames
        ]
        return web.json_response({"frames": encoded})

    @staticmethod
    def _split_jpeg_frames(data: bytes) -> list[bytes]:
        """Split concatenated JPEG data into individual frames.

        Mirrors ``BaseAnalyzer._split_jpeg_frames`` (analyzer.py) exactly —
        duplicated locally rather than imported so this module doesn't need
        a runtime dependency on analyzer.py's heavier imports for one small
        pure function.
        """
        frames: list[bytes] = []
        soi = b"\xff\xd8"
        eoi = b"\xff\xd9"
        pos = 0
        while pos < len(data):
            start = data.find(soi, pos)
            if start == -1:
                break
            end = data.find(eoi, start + 2)
            if end == -1:
                break
            frames.append(data[start : end + 2])
            pos = end + 2
        return frames

    async def _handle_cameras(self, _request: web.Request) -> web.Response:
        camera_stats = await self._db.get_camera_stats()
        return web.json_response(camera_stats)

    async def _handle_stats(self, request: web.Request) -> web.Response:
        stats = await self._db.get_stats()
        # extra_status is MediaServer's own dict (populated by app.py after
        # each poll cycle).  Do NOT read from request.app — that is aiohttp's
        # internal Application dict and is never populated with disk_stats.
        disk_raw = self.extra_status.get("disk")
        if disk_raw:
            stats["disk"] = disk_raw
        stats.update(self.extra_status)
        return web.json_response(stats)

    async def _handle_activity(self, request: web.Request) -> web.Response:
        try:
            # A zero/negative `days` shifts get_activity_data()'s cutoff to
            # today or into the future, silently returning no data instead of
            # erroring — clamp the lower bound like the other paginated
            # endpoints in this file (_handle_list_clips, _handle_ai_suspicious)
            # already do for limit/offset.
            days = max(1, min(int(request.rel_url.query.get("days", 7)), 30))
        except ValueError:
            days = 7
        data = await self._db.get_activity_data(days)
        return web.json_response(data)

    async def _handle_tags(self, _request: web.Request) -> web.Response:
        tags = await self._db.get_distinct_tags()
        return web.json_response(tags)

    async def _handle_export_zip(self, request: web.Request) -> web.Response:
        """Package up to 25 selected clips into a ZIP and return it."""
        try:
            body = await request.json()
            clip_ids = [str(c) for c in body.get("ids", [])][:25]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid request body")

        if not clip_ids:
            raise web.HTTPBadRequest(text="No clip IDs provided")

        buf = io.BytesIO()
        added = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cid in clip_ids:
                clip = await self._db.get_clip(cid)
                if not clip:
                    continue
                fp = Path(clip["file_path"])
                if fp.exists():
                    zf.write(fp, fp.name)
                    added += 1

        if not added:
            raise web.HTTPNotFound(text="No clip files found on disk")

        buf.seek(0)
        return web.Response(
            body=buf.read(),
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="blink-clips.zip"'},
        )

    async def _handle_auth_status(
        self, _request: web.Request
    ) -> web.Response:  # NOSONAR
        if self._auth_state_getter:
            status = self._auth_state_getter()
        else:
            status = {"state": "connected", "message": ""}
        return web.json_response(status)

    async def _handle_two_fa(self, request: web.Request) -> web.Response:
        if not self._two_fa_callback:
            raise web.HTTPServiceUnavailable(text="2FA not available")
        try:
            body = await request.json()
            code = str(body.get("code", "")).strip()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid request body")
        if not code.isdigit() or len(code) != 6:
            raise web.HTTPBadRequest(text="Code must be exactly 6 digits")
        seq = self._two_fa_callback(code)
        return web.json_response({"submitted": True, "seq": seq})

    async def _handle_download_now(self, _request: web.Request) -> web.Response:
        if self._trigger_download:
            self._trigger_download()
            return web.json_response({"triggered": True})
        try:
            Path("/data/trigger_download").touch()
        except OSError:
            pass
        return web.json_response({"triggered": True})

    # ------------------------------------------------------------------
    # AI Analysis handlers
    # ------------------------------------------------------------------

    async def _handle_ai_status(self, _request: web.Request) -> web.Response:
        enabled = self._analyzer is not None
        data: dict = {
            "enabled": enabled,
            "prompt_debug_enabled": self._prompt_debug_enabled,
        }
        if enabled:
            assert self._analyzer is not None
            data["ai_online"] = await self._analyzer.health_check()
            data["provider"] = self._analyzer.provider_name
            data["model"] = self._analyzer.model_name()
            data["car_protection_active"] = self._analyzer.car_protection_active
            # Independent of which ai_provider is configured — gates the
            # enhanced-detection/face-recognition pipeline (vision.py), which
            # any provider can have layered on top. See torch_cpu_compatible().
            data["torch_cpu_compatible"] = torch_cpu_compatible()
            if self._analyzer.provider_name == "moondream_local":
                data["moondream_installed"] = _is_moondream_installed()
                data["moondream_arch_supported"] = _moondream_arch_supported()
            escalation = self._analyzer.escalation_analyzer
            if escalation is not None:
                data["escalation_provider"] = escalation.provider_name
                data["escalation_model"] = escalation.model_name()
                # A misconfigured tier 2 (e.g. wrong API key) should be
                # visible here before it silently falls back on every
                # suspicious clip — see BaseAnalyzer._maybe_escalate.
                data["escalation_online"] = await escalation.health_check()
        data["smtp_configured"] = bool(
            self._notification_dispatcher
            and self._notification_dispatcher.smtp_configured
        )
        if self._analysis_queue:
            data["queue"] = await self._analysis_queue.get_queue_status()
        data["analysis_stats"] = await self._db.get_analysis_stats()
        return web.json_response(data)

    async def _handle_ai_usage(self, _request: web.Request) -> web.Response:
        from .analyzer import lookup_model_pricing  # noqa: PLC0415

        enabled = self._analyzer is not None
        data: dict = {"enabled": enabled}
        if enabled:
            assert self._analyzer is not None
            data["provider"] = self._analyzer.provider_name
            data["model"] = self._analyzer.model_name()
            if hasattr(self._analyzer, "model_pricing"):
                inp, out = self._analyzer.model_pricing()  # type: ignore[union-attr]
                data["cost_per_1m_input"] = inp
                data["cost_per_1m_output"] = out
        usage = await self._db.get_token_usage_stats()
        self._price_usage_by_model(usage, lookup_model_pricing)
        data["daily"] = await self._build_daily_usage(lookup_model_pricing)

        data.update(usage)
        return web.json_response(data)

    @staticmethod
    def _price_usage_by_model(usage: dict[str, Any], lookup_model_pricing: Any) -> None:
        """Price each ``by_model`` row against its own pricing table entry.

        This is done per-row (rather than the blanket "current model" rate)
        so a breakdown that spans an escalation model, or leftover rows from
        a provider the user has since switched away from, isn't priced as if
        every token cost what the active model costs. Mutates *usage*
        in place.
        """
        total_cost = 0.0
        any_priced = False
        for row in usage.get("by_model", []):
            pricing = lookup_model_pricing(row.get("model", ""))
            if pricing is None:
                row["cost"] = None
                continue
            inp, out = pricing
            row_cost = (
                int(row.get("tokens_prompt") or 0) * inp
                + int(row.get("tokens_completion") or 0) * out
            ) / 1_000_000
            row["cost"] = row_cost
            total_cost += row_cost
            any_priced = True
        usage["total_estimated_cost"] = total_cost if any_priced else None

    async def _build_daily_usage(
        self, lookup_model_pricing: Any
    ) -> list[dict[str, Any]]:
        """Build the last-14-days usage table, priced per (day, model) row.

        Each (day, model) row from the DB is priced individually — same
        reasoning as `_price_usage_by_model` — then collapsed into one total
        per day so the UI renders a small, fixed-size table instead of a
        per-model breakdown per day.
        """
        daily_totals: dict[str, dict[str, Any]] = {}
        for row in await self._db.get_daily_usage_stats(days=14):
            day = str(row["day"])
            entry = daily_totals.setdefault(
                day,
                {
                    "day": day,
                    "analyses": 0,
                    "tokens_prompt": 0,
                    "tokens_completion": 0,
                    "cost": 0.0,
                    "any_priced": False,
                },
            )
            tp = int(row.get("tokens_prompt") or 0)
            tc = int(row.get("tokens_completion") or 0)
            if not row.get("escalated"):
                entry["analyses"] += int(row.get("analyses") or 0)
            entry["tokens_prompt"] += tp
            entry["tokens_completion"] += tc
            pricing = lookup_model_pricing(row.get("model", ""))
            if pricing is not None:
                inp, out = pricing
                entry["cost"] += (tp * inp + tc * out) / 1_000_000
                entry["any_priced"] = True

        return [
            {
                "day": e["day"],
                "analyses": e["analyses"],
                "tokens_prompt": e["tokens_prompt"],
                "tokens_completion": e["tokens_completion"],
                "tokens_total": e["tokens_prompt"] + e["tokens_completion"],
                "cost": e["cost"] if e["any_priced"] else None,
            }
            for e in sorted(daily_totals.values(), key=lambda e: e["day"], reverse=True)
        ]

    async def _handle_ai_usage_clear(self, _request: web.Request) -> web.Response:
        await self._db.clear_ai_usage_stats()
        return web.json_response({"cleared": True})

    async def _handle_ai_models(self, _request: web.Request) -> web.Response:
        if not self._analyzer:
            return web.json_response({"enabled": False, "models": []})
        models = await self._analyzer.fetch_models()
        return web.json_response({"enabled": True, "models": models})

    async def _handle_ai_models_escalation(self, _request: web.Request) -> web.Response:
        """Same "fetch models" helper picker as _handle_ai_models above, but
        targeting the tier-2 escalation analyzer instead of tier-1 — has the
        same limitation the tier-1 picker already has: only works once an
        escalation provider is actually configured/attached (see
        ai_escalation_provider), not for a provider being considered but not
        yet saved.
        """
        escalation = self._analyzer.escalation_analyzer if self._analyzer else None
        if escalation is None:
            return web.json_response(
                {
                    "enabled": False,
                    "models": [],
                    "error": "No escalation provider configured",
                },
                status=400,
            )
        models = await escalation.fetch_models()
        return web.json_response({"enabled": True, "models": models})

    async def _handle_ai_queue(self, _request: web.Request) -> web.Response:
        if not self._analysis_queue:
            return web.json_response({"enabled": False})
        status = await self._analysis_queue.get_queue_status()
        return web.json_response({"enabled": True, **status})

    async def _handle_ai_clip_result(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["clip_id"]
        result = await self._db.get_analysis_for_clip(clip_id)
        if not result:
            return web.json_response(None)
        if not self._prompt_debug_enabled:
            # Off means fully hidden — even a clip analyzed while the
            # feature was previously on must not leak its stored prompt_text
            # once the admin has turned this back off.
            result.pop("prompt_text", None)
        return web.json_response(result)

    async def _handle_ai_suspicious(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        try:
            limit = max(0, min(int(q.get("limit", 50)), 200))
            offset = max(0, int(q.get("offset", 0)))
        except ValueError:
            limit, offset = 50, 0
        results = await self._db.get_suspicious_clips(limit=limit, offset=offset)
        return web.json_response(results)

    async def _handle_ai_analyze_now(self, request: web.Request) -> web.Response:
        if not self._analyzer:
            return web.json_response(
                {"error": "AI analysis not configured"}, status=400
            )
        clip_id = request.match_info["clip_id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        try:
            result = await self._analyzer.analyze_clip(
                clip_path=clip["file_path"],
                clip_id=clip_id,
                camera=clip["camera"],
                clip_duration=float(clip.get("duration") or 0),
            )
            await self._db.add_analysis_result(result.to_dict())
            return web.json_response(result.to_dict())
        except Exception as exc:  # noqa: BLE001
            # Mirrors _handle_ai_test's error handling — without this, an
            # unexpected failure here would surface as aiohttp's generic
            # HTML 500 page instead of the {"error": ...} JSON contract the
            # rest of the AI API uses, breaking the web UI's error display.
            _LOGGER.warning("AI analyze-now failed for clip %s: %s", clip_id, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_ai_test(self, _request: web.Request) -> web.Response:
        """Test AI by analyzing the most recently downloaded clip."""
        if not self._analyzer:
            return web.json_response(
                {"error": "AI analysis not configured"}, status=400
            )
        clips = await self._db.get_clips(limit=1, sort="newest")
        if not clips:
            return web.json_response(
                {"error": "No clips in library — download a clip first"},
                status=404,
            )
        clip = clips[0]
        try:
            result = await self._analyzer.analyze_clip(
                clip_path=clip["file_path"],
                clip_id=clip["id"],
                camera=clip["camera"],
                clip_duration=float(clip.get("duration") or 0),
            )
            await self._db.add_analysis_result(result.to_dict())
            return web.json_response(
                {"success": True, "clip_id": clip["id"], **result.to_dict()}
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AI test analysis failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_test_email(self, _request: web.Request) -> web.Response:
        """Send a one-off test email using the configured SMTP settings."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": "Notifications not configured"},
                status=400,
            )
        ok, message = await self._notification_dispatcher.send_test_email()
        return web.json_response(
            {"success": ok, "message": message}, status=200 if ok else 400
        )

    async def _handle_test_discord(self, _request: web.Request) -> web.Response:
        """Send a one-off test message to the configured Discord webhook."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": "Notifications not configured"},
                status=400,
            )
        ok, message = await self._notification_dispatcher.send_test_discord()
        return web.json_response(
            {"success": ok, "message": message}, status=200 if ok else 400
        )

    async def _handle_test_mobile(self, _request: web.Request) -> web.Response:
        """Send a one-off test mobile_app push notification."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": "Notifications not configured"},
                status=400,
            )
        ok, message = await self._notification_dispatcher.send_test_mobile()
        return web.json_response(
            {"success": ok, "message": message}, status=200 if ok else 400
        )

    async def _handle_moondream_install_status(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        return web.json_response(
            {
                "installed": _is_moondream_installed(),
                "arch_supported": _moondream_arch_supported(),
                "install_state": _moondream_install_state.copy(),
            }
        )

    async def _handle_moondream_install(  # NOSONAR
        self, _request: web.Request
    ) -> web.Response:
        global _moondream_install_state  # noqa: PLW0603

        if not _moondream_arch_supported():
            return web.json_response(
                {
                    "status": "unsupported",
                    "log": (
                        f"moondream_local is not supported on {platform.machine()} "
                        "(no pre-built wheels for this architecture). "
                        "Use moondream_cloud or ollama instead."
                    ),
                },
                status=422,
            )

        if _is_moondream_installed():
            return web.json_response({"status": "already_installed"})

        if _moondream_install_state.get("status") == "installing":
            return web.json_response(
                {"status": "installing", "log": _moondream_install_state.get("log", "")}
            )

        try:
            _MOONDREAM_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _LOGGER.warning("Could not create moondream packages dir: %s", exc)
        _moondream_install_state = {
            "status": "installing",
            "log": (
                f"Starting: pip install --target {_MOONDREAM_PACKAGES_DIR} "
                f"{_MOONDREAM_PIP_SPEC}\n"
            ),
        }

        async def _run_install() -> None:
            global _moondream_install_state  # noqa: PLW0603
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip3",
                    "install",
                    "--no-cache-dir",
                    "--target",
                    str(_MOONDREAM_PACKAGES_DIR),
                    _MOONDREAM_PIP_SPEC,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
                log = stdout.decode(errors="replace") if stdout else ""
                if proc.returncode == 0:
                    pkg = str(_MOONDREAM_PACKAGES_DIR)
                    if pkg not in sys.path:
                        sys.path.insert(0, pkg)
                    _moondream_install_state = {"status": "installed", "log": log}
                    _LOGGER.info(
                        "moondream installed successfully to %s",
                        _MOONDREAM_PACKAGES_DIR,
                    )
                else:
                    _moondream_install_state = {"status": "failed", "log": log}
                    _LOGGER.warning("moondream install failed (rc=%d)", proc.returncode)
            except asyncio.TimeoutError:
                _moondream_install_state = {
                    "status": "failed",
                    "log": "Installation timed out after 15 minutes",
                }
            except Exception as exc:  # noqa: BLE001
                _moondream_install_state = {"status": "failed", "log": str(exc)}

        self._moondream_install_task = asyncio.create_task(_run_install())
        return web.json_response({"status": "installing"})

    _CAMERA_CONFIGS_FILE = Path("/data/camera_configs.json")

    async def _handle_ai_camera_configs_get(
        self, _request: web.Request
    ) -> web.Response:
        """Return current per-camera AI configurations."""
        cameras = await self._db.get_camera_stats()
        cam_names = [c["camera"] for c in cameras]
        configs: list[dict] = []
        if self._CAMERA_CONFIGS_FILE.exists():
            try:
                configs = json.loads(self._CAMERA_CONFIGS_FILE.read_text())
            except Exception:  # noqa: BLE001
                configs = []
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
                },
            )
            result.append(
                {
                    "camera": name,
                    "description": str(entry.get("description", "")),
                    "custom_prompt": str(entry.get("custom_prompt", "")),
                    "is_car_camera": bool(entry.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(entry.get("car_zone")),
                }
            )
        # Also include configured cameras not in the current clip list
        for name, entry in configured.items():
            if name not in cam_names:
                result.append(
                    {
                        "camera": name,
                        "description": str(entry.get("description", "")),
                        "custom_prompt": str(entry.get("custom_prompt", "")),
                        "is_car_camera": bool(entry.get("is_car_camera", False)),
                        "car_zone": self._normalize_car_zone(entry.get("car_zone")),
                    }
                )
        return web.json_response(result)

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
                raise web.HTTPBadRequest(text="Invalid JSON body")
            configs = [
                {
                    "camera": str(c["camera"]),
                    "description": str(c.get("description", "")),
                    "custom_prompt": str(c.get("custom_prompt", "")),
                    "is_car_camera": bool(c.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(c.get("car_zone")),
                }
                for c in body
                if isinstance(c, dict) and c.get("camera")
            ]
        except web.HTTPBadRequest:
            raise
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        try:
            self._CAMERA_CONFIGS_FILE.write_text(json.dumps(configs, indent=2))
        except OSError as exc:
            _LOGGER.warning("Could not save camera configs: %s", exc)

        self._apply_camera_configs_to_analyzer(configs)

        return web.json_response({"saved": True, "count": len(configs)})

    def _apply_camera_configs_to_analyzer(self, configs: list[dict[str, Any]]) -> None:
        """Update the live analyzer without restart.

        Every field is a full replace, not a merge — camera_configs.json is
        the single source of truth for these settings (see CLAUDE.md), so
        clearing a value in the AI tab must stop it from applying
        immediately rather than leaving the last non-empty value in place
        until a restart.
        """
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

    # ------------------------------------------------------------------
    # Vehicle settings (Vehicles tab) — the one global (not per-camera)
    # car-protection setting: ai_car_description. Mirrors the
    # camera_configs.json precedent above: a small file the web UI owns,
    # falling back to the config.yaml option only until the file is first
    # written, so this is the first time this setting is editable at all
    # from the web UI rather than only via the HA Supervisor Configuration
    # tab.
    # ------------------------------------------------------------------

    _VEHICLE_SETTINGS_FILE = Path("/data/vehicle_settings.json")

    async def _handle_vehicle_settings_get(self, _request: web.Request) -> web.Response:
        if self._VEHICLE_SETTINGS_FILE.exists():
            try:
                data = json.loads(self._VEHICLE_SETTINGS_FILE.read_text())
                return web.json_response(
                    {"car_description": str(data.get("car_description", ""))}
                )
            except Exception:  # noqa: BLE001
                pass
        # File doesn't exist (or is unreadable) yet — fall back to whatever
        # the live analyzer was started with (ai_car_description).
        fallback = self._analyzer.car_description if self._analyzer else ""
        return web.json_response({"car_description": fallback})

    async def _handle_vehicle_settings_put(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            car_description = str(body.get("car_description", "") or "").strip()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        try:
            self._VEHICLE_SETTINGS_FILE.write_text(
                json.dumps({"car_description": car_description}, indent=2)
            )
        except OSError as exc:
            _LOGGER.warning("Could not save vehicle settings: %s", exc)

        if self._analyzer is not None:
            self._analyzer.update_car_description(car_description)

        return web.json_response({"saved": True})

    # ------------------------------------------------------------------
    # Per-camera car zone (Vehicles tab picker) — a dedicated,
    # immediately-applied endpoint distinct from the AI tab's full-array
    # /api/ai/camera-configs (which only takes effect once "Save Camera
    # Settings" is clicked). The picker's own Save action needs an
    # unambiguous, instant effect, and the reference snapshot below must be
    # captured at the exact moment a zone is saved rather than re-derived
    # from whatever clip happens to be newest whenever the tab is next
    # opened. Only touches car_zone on the target camera's entry — every
    # other field (description, custom_prompt, is_car_camera) is preserved
    # unchanged, same round-trip contract as the batch endpoint.
    # ------------------------------------------------------------------

    _VEHICLE_ZONE_SNAPSHOTS_DIR = Path("/data/vehicle_zone_snapshots")

    def _read_camera_configs(self) -> list[dict[str, Any]]:
        if not self._CAMERA_CONFIGS_FILE.exists():
            return []
        try:
            data = json.loads(self._CAMERA_CONFIGS_FILE.read_text())
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def _vehicle_zone_snapshot_path(cls, camera: str) -> Path:
        slug = re.sub(r"[^a-z0-9]+", "-", camera.lower()).strip("-") or "camera"
        return cls._VEHICLE_ZONE_SNAPSHOTS_DIR / f"{slug}.jpg"

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
            raise web.HTTPBadRequest(text="Invalid JSON body")

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

        configs = self._read_camera_configs()
        entry = next((c for c in configs if c.get("camera") == camera), None)
        if entry is None:
            entry = {
                "camera": camera,
                "description": "",
                "custom_prompt": "",
                "is_car_camera": True,
                "car_zone": None,
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
            self._vehicle_zone_snapshot_path(camera).write_bytes(thumb.read_bytes())
        except OSError as exc:
            _LOGGER.warning(
                "Could not save vehicle zone snapshot for %s: %s", camera, exc
            )

        try:
            self._CAMERA_CONFIGS_FILE.write_text(json.dumps(configs, indent=2))
        except OSError as exc:
            _LOGGER.warning("Could not save camera configs: %s", exc)

        self._apply_camera_configs_to_analyzer(configs)

        return web.json_response({"saved": True, "car_zone": zone})

    async def _handle_vehicle_zone_delete(self, request: web.Request) -> web.Response:
        camera = request.match_info["camera"]
        configs = self._read_camera_configs()
        entry = next((c for c in configs if c.get("camera") == camera), None)
        if entry is not None:
            entry["car_zone"] = None
            try:
                self._CAMERA_CONFIGS_FILE.write_text(json.dumps(configs, indent=2))
            except OSError as exc:
                _LOGGER.warning("Could not save camera configs: %s", exc)
            self._apply_camera_configs_to_analyzer(configs)

        self._vehicle_zone_snapshot_path(camera).unlink(missing_ok=True)

        return web.json_response({"saved": True})

    async def _handle_vehicle_zone_snapshot_get(
        self, request: web.Request
    ) -> web.StreamResponse:
        camera = request.match_info["camera"]
        snapshot_path = self._vehicle_zone_snapshot_path(camera)
        if not snapshot_path.exists():
            raise web.HTTPNotFound(text="No snapshot saved for this camera")
        # Always revalidate rather than a max-age cache: the filename is
        # stable per camera, so a stale browser cache would otherwise keep
        # showing the previous zone's frame after a new save overwrites it.
        return web.FileResponse(snapshot_path, headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------
    # Adaptive learning (human feedback on AI verdicts)
    # ------------------------------------------------------------------

    async def _handle_ai_feedback_stats(self, request: web.Request) -> web.Response:
        camera = request.rel_url.query.get("camera") or None
        stats = await self._db.get_feedback_stats(camera)
        return web.json_response(stats)

    async def _handle_ai_feedback_get(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["clip_id"]
        feedback = await self._db.get_feedback_for_clip(clip_id)
        return web.json_response(feedback)

    async def _handle_ai_feedback_submit(self, request: web.Request) -> web.Response:
        """Record feedback on a clip's stored AI verdict.

        Body: ``{"correct": bool, "correction_note": str,
        "corrected_suspicious": true|false|null}``. Requires the clip to
        already have a stored analysis result — feedback is a correction on
        an existing verdict, not a substitute for one.
        """
        clip_id = request.match_info["clip_id"]
        try:
            body = await request.json()
            correct = bool(body.get("correct"))
            correction_note = str(body.get("correction_note", "") or "")
            corrected_suspicious = body.get("corrected_suspicious")
            if corrected_suspicious is not None:
                corrected_suspicious = bool(corrected_suspicious)
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        result = await self._db.get_analysis_for_clip(clip_id)
        if not result:
            return web.json_response(
                {"error": "Clip has not been analyzed yet"}, status=400
            )

        # correct=False always means the single is_suspicious boolean was
        # wrong — there is no third option, so the corrected value is fully
        # determined by the original one. Derive it whenever the caller
        # doesn't explicitly override it, rather than leaving it null: the
        # Moondream fine-tuning training-example builder
        # (_handle_finetune_train) falls back to original_suspicious for a
        # null corrected_suspicious, which silently trained toward the
        # *wrong* label for exactly the case this is meant to fix (e.g. a
        # false positive marked incorrect with no explicit correction).
        if not correct and corrected_suspicious is None:
            corrected_suspicious = not result["is_suspicious"]

        # A bare thumbs-down with no typed note carries no reusable signal
        # for get_prompt_corrections (see database.py), which only folds in
        # rows with a non-empty correction_note. Synthesize one from the
        # direction of the correction so every "incorrect" rating still
        # becomes usable few-shot guidance for future clips on this camera.
        if not correct and not correction_note.strip():
            correction_note = (
                "Reviewer marked this as ordinary, routine activity that "
                "was incorrectly flagged suspicious."
                if result["is_suspicious"]
                else "Reviewer marked this as genuinely suspicious activity "
                "that was incorrectly cleared."
            )

        try:
            await self._db.add_feedback(
                clip_id=clip_id,
                camera=result["camera"],
                analysis_result_id=result.get("id"),
                original_suspicious=bool(result["is_suspicious"]),
                original_confidence=float(result["confidence"]),
                correct=correct,
                correction_note=correction_note,
                corrected_suspicious=corrected_suspicious,
            )
            return web.json_response({"saved": True})
        except Exception as exc:  # noqa: BLE001
            # Mirrors _handle_ai_analyze_now's error handling — an unexpected
            # DB failure here must surface as clean JSON, not aiohttp's
            # generic HTML 500 page.
            _LOGGER.warning("Feedback submit failed for clip %s: %s", clip_id, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_ai_feedback_delete(self, request: web.Request) -> web.Response:
        """Fully retract stored feedback for a clip (see ClipDatabase.delete_feedback).

        Distinct from resubmitting corrected feedback: this removes the row
        entirely, taking it out of confidence-threshold auto-tuning, prompt
        corrections, and fine-tuning training examples rather than replacing
        it with a different verdict.
        """
        clip_id = request.match_info["clip_id"]
        deleted = await self._db.delete_feedback(clip_id)
        return web.json_response({"deleted": deleted})

    # ------------------------------------------------------------------
    # Local-only face-recognition enrollment (see vision.py,
    # ai_face_recognition_enabled). Enrollment photos and the embeddings
    # computed from them are stored only in this add-on's own database —
    # never uploaded anywhere, regardless of which ai_provider is
    # configured.
    # ------------------------------------------------------------------

    async def _handle_faces_list(self, _request: web.Request) -> web.Response:
        enrollments = await self._db.list_face_enrollments()
        return web.json_response(
            {
                "available": is_face_recognition_available(),
                "faces": [
                    {
                        "id": e["id"],
                        "name": e["name"],
                        "created_at": e["created_at"],
                        "approved": bool(e["approved"]),
                    }
                    for e in enrollments
                ],
            }
        )

    async def _handle_faces_enroll(self, request: web.Request) -> web.Response:
        """Enroll a household member from a single reference photo.

        Body: ``{"name": str, "image_base64": str, "approved"?: bool}`` — a
        data-URL prefix (e.g. ``data:image/jpeg;base64,``) on
        ``image_base64`` is stripped automatically if present. Requires
        exactly one face to be detected in the photo, to avoid an ambiguous
        enrollment. ``approved`` defaults to ``True`` (bypass trust granted
        immediately) — pass ``False`` to enroll someone for recognition
        labeling only, without granting suspicious-flag bypass trust.
        """
        try:
            body = await request.json()
            name = str(body.get("name", "") or "").strip()
            image_b64 = str(body.get("image_base64", "") or "")
            approved = bool(body.get("approved", True))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        if not name:
            return web.json_response({"error": "name is required"}, status=400)
        if not image_b64:
            return web.json_response({"error": "image_base64 is required"}, status=400)
        if "," in image_b64 and image_b64.strip().startswith("data:"):
            image_b64 = image_b64.split(",", 1)[1]

        try:
            image_bytes = base64.b64decode(image_b64)
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"error": "image_base64 is not valid base64"}, status=400
            )

        if not is_face_recognition_available():
            return web.json_response(
                {
                    "error": "Face recognition is not available on this system "
                    "(missing dependencies, or a CPU that can't run them)"
                },
                status=400,
            )

        embeddings = await self._face_embedder.embed(image_bytes)
        if not embeddings:
            return web.json_response(
                {"error": "No face detected in the provided photo"}, status=400
            )
        if len(embeddings) > 1:
            return web.json_response(
                {
                    "error": (
                        f"Detected {len(embeddings)} faces in the provided photo — "
                        "use a photo with only the person being enrolled visible"
                    )
                },
                status=400,
            )

        enrollment_id = await self._db.add_face_enrollment(
            name, embeddings[0], approved=approved
        )
        return web.json_response(
            {"id": enrollment_id, "name": name, "approved": approved}
        )

    async def _handle_faces_delete(self, request: web.Request) -> web.Response:
        try:
            enrollment_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid enrollment id")
        await self._db.delete_face_enrollment(enrollment_id)
        return web.json_response({"deleted": True})

    async def _handle_faces_patch(self, request: web.Request) -> web.Response:
        """Update an enrolled member's ``approved`` flag and/or ``name``.

        Body: ``{"approved"?: bool, "name"?: str}`` — at least one field
        must be present. Lets you flip bypass trust or fix a typo without
        deleting and re-enrolling (which would require a new photo).
        """
        try:
            enrollment_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid enrollment id")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        if "approved" not in body and "name" not in body:
            return web.json_response(
                {"error": "approved and/or name is required"}, status=400
            )

        if "approved" in body:
            await self._db.set_face_enrollment_approved(
                enrollment_id, bool(body["approved"])
            )
        if "name" in body:
            new_name = str(body["name"] or "").strip()
            if not new_name:
                return web.json_response({"error": "name cannot be empty"}, status=400)
            await self._db.rename_face_enrollment(enrollment_id, new_name)

        return web.json_response({"updated": True})

    async def _handle_faces_patch_by_name(self, request: web.Request) -> web.Response:
        """Bulk-update every enrolled photo sharing a name at once.

        Body: ``{"approved"?: bool, "name"?: str}`` — used by the Biometrics
        tab's grouped person view (see the multi-frame enrollment ADVANCED
        FEATURE) so approving/renaming a person affects every photo enrolled
        for them, not just one row.
        """
        name = request.match_info["name"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        if "approved" not in body and "name" not in body:
            return web.json_response(
                {"error": "approved and/or name is required"}, status=400
            )

        if "approved" in body:
            await self._db.set_face_enrollments_approved_by_name(
                name, bool(body["approved"])
            )
        if "name" in body:
            new_name = str(body["name"] or "").strip()
            if not new_name:
                return web.json_response({"error": "name cannot be empty"}, status=400)
            await self._db.rename_face_enrollments_by_name(name, new_name)

        return web.json_response({"updated": True})

    async def _handle_faces_delete_by_name(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        await self._db.delete_face_enrollments_by_name(name)
        return web.json_response({"deleted": True})

    async def _handle_faces_bypass_stats(self, _request: web.Request) -> web.Response:
        stats = await self._db.get_face_bypass_stats()
        return web.json_response(stats)

    _FACE_FEEDBACK_TYPES = {"false_positive", "false_negative"}

    async def _handle_face_recognition_feedback_submit(
        self, request: web.Request
    ) -> web.Response:
        """Record a human report that face recognition got a clip wrong.

        Body: ``{"report_type": "false_positive"|"false_negative", "note": ""}``.
        Requires the clip to exist, but deliberately does not require an
        analysis result — a false negative (an enrolled person present but
        never recognized) can be reported on any clip, not just ones the
        bypass already fired on.
        """
        clip_id = request.match_info["clip_id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        try:
            body = await request.json()
            report_type = str(body.get("report_type", ""))
            note = str(body.get("note", "") or "")
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        if report_type not in self._FACE_FEEDBACK_TYPES:
            raise web.HTTPBadRequest(
                text="report_type must be 'false_positive' or 'false_negative'"
            )

        await self._db.add_face_recognition_feedback(
            clip_id=clip_id,
            camera=clip["camera"],
            report_type=report_type,
            note=note,
        )
        return web.json_response({"saved": True})

    async def _handle_face_recognition_feedback_list(
        self, _request: web.Request
    ) -> web.Response:
        feedback = await self._db.get_face_recognition_feedback()
        return web.json_response(feedback)

    # ------------------------------------------------------------------
    # Moondream Cloud fine-tuning
    # ------------------------------------------------------------------

    def _get_finetune_manager(self) -> MoondreamFineTuneManager | None:
        """Return a fine-tune manager, or None if not configured for it.

        Only meaningful when the active provider is moondream_cloud — the
        only one of the six providers with a fine-tuning API (see the
        module docstring and CHANGELOG for why OpenAI/Anthropic aren't
        supported here).
        """
        if (
            self._analyzer is None
            or self._analyzer.provider_name != "moondream_cloud"
            or not self._moondream_api_key
        ):
            return None
        from .analyzer import MoondreamFineTuneManager  # noqa: PLC0415

        return MoondreamFineTuneManager(api_key=self._moondream_api_key)

    async def _handle_finetune_list(self, _request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response({"enabled": False, "finetunes": []})
        try:
            finetunes = await manager.list_finetunes()
            return web.json_response({"enabled": True, "finetunes": finetunes})
        finally:
            await manager.close()

    async def _handle_finetune_create(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning requires ai_provider=moondream_cloud"},
                status=400,
            )
        try:
            try:
                body = await request.json()
                name = str(body.get("name", "") or "").strip()
                rank = int(body.get("rank", 16))
            except Exception:  # noqa: BLE001
                raise web.HTTPBadRequest(text="Invalid JSON body")

            if not name:
                return web.json_response({"error": "name is required"}, status=400)

            finetune_id = await manager.create_finetune(name, rank=rank)
            if finetune_id is None:
                return web.json_response(
                    {"error": "Failed to create fine-tune"}, status=500
                )
            return web.json_response({"finetune_id": finetune_id})
        except web.HTTPBadRequest:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Moondream create_finetune failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            await manager.close()

    async def _handle_finetune_get(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning not configured"}, status=400
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            finetune = await manager.get_finetune(finetune_id)
            if finetune is None:
                raise web.HTTPNotFound(text="Fine-tune not found")
            return web.json_response(finetune)
        finally:
            await manager.close()

    async def _handle_finetune_delete(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning not configured"}, status=400
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            deleted = await manager.delete_finetune(finetune_id)
            return web.json_response({"deleted": deleted})
        finally:
            await manager.close()

    async def _handle_finetune_checkpoints(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response({"enabled": False, "checkpoints": []})
        finetune_id = request.match_info["finetune_id"]
        try:
            checkpoints = await manager.list_checkpoints(finetune_id)
            return web.json_response({"enabled": True, "checkpoints": checkpoints})
        finally:
            await manager.close()

    _FINETUNE_STATE_FILE = Path("/data/finetune_state.json")

    async def _handle_finetune_activate(self, request: web.Request) -> web.Response:
        """Switch live inference to a fine-tuned checkpoint, no restart.

        Body: ``{"step": int}``. Only valid when the active analyzer is a
        MoondreamCloudAnalyzer (checked via _get_finetune_manager's
        provider_name gate, but the hot-swap itself needs the concrete
        analyzer instance, not just the manager).

        Also persists the activated model id to finetune_state.json —
        mirroring the camera_configs.json/vehicle_settings.json pattern —
        so a later add-on restart resumes on this checkpoint instead of
        silently reverting to whatever moondream_finetune_model was last
        saved in options.json (see App._load_finetune_model_from_ui()).
        """
        from .analyzer import MoondreamCloudAnalyzer, MoondreamFineTuneManager  # noqa: PLC0415

        if self._analyzer is None or not isinstance(
            self._analyzer, MoondreamCloudAnalyzer
        ):
            return web.json_response(
                {"error": "Fine-tuning requires ai_provider=moondream_cloud"},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            body = await request.json()
            step = int(body.get("step"))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        model_id = MoondreamFineTuneManager.get_model_id(finetune_id, step)
        self._analyzer.set_finetune_model(model_id)
        try:
            self._FINETUNE_STATE_FILE.write_text(
                json.dumps({"active_model_id": model_id}, indent=2)
            )
        except OSError as exc:
            _LOGGER.warning("Could not save fine-tune activation state: %s", exc)
        return web.json_response({"activated": True, "model": model_id})

    async def _handle_feedback_untrained_count(
        self, _request: web.Request
    ) -> web.Response:
        """Return how many feedback rows are queued for the next training run."""
        rows = await self._db.get_untrained_feedback(limit=1000)
        return web.json_response({"count": len(rows)})

    async def _build_finetune_examples(
        self, feedback_rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[int]]:
        """Pair each feedback row with a representative frame and ground truth.

        Rows whose clip or frame is no longer available are skipped (and
        left untrained, per _handle_finetune_train's docstring).
        """
        assert self._analyzer is not None
        examples: list[dict[str, Any]] = []
        trained_ids: list[int] = []
        for row in feedback_rows:
            clip = await self._db.get_clip(row["clip_id"])
            if not clip or not clip.get("file_path"):
                continue
            frames = await self._analyzer.extract_frames(clip["file_path"])
            if not frames:
                continue

            if row.get("corrected_suspicious") is not None:
                suspicious = bool(row["corrected_suspicious"])
            else:
                suspicious = bool(row["original_suspicious"])
            description = row.get("correction_note") or (
                "Suspicious activity is happening in this clip."
                if suspicious
                else "Nothing suspicious is happening in this clip."
            )
            ground_truth = json.dumps(
                {
                    "suspicious": suspicious,
                    "confidence": row["original_confidence"],
                    "description": description,
                }
            )
            examples.append(
                {
                    "image": frames[len(frames) // 2],
                    "question": self._analyzer.base_prompt_for_camera(row["camera"]),
                    "ground_truth": ground_truth,
                }
            )
            trained_ids.append(int(row["id"]))
        return examples, trained_ids

    async def _handle_finetune_train(self, request: web.Request) -> web.Response:
        """Turn queued human feedback into Moondream SFT training steps.

        Body: ``{"limit": int}`` (default 10) — how many pending feedback
        rows to consume this run. Each row is paired with a representative
        frame re-extracted from its clip and the camera's base prompt (see
        BaseAnalyzer.base_prompt_for_camera), then trained via
        MoondreamFineTuneManager.train_from_examples(). Rows behind a
        successfully-generated rollout are marked trained so a later run
        doesn't repeat them; rows this run skipped (clip/frame gone) are
        left untrained so a future run can retry them.
        """
        manager = self._get_finetune_manager()
        if manager is None:
            # _get_finetune_manager() only returns a manager once it has
            # already confirmed self._analyzer is set, so there's nothing
            # to close here.
            return web.json_response(
                {"error": "Fine-tuning requires ai_provider=moondream_cloud"},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        limit = int(body.get("limit", 10)) if isinstance(body, dict) else 10

        try:
            feedback_rows = await self._db.get_untrained_feedback(limit=limit)
            if not feedback_rows:
                return web.json_response(
                    {"trained": 0, "message": "No new feedback to train on"}
                )

            examples, trained_ids = await self._build_finetune_examples(feedback_rows)

            if not examples:
                return web.json_response(
                    {
                        "trained": 0,
                        "message": "No usable clip frames for pending feedback",
                    }
                )

            result = await manager.train_from_examples(finetune_id, examples)
            successful_ids = [
                trained_ids[i] for i in result.get("successful_indices", [])
            ]
            if successful_ids:
                await self._db.mark_feedback_trained(successful_ids)
            return web.json_response(
                {
                    "trained": result.get("steps_completed", 0),
                    "finetune_id": finetune_id,
                    "examples_attempted": len(examples),
                }
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Moondream train_from_feedback failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            await manager.close()

    async def _handle_finetune_save_checkpoint(
        self, request: web.Request
    ) -> web.Response:
        """Persist the fine-tune's current trained state as an activatable checkpoint.

        Training steps (see _handle_finetune_train) update the fine-tune's
        model weights in place, but only show up under Checkpoints — and
        become selectable via Activate — once explicitly saved.
        """
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning requires ai_provider=moondream_cloud"},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            saved = await manager.save_checkpoint(finetune_id)
            return web.json_response({"saved": saved})
        finally:
            await manager.close()
