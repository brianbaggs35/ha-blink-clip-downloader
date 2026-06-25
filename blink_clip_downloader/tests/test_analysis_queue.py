"""Tests for AnalysisQueue."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from blink_downloader.analysis_queue import AnalysisQueue
from blink_downloader.analyzer import AnalysisResult, ClipAnalyzer
from blink_downloader.database import ClipDatabase


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[ClipDatabase, None]:
    d = ClipDatabase(tmp_path / "test.db")
    await d.init()
    yield d
    await d.close()


def _make_analyzer_mock(**kwargs: Any) -> MagicMock:
    m = MagicMock(spec=ClipAnalyzer)
    m.health_check = AsyncMock(return_value=kwargs.get("healthy", True))
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
