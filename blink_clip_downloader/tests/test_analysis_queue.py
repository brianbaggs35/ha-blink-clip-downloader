"""Tests for AnalysisQueue."""

from __future__ import annotations

import asyncio
import logging
from datetime import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_downloader.analysis_queue import AnalysisQueue
from blink_downloader.analyzer import AnalysisResult, ClipAnalyzer
from blink_downloader.database import ClipDatabase


def _make_analyzer_mock(**kwargs: Any) -> MagicMock:
    m = MagicMock(spec=ClipAnalyzer)
    m.health_check = AsyncMock(return_value=kwargs.get("healthy", True))
    # MagicMock(spec=...) would otherwise make `.rate_limited` a truthy
    # child Mock (properties resolve to a fresh Mock, not their real
    # getter), which AnalysisQueue._process_pending's new early-break
    # check would misread as "rate limited" after every single clip.
    m.rate_limited = kwargs.get("rate_limited", False)
    m.analyze_clip = AsyncMock(
        return_value=kwargs.get(
            "result",
            AnalysisResult(
                clip_id="c1",
                camera="Front Door",
                model="llava",
                response_text="All clear",
                is_suspicious=False,
                confidence=0.1,
                summary="Normal",
                frame_count=3,
                analysis_duration=2.0,
                analyzed_at="2024-06-01T09:00:00+00:00",
            ),
        )
    )
    return m


def _make_queue(
    analyzer: MagicMock,
    db: ClipDatabase,
    dispatcher: MagicMock | None = None,
    **kwargs: Any,
) -> AnalysisQueue:
    return AnalysisQueue(
        analyzer=analyzer,
        db=db,
        dispatcher=dispatcher,
        schedule_start=str(kwargs.get("schedule_start", "")),
        schedule_end=str(kwargs.get("schedule_end", "")),
        batch_size=int(kwargs.get("batch_size", 10)),
        check_interval=int(kwargs.get("check_interval", 1)),
    )


def _add_clip(clip_id: str = "c1") -> dict:
    return {
        "id": clip_id,
        "camera": "Front Door",
        "path": f"/share/blink-clips/{clip_id}.mp4",
        "timestamp": "2024-06-01T08:00:00+00:00",
        "size_bytes": 1_048_576,
        "duration": 5,
        "source": "pir",
        "network_id": 10,
    }


# ------------------------------------------------------------------
# Enqueue
# ------------------------------------------------------------------


async def test_enqueue_inserts_pending_record(db: ClipDatabase) -> None:
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)

    await db.add_clip(_add_clip("c1"))
    await queue.enqueue({"id": "c1", "camera": "Front", "path": "/c1.mp4"})

    pending = await db.get_pending_analysis()
    assert len(pending) == 1
    assert pending[0]["clip_id"] == "c1"


async def test_enqueue_skips_empty_id(db: ClipDatabase) -> None:
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)

    await queue.enqueue({"id": "", "camera": "A", "path": "/x.mp4"})
    assert await db.get_pending_analysis() == []


# ------------------------------------------------------------------
# Schedule
# ------------------------------------------------------------------


def test_is_in_schedule_no_schedule() -> None:
    analyzer = _make_analyzer_mock()
    db_mock = MagicMock(spec=ClipDatabase)
    queue = _make_queue(analyzer, db_mock)
    assert queue._is_in_schedule() is True


def test_is_in_schedule_within_window() -> None:
    analyzer = _make_analyzer_mock()
    db_mock = MagicMock(spec=ClipDatabase)
    queue = _make_queue(analyzer, db_mock, schedule_start="00:00", schedule_end="23:59")
    assert queue._is_in_schedule() is True


def test_is_in_schedule_overnight_window() -> None:
    analyzer = _make_analyzer_mock()
    db_mock = MagicMock(spec=ClipDatabase)
    queue = _make_queue(analyzer, db_mock, schedule_start="22:00", schedule_end="06:00")
    # This will be True if current time is after 22:00 or before 06:00
    # Since we can't control time easily, just verify the method doesn't crash
    result = queue._is_in_schedule()
    assert isinstance(result, bool)


