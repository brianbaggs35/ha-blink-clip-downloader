"""Entry point: `python3 -m blink_downloader`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Prepend the persistent moondream packages dir to sys.path so that a
# moondream install performed via the web UI survives container restarts.
# Must happen before any local import that might lazily try to import moondream.
_md_packages = Path("/data/moondream_packages")
if _md_packages.exists() and str(_md_packages) not in sys.path:
    sys.path.insert(0, str(_md_packages))

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


def main() -> None:
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
        _logger.error("Configuration error — starting in web-only mode: %s", exc)
        config = AppConfig(username="", password="", startup_error=str(exc))

    app = BlinkClipDownloaderApp(config)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
