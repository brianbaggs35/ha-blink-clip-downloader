"""Tests for blink_downloader.protected_assets — the stored form of marked assets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blink_downloader import protected_assets as pa

RECT = {"shape": "rect", "x_min": 0.1, "y_min": 0.2, "x_max": 0.4, "y_max": 0.6}


def _asset(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "abc123def456",
        "camera": "Porch",
        "name": "Front door",
        "asset_type": "door",
        "description": "",
        "zone": RECT,
        "enabled": True,
        "created_at": "2026-09-24T10:00:00+00:00",
        "updated_at": "2026-09-24T10:00:00+00:00",
    }
    entry.update(overrides)
    return entry


def test_new_asset_ids_are_valid_and_distinct() -> None:
    ids = {pa.new_asset_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(pa.normalize_asset(_asset(id=i)) for i in ids)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Front   door ", "Front door"),
        ('The "good" bike', "The 'good' bike"),
        ("Line one\nLine two\tend", "Line one Line two end"),
        ("a\x00b\x7fc", "a b c"),
        (42, ""),
        (None, ""),
    ],
)
def test_clean_text_makes_one_quotable_line(raw: object, expected: str) -> None:
    assert pa.clean_text(raw, 48) == expected


def test_clean_text_bounds_length_without_trailing_space() -> None:
    assert pa.clean_text("abcde fghij", 6) == "abcde"


@pytest.mark.parametrize(
    "zone",
    [
        None,
        [],
        {"shape": "polygon", "points": [[0.1, 0.1], [0.2, 0.2]]},
        {"shape": "polygon", "points": "nope"},
        {"shape": "polygon", "points": [[0.1, 0.1], [0.2, "x"], [0.3, 0.3]]},
        {"shape": "polygon", "points": [[0.1, 0.1], [0.2], [0.3, 0.3]]},
        {"shape": "polygon", "points": [[0.1, 0.1], [1.2, 0.2], [0.3, 0.3]]},
        {"x_min": 0.5, "y_min": 0.1, "x_max": 0.4, "y_max": 0.6},
        {"x_min": 0.1, "y_min": 0.1, "x_max": 0.4},
        {"x_min": "a", "y_min": 0.1, "x_max": 0.4, "y_max": 0.6},
    ],
)
def test_normalize_zone_rejects_malformed(zone: object) -> None:
    assert pa.normalize_zone(zone) is None


def test_normalize_zone_stamps_a_legacy_rectangle() -> None:
    legacy = {"x_min": 0.1, "y_min": 0.2, "x_max": 0.4, "y_max": 0.6}
    assert pa.normalize_zone(legacy) == RECT


def test_normalize_zone_keeps_a_polygon() -> None:
    zone = {"shape": "polygon", "points": [[0, 0], [1, 0], ["0.5", 1]]}
    assert pa.normalize_zone(zone) == {
        "shape": "polygon",
        "points": [[0.0, 0.0], [1.0, 0.0], [0.5, 1.0]],
    }


def test_normalize_asset_canonical_form() -> None:
    raw = _asset(description='a "red"\ndoor', enabled="yes", created_at=None)
    assert pa.normalize_asset(raw) == {
        **_asset(),
        "description": "a 'red' door",
        "enabled": True,
        "created_at": "",
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "UPPER-CASE"},
        {"id": "short"},
        {"id": 12345678},
        {"camera": "   "},
        {"camera": None},
        {"name": "   "},
        {"asset_type": "vehicle"},
        {"asset_type": "rocket"},
        {"zone": None},
    ],
)
def test_normalize_asset_rejects_unusable(overrides: dict[str, object]) -> None:
    assert pa.normalize_asset(_asset(**overrides)) is None


def test_normalize_asset_rejects_a_non_dict() -> None:
    assert pa.normalize_asset(["not", "a", "dict"]) is None


def test_disabled_survives_normalization() -> None:
    asset = pa.normalize_asset(_asset(enabled=False))
    assert asset is not None
    assert asset["enabled"] is False


def test_parse_assets_keeps_good_entries_and_first_of_each_id() -> None:
    data = {
        "assets": [
            _asset(),
            _asset(name="Duplicate"),
            _asset(id="bbbbbbbbbbbb", asset_type="nonsense"),
            "garbage",
            _asset(id="cccccccccccc", name="Bike", asset_type="bicycle"),
        ]
    }
    parsed = pa.parse_assets(data)
    assert [a["name"] for a in parsed] == ["Front door", "Bike"]


@pytest.mark.parametrize("data", [[], {"assets": "x"}, {"other": []}, None])
def test_parse_assets_of_the_wrong_shape_is_empty(data: object) -> None:
    assert pa.parse_assets(data) == []


def test_read_and_write_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "protected_assets.json"
    assert pa.read_assets(path) == []
    pa.write_assets([_asset()], path)
    assert json.loads(path.read_text()) == {"assets": [_asset()]}
    assert pa.read_assets(path) == [_asset()]


def test_an_unreadable_file_reads_as_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "protected_assets.json"
    path.write_text("{not json")
    assert pa.read_assets(path) == []
    assert "no marked assets loaded" in caplog.text


def test_rename_camera_moves_every_asset_case_insensitively() -> None:
    assets = [
        _asset(),
        _asset(id="bbbbbbbbbbbb", camera="porch"),
        _asset(id="cccccccccccc", camera="Yard"),
    ]
    renamed = pa.rename_camera(assets, "PORCH", "Front Porch")
    assert renamed is not None
    assert [a["camera"] for a in renamed] == ["Front Porch", "Front Porch", "Yard"]
    # The input is untouched.
    assert assets[0]["camera"] == "Porch"


def test_rename_camera_with_nothing_on_it_is_none() -> None:
    assert pa.rename_camera([_asset()], "Garage", "Garage 2") is None


def test_assets_by_camera_groups_enabled_only() -> None:
    assets = [
        _asset(),
        _asset(id="bbbbbbbbbbbb", enabled=False),
        _asset(id="cccccccccccc", camera="Yard"),
    ]
    grouped = pa.assets_by_camera(assets)
    assert list(grouped) == ["Porch", "Yard"]
    assert [a["id"] for a in grouped["Porch"]] == ["abc123def456"]
