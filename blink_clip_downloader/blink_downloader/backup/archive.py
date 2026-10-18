"""The backup archive: its layout, and a reader that trusts nothing in it.

A backup is one ZIP::

    manifest.json              what is inside, with a checksum for each part
    database/<table>.jsonl     one JSON record per line, per table
    files/<name>               settings and pictures from /data

Nothing is ever extracted to disk by name. Every member is looked up by a
name this module worked out from the catalogue, so an archive cannot place a
file anywhere, however its entries are spelled. A file is accepted only if
its manifest parses, every member the manifest lists is present with the
size, checksum and (for records) line count it claims, and the ZIP holds
nothing the manifest does not list. The sizes the ZIP declares are bounded
before any member is read, and a record is bounded as it is read, so a
hostile file costs time at worst, not memory.

Anything wrong with the file raises :class:`BackupError`, whose message is
written to be shown to the person who chose the file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
import zlib
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from ..database.backup import backup_table
from .catalogue import classify_member

FORMAT_NAME = "blink-clip-downloader-backup"
FORMAT_VERSION = 1

MANIFEST_MEMBER = "manifest.json"
DATABASE_DIR = "database"
# The manifest's list of files, and the folder in the ZIP they are kept in.
_FILES_KEY = "files"
FILES_DIR = _FILES_KEY

#: Pictures are one per camera, so this is generous.
MAX_MEMBERS = 4096
#: What the whole archive may expand to. A real library is a small fraction.
MAX_UNCOMPRESSED_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 4 * 1024**2
#: One JSON record; the biggest real one is a few hundred kilobytes of picture.
MAX_RECORD_BYTES = 8 * 1024**2
#: The largest settings file or picture read into memory at once.
MAX_FILE_BYTES = 256 * 1024**2
BATCH_ROWS = 1000

_READ_CHUNK = 1024 * 1024
_BYTES_KEY = "$bytes"
_ENCRYPTED_FLAG = 0x1


class BackupError(Exception):
    """A backup, or a request to use one, that cannot be honoured.

    The message is for the person at the screen, so it says what to do next
    where there is something to do.
    """


@dataclass(frozen=True)
class MemberInfo:
    """One part of an archive, as the manifest describes it."""

    #: Its path inside the ZIP.
    name: str
    size: int
    sha256: str
    #: Records, for a database member; ``None`` for a file.
    rows: int | None = None

    def to_json(self) -> dict[str, Any]:
        info: dict[str, Any] = {"size": self.size, "sha256": self.sha256}
        if self.rows is not None:
            info["rows"] = self.rows
        return info


@dataclass(frozen=True)
class BackupManifest:
    """The archive's table of contents."""

    addon_version: str
    created_at: str
    #: Where the clip videos lived, which a backup does not carry.
    clip_folder: str
    #: Table name -> its member.
    tables: Mapping[str, MemberInfo]
    #: File name relative to ``files/`` -> its member.
    files: Mapping[str, MemberInfo]

    def members(self) -> list[MemberInfo]:
        return [*self.tables.values(), *self.files.values()]

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "format": FORMAT_NAME,
                "format_version": FORMAT_VERSION,
                "addon_version": self.addon_version,
                "created_at": self.created_at,
                "clip_folder": self.clip_folder,
                "tables": {k: v.to_json() for k, v in self.tables.items()},
                _FILES_KEY: {k: v.to_json() for k, v in self.files.items()},
            },
            indent=2,
        ).encode("utf-8")


def table_member(table: str) -> str:
    """The ZIP path of *table*'s records."""
    return f"{DATABASE_DIR}/{table}.jsonl"


def file_member(name: str) -> str:
    """The ZIP path of the file called *name* in the catalogue."""
    return f"{FILES_DIR}/{name}"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _json_default(value: object) -> dict[str, str]:
    """How the one non-JSON value a table holds, bytes, is written."""
    if isinstance(value, bytes):
        return {_BYTES_KEY: base64.b64encode(value).decode("ascii")}
    raise TypeError(f"{type(value).__name__} cannot be written to a backup")


def encode_row(row: Mapping[str, Any]) -> bytes:
    """One record as a line of JSON."""
    line = json.dumps(
        row, separators=(",", ":"), ensure_ascii=False, default=_json_default
    )
    return line.encode("utf-8") + b"\n"


def _revive(value: dict[str, Any]) -> Any:
    if len(value) == 1 and _BYTES_KEY in value:
        return base64.b64decode(value[_BYTES_KEY], validate=True)
    return value


