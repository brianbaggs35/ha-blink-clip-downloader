"""Tests for restoring a backup: the merge, the preview, and the verification.

The property under test throughout is that a restore only ever *adds*: what is
already here is left exactly as it is, running it twice changes nothing, and a
failure leaves the library as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from blink_downloader.backup import (
    BackupArchive,
    RestoreHooks,
    create_backup,
    restore_backup,
)
from blink_downloader.backup.restore import DatabaseOutcome, TableOutcome
from blink_downloader.database import BACKUP_TABLES, ClipDatabase
from blink_downloader.database.backup import RestoreSession
from tests.backup_helpers import (
    add_analysis,
    add_clip,
    insert,
    make_archive,
    records,
    seed_library,
)
from tests.conftest import _ALL_TABLES

#: Tables whose ids the database numbers, so a restored row has a new one.
_NUMBERED = {
    "analysis_results",
    "detected_objects",
    "security_events",
    "analysis_feedback",
    "face_recognition_feedback",
    "face_enrollments",
    "battery_history",
}


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


async def _backup(db: ClipDatabase, folder: Path, **options: bool) -> BackupArchive:
    folder.mkdir(exist_ok=True)
    target = folder / "backup.zip"
    await create_backup(
        db, target, addon_version="6.1.3", clip_folder="/share/blink-clips", **options
    )
    return BackupArchive.open(target)


async def _wipe(db: ClipDatabase) -> None:
    assert db._pool is not None
    await db._pool.execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")


async def _rows(db: ClipDatabase, table: str) -> list[dict[str, Any]]:
    assert db._pool is not None
    spec = next(t for t in BACKUP_TABLES if t.name == table)
    return [
        dict(r)
        for r in await db._pool.fetch(f"SELECT * FROM {table} ORDER BY {spec.order_by}")
    ]


async def _counts(db: ClipDatabase) -> dict[str, int]:
    return await db.backup_row_counts()


async def _restore(
    db: ClipDatabase,
    archive: BackupArchive,
    *,
    replace: bool = False,
    hooks: RestoreHooks | None = None,
):
    return await restore_backup(
        db, archive, replace_settings=replace, hooks=hooks or RestoreHooks()
    )


def _table(outcome: DatabaseOutcome, name: str) -> TableOutcome:
    return next(t for t in outcome.tables if t.name == name)


def _content(rows: list[dict[str, Any]], table: str) -> list[dict[str, Any]]:
    """Rows without what a restore legitimately changes: the ids the database
    numbers, and the flag recording whether a clip's video is here."""
    drop = {"media_missing"} | ({"id"} if table in _NUMBERED else set())
    return [{k: v for k, v in row.items() if k not in drop} for row in rows]


# ---------------------------------------------------------------------------
# Into an empty library
# ---------------------------------------------------------------------------


async def test_everything_comes_back_into_an_empty_library(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    original = {t.name: await _rows(db, t.name) for t in BACKUP_TABLES}
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)

    result = await _restore(db, archive)

    assert result.to_json()["verified"] is True
    for table in BACKUP_TABLES:
        restored = await _rows(db, table.name)
        expected = _content(original[table.name], table.name)
        got = _content(restored, table.name)
        if table.name == "analysis_feedback":
            for row in expected + got:
                row.pop("analysis_result_id")
        assert got == expected, table.name


