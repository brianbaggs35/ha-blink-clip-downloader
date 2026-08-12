"""Tests for GDriveClient."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import aiohttp
import pytest

from blink_downloader.gdrive_client import DriveFolder, GDriveClient


def _mock_response(
    status: int, json_data: dict | None = None, headers: dict | None = None
) -> AsyncMock:
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.headers = headers or {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class _RaiseOnCall:
    """Sentinel: session.<method>(...) itself raises *exc*, simulating a
    network-level failure rather than an HTTP error response."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


def _mock_session(**method_responses: Any) -> MagicMock:
    """Build a mock aiohttp.ClientSession. Pass e.g. post=resp, get=resp —
    each becomes session.<method> = MagicMock(return_value=resp), or wrap in
    _RaiseOnCall(exc) to simulate a network-level error instead."""
    s = MagicMock()
    s.closed = False
    for method, resp in method_responses.items():
        if isinstance(resp, _RaiseOnCall):
            setattr(s, method, MagicMock(side_effect=resp.exc))
        else:
            setattr(s, method, MagicMock(return_value=resp))
    return s


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GDriveClient:
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.CREDENTIALS_FILE", tmp_path / "creds.json"
    )
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.SETTINGS_FILE", tmp_path / "settings.json"
    )
    return GDriveClient()


def _configure(c: GDriveClient) -> GDriveClient:
    c.set_settings("client-id-123", "client-secret-abc", "archived_only")
    return c


def _connect(c: GDriveClient) -> GDriveClient:
    """Pre-set a valid, non-expired token so _ensure_valid_token()'s fast
    path returns True without needing an HTTP call for every single test."""
    c._access_token = "valid-token"
    c._refresh_token = "refresh-token"
    c._expires_at = time.time() + 3600
    c.connected = True
    return c


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------


def test_not_configured_by_default(client: GDriveClient) -> None:
    assert client.is_configured is False
    assert client.has_client_secret is False
    assert client.client_id == ""


def test_set_settings_persists_and_updates_in_memory(client: GDriveClient) -> None:
    client.set_settings("cid", "csecret", "all_clips")

    assert client.client_id == "cid"
    assert client.has_client_secret is True
    assert client.backup_policy == "all_clips"
    assert client.is_configured is True

    from blink_downloader.gdrive_client import SETTINGS_FILE

    saved = json.loads(SETTINGS_FILE.read_text())
    assert saved == {
        "client_id": "cid",
        "client_secret": "csecret",
        "backup_policy": "all_clips",
    }


def test_set_settings_keeps_existing_secret_when_none_given(
    client: GDriveClient,
) -> None:
    client.set_settings("cid", "original-secret", "archived_only")
    client.set_settings("cid", None, "all_clips")

    assert client.has_client_secret is True
    assert client.backup_policy == "all_clips"
    from blink_downloader.gdrive_client import SETTINGS_FILE

    saved = json.loads(SETTINGS_FILE.read_text())
    assert saved["client_secret"] == "original-secret"


def test_set_settings_invalid_policy_falls_back_to_archived_only(
    client: GDriveClient,
) -> None:
    client.set_settings("cid", "csecret", "not-a-real-policy")
    assert client.backup_policy == "archived_only"


def test_set_settings_oserror_propagates_after_updating_in_memory(
    client: GDriveClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", _raise)
    with pytest.raises(OSError):
        client.set_settings("cid", "csecret", "archived_only")

    # The in-memory update happens before the write attempt (see
    # set_settings's docstring), so the change still takes effect for the
    # rest of this session even though persisting it failed — the caller
    # (media_server.py's _handle_gdrive_settings_put) is responsible for
    # turning the propagated OSError into an honest failure response.
    assert client.client_id == "cid"
    assert client.has_client_secret is True


def test_load_settings_from_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.CREDENTIALS_FILE", tmp_path / "creds.json"
    )
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.SETTINGS_FILE", tmp_path / "settings.json"
    )
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "client_id": "existing",
                "client_secret": "s3cr3t",
                "backup_policy": "all_clips",
            }
        )
    )

    c = GDriveClient()
    assert c.client_id == "existing"
    assert c.has_client_secret is True
    assert c.backup_policy == "all_clips"


