"""Reading the library out for a backup, and merging one back in.

``BACKUP_TABLES`` is the one list of what a backup carries from the database
and how a restore merges each table, kept beside the schema because a new
table has to be classified here — ``tests/test_database_backup.py`` fails
until it is either listed or named in ``TRANSIENT_TABLES`` with a reason.

Two transactions do the work, and both hand out a small object rather than a
connection so nothing outside this package writes SQL against these tables:

* :meth:`BackupMixin.backup_snapshot` — one read-only ``REPEATABLE READ``
  transaction, so every table in the backup is read as of the same instant
  while the add-on keeps writing to it.
* :meth:`BackupMixin.restore_session` — one transaction for the whole merge,
  so a restore that fails halfway leaves the library exactly as it was.

Everything that decides *what* to merge (a clip already here is left alone,
a stand-in the library scan made is replaced) is policy and lives in
:mod:`blink_downloader.backup.restore`; this module only answers questions
and performs the inserts it is told to.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, cast

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from .core import _DatabaseBase

#: What ``pool.acquire()`` hands out, which is not quite an ``asyncpg.Connection``.
_Conn = asyncpg.Connection | PoolConnectionProxy

TableRole = Literal["root", "clip_child", "independent"]

ROLE_ROOT: TableRole = "root"
ROLE_CHILD: TableRole = "clip_child"
ROLE_INDEPENDENT: TableRole = "independent"

CLIPS_TABLE = "clips"

_CAMERA = "camera"


@dataclass(frozen=True)
class BackupTable:
    """One table a backup carries, and how a restore merges it back in.

    The ``root`` is ``clips``. A ``clip_child`` row belongs to a clip and is only
    restored with a clip that was not already here. An ``independent`` row
    stands on its own and is skipped when a row with the same ``key`` exists.
    """

    name: str
    label: str
    role: TableRole
    #: ORDER BY for the export, so a backup of the same data is the same bytes.
    order_by: str
    #: Columns that identify an ``independent`` row; empty for the others.
    key: tuple[str, ...] = ()
    #: Holds face data. A backup can be made without it, because unlike the
    #: rest it is the one thing the Biometrics tab promises stays on this
    #: device, so whoever makes the backup chooses whether it travels.
    biometric: bool = False


#: The one table another table points back at: a verdict's feedback names
#: the analysis it was about, and a restored analysis gets a new id, so the
#: feedback's reference has to be rewritten to follow it.
ANALYSIS_TABLE = "analysis_results"
FEEDBACK_TABLE = "analysis_feedback"

#: In restore order: clips first, then the rows hanging off them, then the
#: rest. ``analysis_results`` precedes ``analysis_feedback``, which refers
#: back to it.
BACKUP_TABLES: tuple[BackupTable, ...] = (
    BackupTable(CLIPS_TABLE, "Clips", ROLE_ROOT, "id", ("id",)),
    BackupTable(ANALYSIS_TABLE, "AI analyses", ROLE_CHILD, "id"),
    BackupTable("detected_objects", "Object detections", ROLE_CHILD, "id"),
    BackupTable("security_events", "Security events", ROLE_CHILD, "id"),
    BackupTable(FEEDBACK_TABLE, "Verdict feedback", ROLE_CHILD, "id"),
    BackupTable("face_recognition_feedback", "Face-match reports", ROLE_CHILD, "id"),
    BackupTable(
        "face_enrollments",
        "Enrolled faces",
        ROLE_INDEPENDENT,
        "id",
        ("name", "embedding"),
        biometric=True,
    ),
    BackupTable(
        "camera_vehicle_signatures",
        "Learned vehicle positions",
        ROLE_INDEPENDENT,
        _CAMERA,
        (_CAMERA,),
    ),
    BackupTable(
        "camera_baselines",
        "Hourly activity baselines",
        ROLE_INDEPENDENT,
        "camera, hour",
        (_CAMERA, "hour"),
    ),
    BackupTable(
        "camera_duration_stats",
        "Clip-length statistics",
        ROLE_INDEPENDENT,
        _CAMERA,
        (_CAMERA,),
    ),
    BackupTable(
        "camera_scene_baselines",
        "Learned scene backgrounds",
        ROLE_INDEPENDENT,
        _CAMERA,
        (_CAMERA,),
    ),
    BackupTable(
        "battery_history",
        "Battery history",
        ROLE_INDEPENDENT,
        "id",
        (_CAMERA, "recorded_at"),
    ),
    BackupTable(
        "ai_usage_reset",
        "AI Usage reset point",
        ROLE_INDEPENDENT,
        "id",
        ("id",),
    ),
)

#: Tables a backup deliberately leaves out, and why. Both are work queues:
#: what is in them is about to happen, not something that happened, and a
#: restored row would send the add-on after a file that may not be here.
TRANSIENT_TABLES: dict[str, str] = {
    "analysis_queue": "Clips waiting for AI analysis; refilled as clips arrive.",
    "gdrive_upload_queue": "Uploads waiting for Google Drive; refilled by the backup policy.",
}

_TABLES_BY_NAME = {table.name: table for table in BACKUP_TABLES}


def backup_table(name: str) -> BackupTable:
    """The :class:`BackupTable` called *name*; ``KeyError`` for anything else.

    Every statement below is built from the result's ``name``, never from the
    argument, so a table name that reaches SQL is always one listed above.
    """
    return _TABLES_BY_NAME[name]


def _ident(name: str) -> str:
    """*name* as a quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


