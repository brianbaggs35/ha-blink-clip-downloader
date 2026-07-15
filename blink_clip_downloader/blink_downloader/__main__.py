"""Entry point: `python3 -m blink_downloader`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from .app import BlinkClipDownloaderApp
from .config import AppConfig, load_config


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=numeric,
        stream=sys.stdout,
        force=True,
    )
    # Suppress chatty third-party loggers at INFO level.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
    logging.getLogger("blinkpy.helpers.util").setLevel(logging.WARNING)
    # At DEBUG level specifically, the openai SDK logs the entire outgoing
    # request body — including every frame's full base64 image data and
    # the complete prompt text — on every single analysis call, and
    # httpcore logs raw per-byte connection/TLS/header tracing underneath
    # it. Together these dwarf this add-on's own debug output (the
    # per-clip "Vision pipeline result" / "Car-zone check" / "Motion-
    # trajectory hint" lines in analyzer.py/vision.py), making a debug-mode
    # log effectively unreadable without adding anything a user
    # troubleshooting *this add-on's* behavior can act on. Capped at
    # WARNING (rather than removed) so a genuine SDK-level problem —
    # retries, deprecation notices — still surfaces; httpx is deliberately
    # left alone, since its own contribution is a single concise INFO line
    # per request ("HTTP Request: POST ... 200 OK") that's actually useful.
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # matplotlib logs its font-cache setup at DEBUG once per process
    # (triggered by the first YOLO/object-detection import) — a one-time
    # startup detail, not per-clip signal, so it's noise the same way.
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


def main() -> None:
    # Prepend the persistent moondream packages dir so a moondream install
    # performed via the web UI survives container restarts.  Must run before
    # BlinkClipDownloaderApp is constructed (moondream is imported lazily).
    _md_packages = Path("/data/moondream_packages")
    if _md_packages.exists() and str(_md_packages) not in sys.path:
        sys.path.insert(0, str(_md_packages))

    # Bootstrap minimal logging so any startup error is visible in the HA log.
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stdout,
        force=True,
    )
    _logger = logging.getLogger(__name__)

    config: AppConfig
    try:
        config = load_config()
        _setup_logging(config.log_level)
    except Exception as exc:  # noqa: BLE001
        # Do NOT call sys.exit() — the process must stay alive so the web
        # server can start and HA ingress remains reachable.  The app will run
        # in web-only mode and display the error on the Status tab.
        _logger.exception("Configuration error — starting in web-only mode: %s", exc)
        config = AppConfig(username="", password="", startup_error=str(exc))

    app = BlinkClipDownloaderApp(config)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
