"""Tests for alert_actions: the signed "Not a threat" button and its handler."""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader import alert_actions
from blink_downloader.alert_actions import (
    ACTION_PREFIX,
    AlertActionHandler,
    AlertActionSigner,
)


def test_an_action_names_its_clip_and_verifies(tmp_path: Path) -> None:
    signer = AlertActionSigner(tmp_path / "key")

    action = signer.action_for("local_12_x")

    assert action is not None
    assert action.startswith(ACTION_PREFIX)
    assert action.endswith("_local_12_x")
    assert signer.clip_for(action) == "local_12_x"


def test_the_key_is_owner_only_and_survives_a_restart(tmp_path: Path) -> None:
    key = tmp_path / "key"
    action = AlertActionSigner(key).action_for("c1")

    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert len(key.read_bytes()) == 32
    # A button on a notification sent before a restart still works after it.
    assert AlertActionSigner(key).clip_for(action or "") == "c1"


def test_a_damaged_key_is_replaced(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_bytes(b"short")
    assert AlertActionSigner(key).action_for("c1") is not None
    assert len(key.read_bytes()) == 32


def test_the_default_key_lives_in_the_add_on_data_folder(data_dir: Path) -> None:
    """The autouse data_dir fixture points the module constant at a test
    folder; the signer must look it up at call time to honour that."""
    signer = AlertActionSigner()
    assert signer.action_for("c1") is not None
    assert alert_actions._KEY_FILE.exists()
    assert str(alert_actions._KEY_FILE).startswith(str(data_dir))


@pytest.mark.parametrize(
    "action",
    [
        "SOMETHING_ELSE",
        ACTION_PREFIX,
        f"{ACTION_PREFIX}0123456789abcdef",
        f"{ACTION_PREFIX}0123456789abcdef_",
        f"{ACTION_PREFIX}0123456789abcdefXc1",
        f"{ACTION_PREFIX}0123456789abcdef_c1",
        f"{ACTION_PREFIX}{'\u00e9' * 16}_c1",
    ],
    ids=[
        "foreign",
        "bare",
        "no-clip",
        "empty-clip",
        "no-separator",
        "forged",
        "non-ascii",
    ],
)
def test_anything_but_a_genuine_action_is_refused(tmp_path: Path, action: str) -> None:
    assert AlertActionSigner(tmp_path / "key").clip_for(action) is None


def test_a_signature_for_one_clip_does_not_open_another(tmp_path: Path) -> None:
    signer = AlertActionSigner(tmp_path / "key")
    genuine = signer.action_for("c1") or ""
    signature = genuine[len(ACTION_PREFIX) : len(ACTION_PREFIX) + 16]
    assert signer.clip_for(f"{ACTION_PREFIX}{signature}_c2") is None


def test_another_installs_key_does_not_verify(tmp_path: Path) -> None:
    action = AlertActionSigner(tmp_path / "a").action_for("c1") or ""
    assert AlertActionSigner(tmp_path / "b").clip_for(action) is None


def test_an_unwritable_key_means_no_button(tmp_path: Path) -> None:
    signer = AlertActionSigner(tmp_path / "missing-dir" / "key")
    assert signer.action_for("c1") is None
    assert signer.clip_for(f"{ACTION_PREFIX}0123456789abcdef_c1") is None


async def test_a_genuine_tap_records_not_a_threat(tmp_path: Path) -> None:
    signer = AlertActionSigner(tmp_path / "key")
    db = MagicMock()
    with patch.object(
        alert_actions, "record_not_a_threat", AsyncMock(return_value=True)
    ) as record:
        assert await AlertActionHandler(db, signer).handle(
            signer.action_for("c1") or ""
        )
    record.assert_awaited_once_with(db, "c1")


async def test_a_forged_tap_records_nothing(tmp_path: Path) -> None:
    signer = AlertActionSigner(tmp_path / "key")
    with patch.object(alert_actions, "record_not_a_threat", AsyncMock()) as record:
        handled = await AlertActionHandler(MagicMock(), signer).handle(
            f"{ACTION_PREFIX}0123456789abcdef_c1"
        )
    assert handled is False
    record.assert_not_awaited()


async def test_a_tap_for_a_clip_with_no_verdict_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=alert_actions.__name__)
    signer = AlertActionSigner(tmp_path / "key")
    with patch.object(
        alert_actions, "record_not_a_threat", AsyncMock(return_value=False)
    ):
        handled = await AlertActionHandler(MagicMock(), signer).handle(
            signer.action_for("gone") or ""
        )
    assert handled is False
    assert "no stored verdict" in caplog.text


async def test_a_database_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    signer = AlertActionSigner(tmp_path / "key")
    with patch.object(
        alert_actions, "record_not_a_threat", AsyncMock(side_effect=RuntimeError("db"))
    ):
        handled = await AlertActionHandler(MagicMock(), signer).handle(
            signer.action_for("c1") or ""
        )
    assert handled is False
    assert "Could not record Not a threat" in caplog.text
