"""Tests for blink_downloader.live_view."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from blinkpy.auth import LoginError, TokenRefreshFailed, UnauthorizedError

from blink_downloader.live_view import (
    CameraNotFoundError,
    LiveViewError,
    LiveViewManager,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_SUBPROCESS_EXEC = "blink_downloader.live_view.asyncio.create_subprocess_exec"


class _FakeStderr:
    """Stand-in for the ffmpeg process's stderr StreamReader."""

    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = list(lines or [])

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _RaisingStderr:
    """Stand-in stderr stream whose readline() always raises."""

    async def readline(self) -> bytes:
        raise RuntimeError("pipe broke")


class _FakeProcess:
    """Stand-in for asyncio.subprocess.Process.

    .wait() is awaitable concurrently from multiple callers, matching real
    asyncio subprocess semantics: an internal Event backs it so
    _terminate_ffmpeg's own wait() and _watch_ffmpeg's standing wait() both
    resolve together once the process "exits".
    """

    def __init__(self, stderr_lines: list[bytes] | None = None) -> None:
        self.returncode: int | None = None
        self.stderr: Any = _FakeStderr(stderr_lines)
        self._exited = asyncio.Event()
        self.terminate_called = False
        self.kill_called = False

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True
        self._exit(0)

    def kill(self) -> None:
        self.kill_called = True
        self._exit(-9)

    def _exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
        self._exited.set()

    def crash(self, code: int = 1) -> None:
        """Simulate ffmpeg exiting on its own (not via terminate/kill)."""
        self._exit(code)


class _FakeStream:
    """Stand-in for blinkpy's BlinkLiveStream."""

    def __init__(self, url: str = "tcp://127.0.0.1:12345") -> None:
        self.url = url
        self.started = False
        self.stopped = False
        self.start = AsyncMock(side_effect=self._do_start)
        self._feed_event = asyncio.Event()

    async def _do_start(self, port: int = 0) -> None:
        del port  # required to match BlinkLiveStream.start()'s signature
        self.started = True

    def stop(self) -> None:
        self.stopped = True
        self._feed_event.set()

    async def feed(self) -> None:
        # Real feed() runs until stop()/cancellation — block until stop().
        await self._feed_event.wait()


def _make_camera(stream: _FakeStream | None = None) -> MagicMock:
    camera = MagicMock()
    camera.init_livestream = AsyncMock(return_value=stream or _FakeStream())
    return camera


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def camera_registry() -> dict[str, Any]:
    return {}


def _get_camera_from(registry: dict[str, Any]):
    def get_camera(name: str):
        for cam_name, cam in registry.items():
            if cam_name.lower() == name.lower():
                return cam
        return None

    return get_camera


@pytest.fixture
def manager(camera_registry: dict[str, Any]) -> LiveViewManager:
    return LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        idle_timeout=30.0,
        max_session_duration=120.0,
        sweep_interval=0.05,
        init_timeout=0.2,
        startup_timeout=0.3,
        terminate_timeout=0.05,
    )


def _mock_exec(proc: _FakeProcess | list[_FakeProcess]):
    if isinstance(proc, list):
        return patch(_SUBPROCESS_EXEC, AsyncMock(side_effect=proc))
    return patch(_SUBPROCESS_EXEC, AsyncMock(return_value=proc))


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


