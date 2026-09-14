"""Tests for blink_downloader.security.evidence."""

from __future__ import annotations

import pytest

from blink_downloader.security.evidence import (
    TOTAL_OPTIONAL_SOURCES,
    EvidenceQuality,
    _scale,
    assess_evidence,
)
from blink_downloader.security.geometry import Box
from blink_downloader.security.tracks import ObjectTrack, TrackPoint

FRAME = (640.0, 360.0)
BIG: Box = (100.0, 100.0, 180.0, 320.0)  # 220px tall of 360 => 61% of height
TINY: Box = (100.0, 100.0, 110.0, 120.0)  # 20px tall => 5.5%


def _track(
    box: Box,
    *,
    label: str = "person",
    frames: list[int] | None = None,
    confidence: float = 0.9,
    tracked: bool = True,
) -> ObjectTrack:
    indices = frames if frames is not None else [0, 1, 2]
    return ObjectTrack(
        label=label,
        track_id=1 if tracked else None,
        points=[TrackPoint(i, i * 2.0, box, confidence) for i in indices],
        frame_size=FRAME,
        tracked=tracked,
    )


# ----------------------------------------------------------------------
# EvidenceQuality
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "label"),
    [
        (0.0, "weak"),
        (0.39, "weak"),
        (0.4, "moderate"),
        (0.69, "moderate"),
        (0.7, "strong"),
    ],
)
def test_label_bands(score: float, label: str) -> None:
    assert EvidenceQuality(score=score).label == label


def test_to_dict_rounds_and_copies() -> None:
    quality = EvidenceQuality(
        score=0.123456,
        factors={"frame_coverage": 0.98765},
        notes=["a note"],
        unavailable=["depth estimation"],
    )
    payload = quality.to_dict()
    assert payload["score"] == 0.1235
    assert payload["factors"] == {"frame_coverage": 0.9877}
    assert payload["label"] == "weak"
    assert payload["notes"] == ["a note"]
    assert payload["unavailable"] == ["depth estimation"]


# ----------------------------------------------------------------------
# assess_evidence
# ----------------------------------------------------------------------


def test_clear_close_subject_scores_strongly() -> None:
    quality = assess_evidence(6, 6, [_track(BIG)], 2.0)
    assert quality.score > 0.85
    assert quality.label == "strong"
    assert quality.notes == []


def test_tiny_subject_is_capped_regardless_of_everything_else() -> None:
    """Full frame coverage, a confident detector and unbroken tracking must
    not average a 20-pixel figure up into usable evidence."""
    quality = assess_evidence(6, 6, [_track(TINY)], 2.0)
    assert quality.score < 0.4
    assert quality.label == "weak"
    assert any("frame height" in note for note in quality.notes)


def test_missing_frames_lower_coverage_and_are_noted() -> None:
    quality = assess_evidence(2, 6, [_track(BIG)], 2.0)
    assert quality.factors["frame_coverage"] == pytest.approx(2 / 6)
    assert any("intended frames" in note for note in quality.notes)


def test_full_coverage_does_not_add_a_note() -> None:
    quality = assess_evidence(8, 6, [_track(BIG)], 2.0)
    assert quality.factors["frame_coverage"] == 1.0


def test_unavailable_sources_reduce_stage_coverage_and_are_reported() -> None:
    quality = assess_evidence(6, 6, [_track(BIG)], 2.0, ["depth estimation"])
    assert quality.factors["stage_coverage"] == pytest.approx(
        1 - 1 / TOTAL_OPTIONAL_SOURCES
    )
    assert quality.unavailable == ["depth estimation"]


def test_more_unavailable_sources_than_expected_floors_at_zero() -> None:
    quality = assess_evidence(6, 6, [_track(BIG)], 2.0, ["a", "b", "c", "d"])
    assert quality.factors["stage_coverage"] == 0.0


def test_untracked_subjects_halve_continuity_and_say_why() -> None:
    quality = assess_evidence(6, 6, [_track(BIG, tracked=False)], 2.0)
    assert quality.factors["tracking_continuity"] == pytest.approx(0.5)
    assert any("could not be told apart" in note for note in quality.notes)


def test_broken_tracking_is_noted_separately_from_absent_tracking() -> None:
    quality = assess_evidence(6, 6, [_track(BIG, frames=[0, 4, 8])], 2.0)
    assert quality.factors["tracking_continuity"] < 0.7
    assert any("lost and reacquired" in note for note in quality.notes)


def test_no_subject_drops_subject_factors_and_says_so() -> None:
    """Good frames with nothing in them is strong evidence *of nothing*, so
    the subject-dependent factors are dropped rather than scored zero."""
    quality = assess_evidence(6, 6, [], 2.0)
    assert "subject_size" not in quality.factors
    assert quality.score > 0.9
    assert quality.notes == ["no person or animal was detected in the analyzed frames"]


def test_vehicle_only_tracks_count_as_no_subject() -> None:
    quality = assess_evidence(6, 6, [_track(BIG, label="car")], 2.0)
    assert "subject_size" not in quality.factors


def test_zero_target_frames_scores_no_coverage() -> None:
    quality = assess_evidence(0, 0, [], 2.0)
    assert quality.factors["frame_coverage"] == 0.0


def test_scale_maps_onto_the_unit_range() -> None:
    assert _scale(0.0, 0.0, 10.0) == 0.0
    assert _scale(5.0, 0.0, 10.0) == pytest.approx(0.5)
    assert _scale(50.0, 0.0, 10.0) == 1.0


def test_scale_with_a_degenerate_range_is_zero() -> None:
    """Defensive: an inverted or empty band has no meaningful midpoint, so
    it scores nothing rather than dividing by zero."""
    assert _scale(5.0, 10.0, 10.0) == 0.0