class ClipAtPath(NamedTuple):
    """The records already holding one file path."""

    ids: list[str]
    #: True when every one is a stand-in the library scan made for the file
    #: and carries nothing of its own: no analysis, no star, no tags.
    replaceable: bool


class BackupSnapshot:
    """The library as of one instant, read table by table."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def batches(
        self, table: str, size: int = 1000
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Every row of *table*, *size* at a time, in the table's backup order."""
        spec = backup_table(table)
        cursor = await self._conn.cursor(
            f"SELECT * FROM {_ident(spec.name)} ORDER BY {spec.order_by}"  # nosec B608
        )
        while rows := await cursor.fetch(size):
            yield [dict(row) for row in rows]


class RestoreSession:
    """Questions and inserts inside one restore transaction."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._insertable: dict[str, list[str]] = {}

    async def existing_clip_ids(self, ids: Sequence[str]) -> set[str]:
        """Which of *ids* are already clips here."""
        rows = await self._conn.fetch(
            "SELECT id FROM clips WHERE id = ANY($1::text[])", list(ids)
        )
        return {row["id"] for row in rows}

    async def clips_at_paths(
        self, paths: Sequence[str], placeholder_prefix: str
    ) -> dict[str, ClipAtPath]:
        """The records already holding each of *paths*.

        A record is ``replaceable`` only if its id starts with
        *placeholder_prefix* (the library scan's stand-ins) and it has no
        analysis, is not starred and has no tags — nothing a person or the
        AI added that replacing it would lose.
        """
        rows = await self._conn.fetch(
            """
            SELECT c.file_path,
                   array_agg(c.id) AS ids,
                   bool_and(
                       starts_with(c.id, $2)
                       AND NOT COALESCE(c.starred, FALSE)
                       AND COALESCE(c.tags, '') IN ('', '[]')
                       AND NOT EXISTS (
                           SELECT 1 FROM analysis_results a WHERE a.clip_id = c.id
                       )
                   ) AS replaceable
            FROM clips c
            WHERE c.file_path = ANY($1::text[])
            GROUP BY c.file_path
            """,
            list(paths),
            placeholder_prefix,
        )
        return {
            row["file_path"]: ClipAtPath(list(row["ids"]), row["replaceable"])
            for row in rows
        }

    async def delete_clips(self, ids: Sequence[str]) -> None:
        """Remove clips (and, by cascade, everything hanging off them)."""
        await self._conn.execute(
            "DELETE FROM clips WHERE id = ANY($1::text[])", list(ids)
        )

    async def existing_keys(self, table: str) -> set[tuple[Any, ...]]:
        """The ``key`` of every row already in an ``independent`` *table*."""
        spec = backup_table(table)
        columns = ", ".join(_ident(column) for column in spec.key)
        rows = await self._conn.fetch(
            f"SELECT {columns} FROM {_ident(spec.name)}"  # nosec B608
        )
        return {tuple(row.values()) for row in rows}

    async def insert_rows(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert *rows*, letting the database number any identity column."""
        if not rows:
            return
        query, args = await self._insert_statement(table, rows)
        await self._conn.executemany(query, args)

    async def insert_rows_returning_ids(
        self, table: str, rows: Sequence[Mapping[str, Any]]
    ) -> list[int]:
        """Like :meth:`insert_rows`, and the new ``id`` of each row, in order."""
        if not rows:
            return []
        query, args = await self._insert_statement(table, rows)
        ids: list[int] = []
        for values in args:
            # RETURNING always yields the new row's id, so this is never None.
            ids.append(
                cast("int", await self._conn.fetchval(f"{query} RETURNING id", *values))
            )
        return ids

    async def insert_new_clips(self, rows: Sequence[Mapping[str, Any]]) -> set[str]:
        """Insert clips, returning the ids that really were new.

        One statement per clip, because a clip with this id can appear while
        a restore runs (the downloader fetching the same clip again is the
        likely one) and must be left as the add-on wrote it rather than fail
        the whole restore or be written over. A clip skipped that way is not
        in the result, so nothing that belongs to it is restored either.
        """
        if not rows:
            return set()
        query, args = await self._insert_statement(CLIPS_TABLE, rows)
        inserted: set[str] = set()
        for values in args:
            new_id = await self._conn.fetchval(
                f"{query} ON CONFLICT (id) DO NOTHING RETURNING id", *values
            )
            if new_id is not None:
                inserted.add(new_id)
        return inserted

    async def _insert_statement(
        self, table: str, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[str, list[tuple[Any, ...]]]:
        """``INSERT`` text and one argument tuple per row.

        Only columns this database has *and* every row supplies are named:
        a backup from an older release lacks the newest columns, which then
        take their defaults, and one from a newer release may carry columns
        this one has dropped, which are ignored.
        """
        spec = backup_table(table)
        live = await self._insertable_columns(spec.name)
        columns = [column for column in live if all(column in row for row in rows)]
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        query = (
            f"INSERT INTO {_ident(spec.name)} "  # nosec B608
            f"({', '.join(_ident(c) for c in columns)}) VALUES ({placeholders})"
        )
        return query, [tuple(row[column] for column in columns) for row in rows]

    async def _insertable_columns(self, table: str) -> list[str]:
        """*table*'s columns a plain INSERT may name: every one except an
        identity column, which the database numbers itself."""
        if table not in self._insertable:
            rows = await self._conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = $1 "
                "AND is_identity = 'NO' ORDER BY ordinal_position",
                table,
            )
            self._insertable[table] = [row["column_name"] for row in rows]
        return self._insertable[table]


class BackupMixin(_DatabaseBase):
    """Backup export and restore import."""

    async def backup_row_counts(self) -> dict[str, int]:
        """How many rows each ``BACKUP_TABLES`` table holds right now."""
        assert self._pool is not None
        return {
            table.name: cast(
                "int",
                await self._pool.fetchval(
                    f"SELECT COUNT(*) FROM {_ident(table.name)}"  # nosec B608
                ),
            )
            for table in BACKUP_TABLES
        }

    @asynccontextmanager
    async def backup_snapshot(self) -> AsyncGenerator[BackupSnapshot]:
        """A read-only view of every table as of one instant."""
        assert self._pool is not None
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            yield BackupSnapshot(conn)

    @asynccontextmanager
    async def restore_session(
        self, *, write: bool = True
    ) -> AsyncGenerator[RestoreSession]:
        """One transaction for a whole restore: all of it, or none of it.

        ``write=False`` opens it read-only, which is how the summary asks
        its questions: any insert it made by mistake would be refused.
        """
        assert self._pool is not None
        async with (
            self._pool.acquire() as conn,
            conn.transaction(readonly=not write),
        ):
            yield RestoreSession(conn)
