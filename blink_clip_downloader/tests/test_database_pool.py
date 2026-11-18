"""The connection pool must not log a warning when the server goes away."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from blink_downloader.database import ClipDatabase

from .conftest import TEST_DB_DSN


@pytest.fixture
async def pooled() -> AsyncGenerator[ClipDatabase]:
    database = ClipDatabase(TEST_DB_DSN)
    await database.init()
    yield database
    await database.close()


async def _admin() -> asyncpg.Connection:
    """A connection to the server's ``postgres`` database, outside the pool."""
    parts = urlsplit(TEST_DB_DSN)
    return await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))


async def test_pool_keeps_no_connection_floor(pooled: ClipDatabase) -> None:
    assert pooled._pool is not None
    assert pooled._pool.get_min_size() == 0


async def test_pool_connects_once_at_start_so_a_bad_dsn_fails_early(
    pooled: ClipDatabase,
) -> None:
    assert pooled._pool is not None
    assert pooled._pool.get_init_size() == 1


async def test_losing_every_connection_logs_no_floor_warning(
    pooled: ClipDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """A server stopping at shutdown drops the pool's connections and then
    refuses new ones (``the database system is shutting down``). The pool
    used to answer by retrying to restore a connection floor, logging a
    warning with a traceback each time. A database that no longer allows
    connections fails the same way, on the real server (a superuser would
    ignore a connection limit, so that is not used).
    """
    assert pooled._pool is not None
    database = urlsplit(TEST_DB_DSN).path.lstrip("/")
    admin = await _admin()
    try:
        await admin.execute(f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS false')
        try:
            with caplog.at_level(logging.WARNING, logger="asyncpg.pool"):
                await admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = $1 AND pid <> pg_backend_pid()",
                    database,
                )
                await asyncio.sleep(0.5)
        finally:
            await admin.execute(f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS true')
    finally:
        await admin.close()
    assert caplog.records == []
    # Once the server accepts connections again the pool simply reconnects.
    assert await pooled._pool.fetchval("SELECT 1") == 1
