"""Tests for the backup archive format and its verifying reader."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from blink_downloader.backup import archive as archive_module
from blink_downloader.backup.archive import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MANIFEST_MEMBER,
    BackupArchive,
    BackupError,
    BackupManifest,
    MemberInfo,
    RecordWriter,
    decode_row,
    describe_file,
    encode_row,
    file_member,
    parse_manifest,
    table_member,
    write_archive,
)
from tests.backup_helpers import make_archive, records

CLIP = {"id": "c1", "camera": "Front Door", "file_path": "/share/c1.mp4"}


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "backup.zip"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_a_record_round_trips_including_bytes_and_unicode() -> None:
    row = {"name": "Zoë", "thumbnail": b"\xff\xd8\x00jpeg", "ok": True, "n": None}
    line = encode_row(row)
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert decode_row(line) == row


def test_a_record_is_one_line_however_its_text_is_spelled() -> None:
    assert encode_row({"note": "two\nlines\r\nhere"}).count(b"\n") == 1


def test_a_value_that_is_not_json_or_bytes_is_refused() -> None:
    with pytest.raises(TypeError, match="set"):
        encode_row({"bad": {1, 2}})


@pytest.mark.parametrize(
    "line",
    [b"not json\n", b"[1, 2]\n", b'"just a string"\n', b'{"x": {"$bytes": "***"}}\n'],
)
def test_a_record_that_is_not_a_record_is_a_backup_error(line: bytes) -> None:
    with pytest.raises(BackupError):
        decode_row(line)


def test_an_oversized_record_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_module, "MAX_RECORD_BYTES", 10)
    with pytest.raises(BackupError, match="far larger"):
        decode_row(b'{"a": "' + b"x" * 20 + b'"}\n')


def test_the_record_writer_counts_and_hashes_exactly_what_it_wrote(
    tmp_path: Path,
) -> None:
    target = tmp_path / "clips.jsonl"
    writer = RecordWriter(target, table_member("clips"))
    writer.write([CLIP, {**CLIP, "id": "c2"}])
    writer.write([{**CLIP, "id": "c3"}])
    writer.close()
    member = writer.info()
    assert member.name == "database/clips.jsonl"
    assert member.rows == 3
    assert member == MemberInfo(
        "database/clips.jsonl",
        target.stat().st_size,
        describe_file(target, "database/clips.jsonl").sha256,
        3,
    )


def test_describe_file_measures_size_and_checksum(tmp_path: Path) -> None:
    target = tmp_path / "f.json"
    target.write_bytes(b"hello")
    described = describe_file(target, "files/f.json")
    assert described.size == 5
    assert described.rows is None
    assert described.sha256 == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


# ---------------------------------------------------------------------------
# A good archive
# ---------------------------------------------------------------------------


def test_a_good_archive_opens_and_reads_back(path: Path) -> None:
    make_archive(
        path,
        tables={"clips": records(CLIP, {**CLIP, "id": "c2"})},
        files={"camera_configs.json": b"[]"},
    )
    archive = BackupArchive.open(path)
    assert archive.manifest.addon_version == "6.1.3"
    assert archive.manifest.clip_folder == "/share/blink-clips"
    assert archive.manifest.tables["clips"].rows == 2
    assert archive.read_file("camera_configs.json") == b"[]"
    assert [r["id"] for batch in archive.iter_rows("clips") for r in batch] == [
        "c1",
        "c2",
    ]


def test_iter_rows_batches_and_reports_nothing_for_a_table_not_in_the_backup(
    path: Path,
) -> None:
    make_archive(
        path, tables={"clips": records(*({**CLIP, "id": str(i)} for i in range(5)))}
    )
    archive = BackupArchive.open(path)
    assert [len(b) for b in archive.iter_rows("clips", size=2)] == [2, 2, 1]
    assert list(archive.iter_rows("analysis_results")) == []


def test_iter_rows_ends_cleanly_on_an_exact_multiple_and_on_an_empty_table(
    path: Path,
) -> None:
    make_archive(
        path,
        tables={
            "clips": records(*({**CLIP, "id": str(i)} for i in range(4))),
            "analysis_results": b"",
        },
    )
    archive = BackupArchive.open(path)
    assert [len(b) for b in archive.iter_rows("clips", size=2)] == [2, 2]
    assert list(archive.iter_rows("analysis_results")) == []


def test_an_empty_archive_is_a_valid_backup(path: Path) -> None:
    make_archive(path)
    archive = BackupArchive.open(path)
    assert archive.manifest.members() == []


def test_write_archive_puts_the_manifest_first(tmp_path: Path) -> None:
    source = tmp_path / "s.json"
    source.write_bytes(b"{}")
    manifest = BackupManifest(
        "6.1.3",
        "now",
        "/share",
        {},
        {
            "camera_configs.json": describe_file(
                source, file_member("camera_configs.json")
            )
        },
    )
    target = tmp_path / "out.zip"
    write_archive(target, manifest, {file_member("camera_configs.json"): source})
    with zipfile.ZipFile(target) as written:
        assert written.namelist() == [MANIFEST_MEMBER, "files/camera_configs.json"]
    assert BackupArchive.open(target).read_file("camera_configs.json") == b"{}"


# ---------------------------------------------------------------------------
# Not a backup at all
# ---------------------------------------------------------------------------


def test_a_file_that_is_not_a_zip_is_refused(path: Path) -> None:
    path.write_bytes(b"this is not a zip file")
    with pytest.raises(BackupError, match="not a readable backup"):
        BackupArchive.open(path)


def test_a_zip_without_a_manifest_is_refused(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "hi")
    with pytest.raises(BackupError, match="no manifest.json"):
        BackupArchive.open(path)


def test_a_damaged_zip_is_refused_not_crashed_on(path: Path) -> None:
    make_archive(
        path, tables={"clips": records(*({**CLIP, "id": str(i)} for i in range(200)))}
    )
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        BackupArchive.open(path)


def test_an_unsupported_zip_feature_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_archive(path, tables={"clips": records(CLIP)})

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(zipfile.ZipFile, "open", refuse)
    with pytest.raises(BackupError, match="ZIP feature"):
        BackupArchive.open(path)


# ---------------------------------------------------------------------------
# A manifest that is wrong
# ---------------------------------------------------------------------------


def _manifest(**changes: Any) -> bytes:
    document: dict[str, Any] = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "addon_version": "6.1.3",
        "created_at": "now",
        "clip_folder": "/share",
        "tables": {},
        "files": {},
    }
    document.update(changes)
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"not json", "not valid JSON"),
        (b"[]", "not an object"),
        (_manifest(format="something-else"), "not made by this add-on"),
        (_manifest(format_version=0), "format version is not valid"),
        (_manifest(format_version="1"), "no valid 'format_version'"),
        (_manifest(format_version=True), "no valid 'format_version'"),
        (_manifest(addon_version=None), "no valid 'addon_version'"),
        (_manifest(created_at=5), "no valid 'created_at'"),
        (_manifest(clip_folder=[]), "no valid 'clip_folder'"),
        (_manifest(tables=[]), "no list of tables"),
        (_manifest(files="x"), "no list of files"),
        (_manifest(tables={"clips": "x"}), "entry for clips is malformed"),
        (
            _manifest(tables={"clips": {"size": 1, "sha256": "0" * 64}}),
            "no valid 'rows'",
        ),
        (
            _manifest(tables={"clips": {"size": -1, "sha256": "0" * 64, "rows": 0}}),
            "entry for clips is malformed",
        ),
        (
            _manifest(tables={"clips": {"size": 1, "sha256": "0" * 64, "rows": -1}}),
            "entry for clips is malformed",
        ),
        (
            _manifest(tables={"clips": {"size": 1, "sha256": "short", "rows": 0}}),
            "entry for clips is malformed",
        ),
        (
            _manifest(files={"camera_configs.json": {"size": "1", "sha256": "0" * 64}}),
            "no valid 'size'",
        ),
    ],
)
def test_a_manifest_that_is_not_right_says_so(raw: bytes, message: str) -> None:
    with pytest.raises(BackupError, match=message):
        parse_manifest(raw)


def test_a_backup_from_a_newer_format_tells_you_to_update() -> None:
    raw = _manifest(format_version=FORMAT_VERSION + 1)
    with pytest.raises(BackupError, match="newer version.*Update the add-on"):
        parse_manifest(raw)


def test_a_table_this_version_does_not_know_is_refused_not_ignored() -> None:
    raw = _manifest(tables={"mystery": {"size": 1, "sha256": "0" * 64, "rows": 0}})
    with pytest.raises(BackupError, match="does not know \\(mystery\\)"):
        parse_manifest(raw)


@pytest.mark.parametrize(
    "name",
    [
        "auth_credentials.json",
        "../camera_configs.json",
        "asset_snapshots/../x.jpg",
        "downloaded_clips.json",
    ],
)
def test_a_file_the_catalogue_does_not_carry_is_refused(name: str) -> None:
    raw = _manifest(files={name: {"size": 1, "sha256": "0" * 64}})
    with pytest.raises(BackupError, match="does not know"):
        parse_manifest(raw)


def test_a_credential_cannot_be_smuggled_in_as_a_member(path: Path) -> None:
    """The names a restore would write are the catalogue's, whatever the ZIP says."""
    make_archive(path, extra={"files/auth_credentials.json": b"{}"})
    with pytest.raises(BackupError, match="which its manifest does not list"):
        BackupArchive.open(path)


