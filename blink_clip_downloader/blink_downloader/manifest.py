"""JSON clip manifest — keeps a record of every downloaded clip with metadata."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_MANIFEST_FILE = Path("/data/clip_manifest.json")


class ClipManifest:
    """Appends clip metadata to a line-delimited JSON file (one object per line).

    Using newline-delimited JSON means we can append without re-writing the
    entire file, which keeps the operation O(1) regardless of file size.
    """

    def __init__(self, manifest_file: Path = DEFAULT_MANIFEST_FILE) -> None:
        self._file = manifest_file

    def append(self, clip_result: dict[str, Any]) -> None:
        """Append one clip record to the manifest file."""
        record = {
            **clip_result,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        try:
            with self._file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            _LOGGER.warning("Could not write to manifest %s: %s", self._file, exc)

    def read_all(self) -> list[dict[str, Any]]:
        """Return all manifest records as a list (for inspection / testing)."""
        if not self._file.exists():
            return []
        records = []
        for line in self._file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

    def count(self) -> int:
        """Return the number of records in the manifest."""
        return len(self.read_all())
