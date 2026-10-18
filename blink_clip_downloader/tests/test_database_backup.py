"""Tests for the database half of Backup & Restore (database/backup.py)."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from blink_downloader.database import (
    BACKUP_TABLES,
    TRANSIENT_TABLES,
    ClipDatabase,
)
from blink_downloader.database.backup import backup_table

_PREFIX = "import-"


async def _insert(db: ClipDatabase, table: str, **values: Any) -> None:
    """One row straight into *table*; the tests need columns no public method sets."""
    assert db._pool is not None
    columns = ", ".join(values)
    placeholders = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    await db._pool.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", *values.values()
    )


async def _clip(db: ClipDatabase, clip_id: str, **extra: Any) -> None:
    await _insert(
        db,
        "clips",
        id=clip_id,
        camera=extra.pop("camera", "Front Door"),
        file_path=extra.pop("file_path", f"/share/blink-clips/{clip_id}.mp4"),
        timestamp="2026-01-01T09:00:00+00:00",
        downloaded_at="2026-01-01T09:01:00+00:00",
        **extra,
    )


async def _analysis(db: ClipDatabase, clip_id: str) -> None:
    await _insert(
        db,
        "analysis_results",
        clip_id=clip_id,
        camera="Front Door",
        model="llava",
        analyzed_at="2026-01-01T09:02:00+00:00",
    )


# ---------------------------------------------------------------------------
# The table catalogue
# ---------------------------------------------------------------------------


async def test_every_table_in_the_schema_is_classified(db: ClipDatabase) -> None:
    """A new table must be listed as carried, or named transient with a reason.

    Otherwise a backup would silently leave it out, and the first anyone
    would hear of it is a restore that comes back without it.
    """
    assert db._pool is not None
    rows = await db._pool.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    )
    in_schema = {row["table_name"] for row in rows}
    carried = {table.name for table in BACKUP_TABLES}
    assert in_schema == carried | set(TRANSIENT_TABLES)
    assert carried.isdisjoint(TRANSIENT_TABLES)


def test_every_transient_table_says_why_it_is_left_out() -> None:
    assert all(reason.strip() for reason in TRANSIENT_TABLES.values())


async def test_each_tables_ordering_and_key_columns_exist(db: ClipDatabase) -> None:
    assert db._pool is not None
    for table in BACKUP_TABLES:
        rows = await db._pool.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = $1",
            table.name,
        )
        columns = {row["column_name"] for row in rows}
        ordering = {part.strip() for part in table.order_by.split(",")}
        assert ordering <= columns, table.name
        assert set(table.key) <= columns, table.name


def test_table_roles_agree_with_what_a_restore_does_with_them() -> None:
    roles = {table.role for table in BACKUP_TABLES}
    assert roles == {"root", "clip_child", "independent"}
    assert [t.name for t in BACKUP_TABLES if t.role == "root"] == ["clips"]
    # Parents before children, so a child's clip is already restored.
    assert BACKUP_TABLES[0].name == "clips"
    # A child is matched through its clip, never by a key of its own; anything
    # that stands alone needs one, or a second restore would add it again.
    assert all(not t.key for t in BACKUP_TABLES if t.role == "clip_child")
    assert all(t.key for t in BACKUP_TABLES if t.role == "independent")
    # The verdict feedback points back at an analysis, so that must come first.
    names = [t.name for t in BACKUP_TABLES]
    assert names.index("analysis_results") < names.index("analysis_feedback")


def test_backup_table_rejects_a_name_it_does_not_carry() -> None:
    assert backup_table("clips").label == "Clips"
    with pytest.raises(KeyError):
        backup_table("analysis_queue")


async def test_row_counts_cover_every_carried_table(db: ClipDatabase) -> None:
    await _clip(db, "a")
    await _clip(db, "b")
    await _analysis(db, "a")
    counts = await db.backup_row_counts()
    assert set(counts) == {table.name for table in BACKUP_TABLES}
    assert counts["clips"] == 2
    assert counts["analysis_results"] == 1
    assert counts["face_enrollments"] == 0


# ---------------------------------------------------------------------------
# Reading the library out
# ---------------------------------------------------------------------------


async def test_snapshot_yields_batches_in_the_tables_backup_order(
    db: ClipDatabase,
) -> None:
    for clip_id in ("c", "a", "b"):
        await _clip(db, clip_id)
    async with db.backup_snapshot() as snapshot:
        batches = [batch async for batch in snapshot.batches("clips", size=2)]
    assert [len(batch) for batch in batches] == [2, 1]
    assert [row["id"] for batch in batches for row in batch] == ["a", "b", "c"]


async def test_snapshot_of_an_empty_table_yields_nothing(db: ClipDatabase) -> None:
    async with db.backup_snapshot() as snapshot:
        assert [b async for b in snapshot.batches("security_events")] == []


async def test_snapshot_keeps_binary_columns_as_bytes(db: ClipDatabase) -> None:
    await db.add_face_enrollment("Riley", [0.1, 0.2], thumbnail=b"\xff\xd8jpeg")
    async with db.backup_snapshot() as snapshot:
        (batch,) = [b async for b in snapshot.batches("face_enrollments")]
    assert batch[0]["thumbnail"] == b"\xff\xd8jpeg"
    assert batch[0]["name"] == "Riley"


async def test_snapshot_is_one_instant_across_the_whole_export(
    db: ClipDatabase,
) -> None:
    """The add-on keeps writing while a backup is read; a clip arriving
    halfway must not appear in tables read after it."""
    await _clip(db, "early-1")
    await _clip(db, "early-2")
    async with db.backup_snapshot() as snapshot:
        first = snapshot.batches("clips", size=1)
        assert (await anext(first))[0]["id"] == "early-1"
        await _clip(db, "late")
        await _analysis(db, "late")
        rest = [row["id"] async for batch in first for row in batch]
        assert rest == ["early-2"]
        analyses = [
            r async for batch in snapshot.batches("analysis_results") for r in batch
        ]
        assert analyses == []
    assert (await db.backup_row_counts())["clips"] == 3


async def test_snapshot_refuses_a_table_a_backup_does_not_carry(
    db: ClipDatabase,
) -> None:
    async with db.backup_snapshot() as snapshot:
        batches = snapshot.batches("analysis_queue")
        with pytest.raises(KeyError):
            await anext(batches)


# ---------------------------------------------------------------------------
# Merging one in: the questions
# ---------------------------------------------------------------------------


async def test_existing_clip_ids_reports_only_the_ones_that_are_here(
    db: ClipDatabase,
) -> None:
    await _clip(db, "here")
    async with db.restore_session(write=False) as session:
        assert await session.existing_clip_ids(["here", "gone"]) == {"here"}
        assert await session.existing_clip_ids([]) == set()


async def test_clips_at_paths_is_empty_for_a_path_nobody_holds(
    db: ClipDatabase,
) -> None:
    async with db.restore_session(write=False) as session:
        assert await session.clips_at_paths(["/nowhere.mp4"], _PREFIX) == {}


async def test_a_bare_stand_in_from_the_library_scan_is_replaceable(
    db: ClipDatabase,
) -> None:
    await _clip(db, "import-abc", file_path="/share/a.mp4")
    async with db.restore_session(write=False) as session:
        found = await session.clips_at_paths(["/share/a.mp4"], _PREFIX)
    assert found["/share/a.mp4"].ids == ["import-abc"]
    assert found["/share/a.mp4"].replaceable is True


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"starred": True}, id="starred"),
        pytest.param({"tags": '["porch"]'}, id="tagged"),
    ],
)
async def test_a_stand_in_the_user_has_marked_up_is_not_replaceable(
    db: ClipDatabase, extra: dict[str, Any]
) -> None:
    await _clip(db, "import-abc", file_path="/share/a.mp4", **extra)
    async with db.restore_session(write=False) as session:
        found = await session.clips_at_paths(["/share/a.mp4"], _PREFIX)
    assert found["/share/a.mp4"].replaceable is False


async def test_a_stand_in_that_has_been_analyzed_is_not_replaceable(
    db: ClipDatabase,
) -> None:
    await _clip(db, "import-abc", file_path="/share/a.mp4")
    await _analysis(db, "import-abc")
    async with db.restore_session(write=False) as session:
        found = await session.clips_at_paths(["/share/a.mp4"], _PREFIX)
    assert found["/share/a.mp4"].replaceable is False


async def test_a_real_clip_is_never_replaceable_even_with_nothing_on_it(
    db: ClipDatabase,
) -> None:
    await _clip(db, "blink-9001", file_path="/share/a.mp4")
    async with db.restore_session(write=False) as session:
        found = await session.clips_at_paths(["/share/a.mp4"], _PREFIX)
    assert found["/share/a.mp4"].ids == ["blink-9001"]
    assert found["/share/a.mp4"].replaceable is False


async def test_two_records_on_one_path_are_replaceable_only_if_both_are(
    db: ClipDatabase,
) -> None:
    await _clip(db, "import-1", file_path="/share/a.mp4")
    await _clip(db, "blink-2", file_path="/share/a.mp4")
    async with db.restore_session(write=False) as session:
        found = await session.clips_at_paths(["/share/a.mp4"], _PREFIX)
    assert sorted(found["/share/a.mp4"].ids) == ["blink-2", "import-1"]
    assert found["/share/a.mp4"].replaceable is False


async def test_existing_keys_uses_each_tables_natural_key(db: ClipDatabase) -> None:
    await _insert(db, "camera_baselines", camera="Driveway", hour=3, count=5)
    await db.add_face_enrollment("Riley", [0.5])
    async with db.restore_session(write=False) as session:
        assert await session.existing_keys("camera_baselines") == {("Driveway", 3)}
        ((name, embedding),) = await session.existing_keys("face_enrollments")
        assert name == "Riley"
        assert embedding == "[0.5]"
        assert await session.existing_keys("battery_history") == set()


# ---------------------------------------------------------------------------
# Merging one in: the writes
# ---------------------------------------------------------------------------


async def test_delete_clips_takes_everything_hanging_off_them(
    db: ClipDatabase,
) -> None:
    await _clip(db, "gone")
    await _clip(db, "kept")
    await _analysis(db, "gone")
    async with db.restore_session() as session:
        await session.delete_clips(["gone"])
    counts = await db.backup_row_counts()
    assert (counts["clips"], counts["analysis_results"]) == (1, 0)


async def test_insert_rows_lets_the_database_number_identity_columns(
    db: ClipDatabase,
) -> None:
    rows = [
        {"id": 999, "camera": "Porch", "battery_state": "ok", "recorded_at": "t1"},
        {"id": 998, "camera": "Porch", "battery_state": "low", "recorded_at": "t2"},
    ]
    async with db.restore_session() as session:
        await session.insert_rows("battery_history", rows)
    assert db._pool is not None
    stored = await db._pool.fetch(
        "SELECT id, battery_state FROM battery_history ORDER BY id"
    )
    assert [(r["id"], r["battery_state"]) for r in stored] == [(1, "ok"), (2, "low")]


async def test_insert_rows_ignores_columns_this_release_does_not_have(
    db: ClipDatabase,
) -> None:
    """A backup from a newer release may carry a column this one lacks."""
    row = {
        "id": "x",
        "camera": "Porch",
        "file_path": "/share/x.mp4",
        "timestamp": "t",
        "downloaded_at": "t",
        "added_in_some_future_release": 1,
    }
    async with db.restore_session() as session:
        await session.insert_rows("clips", [row])
    assert (await db.get_clip("x")) is not None


async def test_insert_rows_defaults_columns_an_older_backup_lacks(
    db: ClipDatabase,
) -> None:
    """A backup from an older release lacks the newest columns."""
    row = {
        "id": "x",
        "camera": "Porch",
        "file_path": "/share/x.mp4",
        "timestamp": "t",
        "downloaded_at": "t",
    }
    async with db.restore_session() as session:
        await session.insert_rows("clips", [row])
    clip = await db.get_clip("x")
    assert clip is not None
    assert clip["starred"] is False
    assert clip["media_missing"] is False


async def test_insert_rows_keeps_binary_values_intact(db: ClipDatabase) -> None:
    row = {
        "name": "Riley",
        "embedding": "[0.1]",
        "created_at": "t",
        "approved": False,
        "thumbnail": b"\x00\x01\x02",
    }
    async with db.restore_session() as session:
        await session.insert_rows("face_enrollments", [row])
    (enrollment,) = await db.list_face_enrollments()
    assert enrollment["name"] == "Riley"
    assert enrollment["approved"] is False
    assert enrollment["has_thumbnail"] is True


async def test_one_session_takes_a_table_in_as_many_batches_as_it_likes(
    db: ClipDatabase,
) -> None:
    """A restore sends each table batch by batch through one session."""
    async with db.restore_session() as session:
        for batch in (("a", "b"), ("c", "d")):
            await session.insert_rows("clips", [_clip_row(i) for i in batch])
    assert (await db.backup_row_counts())["clips"] == 4


async def test_inserting_nothing_is_a_no_op(db: ClipDatabase) -> None:
    async with db.restore_session() as session:
        await session.insert_rows("clips", [])
        assert await session.insert_rows_returning_ids("analysis_results", []) == []
        assert await session.insert_new_clips([]) == set()
    assert (await db.backup_row_counts())["clips"] == 0


async def test_insert_rows_returning_ids_gives_each_new_id_in_order(
    db: ClipDatabase,
) -> None:
    await _clip(db, "c1")
    rows = [
        {
            "id": 77,
            "clip_id": "c1",
            "camera": "Front Door",
            "model": model,
            "analyzed_at": "t",
        }
        for model in ("first", "second", "third")
    ]
    async with db.restore_session() as session:
        ids = await session.insert_rows_returning_ids("analysis_results", rows)
    assert len(ids) == 3
    assert ids == sorted(ids)
    assert 77 not in ids
    assert db._pool is not None
    models = await db._pool.fetch(
        "SELECT model FROM analysis_results WHERE id = ANY($1::int[]) ORDER BY id", ids
    )
    assert [m["model"] for m in models] == ["first", "second", "third"]


def _clip_row(clip_id: str) -> dict[str, Any]:
    return {
        "id": clip_id,
        "camera": "Porch",
        "file_path": f"/share/{clip_id}.mp4",
        "timestamp": "t",
        "downloaded_at": "t",
    }


async def test_insert_new_clips_returns_exactly_the_ids_it_added(
    db: ClipDatabase,
) -> None:
    async with db.restore_session() as session:
        added = await session.insert_new_clips([_clip_row("a"), _clip_row("b")])
    assert added == {"a", "b"}


async def test_insert_new_clips_leaves_a_clip_that_appeared_meanwhile_alone(
    db: ClipDatabase,
) -> None:
    """The downloader can fetch the same clip again while a restore runs."""
    await _clip(db, "raced", camera="Written by the add-on")
    async with db.restore_session() as session:
        added = await session.insert_new_clips([_clip_row("raced"), _clip_row("fresh")])
    assert added == {"fresh"}
    clip = await db.get_clip("raced")
    assert clip is not None
    assert clip["camera"] == "Written by the add-on"


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------


async def test_a_read_only_session_refuses_every_write(db: ClipDatabase) -> None:
    """The summary asks its questions this way, so a write by mistake fails."""
    rows = [_clip_row("nope")]
    async with db.restore_session(write=False) as session:
        with pytest.raises(asyncpg.exceptions.ReadOnlySQLTransactionError):
            await session.insert_rows("clips", rows)


async def test_a_restore_that_fails_leaves_the_library_untouched(
    db: ClipDatabase,
) -> None:
    await _clip(db, "before")

    async def fail_halfway() -> None:
        async with db.restore_session() as session:
            await session.insert_rows("clips", [_clip_row("halfway-1")])
            await session.delete_clips(["before"])
            raise RuntimeError("failed halfway")

    with pytest.raises(RuntimeError, match="halfway"):
        await fail_halfway()
    counts = await db.backup_row_counts()
    assert counts["clips"] == 1
    assert (await db.get_clip("before")) is not None
    assert (await db.get_clip("halfway-1")) is None
