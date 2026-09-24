"""Guarantees for an install upgrading from the previous release.

A new user gets whatever ``config.yaml`` currently says. An *existing* user
brings their own ``/data/options.json``, written when they installed the
previous release, and Home Assistant hands it back to the new build
unchanged apart from filling in genuinely new keys. Three things can break
that install, all silently and all outside the reach of every other test in
this suite:

* **A removed or renamed option.** Supervisor validates the stored options
  against the *new* schema before starting the add-on; a key the schema no
  longer knows fails validation and the add-on will not start.
* **A narrowed enum.** Same failure, one level down: ``list(a|b|c)`` that
  drops a value somebody has stored rejects their options.json outright.
  This is exactly why ``yolo11n.pt`` is still listed alongside the YOLO26
  ids even though it is no longer the default.
* **A new option with no default.** The stored options.json has no such
  key, so the parser must supply one rather than raising.

``tests/fixtures/addon_options_5_5_2.json`` is a verbatim snapshot of the
previous release's ``config.yaml`` (its ``options`` and ``schema`` blocks).
It is deliberately a checked-in fixture rather than something read out of
git at test time: the assertion is about a shipped release, which does not
change, and CI checkouts are shallow enough that the other branch may not
even be fetched.

When this file's assertions need updating for a *deliberate* removal, that
is the point — it means existing installs need a migration story, not a
quieter test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from blink_downloader.config import load_config

_FIXTURE = Path(__file__).parent / "fixtures" / "addon_options_5_5_2.json"
_CONFIG_YAML = Path(__file__).parent.parent / "config.yaml"


@pytest.fixture(scope="module")
def previous() -> dict:
    return json.loads(_FIXTURE.read_text())


@pytest.fixture(scope="module")
def current() -> dict:
    return yaml.safe_load(_CONFIG_YAML.read_text())


def _enum_values(schema_entry: str) -> list[str] | None:
    """The allowed values of a ``list(a|b|c)`` schema entry, else None."""
    match = re.fullmatch(r"list\((.+)\)\??", schema_entry.strip())
    return match.group(1).split("|") if match else None


def _int_bounds(schema_entry: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"int\((-?\d+),\s*(-?\d+)\)\??", schema_entry.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def test_no_option_was_removed_or_renamed(previous: dict, current: dict) -> None:
    """Supervisor validates a stored options.json against the new schema, so
    an option that simply disappears stops an existing install from
    starting."""
    missing = sorted(set(previous["schema"]) - set(current["schema"]))
    assert not missing, (
        f"options present in {previous['version']} but gone from this release: "
        f"{missing} — an existing install's options.json still contains these, "
        "and Supervisor will refuse to start the add-on"
    )


def test_every_previously_valid_value_is_still_accepted(
    previous: dict, current: dict
) -> None:
    """A narrowed enum rejects the exact value an existing install has
    stored. The YOLO11 model ids are the live example: no longer the
    default, still selectable, precisely so upgrading installs keep
    working."""
    for key, stored in previous["options"].items():
        entry = current["schema"].get(key)
        if entry is None:
            continue  # covered by the test above
        allowed = _enum_values(str(entry))
        if allowed is not None:
            assert str(stored) in allowed, (
                f"{key}={stored!r} was valid in {previous['version']} but is no "
                f"longer in this release's enum {allowed}"
            )
        bounds = _int_bounds(str(entry))
        if bounds is not None and isinstance(stored, int):
            low, high = bounds
            assert low <= stored <= high, (
                f"{key}={stored!r} was valid in {previous['version']} but falls "
                f"outside this release's range {bounds}"
            )


def test_previous_release_options_load_without_error(
    previous: dict, tmp_path: Path
) -> None:
    """The whole point: the exact options.json an upgrading install brings
    parses into a usable config, with every option added since then taking
    its documented default rather than raising."""
    options = dict(previous["options"])
    options.update({"username": "user@example.com", "password": "secret"})
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps(options))

    config = load_config(options_file)

    assert not config.startup_error
    # Carried across untouched.
    assert config.username == "user@example.com"
    assert config.poll_interval == previous["options"]["poll_interval"]
    assert config.retention_days == previous["options"]["retention_days"]
    # Added in 6.0.0, absent from the stored file, defaulted here.
    assert config.ai_security_events_enabled is True
    assert config.ai_temporal_scan_frames == 12
    assert config.ai_cv_concurrency == 1
    assert config.ai_risk_alert_threshold == 75
    assert config.ai_pose_estimation_enabled is False
    assert config.ai_pose_model == "yolo26n-pose.pt"
    assert "Depth-Anything" in config.ai_depth_estimation_model


def test_a_stored_yolo11_model_choice_survives_the_upgrade(tmp_path: Path) -> None:
    """The one option whose default actually changed in 6.0.0. An install
    that had explicitly chosen (or simply inherited) a YOLO11 model must
    keep running it, not be silently switched or rejected."""
    options_file = tmp_path / "options.json"
    options_file.write_text(
        json.dumps(
            {
                "username": "user@example.com",
                "password": "secret",
                "ai_object_detection_model": "yolo11n.pt",
            }
        )
    )
    config = load_config(options_file)
    assert config.ai_object_detection_model == "yolo11n.pt"


def test_every_option_the_parser_reads_is_declared_in_config_yaml(
    current: dict,
) -> None:
    """The reverse direction: an option the code reads but ``config.yaml``
    never declares can never be set by a user, and one declared but never
    read is a dead control on the Configuration tab."""
    source = (
        Path(__file__).parent.parent / "blink_downloader" / "config.py"
    ).read_text()
    read = set(re.findall(r'data\.get\(\s*["\']([a-z0-9_]+)["\']', source))
    read |= set(re.findall(r'_bounded_int\(\s*data,\s*["\']([a-z0-9_]+)["\']', source))
    undeclared = sorted(read - set(current["schema"]))
    assert not undeclared, (
        f"read from options.json but not in config.yaml: {undeclared}"
    )
    unread = sorted(set(current["schema"]) - read)
    assert not unread, f"declared in config.yaml but never read: {unread}"


# ----------------------------------------------------------------------
# The database half of the same guarantee
# ----------------------------------------------------------------------


_SCHEMA_FIXTURE = Path(__file__).parent / "fixtures" / "schema_5_5_2.sql"


async def test_a_real_previous_release_database_upgrades_in_place() -> None:
    """Build a database from the previous release's own shipped SQL, put real
    rows in it, then run this release's ``init()`` over it.

    ``tests/test_database.py`` already checks that each migration re-adds a
    column it drops, which catches a *missing* migration line. This catches
    the other half: that the whole schema, applied as that release actually
    applied it, survives contact with this one — new tables created, new
    columns defaulted, and every pre-existing row still readable through the
    same API the UI uses.
    """
    import asyncpg

    from blink_downloader.database import ClipDatabase

    from .conftest import TEST_DB_DSN

    admin_dsn, _, _ = TEST_DB_DSN.rpartition("/")
    upgrade_db = "blink_clips_upgrade_test"
    admin = await asyncpg.connect(f"{admin_dsn}/postgres")
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {upgrade_db}")
        await admin.execute(f"CREATE DATABASE {upgrade_db}")
    finally:
        await admin.close()

    dsn = f"{admin_dsn}/{upgrade_db}"
    try:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_SCHEMA_FIXTURE.read_text())
            await conn.execute(
                "INSERT INTO clips (id, camera, timestamp, file_path, source,"
                " downloaded_at, duration, starred, tags)"
                " VALUES ('legacy-1', 'Front Door', '2026-01-01T09:00:00+00:00',"
                " '/share/blink-clips/legacy-1.mp4', 'pir',"
                " '2026-01-01T09:01:00+00:00', 8, TRUE, '[\"porch\"]')"
            )
            await conn.execute(
                "INSERT INTO analysis_results (clip_id, camera, model, response_text,"
                " is_suspicious, confidence, summary, frame_count, analysis_duration,"
                " analyzed_at) VALUES ('legacy-1', 'Front Door', 'llava', '{}', TRUE,"
                " 0.91, 'Someone at the door', 3, 1.25, '2026-01-01T09:02:00+00:00')"
            )
            # A camera's learned background from before the day/night split.
            await conn.execute(
                "INSERT INTO camera_scene_baselines (camera, thumbnail,"
                " sample_count, updated_at, consecutive_deviation_count)"
                " VALUES ('Front Door', $1, 40, '2026-01-01T09:00:00+00:00', 0)",
                json.dumps([0.5] * 256),
            )
            await conn.execute(
                "INSERT INTO analysis_queue (clip_id, camera, clip_path, status,"
                " queued_at) VALUES ('legacy-1', 'Front Door',"
                " '/share/blink-clips/legacy-1.mp4', 'completed',"
                " '2026-01-01T09:00:00+00:00')"
            )
        finally:
            await conn.close()

        db = ClipDatabase(dsn)
        await db.init()
        try:
            # The clip and its verdict came through untouched.
            clip = await db.get_clip("legacy-1")
            assert clip is not None
            assert clip["starred"] is True
            assert clip["duration"] == 8
            analysis = await db.get_analysis_for_clip("legacy-1")
            assert analysis is not None
            assert analysis["summary"] == "Someone at the door"
            # Columns 6.0.0 added, defaulted rather than null.
            assert analysis["risk_score"] == 0.0
            assert analysis["severity"] == "routine"
            assert analysis["event_type"] == ""
            assert analysis["evidence_quality"] == 0.0
            assert analysis["risk_override_applied"] is False
            queued = await db.get_failed_analysis_queue()
            assert queued == []  # the legacy row is 'completed', and readable
            # Tables 6.0.0 added, present and empty rather than missing.
            assert await db.get_security_events("legacy-1") == []
            assert await db.get_detected_object_boxes("legacy-1") == {"objects": []}
            assert await db.get_detected_objects_summary("legacy-1") == []
            assert await db.get_vehicle_signature("Front Door") is None
            timeline = await db.get_security_timeline()
            assert timeline == {"events": [], "total": 0}
            assert (await db.get_security_stats())["total"] == 0
            # And the queue's new retry_count defaults for the legacy row.
            assert db._pool is not None
            pending = await db._pool.fetchrow(
                "SELECT retry_count FROM analysis_queue WHERE clip_id='legacy-1'"
            )
            assert pending["retry_count"] == 0
            # The old single background carries on as the daylight one; the
            # infrared one starts empty and says nothing until it has learned.
            assert await db.get_scene_deviation("Front Door", [0.5] * 256) == 0.0
            assert (
                await db.get_scene_deviation("Front Door", [0.5] * 256, night=True)
                is None
            )
        finally:
            await db.close()
    finally:
        admin = await asyncpg.connect(f"{admin_dsn}/postgres")
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {upgrade_db}")
        finally:
            await admin.close()
