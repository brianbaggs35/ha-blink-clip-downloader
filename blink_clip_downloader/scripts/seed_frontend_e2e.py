"""Seed the add-on's real database with the frontend Playwright fixtures."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from standalone_server import _ALL_TABLES, _seed, _seed_assets

from blink_downloader.config import load_config
from blink_downloader.database import ClipDatabase


async def main() -> None:
    db = ClipDatabase()
    await db.init()
    try:
        assert db._pool is not None
        await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        download_path = load_config().download_path
        # Biometrics extracts frames only from clips inside the configured
        # storage root, so its real video fixture must live under this path.
        data_dir = Path(tempfile.mkdtemp(prefix=".blink-ha-e2e-", dir=download_path))
        await _seed(db, data_dir / "pending-archive-source")
        _seed_assets()
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