def test_load_settings_invalid_json_logs_and_leaves_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.CREDENTIALS_FILE", tmp_path / "creds.json"
    )
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.SETTINGS_FILE", tmp_path / "settings.json"
    )
    (tmp_path / "settings.json").write_text("{not valid json")

    with caplog.at_level("WARNING"):
        c = GDriveClient()

    assert c.client_id == ""
    assert "Could not load Google Drive settings" in caplog.text


# ------------------------------------------------------------------
# Credential persistence
# ------------------------------------------------------------------


def test_load_credentials_missing_file_leaves_defaults(client: GDriveClient) -> None:
    assert client.connected is False
    assert client.account_email == ""


def test_load_credentials_from_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.CREDENTIALS_FILE", tmp_path / "creds.json"
    )
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.SETTINGS_FILE", tmp_path / "settings.json"
    )
    (tmp_path / "creds.json").write_text(
        json.dumps(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_at": 123.0,
                "account_email": "me@example.com",
                "folder_id": "f1",
                "folder_name": "Blink Clips",
            }
        )
    )

    c = GDriveClient()
    assert c.connected is True
    assert c.account_email == "me@example.com"
    assert c.folder_id == "f1"
    assert c.folder_name == "Blink Clips"


def test_load_credentials_invalid_json_logs_and_leaves_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.CREDENTIALS_FILE", tmp_path / "creds.json"
    )
    monkeypatch.setattr(
        "blink_downloader.gdrive_client.SETTINGS_FILE", tmp_path / "settings.json"
    )
    (tmp_path / "creds.json").write_text("{not valid")

    with caplog.at_level("WARNING"):
        c = GDriveClient()

    assert c.connected is False
    assert "Could not load Google Drive credentials" in caplog.text


def test_persist_credentials_writes_atomically(client: GDriveClient) -> None:
    from blink_downloader.gdrive_client import CREDENTIALS_FILE

    client.select_folder("f1", "Blink Clips")

    assert CREDENTIALS_FILE.exists()
    assert not CREDENTIALS_FILE.with_suffix(".json.tmp").exists()
    saved = json.loads(CREDENTIALS_FILE.read_text())
    assert saved["folder_id"] == "f1"
    assert saved["folder_name"] == "Blink Clips"


def test_persist_credentials_oserror_is_logged(
    client: GDriveClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _raise(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", _raise)
    with caplog.at_level("WARNING"):
        client.select_folder("f1", "Blink Clips")

    assert "Could not persist Google Drive credentials" in caplog.text


def test_clear_credentials_removes_file_and_resets_state(client: GDriveClient) -> None:
    from blink_downloader.gdrive_client import CREDENTIALS_FILE

    _connect(client)
    client._account_email = "me@example.com"
    client._persist_credentials()
    assert CREDENTIALS_FILE.exists()

    client._clear_credentials()

    assert client.connected is False
    assert client.account_email == ""
    assert client.folder_id == ""
    assert not CREDENTIALS_FILE.exists()


# ------------------------------------------------------------------
# Device flow
# ------------------------------------------------------------------


async def test_start_device_flow_not_configured_returns_none(
    client: GDriveClient,
) -> None:
    assert await client.start_device_flow() is None


async def test_start_device_flow_success(client: GDriveClient) -> None:
    _configure(client)
    resp = _mock_response(
        200,
        {
            "device_code": "dc123",
            "user_code": "ABCD-1234",
            "verification_url": "https://google.com/device",
            "expires_in": 1800,
            "interval": 5,
        },
    )
    client._session = _mock_session(post=resp)

    info = await client.start_device_flow()

    assert info is not None
    assert info.device_code == "dc123"
    assert info.user_code == "ABCD-1234"
    assert info.verification_url == "https://google.com/device"
    assert info.expires_in == 1800
    assert info.interval == 5


async def test_start_device_flow_uses_verification_uri_fallback(
    client: GDriveClient,
) -> None:
    """Google's docs use both verification_url and verification_uri across
    endpoints/versions — accept either key."""
    _configure(client)
    resp = _mock_response(
        200,
        {
            "device_code": "dc",
            "user_code": "U",
            "verification_uri": "https://google.com/device",
            "expires_in": 1800,
            "interval": 5,
        },
    )
    client._session = _mock_session(post=resp)

    info = await client.start_device_flow()
    assert info is not None
    assert info.verification_url == "https://google.com/device"


async def test_start_device_flow_http_error_returns_none(client: GDriveClient) -> None:
    _configure(client)
    resp = _mock_response(400, {"error": "invalid_client"})
    client._session = _mock_session(post=resp)

    assert await client.start_device_flow() is None


async def test_start_device_flow_network_error_returns_none(
    client: GDriveClient,
) -> None:
    _configure(client)
    client._session = _mock_session(
        post=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client.start_device_flow() is None


# ------------------------------------------------------------------
# Token polling
# ------------------------------------------------------------------


async def test_poll_once_for_token_success(client: GDriveClient) -> None:
    _configure(client)
    token_resp = _mock_response(
        200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}
    )
    email_resp = _mock_response(200, {"user": {"emailAddress": "me@example.com"}})
    client._session = _mock_session(post=token_resp, get=email_resp)

    result = await client.poll_once_for_token("dc123")

    assert result.status == "success"
    assert client.connected is True
    assert client.account_email == "me@example.com"
    from blink_downloader.gdrive_client import CREDENTIALS_FILE

    assert CREDENTIALS_FILE.exists()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        ("authorization_pending", "pending"),
        ("slow_down", "slow_down"),
        ("expired_token", "expired"),
        ("access_denied", "denied"),
    ],
)
async def test_poll_once_for_token_known_errors(
    client: GDriveClient, error: str, expected_status: str
) -> None:
    _configure(client)
    resp = _mock_response(400, {"error": error})
    client._session = _mock_session(post=resp)

    result = await client.poll_once_for_token("dc123")
    assert result.status == expected_status
    assert client.connected is False