def decode_row(line: bytes) -> dict[str, Any]:
    """One record from a line written by :func:`encode_row`."""
    if len(line) > MAX_RECORD_BYTES:
        raise BackupError("A record in the backup is far larger than any real one.")
    try:
        row = json.loads(line, object_hook=_revive)
    except ValueError as exc:  # binascii.Error, a bad $bytes value, is one
        raise BackupError("The backup holds a record that cannot be read.") from exc
    if not isinstance(row, dict):
        raise BackupError("The backup holds a record that is not in the expected form.")
    return row


class RecordWriter:
    """Writes one table's records to a file, counting and hashing as it goes.

    The count and checksum are of exactly the bytes that go into the
    archive, so what the manifest says cannot differ from what is stored.
    Use it under :func:`contextlib.closing`.
    """

    def __init__(self, path: Path, member: str) -> None:
        self._member = member
        self._file: IO[bytes] = path.open("wb")
        self._digest = hashlib.sha256()
        self._size = 0
        self._rows = 0

    def close(self) -> None:
        self._file.close()

    def write(self, rows: list[dict[str, Any]]) -> None:
        chunk = b"".join(encode_row(row) for row in rows)
        self._file.write(chunk)
        self._digest.update(chunk)
        self._size += len(chunk)
        self._rows += len(rows)

    def info(self) -> MemberInfo:
        return MemberInfo(
            self._member, self._size, self._digest.hexdigest(), self._rows
        )


def describe_file(path: Path, member: str) -> MemberInfo:
    """Size and checksum of the file at *path*, as an archive member."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return MemberInfo(member, size, digest.hexdigest())


def write_archive(
    zip_path: Path, manifest: BackupManifest, sources: Mapping[str, Path]
) -> None:
    """Write the ZIP: the manifest first, then each member from its source."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_MEMBER, manifest.to_bytes())
        for name, source in sources.items():
            archive.write(source, name)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _damaged(what: str) -> BackupError:
    return BackupError(
        f"The backup is damaged ({what} does not match its checksum). "
        "Download it again, or use a different copy."
    )


def _not_valid(why: str) -> BackupError:
    return BackupError(f"That is not a usable backup: {why}.")


def _field(raw: Mapping[str, Any], key: str, kind: type) -> Any:
    value = raw.get(key)
    # bool is an int to Python, and no count in a manifest is a boolean.
    if not isinstance(value, kind) or isinstance(value, bool):
        raise _not_valid(f"its manifest has no valid '{key}'")
    return value


def _member_info(name: str, raw: object, *, is_table: bool) -> MemberInfo:
    if not isinstance(raw, dict):
        raise _not_valid(f"its manifest entry for {name} is malformed")
    size = _field(raw, "size", int)
    digest = _field(raw, "sha256", str)
    rows = _field(raw, "rows", int) if is_table else None
    if size < 0 or (rows is not None and rows < 0) or len(digest) != 64:
        raise _not_valid(f"its manifest entry for {name} is malformed")
    return MemberInfo(
        table_member(name) if is_table else file_member(name),
        size,
        digest,
        rows,
    )


def _parse_tables(raw: object) -> dict[str, MemberInfo]:
    if not isinstance(raw, dict):
        raise _not_valid("its manifest has no list of tables")
    tables: dict[str, MemberInfo] = {}
    for name, entry in raw.items():
        try:
            backup_table(name)
        except KeyError:
            raise BackupError(
                f"The backup holds data this version does not know ({name}). "
                "It was probably made by a newer version; update the add-on first."
            ) from None
        tables[name] = _member_info(name, entry, is_table=True)
    return tables


def _parse_files(raw: object) -> dict[str, MemberInfo]:
    if not isinstance(raw, dict):
        raise _not_valid("its manifest has no list of files")
    files: dict[str, MemberInfo] = {}
    for name, entry in raw.items():
        if classify_member(name) is None:
            raise BackupError(
                f"The backup holds a file this version does not know ({name}). "
                "It was probably made by a newer version; update the add-on first."
            )
        files[name] = _member_info(name, entry, is_table=False)
    return files


