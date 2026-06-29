"""Tests for HAEventWatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from blink_downloader.event_watcher import HAEventWatcher


# ------------------------------------------------------------------
# extract_blink_camera (pure static, no I/O)
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "entity_id,expected",
    [
        ("binary_sensor.blink_front_door_motion", "front door"),
        ("binary_sensor.blink_back_yard_motion", "back yard"),
        ("binary_sensor.blink_garage_motion", "garage"),
        # Not a Blink motion entity → None
        ("binary_sensor.smoke_detector", None),
        ("sensor.blink_front_door_motion", None),  # wrong prefix domain
        ("binary_sensor.blink_motion", None),  # no inner slug
        ("binary_sensor.blink__motion", None),  # empty slug
    ],
)
def test_extract_blink_camera(entity_id: str, expected: str | None) -> None:
    result = HAEventWatcher.extract_blink_camera(entity_id)
    assert result == expected


# ------------------------------------------------------------------
# _handle_state_changed — motion ON
# ------------------------------------------------------------------


def _make_watcher(
    cameras: list[str] | None = None,
    with_cleared: bool = False,
) -> tuple[HAEventWatcher, MagicMock, MagicMock | None]:
    cb = MagicMock()
    cb_cleared = MagicMock() if with_cleared else None
    w = HAEventWatcher(
        supervisor_token="tok",
        on_motion=cb,
        on_motion_cleared=cb_cleared,
        event_cameras=cameras or [],
    )
    return w, cb, cb_cleared


def _motion_event(entity_id: str, state: str) -> dict:
    return {
        "event_type": "state_changed",
        "data": {"entity_id": entity_id, "new_state": {"state": state}},
    }


def test_handle_motion_on_fires_callback() -> None:
    w, cb, _ = _make_watcher()
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "on")
    )
    cb.assert_called_once_with("front door")


def test_handle_motion_off_fires_cleared_callback() -> None:
    w, cb, cb_cleared = _make_watcher(with_cleared=True)
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "off")
    )
    cb.assert_not_called()
    if cb_cleared is not None:
        cb_cleared.assert_called_once_with("front door")


def test_handle_motion_off_no_cleared_callback_is_safe() -> None:
    w, cb, cb_cleared = _make_watcher(with_cleared=False)
    # Should not raise even though no cleared callback is registered
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "off")
    )
    cb.assert_not_called()
    assert cb_cleared is None


def test_handle_motion_state_unavailable_ignored() -> None:
    w, cb, _ = _make_watcher()
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "unavailable")
    )
    cb.assert_not_called()


def test_handle_wrong_event_type_ignored() -> None:
    w, cb, _ = _make_watcher()
    event = {
        "event_type": "call_service",
        "data": {
            "entity_id": "binary_sensor.blink_front_door_motion",
            "new_state": {"state": "on"},
        },
    }
    w._handle_state_changed(event)
    cb.assert_not_called()


def test_handle_non_blink_entity_ignored() -> None:
    w, cb, _ = _make_watcher()
    w._handle_state_changed(_motion_event("binary_sensor.smoke_alarm", "on"))
    cb.assert_not_called()


def test_camera_whitelist_allows_matching() -> None:
    w, cb, _ = _make_watcher(cameras=["front door"])
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "on")
    )
    cb.assert_called_once()


def test_camera_whitelist_blocks_non_matching() -> None:
    w, cb, _ = _make_watcher(cameras=["back yard"])
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "on")
    )
    cb.assert_not_called()


def test_camera_whitelist_also_applies_to_cleared() -> None:
    w, cb, cb_cleared = _make_watcher(cameras=["back yard"], with_cleared=True)
    w._handle_state_changed(
        _motion_event("binary_sensor.blink_front_door_motion", "off")
    )
    assert cb_cleared is not None
    cb_cleared.assert_not_called()


def test_empty_camera_list_allows_all() -> None:
    w, cb, _ = _make_watcher(cameras=[])
    for entity in [
        "binary_sensor.blink_cam_a_motion",
        "binary_sensor.blink_cam_b_motion",
    ]:
        w._handle_state_changed(_motion_event(entity, "on"))
    assert cb.call_count == 2


def test_handle_missing_new_state_ignored() -> None:
    w, cb, _ = _make_watcher()
    event = {
        "event_type": "state_changed",
        "data": {
            "entity_id": "binary_sensor.blink_front_door_motion",
            "new_state": None,
        },
    }
    w._handle_state_changed(event)
    cb.assert_not_called()


# ------------------------------------------------------------------
# stop
# ------------------------------------------------------------------


async def test_stop_closes_session() -> None:
    w, _, _ = _make_watcher()
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.close = AsyncMock()
    w._session = mock_session
    w._running = True

    await w.stop()

    assert w._running is False
    mock_session.close.assert_awaited_once()


async def test_stop_skips_already_closed_session() -> None:
    w, _, _ = _make_watcher()
    mock_session = MagicMock()
    mock_session.closed = True
    w._session = mock_session
    await w.stop()  # should not raise


# ------------------------------------------------------------------
# start — exits cleanly on CancelledError
# ------------------------------------------------------------------


async def test_start_exits_cleanly_on_cancel() -> None:
    import asyncio

    w, _, _ = _make_watcher()

    async def fake_connect_and_watch():
        raise asyncio.CancelledError()

    w._connect_and_watch = fake_connect_and_watch
    await w.start()
    assert w._running is True


# ------------------------------------------------------------------
# start — reconnects on non-CancelledError exceptions
# ------------------------------------------------------------------


async def test_start_reconnects_on_exception(monkeypatch) -> None:
    """Exceptions from _connect_and_watch trigger a reconnect delay, then stop."""
    import asyncio

    call_count = 0

    async def fake_sleep(_):
        pass  # skip the 30-second delay

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    w, _, _ = _make_watcher()

    async def fake_connect_and_watch():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionRefusedError("ws refused")
        # Second call: set running=False so the loop exits
        w._running = False

    w._connect_and_watch = fake_connect_and_watch
    await w.start()
    assert call_count == 2


# ------------------------------------------------------------------
# _connect_and_watch — WebSocket integration
# ------------------------------------------------------------------


def _make_ws_message(msg_type, data=None):
    """Create a minimal mock of an aiohttp.WSMessage."""
    import json as _json

    msg = MagicMock()
    msg.type = msg_type
    msg.data = _json.dumps(data) if data is not None else ""
    return msg


class _FakeWS:
    """Minimal async-iterator WebSocket mock."""

    def __init__(self, receive_jsons, ws_messages):
        self._receive_jsons = iter(receive_jsons)
        self._ws_messages = iter(ws_messages)
        self.send_json = AsyncMock()

    async def receive_json(self):
        return next(self._receive_jsons)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._ws_messages)
        except StopIteration:
            raise StopAsyncIteration


class _FakeWSCM:
    """Async context manager returning a _FakeWS."""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *_):
        pass


def _mock_session(fake_ws: _FakeWS) -> MagicMock:
    """Return a MagicMock session whose ws_connect yields *fake_ws*."""
    mock = MagicMock()
    mock.closed = False  # prevents _connect_and_watch from replacing the session
    mock.ws_connect.return_value = _FakeWSCM(fake_ws)
    return mock


async def test_connect_and_watch_fires_motion_on_event() -> None:
    """Full happy-path: auth → subscribe → motion event → close."""
    import aiohttp

    fired = []
    w = HAEventWatcher("tok", on_motion=lambda cam: fired.append(cam))
    w._running = True  # _connect_and_watch checks this; start() sets it, we must too

    event_payload = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "data": {
                "entity_id": "binary_sensor.blink_front_door_motion",
                "new_state": {"state": "on"},
            },
        },
    }

    ws_messages = [
        _make_ws_message(aiohttp.WSMsgType.TEXT, event_payload),
        _make_ws_message(aiohttp.WSMsgType.CLOSE),
    ]
    fake_ws = _FakeWS(
        receive_jsons=[{"type": "auth_required"}, {"type": "auth_ok"}],
        ws_messages=ws_messages,
    )
    w._session = _mock_session(fake_ws)

    await w._connect_and_watch()

    assert fired == ["front door"]
    assert fake_ws.send_json.call_count == 2  # auth + subscribe


async def test_connect_and_watch_auth_required_wrong_type_raises() -> None:
    """auth_required message with wrong type should raise ValueError."""
    fake_ws = _FakeWS(
        receive_jsons=[{"type": "something_else"}],
        ws_messages=[],
    )
    w, _, _ = _make_watcher()
    w._session = _mock_session(fake_ws)

    with pytest.raises(ValueError, match="Expected auth_required"):
        await w._connect_and_watch()


async def test_connect_and_watch_auth_fail_raises() -> None:
    """auth_invalid response should raise ValueError."""
    fake_ws = _FakeWS(
        receive_jsons=[{"type": "auth_required"}, {"type": "auth_invalid"}],
        ws_messages=[],
    )
    w, _, _ = _make_watcher()
    w._session = _mock_session(fake_ws)

    with pytest.raises(ValueError, match="auth failed"):
        await w._connect_and_watch()


async def test_connect_and_watch_ws_error_breaks_loop() -> None:
    """WS ERROR message type causes clean exit (no exception)."""
    import aiohttp

    ws_messages = [_make_ws_message(aiohttp.WSMsgType.ERROR)]
    fake_ws = _FakeWS(
        receive_jsons=[{"type": "auth_required"}, {"type": "auth_ok"}],
        ws_messages=ws_messages,
    )
    w, _, _ = _make_watcher()
    w._running = True
    w._session = _mock_session(fake_ws)
    # Should return without raising
    await w._connect_and_watch()


async def test_connect_and_watch_stops_when_not_running() -> None:
    """If _running is set to False during iteration, loop exits."""
    import aiohttp

    class _StopAfterFirstWS(_FakeWS):
        _watcher: HAEventWatcher | None = None

        async def __anext__(self):
            # Flip running off before yielding so the inner check fires
            assert self._watcher is not None
            self._watcher._running = False
            msg = MagicMock()
            msg.type = aiohttp.WSMsgType.TEXT
            msg.data = "{}"  # empty JSON, won't fire callback
            return msg

    fake_ws = _StopAfterFirstWS(
        receive_jsons=[{"type": "auth_required"}, {"type": "auth_ok"}],
        ws_messages=[],
    )
    w, _, _ = _make_watcher()
    fake_ws._watcher = w
    w._session = _mock_session(fake_ws)
    await w._connect_and_watch()
    assert w._running is False


async def test_connect_and_watch_creates_session_if_none() -> None:
    """If _session is None a new ClientSession is created."""
    from unittest.mock import patch

    import aiohttp

    fake_ws = _FakeWS(
        receive_jsons=[{"type": "auth_required"}, {"type": "auth_ok"}],
        ws_messages=[_make_ws_message(aiohttp.WSMsgType.CLOSE)],
    )

    mock_instance = MagicMock()
    mock_instance.closed = False
    mock_instance.ws_connect.return_value = _FakeWSCM(fake_ws)

    with patch("blink_downloader.event_watcher.aiohttp.ClientSession") as MockSession:
        MockSession.return_value = mock_instance

        w, _, _ = _make_watcher()
        w._session = None
        w._running = True
        await w._connect_and_watch()

    MockSession.assert_called_once()


# ---------------------------------------------------------------------------
# Coverage: invalid JSON in TEXT message triggers json.JSONDecodeError (lines 122-123)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_and_watch_invalid_json_text_is_skipped() -> None:
    """TEXT message with invalid JSON triggers the JSONDecodeError handler and continues."""
    import aiohttp as _aiohttp

    invalid_msg = MagicMock()
    invalid_msg.type = _aiohttp.WSMsgType.TEXT
    invalid_msg.data = "NOT_VALID_JSON"

    ws_messages = [
        invalid_msg,
        _make_ws_message(_aiohttp.WSMsgType.CLOSE),
    ]
    fake_ws = _FakeWS(
        receive_jsons=[{"type": "auth_required"}, {"type": "auth_ok"}],
        ws_messages=ws_messages,
    )
    w, cb, _ = _make_watcher()
    w._running = True
    w._session = _mock_session(fake_ws)
    await w._connect_and_watch()
    cb.assert_not_called()
