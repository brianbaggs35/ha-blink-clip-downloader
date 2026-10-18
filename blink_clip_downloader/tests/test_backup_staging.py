"""Tests for holding an uploaded backup between the steps of a restore."""

from __future__ import annotations

import asyncio
from collections import namedtuple
from pathlib import Path

import pytest

from blink_downloader.backup import staging
from blink_downloader.backup.staging import (
    CHUNK_BYTES,
    MAX_CHUNK_BYTES,
    MAX_UPLOAD_BYTES,
    RestoreStaging,
    UploadError,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def held(tmp_path: Path, clock: _Clock) -> RestoreStaging:
    return RestoreStaging(ttl=60, clock=clock, directory=tmp_path)


def test_pieces_stay_well_under_what_ingress_will_carry() -> None:
    """Supervisor reads a whole request body and refuses one over 16 MiB."""
    assert CHUNK_BYTES < MAX_CHUNK_BYTES < 10 * 1024**2 < 16 * 1024**2


def test_begin_opens_an_empty_file_for_the_upload(held: RestoreStaging) -> None:
    upload = held.begin(10)
    assert upload.path.exists()
    assert upload.path.stat().st_size == 0
    assert (upload.size, upload.received, upload.archive) == (10, 0, None)
    assert held.get(upload.upload_id) is upload


@pytest.mark.parametrize(
    ("size", "status"), [(0, 400), (-5, 400), (MAX_UPLOAD_BYTES + 1, 413)]
)
def test_begin_refuses_a_size_no_backup_has(
    held: RestoreStaging, size: int, status: int
) -> None:
    with pytest.raises(UploadError) as raised:
        held.begin(size)
    assert raised.value.status == status


def test_begin_refuses_a_file_the_disk_cannot_hold(
    held: RestoreStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(staging.shutil, "disk_usage", lambda _p: usage(100, 100, 5))
    with pytest.raises(UploadError, match="not enough free space") as raised:
        held.begin(10)
    assert raised.value.status == 507


def test_beginning_another_upload_deletes_the_first(held: RestoreStaging) -> None:
    first = held.begin(10)
    second = held.begin(10)
    assert not first.path.exists()
    assert second.path.exists()
    with pytest.raises(UploadError) as raised:
        held.get(first.upload_id)
    assert raised.value.status == 404


def test_an_upload_nobody_started_is_not_found(held: RestoreStaging) -> None:
    with pytest.raises(UploadError, match="no longer available") as raised:
        held.get("nope")
    assert raised.value.status == 404


def test_an_upload_expires_after_an_hour_of_silence(
    held: RestoreStaging, clock: _Clock
) -> None:
    upload = held.begin(10)
    clock.now += 61
    with pytest.raises(UploadError) as raised:
        held.get(upload.upload_id)
    assert raised.value.status == 404
    assert not upload.path.exists()


def test_asking_for_an_upload_keeps_it_alive(
    held: RestoreStaging, clock: _Clock
) -> None:
    upload = held.begin(10)
    for _ in range(3):
        clock.now += 50
        assert held.get(upload.upload_id) is upload


async def test_pieces_in_order_build_the_file(held: RestoreStaging) -> None:
    upload = held.begin(10)
    assert await held.append(upload.upload_id, 0, b"hello") == 5
    assert await held.append(upload.upload_id, 5, b"world") == 10
    assert upload.path.read_bytes() == b"helloworld"
    assert upload.received == 10


async def test_a_piece_out_of_order_is_told_where_to_resume(
    held: RestoreStaging,
) -> None:
    """A retry after a dropped connection must not be written twice."""
    upload = held.begin(10)
    await held.append(upload.upload_id, 0, b"hello")
    with pytest.raises(UploadError) as raised:
        await held.append(upload.upload_id, 0, b"hello")
    assert raised.value.status == 409
    assert raised.value.received == 5
    with pytest.raises(UploadError) as skipped:
        await held.append(upload.upload_id, 9, b"x")
    assert skipped.value.received == 5
    assert upload.path.read_bytes() == b"hello"


@pytest.mark.parametrize(
    ("data", "offset"), [(b"", 0), (b"x" * 11, 0)], ids=["empty", "past the end"]
)
async def test_a_piece_that_does_not_fit_is_refused(
    held: RestoreStaging, data: bytes, offset: int
) -> None:
    upload = held.begin(10)
    with pytest.raises(UploadError) as raised:
        await held.append(upload.upload_id, offset, data)
    assert raised.value.status == 400
    assert upload.received == 0


async def test_two_pieces_racing_for_one_offset_are_written_once(
    held: RestoreStaging,
) -> None:
    upload = held.begin(10)
    results = await asyncio.gather(
        held.append(upload.upload_id, 0, b"aaaaa"),
        held.append(upload.upload_id, 0, b"bbbbb"),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, UploadError)]
    assert len(failures) == 1
    assert failures[0].status == 409
    assert upload.received == 5
    assert upload.path.read_bytes() in (b"aaaaa", b"bbbbb")


def test_discard_removes_the_file_and_is_safe_to_repeat(held: RestoreStaging) -> None:
    upload = held.begin(10)
    held.discard()
    held.discard()
    assert not upload.path.exists()
    with pytest.raises(UploadError):
        held.get(upload.upload_id)


def test_the_default_directory_is_the_systems_temp_dir() -> None:
    held = RestoreStaging()
    upload = held.begin(10)
    try:
        assert upload.path.parent == Path(staging.tempfile.gettempdir())
    finally:
        held.discard()
