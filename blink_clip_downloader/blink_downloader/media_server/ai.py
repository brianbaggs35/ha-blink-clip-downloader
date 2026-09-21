"""The AI tab's API: the analyzer's state, queue and results.

Provider status and model lists, the analysis queue and its failures, a
clip's stored verdict and detections, the suspicious-clip feed, running an
analysis on demand, the connection test, and installing the optional
local Moondream package.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import sys
from pathlib import Path

from aiohttp import web

from ..database import SUSPICIOUS_PERIODS
from ..vision import torch_cpu_compatible
from .core import _MediaServerBase
from .support import (
    _CLIP_NOT_FOUND,
    _paging,
)

_LOGGER = logging.getLogger(__name__)

_MOONDREAM_PACKAGES_DIR = Path("/data/moondream_packages")


# Pinned to the >=1.3,<2 range for the same reason as the Dockerfile's build-time
# install — see the comment there and analyzer/moondream_provider.py's
# MoondreamLocalAnalyzer._load_model_sync for the version-drift incident this
# guards against.
_MOONDREAM_PIP_SPEC = "moondream>=1.3,<2"


def _moondream_arch_supported() -> bool:
    """Return True on every architecture the add-on ships for.

    Before 4.1.0 this returned True only on x86_64, since moondream's
    torch/kestrel dependencies had no musllinux (Alpine) wheels for
    aarch64. The add-on's base image switched to Debian (glibc) in 4.1.0
    specifically to support the computer-vision pipeline's own torch
    dependency (see the ``vision`` package) — that switch also removed the musllinux
    constraint here, so this is no longer architecture-gated. Local
    ("Photon") inference still requires an NVIDIA CUDA or Apple Silicon
    GPU regardless of architecture; that check happens separately at
    model-load time (see analyzer/moondream_provider.py's
    MoondreamLocalAnalyzer._load_model_sync)
    and reports the provider unavailable there rather than here.
    """
    return True


def _is_moondream_installed() -> bool:
    pkg = str(_MOONDREAM_PACKAGES_DIR)
    if _MOONDREAM_PACKAGES_DIR.exists() and pkg not in sys.path:
        sys.path.insert(0, pkg)
    try:
        import moondream  # noqa: F401  # type: ignore[import-not-found]

        return True
    except ImportError:
        return False


_moondream_install_state: dict = {"status": "idle", "log": ""}


class AiRoutesMixin(_MediaServerBase):
    """Analyzer status, models, queue, results and on-demand runs."""

    def _register_ai_routes(self, app: web.Application) -> None:
        """Register this area's routes on *app*."""
        # AI Analysis endpoints
        app.router.add_get("/api/ai/status", self._handle_ai_status)
        app.router.add_get("/api/ai/models", self._handle_ai_models)
        app.router.add_get("/api/ai/queue", self._handle_ai_queue)
        app.router.add_get("/api/ai/queue/failed", self._handle_ai_queue_failed)
        app.router.add_get("/api/ai/results/{clip_id}", self._handle_ai_clip_result)
        app.router.add_get("/api/ai/suspicious", self._handle_ai_suspicious)
        app.router.add_post("/api/ai/analyze/{clip_id}", self._handle_ai_analyze_now)
        app.router.add_post("/api/ai/test", self._handle_ai_test)
        app.router.add_get(
            "/api/ai/moondream/install-status", self._handle_moondream_install_status
        )
        app.router.add_post("/api/ai/moondream/install", self._handle_moondream_install)
        app.router.add_get(
            "/api/ai/detections/{clip_id}", self._handle_ai_clip_detections
        )
        app.router.add_get(
            "/api/ai/models/escalation", self._handle_ai_models_escalation
        )

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
            # enhanced-detection/face-recognition pipeline (the vision package), which
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

    async def _handle_ai_queue_failed(self, _request: web.Request) -> web.Response:
        """Failed analysis rows with their error message (AI tab's Queue
        Status "Failed" modal) -- gated on self._db only, mirroring
        _handle_gdrive_queue_failed: a failed row's error is worth seeing
        even if the analysis queue isn't currently running.
        """
        return web.json_response(await self._db.get_failed_analysis_queue())

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
        result["detected_objects"] = await self._db.get_detected_objects_summary(
            clip_id
        )
        result["security_events"] = await self._db.get_security_events(clip_id)
        return web.json_response(result)

    async def _handle_ai_clip_detections(self, request: web.Request) -> web.Response:
        """Per-box detections for one clip, for the clip modal's overlay.

        Kept separate from ``/api/ai/results/{id}``, which returns the
        aggregated chip summary: a minute of footage can hold hundreds of
        boxes, and every clip opened would otherwise pay for them whether or
        not the overlay is ever switched on.
        """
        return web.json_response(
            await self._db.get_detected_object_boxes(request.match_info["clip_id"])
        )

    async def _handle_ai_suspicious(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        limit, offset = _paging(q, default_limit=20, max_limit=200)
        period = q.get("period")
        if period not in SUSPICIOUS_PERIODS:
            period = None
        items = await self._db.get_suspicious_clips(
            limit=limit, offset=offset, period=period
        )
        total = await self._db.count_suspicious_clips(period=period)
        return web.json_response({"items": items, "total": total})

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
            await self._db.save_analysis(result)
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
            await self._db.save_analysis(result)
            return web.json_response(
                {"success": True, "clip_id": clip["id"], **result.to_dict()}
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AI test analysis failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

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
        global _moondream_install_state

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
            global _moondream_install_state
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
            except TimeoutError:
                _moondream_install_state = {
                    "status": "failed",
                    "log": "Installation timed out after 15 minutes",
                }
            except Exception as exc:  # noqa: BLE001
                _moondream_install_state = {"status": "failed", "log": str(exc)}

        self._moondream_install_task = asyncio.create_task(_run_install())
        return web.json_response({"status": "installing"})