async def test_start_session_happy_path(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    stream = _FakeStream(url="tcp://127.0.0.1:9999")
    camera_registry["Front Door"] = _make_camera(stream)
    proc = _FakeProcess()

    with _mock_exec(proc) as mock_exec:
        status = await manager.start_session("Front Door")

    try:
        assert status.active is True
        assert status.camera == "Front Door"
        assert status.state == "starting"
        assert status.session_id
        assert stream.started is True

        args, kwargs = mock_exec.call_args
        assert args[0] == "ffmpeg"
        assert args[args.index("-c") + 1] == "copy"
        assert args[args.index("-f") + 1] == "hls"
        assert args[args.index("-i") + 1] == "tcp://127.0.0.1:9999"
        assert "delete_segments" in args[args.index("-hls_flags") + 1]
        assert "omit_endlist" in args[args.index("-hls_flags") + 1]

        hls_dir = manager.get_hls_dir(status.session_id)
        assert hls_dir is not None
        assert hls_dir.is_dir()
        assert kwargs["cwd"] == str(hls_dir)
        assert kwargs["stderr"] == asyncio.subprocess.PIPE
    finally:
        await manager.stop_session(status.session_id)


async def test_start_session_idempotent_same_camera_case_insensitive(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera = _make_camera()
    camera_registry["Front Door"] = camera
    proc = _FakeProcess()

    with _mock_exec(proc) as mock_exec:
        first = await manager.start_session("Front Door")
        second = await manager.start_session("front door")

    try:
        assert first.session_id == second.session_id
        camera.init_livestream.assert_awaited_once()
        assert mock_exec.call_count == 1
    finally:
        await manager.stop_session(first.session_id)


async def test_start_session_concurrent_calls_serialize_via_lock(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera = _make_camera()
    camera_registry["Front Door"] = camera
    proc = _FakeProcess()

    with _mock_exec(proc) as mock_exec:
        results = await asyncio.gather(
            manager.start_session("Front Door"),
            manager.start_session("Front Door"),
        )

    try:
        assert results[0].session_id == results[1].session_id
        assert mock_exec.call_count == 1
        camera.init_livestream.assert_awaited_once()
    finally:
        await manager.stop_session(results[0].session_id)


async def test_start_session_switching_camera_tears_down_old(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    stream_a = _FakeStream()
    stream_b = _FakeStream()
    camera_registry["Front Door"] = _make_camera(stream_a)
    camera_registry["Backyard"] = _make_camera(stream_b)
    proc_a, proc_b = _FakeProcess(), _FakeProcess()

    with _mock_exec([proc_a, proc_b]):
        status_a = await manager.start_session("Front Door")
        status_b = await manager.start_session("Backyard")

    try:
        assert status_a.session_id != status_b.session_id
        assert stream_a.stopped is True
        assert proc_a.terminate_called is True
        assert stream_b.stopped is False
        current = manager.get_status()
        assert current.camera == "Backyard"
        assert current.session_id == status_b.session_id
    finally:
        await manager.stop_session(status_b.session_id)


async def test_start_session_camera_not_found(manager: LiveViewManager) -> None:
    with pytest.raises(CameraNotFoundError):
        await manager.start_session("Nonexistent")
    assert manager.get_status().active is False


async def test_start_session_init_livestream_timeout(
    camera_registry: dict[str, Any],
) -> None:
    async def _never_returns(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(1)

    camera = MagicMock()
    camera.init_livestream = AsyncMock(side_effect=_never_returns)
    camera_registry["Front Door"] = camera
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        init_timeout=0.05,
    )

    with pytest.raises(LiveViewError, match="did not respond"):
        await mgr.start_session("Front Door")
    assert mgr.get_status().active is False


async def test_start_session_unsupported_camera(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera = MagicMock()
    camera.init_livestream = AsyncMock(side_effect=NotImplementedError("rtsp://x"))
    camera_registry["Front Door"] = camera

    with pytest.raises(LiveViewError, match="does not support live view"):
        await manager.start_session("Front Door")


async def test_start_session_malformed_blink_response(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera = MagicMock()
    camera.init_livestream = AsyncMock(side_effect=KeyError("server"))
    camera_registry["Front Door"] = camera

    with pytest.raises(LiveViewError, match="did not return a valid live view"):
        await manager.start_session("Front Door")


@pytest.mark.parametrize(
    "exc", [TokenRefreshFailed("x"), LoginError("x"), UnauthorizedError("x")]
)
async def test_start_session_auth_fatal_exceptions_propagate_unwrapped(
    manager: LiveViewManager, camera_registry: dict[str, Any], exc: Exception
) -> None:
    camera = MagicMock()
    camera.init_livestream = AsyncMock(side_effect=exc)
    camera_registry["Front Door"] = camera

    with pytest.raises(type(exc)):
        await manager.start_session("Front Door")


async def test_start_session_generic_exception_wrapped(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera = MagicMock()
    camera.init_livestream = AsyncMock(side_effect=RuntimeError("boom"))
    camera_registry["Front Door"] = camera

    with pytest.raises(LiveViewError, match="Could not start live view"):
        await manager.start_session("Front Door")


async def test_start_session_ffmpeg_missing_cleans_up_stream(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    stream = _FakeStream()
    camera_registry["Front Door"] = _make_camera(stream)

    with (
        patch(_SUBPROCESS_EXEC, AsyncMock(side_effect=FileNotFoundError("no ffmpeg"))),
        pytest.raises(LiveViewError, match="ffmpeg is not available"),
    ):
        await manager.start_session("Front Door")

    assert stream.stopped is True
    assert manager.get_status().active is False


async def test_start_session_port_bind_failure(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    stream = _FakeStream()
    stream.start = AsyncMock(side_effect=OSError("address already in use"))
    camera_registry["Front Door"] = _make_camera(stream)

    with pytest.raises(LiveViewError, match="Could not open a local port"):
        await manager.start_session("Front Door")


# ---------------------------------------------------------------------------
# stop_session / heartbeat
# ---------------------------------------------------------------------------


async def test_stop_session_active(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    assert status.session_id
    hls_dir = manager.get_hls_dir(status.session_id)
    assert hls_dir is not None and hls_dir.is_dir()

    stopped = await manager.stop_session(status.session_id)

    assert stopped is True
    assert manager.get_status().active is False
    assert proc.terminate_called is True
    assert not hls_dir.exists()


async def test_stop_session_nothing_active(manager: LiveViewManager) -> None:
    assert await manager.stop_session() is False
    assert await manager.stop_session("some-id") is False


async def test_stop_session_stale_id_leaves_real_session_untouched(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")

    try:
        assert await manager.stop_session("not-the-real-id") is False
        current = manager.get_status()
        assert current.active is True
        assert current.session_id == status.session_id
    finally:
        await manager.stop_session(status.session_id)


async def test_stop_session_omitted_id_stops_whatever_is_active(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        await manager.start_session("Front Door")

    assert await manager.stop_session() is True
    assert manager.get_status().active is False


async def test_stop_session_does_not_record_ffmpeg_exit_as_crash(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    """Validates the `stopping` flag: intentional termination must not be
    misread by _watch_ffmpeg as an unexpected crash."""
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    internal_session = manager._session
    assert internal_session is not None

    await manager.stop_session(status.session_id)

    assert proc.terminate_called is True
    assert internal_session.error is None


async def test_heartbeat_matching_session_advances_last_heartbeat(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    assert status.session_id

    try:
        internal_session = manager._session
        assert internal_session is not None
        before = internal_session.last_heartbeat
        await asyncio.sleep(0.01)

        assert manager.heartbeat(status.session_id) is True
        assert internal_session.last_heartbeat > before
    finally:
        await manager.stop_session(status.session_id)


async def test_heartbeat_unknown_session_returns_false(
    manager: LiveViewManager,
) -> None:
    assert manager.heartbeat("nope") is False


async def test_heartbeat_stale_id_with_active_session_returns_false(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")

    try:
        assert manager.heartbeat("stale-id") is False
    finally:
        await manager.stop_session(status.session_id)


# ---------------------------------------------------------------------------
# Status / list_cameras
# ---------------------------------------------------------------------------


def test_get_status_inactive(manager: LiveViewManager) -> None:
    status = manager.get_status()
    assert status.active is False
    assert status.session_id is None
    assert status.camera is None


async def test_status_transitions_starting_to_live(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    assert status.session_id

    try:
        assert manager.get_status().state == "starting"
        hls_dir = manager.get_hls_dir(status.session_id)
        assert hls_dir is not None
        (hls_dir / "stream.m3u8").write_text("#EXTM3U\n")
        assert manager.get_status().state == "live"
    finally:
        await manager.stop_session(status.session_id)


async def test_status_error_state_includes_stderr_tail(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")

    try:
        internal_session = manager._session
        assert internal_session is not None
        internal_session.error = "ffmpeg exited unexpectedly (code 1)"
        internal_session.stderr_tail = "Connection refused"
        s = manager.get_status()
        assert s.state == "error"
        assert "exited unexpectedly" in (s.error or "")
        assert "Connection refused" in (s.error or "")
    finally:
        await manager.stop_session(status.session_id)


def test_get_hls_dir_unknown_session_returns_none(manager: LiveViewManager) -> None:
    assert manager.get_hls_dir("nope") is None


def test_list_cameras_delegates_to_injected_callable(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    camera_registry["Backyard"] = _make_camera()
    assert set(manager.list_cameras()) == {"Front Door", "Backyard"}


# ---------------------------------------------------------------------------
# Sweep loop — idle timeout / hard cap / crash detection
# ---------------------------------------------------------------------------


async def test_sweep_tears_down_idle_session(camera_registry: dict[str, Any]) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        idle_timeout=0.05,
        max_session_duration=999,
        startup_timeout=999,
    )
    with _mock_exec(proc):
        status = await mgr.start_session("Front Door")
    assert status.session_id
    hls_dir = mgr.get_hls_dir(status.session_id)
    assert hls_dir is not None

    await asyncio.sleep(0.1)
    await mgr._sweep_once()

    assert mgr.get_status().active is False
    assert not hls_dir.exists()


async def test_sweep_leaves_recently_heartbeated_session(
    camera_registry: dict[str, Any],
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        idle_timeout=10.0,
        max_session_duration=999,
        startup_timeout=999,
    )
    with _mock_exec(proc):
        status = await mgr.start_session("Front Door")
    assert status.session_id

    try:
        mgr.heartbeat(status.session_id)
        await mgr._sweep_once()
        assert mgr.get_status().active is True
    finally:
        await mgr.stop_session(status.session_id)


async def test_sweep_tears_down_past_max_duration_regardless_of_heartbeat(
    camera_registry: dict[str, Any],
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        idle_timeout=999,
        max_session_duration=0.05,
        startup_timeout=999,
    )
    with _mock_exec(proc):
        status = await mgr.start_session("Front Door")
    assert status.session_id
    mgr.heartbeat(status.session_id)

    await asyncio.sleep(0.1)
    await mgr._sweep_once()

    assert mgr.get_status().active is False


async def test_sweep_never_went_live_grace_window_then_teardown(
    camera_registry: dict[str, Any],
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        idle_timeout=999,
        max_session_duration=999,
        startup_timeout=0.05,
    )
    with _mock_exec(proc):
        status = await mgr.start_session("Front Door")
    # Never write stream.m3u8 to disk -> state stays "starting" forever.

    await asyncio.sleep(0.1)
    await mgr._sweep_once()
    first = mgr.get_status()
    assert first.active is True
    assert first.state == "error"
    assert status.session_id == first.session_id

    await mgr._sweep_once()
    assert mgr.get_status().active is False


async def test_ffmpeg_crash_sets_error_then_next_tick_tears_down(
    camera_registry: dict[str, Any],
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        idle_timeout=999,
        max_session_duration=999,
        startup_timeout=999,
    )
    with _mock_exec(proc):
        status = await mgr.start_session("Front Door")
    assert status.session_id
    hls_dir = mgr.get_hls_dir(status.session_id)
    assert hls_dir is not None

    proc.crash(code=137)
    await asyncio.sleep(0.05)  # let _watch_ffmpeg's task observe the exit

    first = mgr.get_status()
    assert first.active is True
    assert first.state == "error"
    assert "code 137" in (first.error or "")

    await mgr._sweep_once()
    assert mgr.get_status().active is False
    assert not hls_dir.exists()


async def test_sweep_once_noop_when_nothing_active(manager: LiveViewManager) -> None:
    await manager._sweep_once()
    assert manager.get_status().active is False


# ---------------------------------------------------------------------------
# Manager lifecycle: start()/stop()/close()
# ---------------------------------------------------------------------------


def test_stop_sets_running_false(manager: LiveViewManager) -> None:
    manager._running = True
    manager.stop()
    assert manager._running is False


async def test_start_runs_loop_and_exits_on_stop(manager: LiveViewManager) -> None:
    task = asyncio.create_task(manager.start())
    await asyncio.sleep(0.05)
    assert manager._running is True

    manager.stop()
    await asyncio.wait_for(task, timeout=2)

    assert manager._running is False


async def test_start_exits_on_cancelled_error(manager: LiveViewManager) -> None:
    task = asyncio.create_task(manager.start())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_start_logs_and_reraises_cancellation_during_sweep(
    manager: LiveViewManager,
) -> None:
    """Cancellation while _sweep_once() itself is running (not just during
    the interruptible sleep) must still propagate out of start()."""
    hang = asyncio.Event()

    async def _hang() -> None:
        await hang.wait()

    with patch.object(manager, "_sweep_once", AsyncMock(side_effect=_hang)):
        task = asyncio.create_task(manager.start())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_start_logs_exception_and_continues(manager: LiveViewManager) -> None:
    with patch.object(
        manager, "_sweep_once", AsyncMock(side_effect=RuntimeError("boom"))
    ) as mock_sweep:
        task = asyncio.create_task(manager.start())
        await asyncio.sleep(0.1)
        manager.stop()
        await asyncio.wait_for(task, timeout=2)

    assert mock_sweep.await_count >= 1


async def test_stop_during_sleep_returns_promptly(manager: LiveViewManager) -> None:
    manager._sweep_interval = 5.0  # would time out below if not interruptible
    task = asyncio.create_task(manager.start())
    await asyncio.sleep(0.02)

    manager.stop()
    await asyncio.wait_for(task, timeout=1.0)


async def test_close_stops_loop_and_tears_down_active_session(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        await manager.start_session("Front Door")
    manager._running = True

    await manager.close()

    assert manager._running is False
    assert manager.get_status().active is False
    assert proc.terminate_called is True


async def test_close_noop_when_nothing_active(manager: LiveViewManager) -> None:
    await manager.close()
    assert manager.get_status().active is False


# ---------------------------------------------------------------------------
# ffmpeg terminate/kill escalation
# ---------------------------------------------------------------------------


async def test_teardown_kills_ffmpeg_if_it_ignores_terminate(
    camera_registry: dict[str, Any],
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    # Make terminate() a no-op so the manager's terminate_timeout must elapse
    # and escalate to kill().
    proc.terminate = MagicMock(
        side_effect=lambda: setattr(proc, "terminate_called", True)
    )
    mgr = LiveViewManager(
        get_camera=_get_camera_from(camera_registry),
        list_camera_names=lambda: list(camera_registry),
        terminate_timeout=0.02,
    )
    with _mock_exec(proc):
        status = await mgr.start_session("Front Door")

    await mgr.stop_session(status.session_id)

    assert proc.terminate_called is True
    assert proc.kill_called is True


async def test_teardown_skips_terminate_if_already_exited(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    proc.crash(code=0)  # already exited before stop_session runs
    await asyncio.sleep(0.02)  # let _watch_ffmpeg observe it (harmless here)

    await manager.stop_session(status.session_id)

    assert proc.terminate_called is False


# ---------------------------------------------------------------------------
# _drain_stderr
# ---------------------------------------------------------------------------


async def test_drain_stderr_returns_immediately_when_stderr_is_none(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    proc.stderr = None
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    assert status.session_id

    try:
        internal_session = manager._session
        assert internal_session is not None
        assert internal_session.stderr_task is not None
        await asyncio.wait_for(internal_session.stderr_task, timeout=1)
        assert internal_session.stderr_tail == ""
    finally:
        await manager.stop_session(status.session_id)


async def test_drain_stderr_captures_lines_into_tail(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess(stderr_lines=[b"Connection refused\n", b"retrying...\n"])
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    assert status.session_id

    try:
        internal_session = manager._session
        assert internal_session is not None
        assert internal_session.stderr_task is not None
        await asyncio.wait_for(internal_session.stderr_task, timeout=1)
        assert "Connection refused" in internal_session.stderr_tail
        assert "retrying..." in internal_session.stderr_tail
    finally:
        await manager.stop_session(status.session_id)


async def test_drain_stderr_handles_readline_exception(
    manager: LiveViewManager, camera_registry: dict[str, Any]
) -> None:
    camera_registry["Front Door"] = _make_camera()
    proc = _FakeProcess()
    proc.stderr = _RaisingStderr()
    with _mock_exec(proc):
        status = await manager.start_session("Front Door")
    assert status.session_id

    try:
        internal_session = manager._session
        assert internal_session is not None
        assert internal_session.stderr_task is not None
        # No crash -- the exception is caught and logged, not propagated.
        await asyncio.wait_for(internal_session.stderr_task, timeout=1)
        assert internal_session.stderr_tail == ""
    finally:
        await manager.stop_session(status.session_id)
