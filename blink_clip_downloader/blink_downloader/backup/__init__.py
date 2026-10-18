"""Backup & Restore: a portable copy of everything the add-on has learned.

A backup is one ZIP holding the clip library and its AI analysis (as readable
records, not the database's own files), the household's enrolled faces unless
left out, what each camera has learned, and the settings under ``/data``. It
never holds a password or sign-in, and it does not hold the videos, which are
too large and have a home of their own (``/share``).

    catalogue   what is carried besides the database, and what is left out
    archive     the ZIP's layout, and a reader that trusts nothing in it
    export      making a backup
    restore     previewing and applying one, adding only what is missing
    staging     holding an uploaded file between the steps of a restore

The database side — the table list, the consistent snapshot, the one-
transaction merge — is :mod:`blink_downloader.database.backup`. The web API is
:mod:`blink_downloader.media_server.backup`.
"""

from __future__ import annotations

from .archive import BackupArchive, BackupError, BackupManifest
from .export import create_backup, preview_backup
from .restore import (
    RestoreHooks,
    RestoreResult,
    preview_files,
    preview_restore,
    restore_backup,
    summarize,
)
from .staging import (
    CHUNK_BYTES,
    MAX_CHUNK_BYTES,
    RestoreStaging,
    StagedUpload,
    UploadError,
)

__all__ = [
    "CHUNK_BYTES",
    "MAX_CHUNK_BYTES",
    "BackupArchive",
    "BackupError",
    "BackupManifest",
    "RestoreHooks",
    "RestoreResult",
    "RestoreStaging",
    "StagedUpload",
    "UploadError",
    "create_backup",
    "preview_backup",
    "preview_files",
    "preview_restore",
    "restore_backup",
    "summarize",
]