# ---------------------------------------------------------------------------
# An archive that does not match its manifest
# ---------------------------------------------------------------------------


def test_a_member_the_manifest_lists_but_the_zip_lacks_is_incomplete(
    path: Path,
) -> None:
    make_archive(path, tables={"clips": records(CLIP)}, omit=("database/clips.jsonl",))
    with pytest.raises(BackupError, match="incomplete.*database/clips.jsonl"):
        BackupArchive.open(path)


def test_a_member_the_manifest_does_not_list_is_refused(path: Path) -> None:
    make_archive(
        path,
        tables={"clips": records(CLIP)},
        extra={"files/camera_configs.json": b"[]"},
    )
    with pytest.raises(BackupError, match="files/camera_configs.json.*does not list"):
        BackupArchive.open(path)


def test_a_zip_holding_one_name_twice_is_refused(path: Path) -> None:
    make_archive(path, files={"camera_configs.json": b"[]"})
    with (
        pytest.warns(UserWarning, match="Duplicate name"),  # zipfile's own warning
        zipfile.ZipFile(path, "a") as archive,
    ):
        archive.writestr("files/camera_configs.json", b"[1]")
    with pytest.raises(BackupError, match="same file twice"):
        BackupArchive.open(path)


def test_too_many_members_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_archive(path, files={"camera_configs.json": b"[]"})
    monkeypatch.setattr(archive_module, "MAX_MEMBERS", 1)
    with pytest.raises(BackupError, match="far more files"):
        BackupArchive.open(path)