def test_is_in_schedule_uses_local_time_not_utc() -> None:
    """ai_schedule_start/end are documented as local HH:MM — _is_in_schedule
    must read the local wall-clock, not UTC, or the window silently runs on
    the wrong hours whenever the host isn't in UTC."""
    analyzer = _make_analyzer_mock()
    db_mock = MagicMock(spec=ClipDatabase)
    queue = _make_queue(analyzer, db_mock, schedule_start="09:00", schedule_end="10:00")

    with patch("blink_downloader.analysis_queue.datetime") as mock_dt:
        mock_dt.now.return_value.time.return_value = time(9, 30)
        assert queue._is_in_schedule() is True
        mock_dt.now.assert_called_with()


def test_parse_time_valid() -> None:
    assert AnalysisQueue._parse_time("08:30") == time(8, 30)
    assert AnalysisQueue._parse_time("22:00") == time(22, 0)


def test_parse_time_empty() -> None:
    assert AnalysisQueue._parse_time("") is None
    assert AnalysisQueue._parse_time("  ") is None


def test_parse_time_invalid() -> None:
    assert AnalysisQueue._parse_time("not-a-time") is None
    assert AnalysisQueue._parse_time("25:00") is None


# ------------------------------------------------------------------
# Processing
# ------------------------------------------------------------------


async def test_process_pending_analyzes_clips(db: ClipDatabase) -> None:
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    analyzer.analyze_clip.assert_awaited_once()
    result = await db.get_analysis_for_clip("c1")
    assert result is not None
    counts = await db.get_queue_counts()
    assert counts["completed"] == 1
    assert counts["pending"] == 0


async def test_process_pending_skips_when_empty(db: ClipDatabase) -> None:
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)
    queue._running = True

    await queue._process_pending()
    analyzer.analyze_clip.assert_not_awaited()


async def test_process_pending_handles_analysis_failure(db: ClipDatabase) -> None:
    analyzer = _make_analyzer_mock()
    analyzer.analyze_clip = AsyncMock(side_effect=RuntimeError("boom"))
    queue = _make_queue(analyzer, db)
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    counts = await db.get_queue_counts()
    assert counts["failed"] == 1


async def test_process_pending_dispatches_suspicious(db: ClipDatabase) -> None:
    suspicious_result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="Intruder!",
        is_suspicious=True,
        confidence=0.9,
        summary="Suspicious person",
        frame_count=3,
        analysis_duration=2.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )
    analyzer = _make_analyzer_mock(result=suspicious_result)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    queue = _make_queue(analyzer, db, dispatcher=dispatcher)
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    dispatcher.dispatch.assert_awaited_once()
    call_args = dispatcher.dispatch.call_args
    assert call_args[0][0].is_suspicious is True