async def test_feedback_follows_its_analysis_to_the_id_it_has_now(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    """A restored analysis is numbered afresh, so the feedback that names it
    has to be pointed at the new number, not left naming whatever has the old."""
    seeded = await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    # Already-numbered rows push the restored ones off the ids they had.
    await add_clip(db, "other", media_dir)
    for _ in range(7):
        await add_analysis(db, "other")

    await _restore(db, archive)

    (feedback,) = await _rows(db, "analysis_feedback")
    (named,) = [
        row
        for row in await _rows(db, "analysis_results")
        if row["id"] == feedback["analysis_result_id"]
    ]
    assert (named["clip_id"], named["model"]) == ("c1", "llava")
    assert feedback["analysis_result_id"] != seeded["first_analysis"]["id"]


async def test_feedback_about_an_analysis_the_backup_lacks_names_none(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await add_clip(db, "c1", media_dir)
    await insert(
        db,
        "analysis_feedback",
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=424242,
        original_suspicious=True,
        original_confidence=0.5,
        correct=True,
        created_at="2026-10-01T10:00:00+00:00",
    )
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)

    await _restore(db, archive)

    (feedback,) = await _rows(db, "analysis_feedback")
    assert feedback["analysis_result_id"] is None


async def test_restored_enrolled_faces_keep_their_photos_and_approval(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    await insert(
        db,
        "face_enrollments",
        name="Nanny",
        embedding="[0.9]",
        created_at="2026-09-02T00:00:00+00:00",
        approved=False,
    )
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)

    await _restore(db, archive)

    people = {r["name"]: r for r in await _rows(db, "face_enrollments")}
    assert people["Riley"]["thumbnail"] == b"\xff\xd8\xffthumb"
    assert people["Riley"]["approved"] is True
    assert people["Nanny"]["approved"] is False
    assert people["Nanny"]["thumbnail"] is None


async def test_a_backup_made_without_faces_restores_without_them(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b", include_faces=False)
    await _wipe(db)
    await _restore(db, archive)
    counts = await _counts(db)
    assert counts["face_enrollments"] == 0
    assert counts["clips"] == 3


# ---------------------------------------------------------------------------
# Nothing is ever doubled
# ---------------------------------------------------------------------------


async def test_restoring_twice_adds_nothing_the_second_time(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)

    await _restore(db, archive)
    once = await _counts(db)
    second = await _restore(db, archive)

    assert await _counts(db) == once
    assert all(t.added == 0 for t in second.database.tables)
    assert second.to_json()["verified"] is True
    assert second.checks[1].label == "No new clips needed adding"


async def test_restoring_into_the_library_it_came_from_changes_nothing(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    before = await _counts(db)
    archive = await _backup(db, tmp_path / "b")

    result = await _restore(db, archive)

    assert await _counts(db) == before
    assert result.database.clips.already_here == 3
    assert result.database.added_clip_ids == set()


async def test_a_clip_that_is_already_here_is_left_exactly_as_it_is(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    # The same clip, downloaded again by the add-on since the backup, and
    # analysed there differently.
    await add_clip(db, "c1", media_dir, camera="Seen by the new install")
    await add_analysis(db, "c1", summary="New install's own view")

    result = await _restore(db, archive)

    (clip,) = [c for c in await _rows(db, "clips") if c["id"] == "c1"]
    assert clip["camera"] == "Seen by the new install"
    assert clip["starred"] is False
    analyses = [a for a in await _rows(db, "analysis_results") if a["clip_id"] == "c1"]
    assert [a["summary"] for a in analyses] == ["New install's own view"]
    assert _table(result.database, "clips").added == 2
    assert result.database.clips.already_here == 1
    # c1's two analyses, three detections and event stay in the backup: they
    # are about a clip this restore did not add.
    assert _table(result.database, "analysis_results").skipped == 2
    assert _table(result.database, "detected_objects").skipped == 3
    assert _table(result.database, "security_events").skipped == 1


async def test_what_belongs_to_a_skipped_clip_is_not_added(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await add_clip(db, "c1", media_dir)

    await _restore(db, archive)

    counts = await _counts(db)
    assert counts["detected_objects"] == 0
    assert counts["security_events"] == 0
    assert counts["analysis_feedback"] == 0


@pytest.mark.parametrize(
    ("existing_id", "extra", "result"),
    [
        pytest.param(
            "blink-777", {}, "skipped", id="a real record already holds the file"
        ),
        pytest.param("import-abc", {}, "replaced", id="only a bare scan stand-in"),
        pytest.param(
            "import-abc", {"starred": True}, "skipped", id="a starred stand-in"
        ),
        pytest.param(
            "import-abc", {"tags": '["porch"]'}, "skipped", id="a tagged stand-in"
        ),
    ],
)
async def test_a_clip_whose_video_is_already_tracked_is_not_doubled(
    db: ClipDatabase,
    tmp_path: Path,
    media_dir: Path,
    existing_id: str,
    extra: dict[str, Any],
    result: str,
) -> None:
    """After an add-on reinstall the library scan finds the videos still in
    /share and records each under an id made from its path. The real record
    must replace such a stand-in, but never double up on a real one."""
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await add_clip(
        db, existing_id, media_dir, file_path=str(media_dir / "c1.mp4"), **extra
    )

    report = await _restore(db, archive)

    ids = {c["id"] for c in await _rows(db, "clips")}
    if result == "replaced":
        assert "c1" in ids
        assert existing_id not in ids
        assert report.database.clips.standins_replaced == 1
    else:
        assert "c1" not in ids
        assert existing_id in ids
        assert report.database.clips.same_file == 1
    paths = [c["file_path"] for c in await _rows(db, "clips")]
    assert paths.count(str(media_dir / "c1.mp4")) == 1


async def test_a_stand_in_that_has_been_analyzed_is_kept_and_the_backup_clip_skipped(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await add_clip(db, "import-abc", media_dir, file_path=str(media_dir / "c1.mp4"))
    await add_analysis(db, "import-abc", summary="The new install's own analysis")

    await _restore(db, archive)

    ids = {c["id"] for c in await _rows(db, "clips")}
    assert "import-abc" in ids
    assert "c1" not in ids
    assert [
        a["summary"]
        for a in await _rows(db, "analysis_results")
        if a["clip_id"] == "import-abc"
    ] == ["The new install's own analysis"]


def _clip_row(clip_id: str, path: str = "") -> dict[str, Any]:
    return {
        "id": clip_id,
        "camera": "Porch",
        "file_path": path or f"/share/blink-clips/{clip_id}.mp4",
        "timestamp": "2026-10-01T09:00:00+00:00",
        "downloaded_at": "2026-10-01T09:01:00+00:00",
    }


async def test_a_clip_listed_twice_in_one_backup_is_added_once(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """More than a batch apart, so the second sighting is in a later batch."""
    rows = [_clip_row(f"c{i}") for i in range(1001)] + [_clip_row("c0")]
    archive = BackupArchive.open(
        make_archive(tmp_path / "b.zip", tables={"clips": records(*rows)})
    )

    result = await _restore(db, archive)

    assert (await _counts(db))["clips"] == 1001
    assert result.database.clips.already_here == 1
    assert _table(result.database, "clips").skipped == 1


async def test_two_clips_in_a_backup_do_not_both_take_one_video(
    db: ClipDatabase, tmp_path: Path
) -> None:
    archive = BackupArchive.open(
        make_archive(
            tmp_path / "b.zip",
            tables={
                "clips": records(
                    _clip_row("a", "/share/one.mp4"),
                    _clip_row("b", "/share/one.mp4"),
                    _clip_row("c", "/share/two.mp4"),
                )
            },
        )
    )
    result = await _restore(db, archive)
    assert {c["id"] for c in await _rows(db, "clips")} == {"a", "c"}
    assert result.database.clips.same_file == 1


async def test_clips_with_no_recorded_file_are_not_taken_for_one_file(
    db: ClipDatabase, tmp_path: Path
) -> None:
    """An empty path means "no file recorded", which every such clip shares."""
    rows = [{**_clip_row(i), "file_path": ""} for i in ("a", "b")]
    archive = BackupArchive.open(
        make_archive(tmp_path / "b.zip", tables={"clips": records(*rows)})
    )
    result = await _restore(db, archive)
    assert (await _counts(db))["clips"] == 2
    assert result.database.clips.same_file == 0


async def test_a_clip_that_turns_up_while_restoring_is_left_alone(
    db: ClipDatabase,
    tmp_path: Path,
    media_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The downloader can fetch the same clip again between the restore asking
    whether it is here and writing it; it must not be written over, and its
    backup analysis must not be added to it."""
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    # Under another file name, so the only thing that can stop it is the insert.
    await add_clip(
        db,
        "c1",
        media_dir,
        camera="Written by the add-on",
        file_path=str(media_dir / "elsewhere.mp4"),
    )

    monkeypatch.setattr(
        RestoreSession, "existing_clip_ids", AsyncMock(return_value=set())
    )
    result = await _restore(db, archive)
    monkeypatch.undo()

    (clip,) = [c for c in await _rows(db, "clips") if c["id"] == "c1"]
    assert clip["camera"] == "Written by the add-on"
    assert result.database.clips.already_here == 1
    assert "c1" not in result.database.added_clip_ids
    assert _table(result.database, "analysis_results").added == 1  # only c2's


async def test_things_that_stand_alone_are_added_only_where_missing(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    assert db._pool is not None
    # Lose one baseline, and change the other since the backup was made.
    await db._pool.execute("DELETE FROM camera_baselines WHERE hour = 3")
    await db._pool.execute("UPDATE camera_baselines SET count = 99 WHERE hour = 14")
    await db._pool.execute("DELETE FROM face_enrollments")

    result = await _restore(db, archive)

    baselines = {r["hour"]: r["count"] for r in await _rows(db, "camera_baselines")}
    assert baselines == {3: 7, 14: 99}
    assert len(await _rows(db, "face_enrollments")) == 1
    assert _table(result.database, "camera_baselines").added == 1
    assert _table(result.database, "camera_baselines").skipped == 1
    assert _table(result.database, "camera_vehicle_signatures").added == 0


async def test_a_face_already_enrolled_is_not_enrolled_again(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _restore(db, archive)
    await _restore(db, archive)
    assert (await _counts(db))["face_enrollments"] == 1


async def test_a_child_row_for_a_clip_the_backup_lacks_is_set_aside(
    db: ClipDatabase, tmp_path: Path
) -> None:
    archive = BackupArchive.open(
        make_archive(
            tmp_path / "b.zip",
            tables={
                "clips": records(_clip_row("a")),
                "security_events": records(
                    {
                        "clip_id": "ghost",
                        "camera": "Porch",
                        "event_type": "loitering",
                        "severity": "suspicious",
                        "created_at": "t",
                    }
                ),
            },
        )
    )
    result = await _restore(db, archive)
    assert (await _counts(db))["security_events"] == 0
    assert _table(result.database, "security_events").skipped == 1
    assert result.to_json()["verified"] is True


# ---------------------------------------------------------------------------
# Whether a restored clip's video is here
# ---------------------------------------------------------------------------


async def test_a_restored_clip_is_flagged_when_its_video_is_not_on_this_install(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    """The archiver deletes the record of any clip whose file is gone; the flag
    is what stops it erasing a restored record and its analysis."""
    (media_dir / "archives").mkdir()
    zipped = media_dir / "archives" / "blink_archive_2026-10.zip"
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    (media_dir / "c1.mp4").write_bytes(b"video")
    zipped.write_bytes(b"zip")

    result = await _restore(db, archive)

    flags = {c["id"]: c["media_missing"] for c in await _rows(db, "clips")}
    assert flags == {"c1": False, "c2": True, "c3": False}
    assert result.database.clips.with_video == 2
    assert result.database.clips.without_video == 1


async def test_a_restored_archived_clip_without_its_zip_is_flagged(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await _restore(db, archive)
    flags = {c["id"]: c["media_missing"] for c in await _rows(db, "clips")}
    assert flags["c3"] is True


async def test_a_flagged_clip_survives_the_archive_runs_that_prune_missing_videos(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    """End to end against the real archiver, which deletes any clip whose file
    is gone and whose archive is missing."""
    from blink_downloader.archiver import ClipArchiver

    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await _restore(db, archive)

    archiver = ClipArchiver(
        db, tmp_path / "archives", archive_after_days=1, enabled=True
    )
    await archiver.run()
    await archiver.prune_orphaned_archives()

    assert {c["id"] for c in await _rows(db, "clips")} == {"c1", "c2", "c3"}
    assert (await _counts(db))["analysis_results"] == 3


async def test_without_the_flag_the_archiver_really_would_erase_them(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    """The control for the test above: the hazard the flag exists for is real."""
    from blink_downloader.archiver import ClipArchiver

    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await _restore(db, archive)
    assert db._pool is not None
    await db._pool.execute("UPDATE clips SET media_missing = FALSE")

    archiver = ClipArchiver(
        db, tmp_path / "archives", archive_after_days=1, enabled=True
    )
    await archiver.run()
    await archiver.prune_orphaned_archives()

    assert await _rows(db, "clips") == []
    assert (await _counts(db))["analysis_results"] == 0


# ---------------------------------------------------------------------------
# The preview is the same merge, run without writing
# ---------------------------------------------------------------------------


async def test_the_preview_promises_exactly_what_the_restore_then_does(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    from blink_downloader.backup import preview_restore

    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await add_clip(db, "c2", media_dir)  # one already here
    (media_dir / "c1.mp4").write_bytes(b"video")

    preview = await preview_restore(db, archive)
    result = await _restore(db, archive)

    assert [t.to_json() for t in preview.tables] == [
        t.to_json() for t in result.database.tables
    ]
    assert preview.clips == result.database.clips
    assert preview.added_clip_ids == result.database.added_clip_ids == {"c1", "c3"}


async def test_the_preview_changes_nothing(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    from blink_downloader.backup import preview_restore

    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await add_clip(db, "import-abc", media_dir, file_path=str(media_dir / "c1.mp4"))
    before = await _counts(db)

    preview = await preview_restore(db, archive)

    assert await _counts(db) == before
    assert preview.clips.standins_replaced == 1
    assert (await db.get_clip("import-abc")) is not None


# ---------------------------------------------------------------------------
# When it goes wrong
# ---------------------------------------------------------------------------


async def test_a_restore_that_fails_partway_leaves_the_library_as_it_was(
    db: ClipDatabase,
    tmp_path: Path,
    media_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    await add_clip(db, "existing", media_dir)
    before = await _counts(db)
    real = RestoreSession.insert_rows

    async def fail_on_detections(
        self: RestoreSession, table: str, rows: list[dict[str, Any]]
    ) -> None:
        if table == "detected_objects":
            raise RuntimeError("disk gone")
        await real(self, table, rows)

    monkeypatch.setattr(RestoreSession, "insert_rows", fail_on_detections)
    with pytest.raises(RuntimeError, match="disk gone"):
        await _restore(db, archive)

    # The clips and analyses written before the failure were rolled back.
    assert await _counts(db) == before


@pytest.mark.parametrize(
    ("table", "row"),
    [
        pytest.param(
            "clips", {"id": "a", "camera": "Porch"}, id="clip with no file path"
        ),
        pytest.param(
            "analysis_results", {"camera": "Porch"}, id="analysis with no clip"
        ),
        pytest.param(
            "camera_baselines", {"camera": "Porch"}, id="baseline with no hour"
        ),
    ],
)
async def test_a_record_this_version_cannot_use_stops_the_restore_cleanly(
    db: ClipDatabase, tmp_path: Path, table: str, row: dict[str, Any]
) -> None:
    tables = {"clips": records(_clip_row("good")), table: records(row)}
    archive = BackupArchive.open(make_archive(tmp_path / "b.zip", tables=tables))
    before = await _counts(db)

    with pytest.raises(Exception, match="cannot use") as raised:
        await _restore(db, archive)

    assert raised.type.__name__ == "BackupError"
    assert await _counts(db) == before


async def test_a_column_this_release_lacks_is_ignored_not_fatal(
    db: ClipDatabase, tmp_path: Path
) -> None:
    row = {**_clip_row("a"), "added_by_a_future_release": "x"}
    archive = BackupArchive.open(
        make_archive(tmp_path / "b.zip", tables={"clips": records(row)})
    )
    await _restore(db, archive)
    assert (await db.get_clip("a")) is not None


async def test_a_backup_from_an_older_release_gets_defaults_for_what_it_lacks(
    db: ClipDatabase, tmp_path: Path
) -> None:
    archive = BackupArchive.open(
        make_archive(tmp_path / "b.zip", tables={"clips": records(_clip_row("old"))})
    )
    await _restore(db, archive)
    clip = await db.get_clip("old")
    assert clip is not None
    assert clip["starred"] is False
    assert clip["tags"] == []


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _files_archive(tmp_path: Path, files: dict[str, bytes]) -> BackupArchive:
    return BackupArchive.open(make_archive(tmp_path / "f.zip", files=files))


async def test_a_settings_file_is_written_where_there_is_none(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    archive = _files_archive(tmp_path, {"camera_configs.json": b'[{"camera": "A"}]'})
    result = await _restore(db, archive)
    assert (
        catalogue.DATA_DIR / "camera_configs.json"
    ).read_bytes() == b'[{"camera": "A"}]'
    assert [(f.status, f.detail) for f in result.files] == [("restored", "Restored.")]
    assert result.restart_needed is True


async def test_a_settings_file_that_is_there_is_kept_unless_replacing_is_asked_for(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    target = catalogue.DATA_DIR / "camera_configs.json"
    target.write_bytes(b"mine")
    archive = _files_archive(tmp_path, {"camera_configs.json": b"theirs"})

    kept = await _restore(db, archive)
    assert target.read_bytes() == b"mine"
    assert kept.files[0].status == "kept"
    assert kept.restart_needed is False

    replaced = await _restore(db, archive, replace=True)
    assert target.read_bytes() == b"theirs"
    assert replaced.files[0].status == "replaced"
    assert replaced.restart_needed is True


async def test_pictures_are_restored_one_by_one_and_reported_together(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    folder = catalogue.DATA_DIR / "asset_snapshots"
    folder.mkdir()
    (folder / "have-0123456789.jpg").write_bytes(b"mine")
    archive = _files_archive(
        tmp_path,
        {
            "asset_snapshots/have-0123456789.jpg": b"theirs",
            "asset_snapshots/new-0123456789.jpg": b"new",
        },
    )

    result = await _restore(db, archive)

    assert (folder / "have-0123456789.jpg").read_bytes() == b"mine"
    assert (folder / "new-0123456789.jpg").read_bytes() == b"new"
    assert [(f.status, f.detail) for f in result.files] == [
        ("restored", "1 restored, 1 already here")
    ]
    # Pictures are read when asked for; only settings need a restart.
    assert result.restart_needed is False


async def test_pictures_that_are_all_already_here_are_reported_as_kept(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    folder = catalogue.DATA_DIR / "vehicle_zone_snapshots"
    folder.mkdir()
    (folder / "driveway-0123456789.jpg").write_bytes(b"mine")
    archive = _files_archive(
        tmp_path, {"vehicle_zone_snapshots/driveway-0123456789.jpg": b"theirs"}
    )
    result = await _restore(db, archive)
    assert result.files[0].status == "kept"


async def test_the_clip_manifest_gains_only_the_lines_it_lacks(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    target = catalogue.DATA_DIR / "clip_manifest.json"
    target.write_bytes(b'{"id": "c1"}\n{"id": "c3"}\n')
    archive = _files_archive(
        tmp_path, {"clip_manifest.json": b'{"id": "c1"}\n{"id": "c2"}\n\n'}
    )

    first = await _restore(db, archive)
    second = await _restore(db, archive)

    assert target.read_bytes() == b'{"id": "c1"}\n{"id": "c3"}\n{"id": "c2"}\n'
    assert first.files[0].detail == "1 added"
    assert second.files[0].detail == "0 added"


async def test_the_clip_manifest_is_created_when_there_is_none(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    archive = _files_archive(tmp_path, {"clip_manifest.json": b'{"id": "c1"}\n'})
    await _restore(db, archive)
    assert (catalogue.DATA_DIR / "clip_manifest.json").read_bytes() == b'{"id": "c1"}\n'


async def test_a_clip_manifest_that_ends_mid_line_is_not_run_into_the_next(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    target = catalogue.DATA_DIR / "clip_manifest.json"
    target.write_bytes(b'{"id": "c1"}')
    archive = _files_archive(tmp_path, {"clip_manifest.json": b'{"id": "c2"}\n'})
    await _restore(db, archive)
    assert target.read_bytes() == b'{"id": "c1"}\n{"id": "c2"}\n'


async def test_the_download_history_goes_to_the_running_downloader(
    db: ClipDatabase, tmp_path: Path
) -> None:
    received: list[list[str]] = []

    def merge(ids: Any) -> int:
        received.append(list(ids))
        return 1

    archive = _files_archive(
        tmp_path, {"downloaded_clip_ids.json": b'{"downloaded_ids": ["c1", "c2"]}'}
    )
    result = await _restore(db, archive, hooks=RestoreHooks(merge_downloaded_ids=merge))
    assert received == [["c1", "c2"]]
    assert (result.files[0].status, result.files[0].detail) == ("merged", "1 added")
    assert result.restart_needed is False


async def test_the_download_history_is_not_written_behind_the_downloaders_back(
    db: ClipDatabase, tmp_path: Path
) -> None:
    from blink_downloader.backup import catalogue

    archive = _files_archive(
        tmp_path, {"downloaded_clip_ids.json": b'{"downloaded_ids": ["c1"]}'}
    )
    result = await _restore(db, archive)  # no downloader to give it to
    assert result.files[0].status == "skipped"
    assert not (catalogue.DATA_DIR / "downloaded_clips.json").exists()


@pytest.mark.parametrize(
    "contents",
    [
        b"{not json",
        b'["c1"]',
        b'{"downloaded_ids": "c1"}',
        b'{"downloaded_ids": [1, 2]}',
    ],
)
async def test_a_download_history_that_cannot_be_read_fails_on_its_own(
    db: ClipDatabase, tmp_path: Path, contents: bytes
) -> None:
    archive = _files_archive(
        tmp_path,
        {"downloaded_clip_ids.json": contents, "camera_configs.json": b"[]"},
    )
    result = await _restore(
        db, archive, hooks=RestoreHooks(merge_downloaded_ids=lambda _ids: 0)
    )
    statuses = {f.label: f.status for f in result.files}
    assert statuses["Download history (clips already fetched from Blink)"] == "failed"
    # The failure did not stop the file after it.
    assert statuses["Camera settings (descriptions, prompts, car zones)"] == "restored"
    assert result.to_json()["verified"] is False


async def test_a_downloader_that_cannot_save_is_reported_not_raised(
    db: ClipDatabase, tmp_path: Path
) -> None:
    def cannot_save(_ids: Any) -> int:
        raise PermissionError(13, "Permission denied")

    archive = _files_archive(
        tmp_path, {"downloaded_clip_ids.json": b'{"downloaded_ids": ["c1"]}'}
    )
    result = await _restore(
        db, archive, hooks=RestoreHooks(merge_downloaded_ids=cannot_save)
    )
    assert (result.files[0].status, result.files[0].detail) == (
        "failed",
        "Permission denied",
    )


async def test_one_file_failing_does_not_stop_the_others(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blink_downloader.backup import catalogue, restore

    real = restore._write_checked

    def fail_for_vehicles(target: Path, data: bytes, expected: Any) -> None:
        if target.name == "vehicle_settings.json":
            raise OSError(28, "No space left on device")
        real(target, data, expected)

    monkeypatch.setattr(restore, "_write_checked", fail_for_vehicles)
    archive = _files_archive(
        tmp_path, {"vehicle_settings.json": b"{}", "protected_assets.json": b"{}"}
    )

    result = await _restore(db, archive)

    by_label = {f.label: f for f in result.files}
    assert by_label["Protected vehicle description"].status == "failed"
    assert by_label["Protected vehicle description"].detail == "No space left on device"
    assert by_label["Protected assets"].status == "restored"
    assert (catalogue.DATA_DIR / "protected_assets.json").exists()
    assert not (catalogue.DATA_DIR / "vehicle_settings.json").exists()


async def test_a_failed_write_leaves_no_scratch_file_behind(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blink_downloader.backup import catalogue, restore

    def disk_full(*_args: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(restore.os, "replace", disk_full)
    result = await _restore(
        db, _files_archive(tmp_path, {"camera_configs.json": b"[]"})
    )
    assert result.files[0].status == "failed"
    assert list(catalogue.DATA_DIR.glob("*.restoring")) == []
    assert not (catalogue.DATA_DIR / "camera_configs.json").exists()


async def test_a_file_that_does_not_read_back_the_same_is_a_failure(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blink_downloader.backup import restore

    real = restore.os.replace

    def corrupting_replace(source: Path, target: Path) -> None:
        real(source, target)
        Path(target).write_bytes(b"bit rot")

    monkeypatch.setattr(restore.os, "replace", corrupting_replace)
    archive = _files_archive(tmp_path, {"camera_configs.json": b"[]"})
    result = await _restore(db, archive)
    assert result.files[0].status == "failed"
    assert "did not read back" in result.files[0].detail
    assert result.restart_needed is False


async def test_an_unexpected_error_in_one_file_is_logged_and_reported_plainly(
    db: ClipDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blink_downloader.backup import restore

    def explode(*_args: Any) -> None:
        raise ZeroDivisionError("internal detail that is nobody's business")

    monkeypatch.setattr(restore, "_restore_plain", explode)
    archive = _files_archive(tmp_path, {"camera_configs.json": b"[]"})
    result = await _restore(db, archive)
    assert result.files[0].status == "failed"
    assert "internal detail" not in result.files[0].detail
    assert "add-on logs" in result.files[0].detail


# ---------------------------------------------------------------------------
# What the summary says about files, before anything is written
# ---------------------------------------------------------------------------


def test_the_file_preview_says_what_would_happen_to_each(tmp_path: Path) -> None:
    from blink_downloader.backup import catalogue, preview_files

    (catalogue.DATA_DIR / "vehicle_settings.json").write_text("{}")
    folder = catalogue.DATA_DIR / "asset_snapshots"
    folder.mkdir()
    (folder / "have-0123456789.jpg").write_bytes(b"x")
    archive = _files_archive(
        tmp_path,
        {
            "camera_configs.json": b"[]",
            "vehicle_settings.json": b"{}",
            "clip_manifest.json": b"{}\n",
            "downloaded_clip_ids.json": b'{"downloaded_ids": []}',
            "asset_snapshots/have-0123456789.jpg": b"x",
            "asset_snapshots/new-0123456789.jpg": b"x",
            "vehicle_zone_snapshots/new-0123456789.jpg": b"x",
        },
    )

    described = {f.label: (f.status, f.detail) for f in preview_files(archive)}

    assert described["Camera settings (descriptions, prompts, car zones)"][0] == "new"
    assert described["Protected vehicle description"][0] == "exists"
    assert described["Clip manifest (download log)"][0] == "merge"
    assert (
        described["Download history (clips already fetched from Blink)"][0] == "merge"
    )
    assert described["Asset reference pictures"] == (
        "exists",
        "2 in the backup, 1 already here",
    )
    assert described["Vehicle zone reference pictures"] == (
        "new",
        "1 in the backup, 0 already here",
    )
    assert not (catalogue.DATA_DIR / "camera_configs.json").exists()


# ---------------------------------------------------------------------------
# The verification
# ---------------------------------------------------------------------------


def test_the_ledger_check_notices_a_record_that_went_missing() -> None:
    from blink_downloader.backup.restore import _ledger_check

    balanced = DatabaseOutcome(tables=[TableOutcome("clips", "Clips", 3, 3, 2, 1)])
    assert _ledger_check(balanced).ok is True

    unread = DatabaseOutcome(tables=[TableOutcome("clips", "Clips", 3, 2, 2, 0)])
    lost = DatabaseOutcome(tables=[TableOutcome("clips", "Clips", 3, 3, 1, 1)])
    for outcome in (unread, lost):
        check = _ledger_check(outcome)
        assert check.ok is False
        assert "Clips" in check.detail


async def test_the_readback_check_notices_a_clip_that_is_not_in_the_library(
    db: ClipDatabase,
) -> None:
    from blink_downloader.backup.restore import _readback_check

    await add_clip(db, "here", Path("/share"))
    outcome = DatabaseOutcome(added_clip_ids={"here", "gone"})
    check = await _readback_check(db, outcome)
    assert check.ok is False
    assert check.detail == "1 are missing"


async def test_the_readback_check_reads_naturally_for_a_single_clip(
    db: ClipDatabase,
) -> None:
    from blink_downloader.backup.restore import _readback_check

    await add_clip(db, "only", Path("/share"))
    check = await _readback_check(db, DatabaseOutcome(added_clip_ids={"only"}))
    assert (check.label, check.ok) == ("The added clip is in the library", True)


async def test_the_readback_check_reports_a_database_it_cannot_ask(
    db: ClipDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blink_downloader.backup.restore import _readback_check

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("database gone")

    monkeypatch.setattr(ClipDatabase, "restore_session", refuse)
    check = await _readback_check(db, DatabaseOutcome(added_clip_ids={"x"}))
    assert check.ok is False
    assert "Could not check" in check.detail


async def test_the_result_reads_as_json_the_page_can_use(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    await _wipe(db)
    document = (await _restore(db, archive)).to_json()
    assert set(document) == {
        "tables",
        "clips",
        "files",
        "checks",
        "verified",
        "restart_needed",
    }
    assert document["verified"] is True
    assert all(c["ok"] for c in document["checks"])
    clips = next(t for t in document["tables"] if t["name"] == "clips")
    assert (clips["rows"], clips["added"], clips["skipped"]) == (3, 3, 0)


async def test_the_summary_describes_the_backup_and_what_restoring_would_do(
    db: ClipDatabase, tmp_path: Path, media_dir: Path
) -> None:
    from blink_downloader.backup import preview_files, preview_restore, summarize

    await seed_library(db, media_dir)
    archive = await _backup(db, tmp_path / "b")
    summary = summarize(
        archive, await preview_restore(db, archive), preview_files(archive)
    )
    assert summary["addon_version"] == "6.1.3"
    assert summary["clip_folder"] == "/share/blink-clips"
    assert summary["verified_parts"] == len(archive.manifest.members())
    assert {t["name"] for t in summary["tables"]} == {t.name for t in BACKUP_TABLES}
    assert summary["clips"]["already_here"] == 3