def parse_manifest(raw: bytes) -> BackupManifest:
    """The manifest in *raw*, or a :class:`BackupError` saying what is wrong."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise _not_valid("its manifest is not valid JSON") from None
    if not isinstance(document, dict):
        raise _not_valid("its manifest is not an object")
    if document.get("format") != FORMAT_NAME:
        raise _not_valid("it was not made by this add-on")
    version = _field(document, "format_version", int)
    if version > FORMAT_VERSION:
        raise BackupError(
            "This backup was made by a newer version of the add-on "
            f"(backup format {version}). Update the add-on, then try again."
        )
    if version < 1:
        raise _not_valid("its format version is not valid")
    return BackupManifest(
        addon_version=_field(document, "addon_version", str),
        created_at=_field(document, "created_at", str),
        clip_folder=_field(document, "clip_folder", str),
        tables=_parse_tables(document.get("tables")),
        files=_parse_files(document.get(_FILES_KEY)),
    )


def _check_structure(archive: zipfile.ZipFile, manifest: BackupManifest) -> None:
    """The ZIP holds exactly the manifest and the members it lists."""
    infos = [info for info in archive.infolist() if not info.is_dir()]
    names = [info.filename for info in infos]
    if len(names) > MAX_MEMBERS:
        raise _not_valid("it holds far more files than a backup does")
    if len(set(names)) != len(names):
        raise _not_valid("it holds the same file twice")
    if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
        raise _not_valid("it would expand to an unreasonable size")
    if any(info.flag_bits & _ENCRYPTED_FLAG for info in infos):
        raise _not_valid("it is password-protected")
    listed = {MANIFEST_MEMBER, *(member.name for member in manifest.members())}
    if set(names) != listed:
        extra = sorted(set(names) - listed)
        if extra:
            raise _not_valid(f"it holds {extra[0]}, which its manifest does not list")
        missing = sorted(listed - set(names))
        raise BackupError(f"The backup is incomplete: {missing[0]} is missing.")


def _check_member(archive: zipfile.ZipFile, member: MemberInfo) -> None:
    """Read *member* through once and compare it with what the manifest says."""
    if archive.getinfo(member.name).file_size != member.size:
        raise _damaged(member.name)
    digest = hashlib.sha256()
    lines = 0
    with archive.open(member.name) as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            lines += chunk.count(b"\n")
    if digest.hexdigest() != member.sha256:
        raise _damaged(member.name)
    if member.rows is not None and lines != member.rows:
        raise _damaged(member.name)


class BackupArchive:
    """A backup ZIP on disk that has been checked end to end."""

    def __init__(self, path: Path, manifest: BackupManifest) -> None:
        self.path = path
        self.manifest = manifest

    @classmethod
    def open(cls, path: Path) -> BackupArchive:
        """Open and verify the ZIP at *path*; ``BackupError`` if it will not do."""
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = cls._read_manifest(archive)
                _check_structure(archive, manifest)
                for member in manifest.members():
                    _check_member(archive, member)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise BackupError(
                "That file is not a readable backup. It may be damaged, "
                "or not a backup at all."
            ) from exc
        except RuntimeError as exc:  # NotImplementedError, an unknown method, is one
            raise _not_valid("it uses a ZIP feature backups never use") from exc
        return cls(path, manifest)

    @staticmethod
    def _read_manifest(archive: zipfile.ZipFile) -> BackupManifest:
        try:
            info = archive.getinfo(MANIFEST_MEMBER)
        except KeyError:
            raise _not_valid("it has no manifest.json") from None
        if info.file_size > MAX_MANIFEST_BYTES:
            raise _not_valid("its manifest is far larger than any real one")
        return parse_manifest(archive.read(MANIFEST_MEMBER))

    def iter_rows(
        self, table: str, size: int = BATCH_ROWS
    ) -> Generator[list[dict[str, Any]]]:
        """*table*'s records, *size* at a time; nothing if it is not in the backup."""
        member = self.manifest.tables.get(table)
        if member is None:
            return
        batch: list[dict[str, Any]] = []
        for line in self.iter_lines(member):
            batch.append(decode_row(line))
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch

    def iter_lines(self, member: MemberInfo) -> Iterator[bytes]:
        """The lines of *member*, none longer than a record may be."""
        with zipfile.ZipFile(self.path) as archive, archive.open(member.name) as handle:
            while line := handle.readline(MAX_RECORD_BYTES + 1):
                if len(line) > MAX_RECORD_BYTES:
                    raise BackupError(
                        "A record in the backup is far larger than any real one."
                    )
                yield line

    def read_file(self, name: str) -> bytes:
        """The whole of the file called *name* (relative to ``files/``)."""
        member = self.manifest.files[name]
        if member.size > MAX_FILE_BYTES:
            raise BackupError(f"{name} in the backup is too large to restore.")
        with zipfile.ZipFile(self.path) as archive:
            return archive.read(member.name)
