"""Connection lifecycle, and the state every query mixin reads.

``_DatabaseBase`` exists so each domain mixin can be type-checked on its
own: it declares the two attributes they all reach for without any of them
having to redeclare (or guess at) them.
"""

from __future__ import annotations

import logging
import os

import asyncpg

from .schema import _MIGRATIONS, _SCHEMA

_LOGGER = logging.getLogger(__name__)

# Overridable via BLINK_DB_DSN for local dev/testing against a different
# PostgreSQL instance; the container always uses the bundled server.
#
# SonarCloud flags this connection as unprotected by a password — but (see
# the module docstring above, and rootfs/etc/cont-init.d/01-postgres-init.sh)
# this server has no TCP listener at all (listen_addresses=''), only a Unix
# socket reachable from inside this exact container, with
# --auth-host=reject on top. A password here would have to live in the
# same trust boundary this process already reads from, so it would add
# real complexity (generation, migration, breaking the BLINK_DB_DSN
# override below) for no actual defense-in-depth.
DEFAULT_DSN = os.environ.get(
    "BLINK_DB_DSN",
    "postgresql://blink@/blink_clips?host=/var/run/postgresql",  # NOSONAR
)


class _DatabaseBase:
    """The connection state shared by every :class:`ClipDatabase` mixin."""

    _dsn: str
    _pool: asyncpg.Pool | None


class ConnectionMixin(_DatabaseBase):
    """Opening the pool, applying the schema, and shutting down cleanly."""

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        """Connect to PostgreSQL and create tables if needed."""
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            # Every timestamp column in this schema is a UTC ISO-8601 TEXT
            # string (see datetime.now(timezone.utc).isoformat() throughout
            # this module) compared/bucketed with plain string ops
            # (LIKE 'YYYY-MM-DD%') or cast to timestamptz for date/hour
            # extraction — EXTRACT()/::date on a timestamptz apply the
            # *session* time zone, so without pinning it here those casts
            # would silently bucket by the server's local zone instead of
            # UTC, shifting every "today"/hourly-activity figure.
            server_settings={"timezone": "UTC"},
        )
        await self._pool.execute(_SCHEMA)
        await self._pool.execute(_MIGRATIONS)
        await self._reset_stale_processing()
        _LOGGER.debug("Clip database connected (dsn=%s)", self._dsn)

    async def _reset_stale_processing(self) -> None:
        """Reset any items stuck in 'processing' back to 'pending'.

        Items land in 'processing' when the app crashes or is restarted
        mid-analysis/mid-upload. They are never retried otherwise because
        each queue only fetches status='pending'. Covers both background
        queue tables (analysis_queue, gdrive_upload_queue) — table names
        are hardcoded literals from this tuple, not user input, so an
        f-string is safe here (asyncpg placeholders can't parameterize
        identifiers).
        """
        assert self._pool is not None
        for table in ("analysis_queue", "gdrive_upload_queue"):
            count = await self._pool.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE status='processing'"
            )
            if count:
                await self._pool.execute(
                    f"UPDATE {table} SET status='pending', completed_at='', "
                    "error_message='' WHERE status='processing'"
                )
                _LOGGER.info(
                    "Reset %d stale processing item(s) to pending in %s", count, table
                )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