async def test_poll_once_for_token_unknown_error(client: GDriveClient) -> None:
    _configure(client)
    resp = _mock_response(
        400, {"error": "something_weird", "error_description": "Odd failure"}
    )
    client._session = _mock_session(post=resp)

    result = await client.poll_once_for_token("dc123")
    assert result.status == "error"
    assert result.message == "Odd failure"


async def test_poll_once_for_token_network_error(client: GDriveClient) -> None:
    _configure(client)
    client._session = _mock_session(
        post=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    result = await client.poll_once_for_token("dc123")
    assert result.status == "error"


async def test_poll_once_for_token_email_fetch_failure_does_not_break_success(
    client: GDriveClient,
) -> None:
    """A transient failure fetching the account email must not turn an
    otherwise-successful token exchange into a failure — email is cosmetic."""
    _configure(client)
    token_resp = _mock_response(
        200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600}
    )
    client._session = _mock_session(
        post=token_resp, get=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    result = await client.poll_once_for_token("dc123")
    assert result.status == "success"
    assert client.account_email == ""


# ------------------------------------------------------------------
# Token refresh (_ensure_valid_token)
# ------------------------------------------------------------------


async def test_ensure_valid_token_no_refresh_token_returns_false(
    client: GDriveClient,
) -> None:
    assert await client._ensure_valid_token() is False


async def test_ensure_valid_token_already_valid_skips_http_call(
    client: GDriveClient,
) -> None:
    _connect(client)
    client._session = _mock_session()  # no methods configured — any call would error

    assert await client._ensure_valid_token() is True


async def test_ensure_valid_token_refreshes_when_expired(client: GDriveClient) -> None:
    client._refresh_token = "old-refresh"
    client._expires_at = time.time() - 10  # already expired
    resp = _mock_response(
        200, {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 3600}
    )
    client._session = _mock_session(post=resp)

    assert await client._ensure_valid_token() is True
    assert client._access_token == "new-at"
    assert client._refresh_token == "new-rt"


async def test_ensure_valid_token_preserves_refresh_token_when_response_omits_it(
    client: GDriveClient,
) -> None:
    """Regression test: Google's refresh-grant response frequently omits
    refresh_token entirely. Naively overwriting the stored value with a
    missing key would silently erase it and brick the connection on the
    *next* refresh attempt."""
    client._refresh_token = "must-survive"
    client._expires_at = time.time() - 10
    resp = _mock_response(
        200, {"access_token": "new-at", "expires_in": 3600}
    )  # no refresh_token key
    client._session = _mock_session(post=resp)

    assert await client._ensure_valid_token() is True
    assert client._refresh_token == "must-survive"


async def test_ensure_valid_token_invalid_grant_clears_credentials(
    client: GDriveClient,
) -> None:
    _connect(client)
    client._expires_at = time.time() - 10
    client._persist_credentials()
    resp = _mock_response(400, {"error": "invalid_grant"})
    client._session = _mock_session(post=resp)

    assert await client._ensure_valid_token() is False
    assert client.connected is False
    assert client._refresh_token == ""


async def test_ensure_valid_token_other_error_returns_false_without_clearing(
    client: GDriveClient,
) -> None:
    _connect(client)
    client._expires_at = time.time() - 10
    resp = _mock_response(500, {"error": "server_error"})
    client._session = _mock_session(post=resp)

    assert await client._ensure_valid_token() is False
    assert client._refresh_token == "refresh-token"  # not cleared — transient error


async def test_ensure_valid_token_network_error_returns_false(
    client: GDriveClient,
) -> None:
    _connect(client)
    client._expires_at = time.time() - 10
    client._session = _mock_session(
        post=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client._ensure_valid_token() is False


# ------------------------------------------------------------------
# Disconnect
# ------------------------------------------------------------------


async def test_disconnect_revokes_and_clears(client: GDriveClient) -> None:
    _connect(client)
    client._persist_credentials()
    resp = _mock_response(200)
    client._session = _mock_session(post=resp)

    await client.disconnect()

    assert client.connected is False
    from blink_downloader.gdrive_client import CREDENTIALS_FILE

    assert not CREDENTIALS_FILE.exists()


async def test_disconnect_revoke_failure_still_clears_locally(
    client: GDriveClient,
) -> None:
    _connect(client)
    client._session = _mock_session(
        post=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    await client.disconnect()
    assert client.connected is False


async def test_disconnect_with_no_token_skips_revoke_call(client: GDriveClient) -> None:
    session = _mock_session()
    client._session = session
    await client.disconnect()
    session.post.assert_not_called()


# ------------------------------------------------------------------
# Folders
# ------------------------------------------------------------------


async def test_list_folders_not_connected_returns_empty(client: GDriveClient) -> None:
    assert await client.list_folders() == []


async def test_list_folders_success(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(
        200,
        {
            "files": [
                {
                    "id": "f1",
                    "name": "Blink Clips",
                    "modifiedTime": "2026-01-01T00:00:00Z",
                },
                {"id": "f2", "name": "Other"},
            ]
        },
    )
    client._session = _mock_session(get=resp)

    folders = await client.list_folders("root")
    assert [f.id for f in folders] == ["f1", "f2"]
    assert folders[0].name == "Blink Clips"
    assert folders[1].modified_time == ""


async def test_list_folders_filters_by_parent(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(200, {"files": []})
    session = _mock_session(get=resp)
    client._session = session

    await client.list_folders("parent-xyz")

    params = session.get.call_args.kwargs["params"]
    assert "'parent-xyz' in parents" in params["q"]


async def test_list_folders_rate_limited_sets_flag(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(429, {})
    client._session = _mock_session(get=resp)

    folders = await client.list_folders()
    assert folders == []
    assert client.rate_limited is True


async def test_list_folders_http_error_returns_empty(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(500, {})
    client._session = _mock_session(get=resp)

    assert await client.list_folders() == []


async def test_list_folders_network_error_returns_empty(client: GDriveClient) -> None:
    _connect(client)
    client._session = _mock_session(
        get=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client.list_folders() == []


async def test_create_folder_not_connected_returns_none(client: GDriveClient) -> None:
    assert await client.create_folder("New Folder") is None


async def test_create_folder_success(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(
        200,
        {"id": "f-new", "name": "New Folder", "modifiedTime": "2026-01-01T00:00:00Z"},
    )
    session = _mock_session(post=resp)
    client._session = session

    folder = await client.create_folder("New Folder", "parent-1")

    assert folder is not None
    assert folder.id == "f-new"
    body = session.post.call_args.kwargs["json"]
    assert body["parents"] == ["parent-1"]


async def test_create_folder_failure_returns_none(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(403, {})
    client._session = _mock_session(post=resp)

    assert await client.create_folder("New Folder") is None


async def test_create_folder_network_error_returns_none(client: GDriveClient) -> None:
    _connect(client)
    client._session = _mock_session(
        post=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client.create_folder("New Folder") is None


def test_select_folder_persists(client: GDriveClient) -> None:
    client.select_folder("f1", "Blink Clips")
    assert client.folder_id == "f1"
    assert client.folder_name == "Blink Clips"

    from blink_downloader.gdrive_client import CREDENTIALS_FILE

    saved = json.loads(CREDENTIALS_FILE.read_text())
    assert saved["folder_id"] == "f1"
    assert saved["folder_name"] == "Blink Clips"


# ------------------------------------------------------------------
# get_or_create_folder_path
#
# list_folders/create_folder are exercised at the HTTP level above — these
# tests mock the client's own already-tested methods instead, to isolate
# what's actually new here: the path-walking/caching orchestration.
# ------------------------------------------------------------------


async def test_get_or_create_folder_path_no_root_returns_none(
    client: GDriveClient,
) -> None:
    """No root_id argument and no connected default folder — nothing to
    resolve under."""
    assert await client.get_or_create_folder_path(["2026-06-05"]) is None


async def test_get_or_create_folder_path_uses_connected_folder_as_default_root(
    client: GDriveClient,
) -> None:
    client.select_folder("connected-root", "Blink Clips")
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(
        return_value=DriveFolder(id="new-id", name="2026-06-05", modified_time="")
    )

    result = await client.get_or_create_folder_path(["2026-06-05"])

    assert result == "new-id"
    client.list_folders.assert_awaited_once_with("connected-root")
    client.create_folder.assert_awaited_once_with(
        "2026-06-05", parent_id="connected-root"
    )


async def test_get_or_create_folder_path_explicit_root_overrides_connected(
    client: GDriveClient,
) -> None:
    client.select_folder("connected-root", "Blink Clips")
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(
        return_value=DriveFolder(id="new-id", name="2026-06-05", modified_time="")
    )

    await client.get_or_create_folder_path(["2026-06-05"], root_id="explicit-root")

    client.list_folders.assert_awaited_once_with("explicit-root")


async def test_get_or_create_folder_path_reuses_existing_folder(
    client: GDriveClient,
) -> None:
    """A folder with the target name already exists under the parent — must
    not create a duplicate."""
    client.list_folders = AsyncMock(
        return_value=[
            DriveFolder(id="other-id", name="Not It", modified_time=""),
            DriveFolder(id="existing-id", name="Driveway", modified_time=""),
        ]
    )
    client.create_folder = AsyncMock()

    result = await client.get_or_create_folder_path(["Driveway"], root_id="root")

    assert result == "existing-id"
    client.create_folder.assert_not_awaited()


async def test_get_or_create_folder_path_creates_nested_path_in_order(
    client: GDriveClient,
) -> None:
    """A multi-part path (date/camera) walks one level at a time, each new
    folder created under the previous level's freshly-created id."""
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(
        side_effect=[
            DriveFolder(id="date-id", name="2026-06-05", modified_time=""),
            DriveFolder(id="camera-id", name="Driveway", modified_time=""),
        ]
    )

    result = await client.get_or_create_folder_path(
        ["2026-06-05", "Driveway"], root_id="root"
    )

    assert result == "camera-id"
    assert client.create_folder.call_args_list == [
        call("2026-06-05", parent_id="root"),
        call("Driveway", parent_id="date-id"),
    ]


async def test_get_or_create_folder_path_create_failure_returns_none(
    client: GDriveClient,
) -> None:
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(return_value=None)

    result = await client.get_or_create_folder_path(["2026-06-05"], root_id="root")

    assert result is None


async def test_get_or_create_folder_path_create_failure_on_second_level_returns_none(
    client: GDriveClient,
) -> None:
    """The first level resolves fine; the second (camera) level fails to
    create — the whole call must fail rather than return a partial path."""
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(
        side_effect=[
            DriveFolder(id="date-id", name="2026-06-05", modified_time=""),
            None,
        ]
    )

    result = await client.get_or_create_folder_path(
        ["2026-06-05", "Driveway"], root_id="root"
    )

    assert result is None


async def test_get_or_create_folder_path_caches_repeat_lookups(
    client: GDriveClient,
) -> None:
    """The same date/camera pair resolved twice (two clips backed up the
    same day) must hit the cache on the second call, not Drive again."""
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(
        side_effect=[
            DriveFolder(id="date-id", name="2026-06-05", modified_time=""),
            DriveFolder(id="camera-id", name="Driveway", modified_time=""),
        ]
    )

    first = await client.get_or_create_folder_path(
        ["2026-06-05", "Driveway"], root_id="root"
    )
    second = await client.get_or_create_folder_path(
        ["2026-06-05", "Driveway"], root_id="root"
    )

    assert first == second == "camera-id"
    assert client.list_folders.await_count == 2  # only the first call's two levels
    assert client.create_folder.await_count == 2


async def test_get_or_create_folder_path_cache_scoped_per_parent(
    client: GDriveClient,
) -> None:
    """Same camera name under two different dates must not collide in the
    cache — each date's "Driveway" subfolder is a distinct Drive folder."""
    client.list_folders = AsyncMock(return_value=[])
    client.create_folder = AsyncMock(
        side_effect=[
            DriveFolder(id="date1-id", name="2026-06-05", modified_time=""),
            DriveFolder(id="cam1-id", name="Driveway", modified_time=""),
            DriveFolder(id="date2-id", name="2026-06-06", modified_time=""),
            DriveFolder(id="cam2-id", name="Driveway", modified_time=""),
        ]
    )

    first = await client.get_or_create_folder_path(
        ["2026-06-05", "Driveway"], root_id="root"
    )
    second = await client.get_or_create_folder_path(
        ["2026-06-06", "Driveway"], root_id="root"
    )

    assert first == "cam1-id"
    assert second == "cam2-id"


# ------------------------------------------------------------------
# Upload
# ------------------------------------------------------------------


async def test_upload_file_not_connected_returns_none(
    client: GDriveClient, tmp_path: Path
) -> None:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    assert await client.upload_file(src, "clip.mp4") is None


async def test_upload_file_no_folder_selected_returns_none(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")
    assert await client.upload_file(src, "clip.mp4") is None


async def test_upload_file_success(client: GDriveClient, tmp_path: Path) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"video bytes")

    initiate_resp = _mock_response(
        200, headers={"Location": "https://upload.example/session1"}
    )
    put_resp = _mock_response(200, {"id": "uploaded-id"})
    client._session = _mock_session(post=initiate_resp, put=put_resp)

    file_id = await client.upload_file(src, "clip.mp4")
    assert file_id == "uploaded-id"


async def test_upload_file_with_folder_override(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("default-folder", "Default")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    initiate_resp = _mock_response(
        200, headers={"Location": "https://upload.example/session1"}
    )
    put_resp = _mock_response(200, {"id": "uploaded-id"})
    session = _mock_session(post=initiate_resp, put=put_resp)
    client._session = session

    await client.upload_file(src, "clip.mp4", folder_id="override-folder")

    body = session.post.call_args.kwargs["json"]
    assert body["parents"] == ["override-folder"]


async def test_upload_file_rate_limited_sets_flag(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    resp = _mock_response(429, {})
    client._session = _mock_session(post=resp)

    assert await client.upload_file(src, "clip.mp4") is None
    assert client.rate_limited is True


async def test_upload_file_quota_exceeded_sets_flag(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    resp = _mock_response(
        403, {"error": {"errors": [{"reason": "storageQuotaExceeded"}]}}
    )
    client._session = _mock_session(post=resp)

    assert await client.upload_file(src, "clip.mp4") is None
    assert client.quota_exceeded is True
    assert client.rate_limited is False


async def test_upload_file_403_without_quota_reason_does_not_set_quota_flag(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    resp = _mock_response(
        403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
    )
    client._session = _mock_session(post=resp)

    assert await client.upload_file(src, "clip.mp4") is None
    assert client.quota_exceeded is False


async def test_upload_file_403_body_not_json_falls_back_to_empty(
    client: GDriveClient, tmp_path: Path
) -> None:
    """A 403 whose body itself fails to parse as JSON must not crash the
    upload — just skip the quota/rate-limit classification."""
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    resp = _mock_response(403, {})
    resp.json = AsyncMock(side_effect=json.JSONDecodeError("bad", "", 0))
    client._session = _mock_session(post=resp)

    assert await client.upload_file(src, "clip.mp4") is None
    assert client.quota_exceeded is False


async def test_upload_file_403_with_ratelimit_reason_sets_rate_limited(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    resp = _mock_response(403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
    client._session = _mock_session(post=resp)

    assert await client.upload_file(src, "clip.mp4") is None
    assert client.rate_limited is True
    assert client.quota_exceeded is False


async def test_upload_file_no_session_uri_returns_none(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    resp = _mock_response(200, headers={})  # no Location header
    client._session = _mock_session(post=resp)

    assert await client.upload_file(src, "clip.mp4") is None


async def test_upload_file_put_failure_returns_none(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    initiate_resp = _mock_response(
        200, headers={"Location": "https://upload.example/session1"}
    )
    put_resp = _mock_response(500, {})
    client._session = _mock_session(post=initiate_resp, put=put_resp)

    assert await client.upload_file(src, "clip.mp4") is None


async def test_upload_file_network_error_returns_none(
    client: GDriveClient, tmp_path: Path
) -> None:
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    client._session = _mock_session(
        post=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client.upload_file(src, "clip.mp4") is None


async def test_upload_file_resets_flags_at_start_of_each_attempt(
    client: GDriveClient, tmp_path: Path
) -> None:
    """A rate limit/quota flag from a previous attempt must not permanently
    block every future upload — only the current attempt's outcome counts."""
    _connect(client)
    client.select_folder("f1", "Blink Clips")
    client.rate_limited = True
    client.quota_exceeded = True
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"data")

    initiate_resp = _mock_response(
        200, headers={"Location": "https://upload.example/session1"}
    )
    put_resp = _mock_response(200, {"id": "uploaded-id"})
    client._session = _mock_session(post=initiate_resp, put=put_resp)

    file_id = await client.upload_file(src, "clip.mp4")
    assert file_id == "uploaded-id"
    assert client.rate_limited is False
    assert client.quota_exceeded is False


# ------------------------------------------------------------------
# Delete (trash)
# ------------------------------------------------------------------


async def test_delete_file_not_connected_returns_false(client: GDriveClient) -> None:
    assert await client.delete_file("f1") is False


async def test_delete_file_success(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(200, {})
    session = _mock_session(patch=resp)
    client._session = session

    assert await client.delete_file("f1") is True
    body = session.patch.call_args.kwargs["json"]
    assert body == {"trashed": True}


async def test_delete_file_failure_returns_false(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(404, {})
    client._session = _mock_session(patch=resp)

    assert await client.delete_file("f1") is False


async def test_delete_file_rate_limited_sets_flag(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(429, {})
    client._session = _mock_session(patch=resp)

    assert await client.delete_file("f1") is False
    assert client.rate_limited is True


async def test_delete_file_network_error_returns_false(client: GDriveClient) -> None:
    _connect(client)
    client._session = _mock_session(
        patch=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client.delete_file("f1") is False


# ------------------------------------------------------------------
# Quota
# ------------------------------------------------------------------


async def test_get_quota_not_connected_returns_none(client: GDriveClient) -> None:
    assert await client.get_quota() is None


async def test_get_quota_success(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(
        200,
        {
            "storageQuota": {
                "limit": "1000000000",
                "usage": "500000000",
                "usageInDrive": "400000000",
            }
        },
    )
    client._session = _mock_session(get=resp)

    quota = await client.get_quota()
    assert quota is not None
    assert quota.limit == 1_000_000_000
    assert quota.usage == 500_000_000
    assert quota.usage_in_drive == 400_000_000


async def test_get_quota_unlimited_when_no_limit(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(200, {"storageQuota": {"usage": "500"}})
    client._session = _mock_session(get=resp)

    quota = await client.get_quota()
    assert quota is not None
    assert quota.limit is None


async def test_get_quota_caches_within_window(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(200, {"storageQuota": {"limit": "100", "usage": "50"}})
    session = _mock_session(get=resp)
    client._session = session

    await client.get_quota()
    await client.get_quota()

    assert session.get.call_count == 1


async def test_get_quota_http_error_returns_none(client: GDriveClient) -> None:
    _connect(client)
    resp = _mock_response(500, {})
    client._session = _mock_session(get=resp)

    assert await client.get_quota() is None


async def test_get_quota_network_error_returns_none(client: GDriveClient) -> None:
    _connect(client)
    client._session = _mock_session(
        get=_RaiseOnCall(aiohttp.ClientConnectionError("down"))
    )

    assert await client.get_quota() is None


# ------------------------------------------------------------------
# Session lifecycle
# ------------------------------------------------------------------


async def test_close_closes_open_session(client: GDriveClient) -> None:
    session = client._get_session()
    assert session.closed is False
    await client.close()
    assert session.closed is True


async def test_close_noop_when_no_session(client: GDriveClient) -> None:
    await client.close()  # must not raise


async def test_get_session_reuses_open_session(client: GDriveClient) -> None:
    s1 = client._get_session()
    s2 = client._get_session()
    assert s1 is s2