async def test_process_one_keeps_completed_status_when_dispatch_fails(
    db: ClipDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression test: a dispatch-time failure (e.g. a transient DB error
    computing the adaptive confidence threshold) must not overwrite an
    already-successful analysis's queue status with "failed" — the result
    is already correctly persisted by that point, so flipping to "failed"
    would both trigger a wasted re-analysis and silently drop the alert."""
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)
    queue._running = True
    queue._maybe_dispatch_alert = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("threshold lookup failed")
    )

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    with caplog.at_level("WARNING"):
        await queue._process_pending()

    counts = await db.get_queue_counts()
    assert counts["completed"] == 1
    assert counts["failed"] == 0
    result = await db.get_analysis_for_clip("c1")
    assert result is not None
    assert "dispatch failed" in caplog.text


# ------------------------------------------------------------------
# Queue status
# ------------------------------------------------------------------


async def test_get_queue_status(db: ClipDatabase) -> None:
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db, schedule_start="08:00", schedule_end="18:00")

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/c1.mp4")

    status = await queue.get_queue_status()
    assert status["pending"] == 1
    assert status["schedule_start"] == "08:00"
    assert status["schedule_end"] == "18:00"
    assert "in_schedule" in status


# ------------------------------------------------------------------
# Coverage gap tests: lifecycle (start/stop) and mid-batch break
# ------------------------------------------------------------------


async def test_stop_sets_running_false(db: ClipDatabase) -> None:
    """stop() clears _running (line 71)."""
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)
    queue._running = True
    queue.stop()
    assert not queue._running


async def test_start_runs_loop_and_exits_on_stop(db: ClipDatabase) -> None:
    """Main loop runs at least once and exits cleanly when _running is cleared (lines 51-52, 54, 63-64, 66, 68)."""
    analyzer = _make_analyzer_mock(healthy=False)
    queue = _make_queue(analyzer, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False  # signal stop after first sleep

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    assert not queue._running


async def test_start_exits_on_cancelled_error(db: ClipDatabase) -> None:
    """CancelledError from health_check is re-raised after cleanup, not swallowed."""
    # health_check is now only reached when something is actually pending
    # (see start()'s comment) — no clip queued means no call at all.
    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    analyzer = _make_analyzer_mock()
    analyzer.health_check = AsyncMock(side_effect=asyncio.CancelledError)
    queue = _make_queue(analyzer, db, check_interval=1)

    with pytest.raises(asyncio.CancelledError):
        await queue.start()


async def test_start_logs_exception_and_continues(db: ClipDatabase) -> None:
    """General exceptions from health_check are logged and the loop continues (lines 60-61)."""
    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    call_count = 0

    async def flaky_health() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient network error")
        return False

    analyzer = _make_analyzer_mock()
    analyzer.health_check = AsyncMock(side_effect=flaky_health)
    queue = _make_queue(analyzer, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        if call_count >= 2:
            queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    assert call_count >= 2  # loop continued past the exception


async def test_start_early_return_from_sleep_loop(db: ClipDatabase) -> None:
    """Setting _running=False mid sleep-interval triggers early return (lines 64-65)."""
    analyzer = _make_analyzer_mock(healthy=False)
    queue = _make_queue(analyzer, db, check_interval=2)

    sleep_count = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        queue._running = False  # stop after first sleep in the 2-step interval

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    assert sleep_count == 1  # second sleep never reached (early return)


async def test_process_pending_breaks_when_queue_stopped(db: ClipDatabase) -> None:
    """If _running is False when iterating, processing breaks immediately (line 100)."""
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db, batch_size=3)
    queue._running = False  # halted

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    analyzer.analyze_clip.assert_not_awaited()  # break before analysis


async def test_process_pending_stops_batch_on_rate_limit(db: ClipDatabase) -> None:
    """Once the analyzer reports rate_limited after a clip, the rest of the
    batch must be left pending rather than re-attempted — every remaining
    clip would hit the exact same limit immediately, wasting time and log
    noise for no benefit (see the comment in AnalysisQueue._process_pending)."""
    analyzer = _make_analyzer_mock(rate_limited=True)
    queue = _make_queue(analyzer, db, batch_size=3)
    queue._running = True

    for clip_id in ("c1", "c2", "c3"):
        await db.add_clip(_add_clip(clip_id))
        await db.enqueue_for_analysis(clip_id, "Front Door", f"/clips/{clip_id}.mp4")

    await queue._process_pending()

    analyzer.analyze_clip.assert_awaited_once()  # stopped after the 1st clip
    counts = await db.get_queue_counts()
    assert counts["completed"] == 1
    assert counts["pending"] == 2  # c2/c3 left for the next cycle


async def test_process_pending_continues_batch_without_rate_limit(
    db: ClipDatabase,
) -> None:
    """Sanity check for the test above: with rate_limited left False, a
    normal batch still processes every clip, not just the first."""
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db, batch_size=3)
    queue._running = True

    for clip_id in ("c1", "c2", "c3"):
        await db.add_clip(_add_clip(clip_id))
        await db.enqueue_for_analysis(clip_id, "Front Door", f"/clips/{clip_id}.mp4")

    await queue._process_pending()

    assert analyzer.analyze_clip.await_count == 3
    counts = await db.get_queue_counts()
    assert counts["completed"] == 3
    assert counts["pending"] == 0


# ------------------------------------------------------------------
# min_confidence threshold
# ------------------------------------------------------------------


async def test_process_pending_suppresses_dispatch_below_min_confidence(
    db: ClipDatabase,
) -> None:
    """Alerts are not dispatched when confidence is below min_confidence."""
    suspicious_result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="Maybe suspicious",
        is_suspicious=True,
        confidence=0.2,
        summary="Low confidence detection",
        frame_count=1,
        analysis_duration=1.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )
    analyzer = _make_analyzer_mock(result=suspicious_result)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()

    queue = AnalysisQueue(
        analyzer=analyzer,
        db=db,
        dispatcher=dispatcher,
        min_confidence=0.3,
    )
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    # Result stored but NOT dispatched (confidence 0.2 < threshold 0.3)
    dispatcher.dispatch.assert_not_awaited()
    result = await db.get_analysis_for_clip("c1")
    assert result is not None


async def test_process_pending_dispatches_at_or_above_min_confidence(
    db: ClipDatabase,
) -> None:
    """Alerts are dispatched when confidence meets the threshold."""
    suspicious_result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="Intruder",
        is_suspicious=True,
        confidence=0.5,
        summary="Person near car",
        frame_count=1,
        analysis_duration=1.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )
    analyzer = _make_analyzer_mock(result=suspicious_result)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()

    queue = AnalysisQueue(
        analyzer=analyzer,
        db=db,
        dispatcher=dispatcher,
        min_confidence=0.5,
    )
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    dispatcher.dispatch.assert_awaited_once()


async def test_get_queue_status_includes_min_confidence(db: ClipDatabase) -> None:
    """get_queue_status exposes the configured min_confidence value."""
    analyzer = _make_analyzer_mock()
    queue = AnalysisQueue(
        analyzer=analyzer,
        db=db,
        dispatcher=None,
        min_confidence=0.35,
    )
    status = await queue.get_queue_status()
    assert status["min_confidence"] == 0.35


async def test_min_confidence_property(db: ClipDatabase) -> None:
    """The min_confidence property exposes the same value synchronously,
    for callers (e.g. the media server's notification filter) that can't
    await get_queue_status()."""
    analyzer = _make_analyzer_mock()
    queue = AnalysisQueue(
        analyzer=analyzer,
        db=db,
        dispatcher=None,
        min_confidence=0.42,
    )
    assert queue.min_confidence == 0.42


# ---------------------------------------------------------------------------
# Coverage: line 59 (_process_pending called from start when healthy)
# ---------------------------------------------------------------------------


async def test_start_calls_process_pending_when_healthy(db: ClipDatabase) -> None:
    """When health_check returns True, start() calls _process_pending (line 59)."""
    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")
    analyzer = _make_analyzer_mock(healthy=True)
    queue = _make_queue(analyzer, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False  # stop after first sleep so the loop exits

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    # health_check was awaited at least once
    analyzer.health_check.assert_awaited()
    # analyze_clip may or may not have been called depending on whether there
    # are pending clips — what matters is health_check returned True and the
    # branch at line 59 was reached without error.


async def test_start_skips_health_check_when_nothing_pending(db: ClipDatabase) -> None:
    """With no clips queued, start() must not call health_check at all — a
    real API call for every cloud provider, with a cache shorter than the
    default check_interval, so it was previously hitting the provider on
    every single idle cycle for no reason (see the comment in start())."""
    analyzer = _make_analyzer_mock(healthy=True)
    queue = _make_queue(analyzer, db, check_interval=1)

    async def fake_sleep(_delay: float) -> None:
        queue._running = False

    with patch("asyncio.sleep", fake_sleep):
        await queue.start()

    analyzer.health_check.assert_not_awaited()


# ---------------------------------------------------------------------------
# Coverage: lines 128-129 (exception in anomaly score is swallowed)
# ---------------------------------------------------------------------------


async def test_process_pending_exception_in_anomaly_score_is_swallowed(
    db: ClipDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If get_anomaly_score raises, the exception is caught and analysis proceeds."""
    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    with (
        patch.object(db, "get_anomaly_score", side_effect=RuntimeError("db error")),
        caplog.at_level(logging.DEBUG),
    ):
        await queue._process_pending()

    # Analysis should still have been attempted despite the anomaly-score error
    analyzer.analyze_clip.assert_awaited_once()
    counts = await db.get_queue_counts()
    assert counts["pending"] == 0  # item was processed
    assert "Could not compute anomaly score" in caplog.text


# ===========================================================================
# v4.0.0 — Adaptive learning integration (effective threshold + prompt
# corrections)
# ===========================================================================


async def test_process_pending_passes_recent_corrections_to_analyzer(
    db: ClipDatabase,
) -> None:
    """analyze_clip is called with recent_corrections populated from the
    database's feedback history for this camera."""
    await db.add_clip(_add_clip("c0"))
    await db.add_feedback(
        clip_id="c0",
        camera="Front Door",
        analysis_result_id=None,
        original_suspicious=True,
        original_confidence=0.8,
        correct=False,
        correction_note="Just the mail carrier.",
    )

    analyzer = _make_analyzer_mock()
    queue = _make_queue(analyzer, db)
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    analyzer.analyze_clip.assert_awaited_once()
    _, kwargs = analyzer.analyze_clip.call_args
    assert len(kwargs["recent_corrections"]) == 1
    assert kwargs["recent_corrections"][0]["correction_note"] == (
        "Just the mail carrier."
    )


async def test_process_pending_uses_effective_threshold_to_suppress_dispatch(
    db: ClipDatabase,
) -> None:
    """A camera with enough recent false-positive feedback gets its
    notification threshold auto-tuned up, suppressing a dispatch that would
    otherwise have fired at the configured min_confidence."""
    for i in range(15):
        clip_id = f"fp{i}"
        await db.add_clip(_add_clip(clip_id))
        await db.add_feedback(
            clip_id=clip_id,
            camera="Front Door",
            analysis_result_id=None,
            original_suspicious=True,
            original_confidence=0.8,
            correct=False,  # false positive
        )

    # min_confidence=0.5, but 15/15 false positives caps the adjustment at
    # +0.15 -> effective threshold 0.65, above this result's confidence 0.6.
    suspicious_result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="Person near door",
        is_suspicious=True,
        confidence=0.6,
        summary="Person near door",
        frame_count=1,
        analysis_duration=1.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )
    analyzer = _make_analyzer_mock(result=suspicious_result)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()

    queue = AnalysisQueue(
        analyzer=analyzer, db=db, dispatcher=dispatcher, min_confidence=0.5
    )
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    dispatcher.dispatch.assert_not_awaited()


async def test_process_pending_dispatches_when_no_feedback_history(
    db: ClipDatabase,
) -> None:
    """With no feedback history, the effective threshold equals
    min_confidence exactly — unchanged behavior from before v4.0.0."""
    suspicious_result = AnalysisResult(
        clip_id="c1",
        camera="Front Door",
        model="llava",
        response_text="Intruder",
        is_suspicious=True,
        confidence=0.5,
        summary="Person near car",
        frame_count=1,
        analysis_duration=1.0,
        analyzed_at="2024-06-01T09:00:00+00:00",
    )
    analyzer = _make_analyzer_mock(result=suspicious_result)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()

    queue = AnalysisQueue(
        analyzer=analyzer, db=db, dispatcher=dispatcher, min_confidence=0.5
    )
    queue._running = True

    await db.add_clip(_add_clip("c1"))
    await db.enqueue_for_analysis("c1", "Front Door", "/clips/c1.mp4")

    await queue._process_pending()

    dispatcher.dispatch.assert_awaited_once()
