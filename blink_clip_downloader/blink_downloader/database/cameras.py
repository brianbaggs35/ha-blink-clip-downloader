"""Per-camera state that outlives any one clip.

Battery history and low-battery tracking, and the rename path. Renaming is
the awkward one: a camera's name is the key half a dozen tables use, so a
rename in the Blink app has to be carried across all of them at once or
the camera appears twice — once with its history and no new clips, once
with new clips and no history. Every ``_migrate_camera_*`` helper here
exists for one of those tables, and they merge rather than overwrite,
because both names may already have rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from .core import _DatabaseBase
from .sql import (
    _affected,
    _qm,
)

_LOGGER = logging.getLogger(__name__)


class CameraStateMixin(_DatabaseBase):
    """Battery history, baseline resets, and carrying a rename across tables."""

    async def add_battery_reading(
        self,
        camera: str,
        battery_state: str,
        battery_level: int | None,
        battery_voltage: int | None,
    ) -> bool:
        """Record a battery reading if it differs from this camera's last one.

        Dedupes on ``battery_state`` alone (not level/voltage — see the
        column comment on ``battery_history`` in ``_SCHEMA``). Returns
        ``True`` only when this is a genuine transition from a
        *previously recorded, different* state — the very first reading
        ever seen for a camera is still inserted (so the Status tab isn't
        empty), but returns ``False``, since there is nothing to have
        transitioned from. Callers use this return value to decide whether
        to fire a low-battery alert, so a fresh install or a newly
        discovered camera that already happens to be low must not alert on
        its first-ever reading.
        """
        if self._pool is None:
            return False
        last_state = await self._pool.fetchval(
            _qm(
                "SELECT battery_state FROM battery_history "
                "WHERE camera=? ORDER BY id DESC LIMIT 1"
            ),
            camera,
        )
        if last_state == battery_state:
            return False
        await self._pool.execute(
            _qm(
                """
                INSERT INTO battery_history
                  (camera, battery_state, battery_level, battery_voltage, recorded_at)
                VALUES (?, ?, ?, ?, ?)
                """
            ),
            camera,
            battery_state,
            battery_level,
            battery_voltage,
            datetime.now(UTC).isoformat(),
        )
        return last_state is not None

    async def reset_battery_history(self, camera: str) -> None:
        """Drop readings when a physical camera is replaced under the same name."""
        if self._pool is None:
            return
        await self._pool.execute(
            _qm("DELETE FROM battery_history WHERE LOWER(camera)=LOWER(?)"),
            camera,
        )

    async def reset_camera_baselines(self, camera: str) -> None:
        """Drop AI baseline state when a physical camera is replaced under the
        same name.

        A replacement's field of view, mount angle, and typical clip
        duration/motion frequency can all differ from the unit it replaced,
        so keeping the old baselines would compare the new hardware against
        a reference that no longer describes what it actually sees. Unlike
        ``rename_camera``'s baseline migration (same camera, new name --
        the data is still valid and worth carrying forward), a replacement
        means the *data itself* is stale, so it's dropped rather than kept.
        """
        if self._pool is None:
            return
        for table in (
            "camera_baselines",
            "camera_duration_stats",
            "camera_scene_baselines",
            # A replacement unit's field of view and mount angle differ, so
            # where the protected vehicle "normally sits" in frame is stale
            # by exactly the reasoning that drops the scene baseline above —
            # and a signature pointing at the wrong part of the new frame
            # would keep confirming its own mistake.
            "camera_vehicle_signatures",
        ):
            await self._pool.execute(
                _qm(f"DELETE FROM {table} WHERE LOWER(camera)=LOWER(?)"),
                camera,
            )

    async def get_battery_history(
        self, camera: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Most recent battery state-change rows for one camera, newest
        first — powers the Status tab's battery history modal."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            _qm(
                """
                SELECT camera, battery_state, battery_level, battery_voltage, recorded_at
                FROM battery_history
                WHERE LOWER(camera) = LOWER(?)
                ORDER BY id DESC
                LIMIT ?
                """
            ),
            camera,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_latest_battery_state(self) -> list[dict[str, Any]]:
        """Current battery state for every camera with a recorded reading —
        one row per camera — powers the Status tab's battery strip."""
        if self._pool is None:
            return []
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (camera)
                camera, battery_state, battery_level, battery_voltage, recorded_at
            FROM battery_history
            ORDER BY camera, id DESC
            """
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def _migrate_camera_baselines(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        rows = await conn.fetch(
            _qm(
                "SELECT hour, count FROM camera_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        await conn.execute(
            _qm(
                "DELETE FROM camera_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        counts: dict[int, int] = {}
        for row in rows:
            hour = int(row["hour"])
            counts[hour] = counts.get(hour, 0) + int(row["count"] or 0)
        for hour, count in counts.items():
            await conn.execute(
                _qm(
                    "INSERT INTO camera_baselines (camera, hour, count) "
                    "VALUES (?, ?, ?)"
                ),
                new_name,
                hour,
                count,
            )
        return True

    @staticmethod
    async def _migrate_camera_duration_stats(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        rows = await conn.fetch(
            _qm(
                "SELECT avg_duration, sample_count FROM camera_duration_stats "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        await conn.execute(
            _qm(
                "DELETE FROM camera_duration_stats "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        sample_count = sum(int(row["sample_count"] or 0) for row in rows)
        if sample_count:
            weighted_duration = sum(
                float(row["avg_duration"] or 0) * int(row["sample_count"] or 0)
                for row in rows
            )
            await conn.execute(
                _qm(
                    "INSERT INTO camera_duration_stats "
                    "(camera, avg_duration, sample_count) VALUES (?, ?, ?)"
                ),
                new_name,
                weighted_duration / sample_count,
                sample_count,
            )
        return True

    @staticmethod
    async def _migrate_camera_scene_baseline(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        rows = await conn.fetch(
            _qm(
                "SELECT thumbnail, sample_count, updated_at, "
                "consecutive_deviation_count FROM camera_scene_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?) "
                "ORDER BY sample_count DESC LIMIT 1"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        scene = rows[0]
        await conn.execute(
            _qm(
                "DELETE FROM camera_scene_baselines "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        await conn.execute(
            _qm(
                "INSERT INTO camera_scene_baselines "
                "(camera, thumbnail, sample_count, updated_at, "
                "consecutive_deviation_count) VALUES (?, ?, ?, ?, ?)"
            ),
            new_name,
            scene["thumbnail"],
            scene["sample_count"],
            scene["updated_at"],
            scene["consecutive_deviation_count"],
        )
        return True

    @staticmethod
    async def _migrate_camera_vehicle_signature(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        """Carry a learned protected-vehicle signature across a rename.

        Same camera, same car, same parking spot — only the name changed, so
        throwing the signature away would restart four clips' worth of
        learning for nothing. Keyed on camera like camera_scene_baselines,
        so the same delete-then-insert dance is needed to avoid a primary
        key collision when a row already exists under the new name.
        """
        rows = await conn.fetch(
            _qm(
                "SELECT box, histogram, sample_count, updated_at "
                "FROM camera_vehicle_signatures "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?) "
                "ORDER BY sample_count DESC LIMIT 1"
            ),
            old_name,
            new_name,
        )
        if not rows:
            return False
        signature = rows[0]
        await conn.execute(
            _qm(
                "DELETE FROM camera_vehicle_signatures "
                "WHERE LOWER(camera) = LOWER(?) OR LOWER(camera) = LOWER(?)"
            ),
            old_name,
            new_name,
        )
        await conn.execute(
            _qm(
                "INSERT INTO camera_vehicle_signatures "
                "(camera, box, histogram, sample_count, updated_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            new_name,
            signature["box"],
            signature["histogram"],
            signature["sample_count"],
            signature["updated_at"],
        )
        return True

    @staticmethod
    async def _rename_camera_rows(
        conn: asyncpg.Connection | PoolConnectionProxy, old_name: str, new_name: str
    ) -> bool:
        changed = False
        for table in (
            "clips",
            "analysis_results",
            "analysis_queue",
            "gdrive_upload_queue",
            "analysis_feedback",
            "face_recognition_feedback",
            "battery_history",
            "security_events",
        ):
            status = await conn.execute(
                _qm(f"UPDATE {table} SET camera=? WHERE LOWER(camera)=LOWER(?)"),
                new_name,
                old_name,
            )
            changed = changed or _affected(status) > 0
        return changed

    async def rename_camera(self, old_name: str, new_name: str) -> bool:
        """Migrate all persisted camera-keyed state after a Blink rename."""
        if self._pool is None or not old_name or not new_name:
            return False
        if old_name == new_name:
            return False

        async with self._pool.acquire() as conn, conn.transaction():
            changed = await self._migrate_camera_baselines(conn, old_name, new_name)
            changed = (
                await self._migrate_camera_duration_stats(conn, old_name, new_name)
            ) or changed
            changed = (
                await self._migrate_camera_scene_baseline(conn, old_name, new_name)
            ) or changed
            changed = (
                await self._migrate_camera_vehicle_signature(conn, old_name, new_name)
            ) or changed
            changed = (
                await self._rename_camera_rows(conn, old_name, new_name)
            ) or changed

        if changed:
            _LOGGER.info(
                "Migrated persisted state for camera %r -> %r", old_name, new_name
            )
        return changed
