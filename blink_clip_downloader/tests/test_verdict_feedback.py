"""Tests for verdict_feedback: the feedback row the Library and phone alerts share."""

from __future__ import annotations

from blink_downloader.database import ClipDatabase
from blink_downloader.verdict_feedback import (
    FALSE_NEGATIVE_NOTE,
    FALSE_POSITIVE_NOTE,
    record_not_a_threat,
    record_verdict_feedback,
)


async def _analyzed_clip(db: ClipDatabase, clip_id: str, *, suspicious: bool) -> None:
    await db.add_clip(
        {
            "id": clip_id,
            "camera": "Front Door",
            "path": f"/share/{clip_id}.mp4",
            "timestamp": "2026-09-25T04:00:00+00:00",
            "size_bytes": 1,
            "duration": 5,
            "source": "pir",
            "network_id": 1,
        }
    )
    await db.add_analysis_result(
        {
            "clip_id": clip_id,
            "camera": "Front Door",
            "model": "llava",
            "response_text": "",
            "is_suspicious": suspicious,
            "confidence": 0.8,
            "summary": "Person at door",
            "frame_count": 1,
            "analysis_duration": 1.0,
            "analyzed_at": "2026-09-25T04:01:00+00:00",
        }
    )


async def test_nothing_is_recorded_for_a_clip_with_no_verdict(db: ClipDatabase) -> None:
    assert await record_verdict_feedback(db, "ghost", correct=False) is False
    assert await record_not_a_threat(db, "ghost") is False
    assert await db.get_feedback_for_clip("ghost") is None


async def test_a_bare_thumbs_down_on_a_flag_derives_the_label_and_the_note(
    db: ClipDatabase,
) -> None:
    await _analyzed_clip(db, "c1", suspicious=True)

    assert await record_verdict_feedback(db, "c1", correct=False) is True

    row = await db.get_feedback_for_clip("c1")
    assert row is not None
    assert row["correct"] is False
    assert row["corrected_suspicious"] is False
    assert row["correction_note"] == FALSE_POSITIVE_NOTE
    assert row["original_suspicious"] is True
    assert row["original_confidence"] == 0.8


async def test_a_thumbs_down_on_a_clear_verdict_means_it_should_have_flagged(
    db: ClipDatabase,
) -> None:
    await _analyzed_clip(db, "c1", suspicious=False)

    await record_verdict_feedback(db, "c1", correct=False)

    row = await db.get_feedback_for_clip("c1")
    assert row is not None
    assert row["corrected_suspicious"] is True
    assert row["correction_note"] == FALSE_NEGATIVE_NOTE


async def test_what_the_reviewer_said_is_kept(db: ClipDatabase) -> None:
    await _analyzed_clip(db, "c1", suspicious=True)

    await record_verdict_feedback(
        db,
        "c1",
        correct=False,
        correction_note="The mail carrier.",
        corrected_suspicious=True,
    )

    row = await db.get_feedback_for_clip("c1")
    assert row is not None
    assert row["correction_note"] == "The mail carrier."
    assert row["corrected_suspicious"] is True


async def test_a_thumbs_up_needs_no_note_and_no_corrected_label(
    db: ClipDatabase,
) -> None:
    await _analyzed_clip(db, "c1", suspicious=True)

    await record_verdict_feedback(db, "c1", correct=True)

    row = await db.get_feedback_for_clip("c1")
    assert row is not None
    assert row["correct"] is True
    assert row["corrected_suspicious"] is None
    assert row["correction_note"] == ""


async def test_not_a_threat_on_a_flag_is_the_librarys_thumbs_down(
    db: ClipDatabase,
) -> None:
    await _analyzed_clip(db, "c1", suspicious=True)

    assert await record_not_a_threat(db, "c1") is True

    row = await db.get_feedback_for_clip("c1")
    assert row is not None
    assert row["correct"] is False
    assert row["corrected_suspicious"] is False
    assert row["correction_note"] == FALSE_POSITIVE_NOTE


async def test_not_a_threat_agrees_with_a_verdict_that_is_already_clear(
    db: ClipDatabase,
) -> None:
    """Re-analysis can clear a clip after its alert went out. Tapping "Not a
    threat" then agrees with the stored verdict; recording it as a
    thumbs-down would teach the camera the opposite — that it should have
    flagged the clip."""
    await _analyzed_clip(db, "c1", suspicious=False)

    assert await record_not_a_threat(db, "c1") is True

    row = await db.get_feedback_for_clip("c1")
    assert row is not None
    assert row["correct"] is True
    assert row["corrected_suspicious"] is None
