"""Live camera streaming via blinkpy's live-view session API.

Blink's live view is not a standard video URL: blinkpy's ``BlinkLiveStream``
relays Blink's proprietary "IMMIS" binary protocol (raw H.264-in-MPEG-TS over
an authenticated TLS connection to Blink's cloud) onto a local raw-TCP
socket. HA ingress only proxies HTTP/WebSocket through this add-on's single
port, so that socket is unreachable from a browser no matter what — this
module bridges it by spawning ffmpeg to remux (not re-encode) the local TCP
stream into a short rolling HLS playlist, served as ordinary files through
media_server.py's existing aiohttp app.

Exactly one session is active at a time (module-level design: "one camera at
a time"). Starting the camera that's already active is idempotent; starting
a different camera stops the old session first. Sessions are in-memory only
— there is nothing to persist across a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .downloader import AUTH_FATAL_EXCEPTIONS

if TYPE_CHECKING:
    from blinkpy.camera import BlinkCamera
    from blinkpy.livestream import BlinkLiveStream

_LOGGER = logging.getLogger(__name__)

_HLS_SEGMENT_PATTERN = "seg_%05d.ts"
_HLS_PLAYLIST_NAME = "stream.m3u8"

# How many trailing ffmpeg stderr lines to keep for crash diagnostics (see
# _drain_stderr) — folded into the error message a crashed session reports,
# since real-hardware behavior here is otherwise unverifiable in tests.
_STDERR_TAIL_LINES = 20


class _AuthenticatingStream(Protocol):
    """The minimal shape ``_patch_short_reads`` actually depends on.

    Deliberately narrower than the full ``BlinkLiveStream`` surface — this
    is the whole contract the patch touches, matched structurally by both
    the real class and this module's tests' fakes, with neither needing to
    nominally subtype the other.
    """

    target_reader: Any

    async def auth(self) -> None: ...


def _patch_short_reads(stream: _AuthenticatingStream) -> None:
    """Work around a blinkpy bug where ``BlinkLiveStream.recv()`` misreads
    an ordinary mid-stream buffering gap as a fatal protocol error.

    ``recv()`` reads each frame as ``target_reader.read(payload_length)``
    and treats any short read as "insufficient data" — logged, then the
    whole session is torn down. But ``asyncio.StreamReader.read(n)`` is
    documented to legitimately return *fewer* than ``n`` bytes whenever
    anything is already buffered, with no EOF in sight; it is not
    ``readexactly()``. Video payloads here are ~1316 bytes and routinely
    arrive split across more than one TLS record, so this fires on
    completely healthy streams within the first few seconds — the "ffmpeg
    exited unexpectedly (code 0)" error users saw was this, not an
    offline camera. (Verified against blinkpy 0.25.9, currently latest —
    no upstream fix to pick up instead.)

    Patched narrowly on this one stream's reader, once ``auth()`` creates
    it, rather than touching any of ``recv()``'s own protocol-parsing
    logic: giving ``.read()`` ``readexactly()``'s accumulate-until-n-or-EOF
    semantics is exactly the contract ``recv()``'s own short-read check
    already assumes, so this can't drift out of sync with blinkpy's actual
    frame parsing on a future bump.
    """
    original_auth = stream.auth

    async def _auth_then_patch_reader() -> None:
        await original_auth()
        # auth() always sets target_reader before returning without
        # raising — see blinkpy's BlinkLiveStream.auth().
        assert stream.target_reader is not None
        original_read = stream.target_reader.read

        async def _accumulated_read(n: int = -1) -> bytes:
            if n <= 0:
                return await original_read(n)
            chunks = bytearray()
            while len(chunks) < n:
                chunk = await original_read(n - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            return bytes(chunks)

        stream.target_reader.read = _accumulated_read

    stream.auth = _auth_then_patch_reader


class LiveViewError(Exception):
    """A live-view operation failed in a way that should reach the caller as
    a clean message, not a raw traceback — ffmpeg missing/crashed, the
    camera not responding in time, an unsupported camera type, or a
    malformed Blink response.

    Deliberately never raised for AUTH_FATAL_EXCEPTIONS (downloader.py) —
    those propagate through this module untouched so app.py's reconnect
    logic can see them; only media_server.py's HTTP handlers catch
    AUTH_FATAL_EXCEPTIONS directly, turning them into a 503.
    """


class CameraNotFoundError(LiveViewError):
    """*camera_name* isn't on the currently authenticated Blink account.

    Also raised when Blink isn't connected yet — every name looks "not
    found" in that case, since list_camera_names() returns [] either way.
    """


@dataclass
class LiveViewStatus:
    """Public, JSON-serializable snapshot of the current session, if any."""

    active: bool
    session_id: str | None = None
    camera: str | None = None
    state: str | None = None  # "starting" | "live" | "error"
    error: str | None = None


@dataclass
class _LiveViewSession:
    """Internal session state — never returned from a public method."""

    session_id: str
    camera_name: str
    stream: BlinkLiveStream
    hls_dir: Path
    ffmpeg_proc: asyncio.subprocess.Process
    started_at: float
    last_heartbeat: float
    feed_task: asyncio.Task | None = None
    watcher_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    error: str | None = None
    stderr_tail: str = ""
    # Set before an intentional kill (explicit stop / idle-timeout / hard-cap
    # / camera switch) so _watch_ffmpeg can tell that apart from a genuine
    # crash instead of recording a spurious error during a clean shutdown.
    stopping: bool = False
    # True once one _sweep_once tick has already observed `error` set —
    # see _sweep_once for why a session isn't torn down the very first tick
    # that notices its error.
    error_seen_by_sweep: bool = False


class LiveViewManager:
    """Starts/stops/monitors the single active Blink live-view session.

    ``get_camera``/``list_camera_names`` are narrow callables (matching the
    ``trigger_download``/``two_fa_callback`` injection idiom already used by
    ``MediaServer``) rather than a reference to ``BlinkDownloader``/``Blink``
    itself — they're called fresh on every invocation so this manager always
    sees the current Blink session even after a reconnect replaces it.
    """

    def __init__(
        self,
        get_camera: Callable[[str], BlinkCamera | None],
        list_camera_names: Callable[[], list[str]],
        idle_timeout: float = 45.0,
        max_session_duration: float = 1800.0,
        sweep_interval: float = 10.0,
        init_timeout: float = 25.0,
        startup_timeout: float = 20.0,
        terminate_timeout: float = 5.0,
    ) -> None:
        self._get_camera = get_camera
        self._list_camera_names = list_camera_names
        self._idle_timeout = idle_timeout
        self._max_session_duration = max_session_duration
        self._sweep_interval = sweep_interval
        self._init_timeout = init_timeout
        self._startup_timeout = startup_timeout
        self._terminate_timeout = terminate_timeout
        self._session: _LiveViewSession | None = None
        self._lock = asyncio.Lock()
        self._running = False

    # ------------------------------------------------------------------
    # HTTP-facing
    # ------------------------------------------------------------------

    def list_cameras(self) -> list[str]:
        """Every camera name on the currently authenticated account."""
        return self._list_camera_names()

    async def start_session(self, camera_name: str) -> LiveViewStatus:
        """Start (or join) a live-view session for *camera_name*.

        Idempotent when a healthy session for the same camera (case
        insensitive) is already active — returns that session's status
        rather than starting a second one, so a second browser tab can join
        the same stream for free. Starting a different camera cleanly stops
        whatever was active first, enforcing "one camera at a time".
        """
        async with self._lock:
            existing = self._session
            if (
                existing is not None
                and existing.error is None
                and existing.camera_name.lower() == camera_name.lower()
            ):
                return self._build_status(existing)

            if existing is not None:
                await self._teardown_locked(existing)

            camera = self._get_camera(camera_name)
            if camera is None:
                raise CameraNotFoundError(camera_name)

            session = await self._create_session(camera_name, camera)
            self._session = session
            return self._build_status(session)

    async def stop_session(self, session_id: str | None = None) -> bool:
        """Stop the active session, if any.

        Returns False (not an error) when nothing is active or *session_id*
        doesn't match the active session — the frontend's stop call arriving
        just after the session already ended on its own (idle timeout, hard
        cap, or another tab switching cameras) is an expected, routine race,
        not a failure.
        """
        async with self._lock:
            session = self._session
            if session is None:
                return False
            if session_id is not None and session.session_id != session_id:
                return False
            await self._teardown_locked(session)
            return True

    def heartbeat(self, session_id: str) -> bool:
        """Record that *session_id* is still being watched.

        Synchronous and lock-free by design: a pure attribute read/write
        with no ``await`` inside can't be interleaved with a concurrent
        mutation under asyncio's cooperative scheduling, and this is the hot
        ~15s-interval path — no reason to make it wait on a lock that might
        be held for the many-second duration of a session start/stop.
        """
        session = self._session
        if session is None or session.session_id != session_id:
            return False
        session.last_heartbeat = time.monotonic()
        return True

    def get_status(self) -> LiveViewStatus:
        """Current session snapshot, or an inactive status if none."""
        session = self._session
        if session is None:
            return LiveViewStatus(active=False)
        return self._build_status(session)

    def get_hls_dir(self, session_id: str) -> Path | None:
        """The HLS output directory for *session_id*, if it's the active one."""
        session = self._session
        if session is None or session.session_id != session_id:
            return None
        return session.hls_dir

    # ------------------------------------------------------------------
    # Manager lifecycle — named to match AnalysisQueue.start()/stop() so
    # app.py's background-task wiring reads the same way for every manager.
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the idle/hard-cap/crash sweep loop (blocks until stopped)."""
        self._running = True
        _LOGGER.info(
            "Live view sweep loop started (check every %.0fs)", self._sweep_interval
        )
        while self._running:
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                _LOGGER.info("Live view sweep loop stopped")
                raise
            except Exception:
                _LOGGER.exception("Live view sweep loop error")

            remaining = self._sweep_interval
            while remaining > 0 and self._running:
                step = min(1.0, remaining)
                await asyncio.sleep(step)
                remaining -= step

        _LOGGER.info("Live view sweep loop stopped")

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        """Stop the sweep loop and tear down any active session."""
        self.stop()
        async with self._lock:
            session = self._session
            if session is not None:
                await self._teardown_locked(session)

    # ------------------------------------------------------------------
    # Session creation
    # ------------------------------------------------------------------

    async def _create_session(
        self, camera_name: str, camera: BlinkCamera
    ) -> _LiveViewSession:
        try:
            stream = await asyncio.wait_for(
                camera.init_livestream(), timeout=self._init_timeout
            )
        except TimeoutError as exc:
            raise LiveViewError(
                "The camera did not respond to the live view request in "
                "time — check that it's online."
            ) from exc
        except NotImplementedError as exc:
            # blinkpy raises this when the liveview response isn't an
            # immis:// URL — see BlinkCamera.init_livestream().
            raise LiveViewError("This camera does not support live view.") from exc
        except KeyError as exc:
            # Defensive: a malformed Blink response missing server/command_id.
            raise LiveViewError(
                "Blink did not return a valid live view session for this camera."
            ) from exc
        except AUTH_FATAL_EXCEPTIONS:
            # Let TokenRefreshFailed/LoginError/UnauthorizedError through
            # untouched so app.py's reconnect logic can see them — only the
            # HTTP handler layer (media_server.py) catches these.
            raise
        except Exception as exc:
            _LOGGER.exception(
                "Live view: could not start live view for %r", camera_name
            )
            raise LiveViewError(f"Could not start live view: {exc}") from exc

        _patch_short_reads(stream)

        try:
            await stream.start(port=0)
        except OSError as exc:
            raise LiveViewError(
                "Could not open a local port for the live view relay."
            ) from exc

        hls_dir = Path(tempfile.mkdtemp(prefix="blink-liveview-"))
        try:
            ffmpeg_proc = await self._spawn_ffmpeg(hls_dir, stream.url)
        except Exception:
            stream.stop()
            await asyncio.to_thread(shutil.rmtree, hls_dir, ignore_errors=True)
            raise

        now = time.monotonic()
        session = _LiveViewSession(
            session_id=uuid.uuid4().hex,
            camera_name=camera_name,
            stream=stream,
            hls_dir=hls_dir,
            ffmpeg_proc=ffmpeg_proc,
            started_at=now,
            last_heartbeat=now,
        )
        # feed() relays bytes/keepalives/command-polling for the session's
        # entire life and must run concurrently — see BlinkLiveStream.feed().
        session.feed_task = asyncio.create_task(
            stream.feed(), name=f"liveview-feed-{session.session_id}"
        )
        session.watcher_task = asyncio.create_task(
            self._watch_ffmpeg(session), name=f"liveview-watch-{session.session_id}"
        )
        session.stderr_task = asyncio.create_task(
            self._drain_stderr(session), name=f"liveview-stderr-{session.session_id}"
        )
        return session

    async def _spawn_ffmpeg(
        self, hls_dir: Path, source_url: str
    ) -> asyncio.subprocess.Process:
        # -c copy: stream copy, no re-encode — near-zero CPU, matching this
        # add-on's existing "cheap on a Raspberry Pi" performance bar.
        # delete_segments is load-bearing (without it, segments pile up in
        # /tmp for the whole session); omit_endlist is what makes Video.js
        # treat the manifest as genuinely live. Relative output paths (not
        # absolute) are required, not cosmetic: cwd=hls_dir is what makes
        # ffmpeg write bare relative segment names into the manifest, which
        # a browser can resolve against the .m3u8's own request URL —
        # absolute paths would end up nonsensical there.
        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-loglevel",
            "error",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-i",
            source_url,
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "6",
            "-hls_flags",
            "delete_segments+omit_endlist+independent_segments",
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            _HLS_SEGMENT_PATTERN,
            _HLS_PLAYLIST_NAME,
        ]
        try:
            return await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(hls_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise LiveViewError("ffmpeg is not available on this system.") from exc

    async def _drain_stderr(self, session: _LiveViewSession) -> None:
        """Actively drain ffmpeg's stderr pipe for the session's whole life.

        Every *other* ffmpeg call in this codebase is short-lived (one-shot
        ``communicate()`` or ``DEVNULL``) — none are long-running. With a
        long-running process and ``stderr=PIPE``, an undrained pipe
        eventually fills its OS buffer and ffmpeg blocks on its own stderr
        write, stalling the whole stream. This also captures a short
        crash-diagnostic tail, folded into the error message a crashed
        session reports.
        """
        tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        stderr = session.ffmpeg_proc.stderr
        if stderr is None:
            return
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                tail.append(line.decode(errors="replace").rstrip())
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Live view: stderr drain for session %s ended: %s",
                session.session_id,
                exc,
            )
        finally:
            session.stderr_tail = "\n".join(tail)

    async def _watch_ffmpeg(self, session: _LiveViewSession) -> None:
        """Detect ffmpeg exiting on its own (a crash, not a requested stop)."""
        returncode = await session.ffmpeg_proc.wait()
        if session.stopping:
            return  # We killed it ourselves — not a crash.
        _LOGGER.warning(
            "Live view: ffmpeg for session %s (camera %r) exited "
            "unexpectedly (code %s)",
            session.session_id,
            session.camera_name,
            returncode,
        )
        session.error = (
            f"ffmpeg exited unexpectedly (code {returncode}) — the camera "
            "may have gone offline or the stream ended."
        )
        # No reason to keep the upstream Blink connection open once ffmpeg
        # can no longer consume it.
        session.stream.stop()
        await self._cancel_task(session.feed_task)

    # ------------------------------------------------------------------
    # Sweep loop
    # ------------------------------------------------------------------

    async def _sweep_once(self) -> None:
        async with self._lock:
            session = self._session
            if session is None:
                return

            if session.error is not None:
                # Give the frontend's status poll (every 4s, see
                # LiveViewPage.vue's STATUS_POLL_INTERVAL_MS) at least one
                # full sweep_interval to observe the error via
                # /api/liveview/status before the session disappears out
                # from under it — tearing down on the very same tick that
                # first notices the error is a race a slow/unlucky poll can
                # lose outright, showing nothing but "Starting live view…"
                # forever instead of the actual error.
                if not session.error_seen_by_sweep:
                    session.error_seen_by_sweep = True
                    return
                await self._teardown_locked(session)
                return

            now = time.monotonic()
            if (
                self._derive_state(session) == "starting"
                and now - session.started_at > self._startup_timeout
            ):
                session.error = (
                    "Live view did not start streaming in time — the "
                    "camera may be offline or slow to respond."
                )
                return

            # Kept as two branches rather than combined with "or" (despite
            # the identical action) so the log line always says which real
            # -world condition actually fired — worth knowing when someone
            # asks "why did my live view stop".
            if now - session.last_heartbeat > self._idle_timeout:
                _LOGGER.info(
                    "Live view: session %s idle-timed-out (no heartbeat for "
                    "over %.0fs)",
                    session.session_id,
                    self._idle_timeout,
                )
                await self._teardown_locked(session)
            elif now - session.started_at > self._max_session_duration:
                _LOGGER.info(
                    "Live view: session %s hit the %.0fs max session duration",
                    session.session_id,
                    self._max_session_duration,
                )
                await self._teardown_locked(session)

    def _derive_state(self, session: _LiveViewSession) -> str:
        """State derived from the filesystem — one source of truth, nothing
        to keep in sync by hand."""
        if session.error is not None:
            return "error"
        if self._playlist_is_ready(session.hls_dir):
            return "live"
        return "starting"

    @staticmethod
    def _playlist_is_ready(hls_dir: Path) -> bool:
        """The manifest merely *existing* isn't enough to call the session
        "live": ffmpeg creates the file before it has written a single
        segment into it, and Video.js fatally errors — with no retry — if
        it's sourced against an empty or partially-written manifest. Require
        at least one #EXTINF segment entry, which ffmpeg only ever writes
        once that segment has been fully flushed to disk."""
        playlist = hls_dir / _HLS_PLAYLIST_NAME
        try:
            return "#EXTINF" in playlist.read_text(errors="ignore")
        except OSError:
            return False

    def _build_status(self, session: _LiveViewSession) -> LiveViewStatus:
        error = session.error
        if error and session.stderr_tail:
            error = f"{error}\n{session.stderr_tail}"
        return LiveViewStatus(
            active=True,
            session_id=session.session_id,
            camera=session.camera_name,
            state=self._derive_state(session),
            error=error,
        )

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _teardown_locked(self, session: _LiveViewSession) -> None:
        """Tear down *session* completely. Caller must hold ``self._lock``."""
        self._session = None
        # Must be set BEFORE terminating ffmpeg: asyncio subprocess .wait()
        # can be awaited concurrently from both _terminate_ffmpeg below and
        # the standing _watch_ffmpeg task, and both resolve together once
        # the process exits — without this flag, _watch_ffmpeg would
        # misread this intentional kill as a crash.
        session.stopping = True
        await self._terminate_ffmpeg(session)
        session.stream.stop()
        await self._cancel_task(session.feed_task)
        await self._cancel_task(session.watcher_task)
        await self._cancel_task(session.stderr_task)
        await asyncio.to_thread(shutil.rmtree, session.hls_dir, ignore_errors=True)

    async def _terminate_ffmpeg(self, session: _LiveViewSession) -> None:
        proc = session.ffmpeg_proc
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._terminate_timeout)
        except TimeoutError:
            # Timing out leaves the child process running; kill it and reap
            # it so it doesn't linger as a zombie/orphan — mirrors
            # downloader.py's _generate_thumbnail kill-then-reap handling.
            proc.kill()
            await proc.wait()

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
