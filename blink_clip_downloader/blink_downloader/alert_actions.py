"""The "Not a threat" button on phone alerts, and what tapping it does.

The companion app reports a tapped button by firing Home Assistant's
``mobile_app_notification_action`` event, carrying the button's ``action``
string back verbatim; ``event_watcher.py`` hears it over the WebSocket the
add-on already holds, so nothing reaches the add-on's own port. The action
string names the clip and carries a signature made with a key only this
add-on holds: anyone able to fire an event in Home Assistant could otherwise
mark any clip "not a threat", and feedback is exactly what teaches a camera
to alert less.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from .verdict_feedback import record_not_a_threat

if TYPE_CHECKING:
    from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)

ACTION_PREFIX = "BLINK_NOT_A_THREAT_"
# Hex characters of the signature kept in the action string: 64 bits is far
# beyond guessing through Home Assistant's event API, and short enough that
# the string stays readable in Home Assistant's event log.
_SIGNATURE_CHARS = 16
_KEY_BYTES = 32
_KEY_FILE = Path("/data/alert_action_key")


class AlertActionSigner:
    """Builds and checks the action strings behind "Not a threat"."""

    def __init__(self, key_file: Path | None = None) -> None:
        # None looks the module default up at call time, so tests can redirect it.
        self._key_file = key_file
        self._key: bytes | None = None

    def _path(self) -> Path:
        return self._key_file if self._key_file is not None else _KEY_FILE

    def _load_key(self) -> bytes:
        """This install's signing key, created on first use (owner-only)."""
        if self._key is not None:
            return self._key
        path = self._path()
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = b""
        if len(key) != _KEY_BYTES:
            key = secrets.token_bytes(_KEY_BYTES)
            tmp = path.with_name(f".{path.name}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
            tmp.replace(path)
        self._key = key
        return key

    def _signature(self, clip_id: str) -> str:
        digest = hmac.new(self._load_key(), clip_id.encode(), hashlib.sha256)
        return digest.hexdigest()[:_SIGNATURE_CHARS]

    def action_for(self, clip_id: str) -> str | None:
        """The action string for *clip_id*'s button; None if no key can be made."""
        try:
            return f"{ACTION_PREFIX}{self._signature(clip_id)}_{clip_id}"
        except OSError as exc:
            _LOGGER.warning("Could not create the alert action key: %s", exc)
            return None

    def clip_for(self, action: str) -> str | None:
        """The clip a genuine action string names; None for anything else."""
        if not action.startswith(ACTION_PREFIX):
            return None
        rest = action[len(ACTION_PREFIX) :]
        signature, sep, clip_id = (
            rest[:_SIGNATURE_CHARS],
            rest[_SIGNATURE_CHARS : _SIGNATURE_CHARS + 1],
            rest[_SIGNATURE_CHARS + 1 :],
        )
        if sep != "_" or not clip_id:
            return None
        try:
            expected = self._signature(clip_id)
        except OSError as exc:
            _LOGGER.warning("Could not read the alert action key: %s", exc)
            return None
        # As bytes: compare_digest raises on a str holding non-ASCII text.
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            _LOGGER.warning("Ignored a Not a threat action with a bad signature")
            return None
        return clip_id


class AlertActionHandler:
    """Turns a tapped "Not a threat" into the Library's thumbs-down."""

    def __init__(self, db: ClipDatabase, signer: AlertActionSigner) -> None:
        self._db = db
        self._signer = signer

    async def handle(self, action: str) -> bool:
        """Record feedback for a genuine action; True when something was saved.

        Never raises: a failure here is logged, since the only thing waiting
        on it is Home Assistant's event stream.
        """
        clip_id = self._signer.clip_for(action)
        if clip_id is None:
            return False
        try:
            saved = await record_not_a_threat(self._db, clip_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Could not record Not a threat for clip %s: %s", clip_id, exc
            )
            return False
        if saved:
            _LOGGER.info("Clip %s marked not a threat from a phone alert", clip_id)
        else:
            _LOGGER.info(
                "Not a threat tapped for clip %s, which has no stored verdict", clip_id
            )
        return saved
