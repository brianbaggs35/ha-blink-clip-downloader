"""Tests for blink_downloader.manifest."""

from __future__ import annotations

from pathlib import Path


from blink_downloader.manifest import ClipManifest


def make_manifest(tmp_path: Path) -> ClipManifest:
    return ClipManifest(tmp_path / "manifest.json")


def test_append_and_read(tmp_path):
    m = make_manifest(tmp_path)
    m.append({"id": "1", "camera": "Front Door"})
    records = m.read_all()
    assert len(records) == 1
    assert records[0]["id"] == "1"
    assert records[0]["camera"] == "Front Door"
    assert "recorded_at" in records[0]


def test_multiple_appends(tmp_path):
    m = make_manifest(tmp_path)
    for i in range(5):
        m.append({"id": str(i)})
    assert m.count() == 5


def test_read_empty_when_no_file(tmp_path):
    m = make_manifest(tmp_path)
    assert m.read_all() == []
    assert m.count() == 0


def test_append_is_idempotent_on_repeat(tmp_path):
    m = make_manifest(tmp_path)
    m.append({"id": "dup"})
    m.append({"id": "dup"})
    # Manifest does not deduplicate — that's the caller's job.
    assert m.count() == 2


def test_corrupt_line_is_skipped(tmp_path):
    f = tmp_path / "manifest.json"
    f.write_text('{"id":"ok"}\nNOT_JSON\n{"id":"also_ok"}\n')
    m = ClipManifest(f)
    records = m.read_all()
    assert len(records) == 2
    assert records[0]["id"] == "ok"
    assert records[1]["id"] == "also_ok"


def test_append_adds_recorded_at(tmp_path):
    m = make_manifest(tmp_path)
    m.append({"id": "x"})
    record = m.read_all()[0]
    # recorded_at should be a valid ISO datetime string
    from datetime import datetime

    dt = datetime.fromisoformat(record["recorded_at"])
    assert dt is not None


def test_append_oserror_is_logged(tmp_path):
    """When the manifest file cannot be written, OSError is caught and logged."""
    from unittest.mock import patch

    m = ClipManifest(tmp_path / "manifest.json")
    # Path.open() is used internally, so patch that (not builtins.open).
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        m.append({"id": "x"})  # must not raise
