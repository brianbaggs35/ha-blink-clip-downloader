"""Holding an uploaded backup between the steps of a restore.

A restore is several requests — send the file, look at what is in it,
confirm — so the file has to be kept somewhere in between, and the browser
only ever holds an opaque id for it. Two facts about how it arrives shape
this:

* **It arrives in pieces.** Home Assistant's ingress proxy reads a request's
  whole body, and Supervisor refuses one over 16 MiB (``MAX_CLIENT_SIZE`` in
  its ``api/__init__.py``), so a backup sent as one request would fail in the
  Home Assistant sidebar while working on the direct port. The page slices
  the file into ``CHUNK_BYTES`` pieces and sends them in order; each piece
  names the offset it starts at, so a retry after a dropped connection is
  recognised (and told where to resume) instead of being written twice.
* **Only one is kept.** Starting another upload, finishing a restore, or an
  hour of silence removes the file, so abandoned uploads cannot fill the
  disk.

Nothing here reads the archive; :class:`~.archive.BackupArchive` does that,
and is stored on the upload once it has been checked, which is what lets the
restore step refuse a file nobody has looked over.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .archive import BackupArchive

#: The largest backup file accepted. Real ones are far smaller.
MAX_UPLOAD_BYTES = 1024**3
#: How big the pieces are that the page sends. Well under both Supervisor's
#: 16 MiB and this server's own 10 MiB request limit.
CHUNK_BYTES = 4 * 1024**2
MAX_CHUNK_BYTES = 8 * 1024**2
#: Free space to leave over and above the upload itself.
_SPARE_BYTES = 64 * 1024**2
UPLOAD_TTL_SECONDS = 3600.0

_GONE = "That upload is no longer available. Choose the file again."


class UploadError(Exception):
    """A problem with the upload itself, with the HTTP status that suits it.

    ``received`` accompanies an out-of-order piece, so the page can carry on
    from where the server actually is.
    """

    def __init__(self, message: str, status: int, received: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.received = received


@dataclass
class StagedUpload:
    """A backup file on its way in, or in and waiting."""

    upload_id: str
    path: Path
    size: int
    touched: float
    received: int = 0
    #: Set once the file has been opened and verified; the restore step
    #: refuses an upload without it.
    archive: BackupArchive | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


class RestoreStaging:
    """The single upload a restore is working from."""

    def __init__(
        self,
        *,
        ttl: float = UPLOAD_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        directory: Path | None = None,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._directory = directory
        self._current: StagedUpload | None = None

    def begin(self, size: int) -> StagedUpload:
        """Start receiving a file of *size* bytes, dropping any earlier one."""
        if size <= 0:
            raise UploadError("The file is empty.", 400)
        if size > MAX_UPLOAD_BYTES:
            raise UploadError(
                f"That file is larger than the {MAX_UPLOAD_BYTES // 1024**3} GiB "
                "a backup can be.",
                413,
            )
        self.discard()
        directory = self._directory or Path(tempfile.gettempdir())
        if shutil.disk_usage(directory).free < size + _SPARE_BYTES:
            raise UploadError(
                "There is not enough free space to receive that file.", 507
            )
        handle, name = tempfile.mkstemp(
            prefix="blink-restore-", suffix=".zip", dir=directory
        )
        os.close(handle)
        self._current = StagedUpload(
            secrets.token_urlsafe(16), Path(name), size, self._clock()
        )
        return self._current

    def get(self, upload_id: str) -> StagedUpload:
        """The upload called *upload_id*, kept alive by being asked for."""
        upload = self._current
        if upload is None or upload.upload_id != upload_id:
            raise UploadError(_GONE, 404)
        if self._clock() - upload.touched > self._ttl:
            self.discard()
            raise UploadError(_GONE, 404)
        upload.touched = self._clock()
        return upload

    async def append(self, upload_id: str, offset: int, data: bytes) -> int:
        """Add the piece that starts at *offset*; returns bytes received so far."""
        upload = self.get(upload_id)
        # Held across the write: two pieces for one offset must not both pass
        # the check below before either has been written.
        async with upload.lock:
            if offset != upload.received:
                raise UploadError(
                    "That part is out of order; carrying on from where the server is.",
                    409,
                    upload.received,
                )
            if len(data) > MAX_CHUNK_BYTES:
                raise UploadError("That part is larger than a part may be.", 413)
            if not data or upload.received + len(data) > upload.size:
                raise UploadError("That part does not fit the file being sent.", 400)
            await asyncio.to_thread(_append, upload.path, data)
            upload.received += len(data)
            return upload.received

    def discard(self) -> None:
        """Delete whatever upload is held."""
        upload, self._current = self._current, None
        if upload is not None:
            upload.path.unlink(missing_ok=True)
