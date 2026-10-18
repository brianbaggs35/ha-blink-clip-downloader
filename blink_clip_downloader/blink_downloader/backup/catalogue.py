"""What a backup holds besides the database, and what it deliberately leaves out.

The database's tables are listed in :mod:`blink_downloader.database.backup`;
this is the add-on's own state under ``/data``. Every path the package keeps
there is either carried (``DATA_FILES``/``DATA_DIRS``) or named in
``LEFT_OUT`` with a reason, and ``tests/test_backup_catalogue.py`` walks the
package to fail the moment a new one is neither — the same way conftest's
``data_dir`` fixture finds them. A file added later therefore cannot be
silently missing from a backup.

``DATA_DIR`` is a module constant, not a literal in a function, for the
reason ``tests/test_data_dir.py`` spells out: the suite redirects every
``/data`` path it can find, and one it cannot find writes the real volume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DATA_DIR = Path("/data")

#: How a restore treats a file. A ``settings`` file is written only where
#: there is none, unless told to replace it; an ``event_log`` is merged line
#: by line; ``download_history`` goes to the running downloader, which would
#: write its own copy back over a file replaced behind its back. (Pictures
#: are not files of their own: they are a :class:`DataDir`, handled like a
#: setting, one picture at a time.)
FileRole = Literal["settings", "event_log", "download_history"]
_SETTINGS: FileRole = "settings"


@dataclass(frozen=True)
class DataFile:
    """One file under ``/data`` that a backup carries."""

    name: str
    label: str
    role: FileRole
    #: Its name inside the archive, when that differs from the file's own.
    member: str = ""

    @property
    def path(self) -> Path:
        return DATA_DIR / self.name

    @property
    def archive_name(self) -> str:
        return self.member or self.name


@dataclass(frozen=True)
class DataDir:
    """A directory under ``/data`` of per-camera reference pictures."""

    name: str
    label: str

    @property
    def path(self) -> Path:
        return DATA_DIR / self.name


DATA_FILES: tuple[DataFile, ...] = (
    DataFile(
        "camera_configs.json",
        "Camera settings (descriptions, prompts, car zones)",
        _SETTINGS,
    ),
    DataFile("camera_name_aliases.json", "Camera rename history", _SETTINGS),
    DataFile("vehicle_settings.json", "Protected vehicle description", _SETTINGS),
    DataFile("protected_assets.json", "Protected assets", _SETTINGS),
    DataFile("security_feed_settings.json", "Security Feed layout", _SETTINGS),
    DataFile("finetune_state.json", "Moondream fine-tune selection", _SETTINGS),
    DataFile("clip_manifest.json", "Clip manifest (download log)", "event_log"),
    DataFile(
        "downloaded_clips.json",
        "Download history (clips already fetched from Blink)",
        "download_history",
        member="downloaded_clip_ids.json",
    ),
)

DATA_DIRS: tuple[DataDir, ...] = (
    DataDir("asset_snapshots", "Asset reference pictures"),
    DataDir("vehicle_zone_snapshots", "Vehicle zone reference pictures"),
)

#: What the pictures in ``DATA_DIRS`` may be called. Both directories are
#: written as a slug of the camera name plus a hash, so nothing else — no
#: separator, no ``..`` — can be a legitimate name, and a restore refuses
#: anything that does not look like one.
PICTURE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,250}\.jpg")

#: Paths under ``/data`` a backup leaves out, and why. A directory covers
#: everything beneath it.
LEFT_OUT: dict[str, str] = {
    "options.json": "The add-on's own settings, which Home Assistant keeps and restores itself.",
    "auth_credentials.json": "Your Blink sign-in. Credentials never go into a backup; sign in again.",
    "blink_hardware_id.txt": "The device identity Blink knows this install by; part of the Blink sign-in.",
    "two_fa_code.txt": "A one-time code, deleted as soon as it is used.",
    "google_drive_credentials.json": "Your Google Drive sign-in; reconnect on the Storage tab.",
    "google_drive_settings.json": "Your Google OAuth client secret; enter it again on the Storage tab.",
    "web_access.json": "The secret that signs browsers in and the access token Home Assistant's calls carry.",
    "alert_action_key": "The key that signs the Not a threat button on alerts; made again when needed.",
    "camera_identities.json": "Rebuilt from your Blink account on the next poll.",
    "stats.json": "Rebuilt from the download history on the next poll.",
    "last_digest.json": "Only remembers when the last daily digest went out.",
    "trigger_download": "A one-poll signal, not state.",
    "clip_library.db": "The pre-5.0.0 library, imported once at upgrade and not used since.",
    "moondream_packages": "Software installed from the AI tab; install it there again.",
    "model_cache": "Downloaded models; fetched again the first time they are needed.",
    "postgresql": "The raw database files; the records are carried instead, in a readable form.",
}


def picture_member(directory: DataDir, leaf: str) -> str:
    """The archive-relative name of picture *leaf* in *directory*."""
    return f"{directory.name}/{leaf}"


def classify_member(name: str) -> DataFile | DataDir | None:
    """Which catalogue entry the archive-relative file *name* belongs to.

    ``None`` for anything that is not exactly one of them: a restore carries
    nothing it was not built to carry, whatever an archive claims.
    """
    for entry in DATA_FILES:
        if entry.archive_name == name:
            return entry
    parent, _, leaf = name.partition("/")
    for directory in DATA_DIRS:
        if directory.name == parent and PICTURE_NAME.fullmatch(leaf):
            return directory
    return None
