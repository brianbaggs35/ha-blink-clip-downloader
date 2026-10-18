"""Guards on what a backup carries from /data, and what it leaves out."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from blink_downloader.backup import catalogue
from blink_downloader.backup.catalogue import (
    DATA_DIRS,
    DATA_FILES,
    LEFT_OUT,
    PICTURE_NAME,
    classify_member,
    picture_member,
)
from blink_downloader.media_server.assets import AssetsRoutesMixin
from blink_downloader.media_server.vehicles import VehicleRoutesMixin
from tests.conftest import data_path_attributes


def _within(path: str, names: set[str]) -> bool:
    """Whether *path* (relative to /data) is, or is inside, one of *names*."""
    parts = PurePosixPath(path).parts
    return any("/".join(parts[:end]) in names for end in range(1, len(parts) + 1))


def carried(path: str) -> bool:
    return _within(path, {entry.name for entry in (*DATA_FILES, *DATA_DIRS)})


def left_out(path: str) -> bool:
    return _within(path, set(LEFT_OUT))


def _relative(value: object) -> str:
    return PurePosixPath(str(value)).relative_to("/data").as_posix()


def _package_data_paths() -> set[str]:
    """Every path under /data the package names, relative to it."""
    paths = {_relative(value) for _, _, value in data_path_attributes()}
    paths.discard(".")  # /data itself, which is where the catalogue starts
    return paths


def test_every_path_the_package_keeps_in_data_is_either_carried_or_left_out() -> None:
    """A path added to the add-on later must be classified, or a backup would
    silently omit it and nobody would find out until a restore."""
    unclassified = sorted(
        path
        for path in _package_data_paths()
        if not carried(path) and not left_out(path)
    )
    assert not unclassified, (
        "These /data paths are in neither blink_downloader/backup/catalogue.py's "
        f"DATA_FILES/DATA_DIRS nor LEFT_OUT: {unclassified}"
    )


def test_no_path_is_both_carried_and_left_out() -> None:
    names = {entry.name for entry in (*DATA_FILES, *DATA_DIRS)}
    assert names.isdisjoint(LEFT_OUT)


def test_every_carried_file_is_one_the_package_really_uses() -> None:
    """The other direction: a typo here would back up a file that is never there."""
    real = _package_data_paths()
    assert {entry.name for entry in (*DATA_FILES, *DATA_DIRS)} <= real


def test_every_left_out_path_says_why() -> None:
    assert all(reason.strip().endswith(".") for reason in LEFT_OUT.values())


def test_no_credential_is_carried() -> None:
    carried_names = {entry.name for entry in (*DATA_FILES, *DATA_DIRS)}
    for secret in (
        "auth_credentials.json",
        "google_drive_credentials.json",
        "google_drive_settings.json",
        "web_access.json",
        "alert_action_key",
        "options.json",
    ):
        assert secret not in carried_names
        assert secret in LEFT_OUT


def test_a_files_path_is_under_data() -> None:
    # Through the module, not a name imported from it: the suite rebinds
    # catalogue.DATA_DIR, and an imported copy would still say /data.
    assert DATA_FILES[0].path == catalogue.DATA_DIR / DATA_FILES[0].name
    assert DATA_DIRS[0].path == catalogue.DATA_DIR / DATA_DIRS[0].name


def test_the_download_history_is_stored_under_its_own_name() -> None:
    (history,) = [e for e in DATA_FILES if e.role == "download_history"]
    assert history.name == "downloaded_clips.json"
    assert history.archive_name == "downloaded_clip_ids.json"


def test_carried_and_left_out_cover_what_is_inside_a_directory() -> None:
    assert carried("asset_snapshots/anything.jpg")
    assert carried("camera_configs.json")
    assert not carried("camera_configs.json.bak")
    assert left_out("model_cache/yolo/yolo11n.pt")
    assert left_out("auth_credentials.json")
    assert not left_out("camera_configs.json")


@pytest.mark.parametrize(
    "camera", ["Front Door", "Driveway", "", "Garáge Cam 2", "a" * 80]
)
def test_picture_names_the_add_on_writes_are_accepted(camera: str) -> None:
    """Both snapshot folders are named by their own code; a restore must accept
    exactly what that code produces, including the older name with no hash."""
    names = [
        AssetsRoutesMixin._asset_snapshot_path(camera).name,
        VehicleRoutesMixin._vehicle_zone_snapshot_path(camera).name,
        VehicleRoutesMixin._legacy_vehicle_zone_snapshot_path(camera).name,
    ]
    for name in names:
        assert PICTURE_NAME.fullmatch(name), name


@pytest.mark.parametrize(
    "name",
    [
        "../x.jpg",
        "a/b.jpg",
        "a\\b.jpg",
        ".hidden.jpg",
        "-leading-dash.jpg",
        "UPPER.jpg",
        "x.png",
        "x.jpg.exe",
        "x.jpg\n",
        "",
        "a" * 300 + ".jpg",
    ],
)
def test_picture_names_that_could_escape_or_surprise_are_refused(name: str) -> None:
    assert PICTURE_NAME.fullmatch(name) is None


def test_classify_member_recognises_only_what_the_catalogue_carries() -> None:
    assert classify_member("camera_configs.json") is DATA_FILES[0]
    assert classify_member("downloaded_clip_ids.json") is not None
    picture = picture_member(DATA_DIRS[0], "front-door-0123456789.jpg")
    assert classify_member(picture) is DATA_DIRS[0]


@pytest.mark.parametrize(
    "name",
    [
        "downloaded_clips.json",  # the live file's name; the archive's differs
        "auth_credentials.json",
        "../camera_configs.json",
        "asset_snapshots/../camera_configs.json",
        "asset_snapshots/nested/x.jpg",
        "other_dir/x.jpg",
        "asset_snapshots",
        "",
    ],
)
def test_classify_member_refuses_everything_else(name: str) -> None:
    assert classify_member(name) is None


def test_catalogue_paths_follow_the_redirected_data_dir(data_dir: Path) -> None:
    """The suite redirects /data; the catalogue must follow it or a test
    would read and write the real volume."""
    assert catalogue.DATA_DIR == data_dir
    assert DATA_FILES[0].path.parent == data_dir