def test_an_archive_that_would_expand_enormously_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_archive(path, files={"camera_configs.json": b"x" * 1000})
    monkeypatch.setattr(archive_module, "MAX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(BackupError, match="unreasonable size"):
        BackupArchive.open(path)


def test_a_huge_manifest_is_refused_before_it_is_read(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_archive(path)
    monkeypatch.setattr(archive_module, "MAX_MANIFEST_BYTES", 10)
    with pytest.raises(BackupError, match="far larger than any real one"):
        BackupArchive.open(path)


def test_an_encrypted_member_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # zipfile cannot write an encrypted entry (it clears the flag), so the
    # entries are presented as flagged when the reader lists them.
    make_archive(path, files={"camera_configs.json": b"[]"})
    listed = zipfile.ZipFile.infolist

    def flagged(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        infos = listed(self)
        for entry in infos:
            entry.flag_bits |= 0x1
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", flagged)
    with pytest.raises(BackupError, match="password-protected"):
        BackupArchive.open(path)


def test_a_changed_checksum_means_damaged(path: Path) -> None:
    make_archive(
        path,
        files={"camera_configs.json": b"[]"},
        edit=lambda doc: doc["files"]["camera_configs.json"].update(sha256="0" * 64),
    )
    with pytest.raises(BackupError, match="damaged.*camera_configs.json"):
        BackupArchive.open(path)


def test_a_changed_size_means_damaged(path: Path) -> None:
    make_archive(
        path,
        files={"camera_configs.json": b"[]"},
        edit=lambda doc: doc["files"]["camera_configs.json"].update(size=99),
    )
    with pytest.raises(BackupError, match="damaged"):
        BackupArchive.open(path)


def test_a_changed_record_count_means_damaged(path: Path) -> None:
    make_archive(
        path,
        tables={"clips": records(CLIP)},
        edit=lambda doc: doc["tables"]["clips"].update(rows=2),
    )
    with pytest.raises(BackupError, match="damaged.*clips.jsonl"):
        BackupArchive.open(path)


# ---------------------------------------------------------------------------
# Reading records and files out of a verified archive
# ---------------------------------------------------------------------------


def test_a_record_that_is_not_json_fails_when_it_is_read(path: Path) -> None:
    make_archive(path, tables={"clips": b"definitely not json\n"})
    archive = BackupArchive.open(path)  # the checksum is right; the content is not
    with pytest.raises(BackupError, match="cannot be read"):
        list(archive.iter_rows("clips"))


def test_a_record_that_is_too_long_fails_when_it_is_read(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_archive(path, tables={"clips": records({"id": "x" * 100})})
    archive = BackupArchive.open(path)
    monkeypatch.setattr(archive_module, "MAX_RECORD_BYTES", 20)
    with pytest.raises(BackupError, match="far larger"):
        list(archive.iter_rows("clips"))


def test_a_file_too_large_to_hold_in_memory_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_archive(path, files={"camera_configs.json": b"x" * 50})
    archive = BackupArchive.open(path)
    monkeypatch.setattr(archive_module, "MAX_FILE_BYTES", 10)
    with pytest.raises(BackupError, match="too large to restore"):
        archive.read_file("camera_configs.json")
