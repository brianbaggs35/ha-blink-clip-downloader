"""Builders for backup archives, so a test can make a good one or a bad one."""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from blink_downloader.backup.archive import (
    MANIFEST_MEMBER,
    BackupManifest,
    MemberInfo,
    encode_row,
    file_member,
    table_member,
)

NOW = "2026-10-18T12:00:00+00:00"


def records(*rows: Mapping[str, Any]) -> bytes:
    """*rows* as the JSON lines a table's member holds."""
    return b"".join(encode_row(row) for row in rows)


def info(name: str, data: bytes, rows: int | None = None) -> MemberInfo:
    return MemberInfo(name, len(data), hashlib.sha256(data).hexdigest(), rows)


def make_archive(
    path: Path,
    *,
    tables: Mapping[str, bytes] | None = None,
    files: Mapping[str, bytes] | None = None,
    edit: Callable[[dict[str, Any]], None] | None = None,
    extra: Mapping[str, bytes] | None = None,
    omit: tuple[str, ...] = (),
) -> Path:
    """A backup ZIP with exactly these tables (raw record bytes) and files.

    Every checksum and count in its manifest is right unless ``edit`` changes
    the manifest, ``extra`` adds members the manifest does not list, or
    ``omit`` leaves out members it does.
    """
    tables = tables or {}
    files = files or {}
    manifest = BackupManifest(
        addon_version="6.1.4",
        created_at=NOW,
        clip_folder="/share/blink-clips",
        tables={
            name: info(table_member(name), data, data.count(b"\n"))
            for name, data in tables.items()
        },
        files={name: info(file_member(name), data) for name, data in files.items()},
    )
    document = json.loads(manifest.to_bytes())
    if edit is not None:
        edit(document)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_MEMBER, json.dumps(document))
        for name, data in tables.items():
            if table_member(name) not in omit:
                archive.writestr(table_member(name), data)
        for name, data in files.items():
            if file_member(name) not in omit:
                archive.writestr(file_member(name), data)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path


# ---------------------------------------------------------------------------
# A library with something in every table
# ---------------------------------------------------------------------------


async def insert(db: Any, table: str, **values: Any) -> dict[str, Any]:
    """One row straight into *table*, returned as the database stored it."""
    columns = ", ".join(values)
    placeholders = ", ".join(f"${i}" for i in range(1, len(values) + 1))
    row = await db._pool.fetchrow(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING *",
        *values.values(),
    )
    return dict(row)


async def add_clip(
    db: Any, clip_id: str, media_dir: Path, **extra: Any
) -> dict[str, Any]:
    """A clip whose video is (or, if never written, is not) under *media_dir*."""
    return await insert(
        db,
        "clips",
        id=clip_id,
        camera=extra.pop("camera", "Front Door"),
        file_path=extra.pop("file_path", str(media_dir / f"{clip_id}.mp4")),
        timestamp=extra.pop("timestamp", "2026-10-01T09:00:00+00:00"),
        downloaded_at="2026-10-01T09:01:00+00:00",
        **extra,
    )


async def add_analysis(db: Any, clip_id: str, **extra: Any) -> dict[str, Any]:
    return await insert(
        db,
        "analysis_results",
        clip_id=clip_id,
        camera="Front Door",
        model=extra.pop("model", "llava"),
        summary=extra.pop("summary", "Someone at the door"),
        analyzed_at=extra.pop("analyzed_at", "2026-10-01T09:02:00+00:00"),
        **extra,
    )


async def seed_library(db: Any, media_dir: Path) -> dict[str, Any]:
    """Rows in every carried table, with relationships between them.

    Returns the pieces a test wants to look at again. Every video named here
    is missing from *media_dir* until the test writes it.
    """
    c1 = await add_clip(db, "c1", media_dir, starred=True, tags='["porch"]')
    await add_clip(db, "c2", media_dir, camera="Driveway")
    await add_clip(
        db,
        "c3",
        media_dir,
        archived=True,
        archive_path=str(media_dir / "archives" / "blink_archive_2026-10.zip"),
    )
    first = await add_analysis(db, "c1", is_suspicious=True, confidence=0.9)
    await add_analysis(
        db, "c1", model="second-pass", analyzed_at="2026-10-01T09:03:00+00:00"
    )
    await add_analysis(db, "c2", summary="A car passes")
    for label in ("person", "person", "car"):
        await insert(db, "detected_objects", clip_id="c1", label=label, confidence=0.8)
    await insert(
        db,
        "security_events",
        clip_id="c1",
        camera="Front Door",
        event_type="loitering",
        severity="suspicious",
        created_at="2026-10-01T09:02:00+00:00",
    )
    feedback = await insert(
        db,
        "analysis_feedback",
        clip_id="c1",
        camera="Front Door",
        analysis_result_id=first["id"],
        original_suspicious=True,
        original_confidence=0.9,
        correct=False,
        created_at="2026-10-01T10:00:00+00:00",
    )
    await insert(
        db,
        "face_recognition_feedback",
        clip_id="c1",
        camera="Front Door",
        report_type="false_positive",
        person_name="Riley",
        created_at="2026-10-01T10:05:00+00:00",
    )
    await insert(
        db,
        "face_enrollments",
        name="Riley",
        embedding="[0.1, 0.2, 0.3]",
        created_at="2026-09-01T00:00:00+00:00",
        approved=True,
        thumbnail=b"\xff\xd8\xffthumb",
        frame_width=640,
        camera="Front Door",
    )
    await insert(
        db,
        "camera_vehicle_signatures",
        camera="Driveway",
        box="[0.1, 0.2, 0.6, 0.9]",
        histogram="[0.5, 0.5]",
        sample_count=12,
        updated_at="2026-10-01T00:00:00+00:00",
    )
    await insert(db, "camera_baselines", camera="Front Door", hour=3, count=7)
    await insert(db, "camera_baselines", camera="Front Door", hour=14, count=21)
    await insert(
        db,
        "camera_duration_stats",
        camera="Front Door",
        avg_duration=8.5,
        sample_count=40,
    )
    await insert(
        db,
        "camera_scene_baselines",
        camera="Front Door",
        thumbnail="[0.5, 0.5]",
        sample_count=30,
        updated_at="2026-10-01T00:00:00+00:00",
    )
    await insert(
        db,
        "battery_history",
        camera="Front Door",
        battery_state="ok",
        battery_level=80,
        recorded_at="2026-10-01T00:00:00+00:00",
    )
    await insert(db, "ai_usage_reset", id=1, reset_at="2026-09-15T00:00:00+00:00")
    return {"c1": c1, "first_analysis": first, "feedback": feedback}
