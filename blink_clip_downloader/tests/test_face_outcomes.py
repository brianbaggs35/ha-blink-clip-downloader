"""Tests for face_outcomes.py: the tally, the outcome it adds up to, and the
totals the Biometrics tab's Recognition results panel shows."""

from __future__ import annotations

import pytest

from blink_downloader.face_outcomes import (
    HELD_BACK_OUTCOMES,
    FaceOutcome,
    FaceTally,
    classify_face_outcome,
    summarize_face_outcomes,
)
from blink_downloader.vision import FaceRecognitionResult


def _outcome_result(
    tally: FaceTally | None,
    *,
    approved: tuple[str, ...] = (),
    other: tuple[str, ...] = (),
    unrecognized: bool = False,
    set_aside: int = 0,
) -> FaceRecognitionResult:
    return FaceRecognitionResult(
        approved_names=list(approved),
        other_names=list(other),
        unrecognized_present=unrecognized,
        unclear_faces=set_aside,
        tally=tally,
    )


@pytest.mark.parametrize(
    ("result", "recognized", "expected"),
    [
        pytest.param(_outcome_result(None), False, "", id="nothing examined"),
        pytest.param(
            _outcome_result(FaceTally()), False, FaceOutcome.NO_FACES, id="no faces"
        ),
        pytest.param(
            _outcome_result(FaceTally(unexamined=2)),
            False,
            FaceOutcome.UNEXAMINED,
            id="nothing could be read",
        ),
        pytest.param(
            _outcome_result(FaceTally(seen=2, matched=2), approved=("Amy",)),
            True,
            FaceOutcome.RECOGNIZED,
            id="recognized",
        ),
        pytest.param(
            _outcome_result(
                FaceTally(seen=3, matched=2, unclear=1, would_clear=True),
                approved=("Amy", "Brian"),
                set_aside=1,
            ),
            True,
            FaceOutcome.SET_ASIDE,
            id="recognized with a face set aside",
        ),
        pytest.param(
            _outcome_result(
                FaceTally(seen=3, matched=2), approved=("Amy",), unrecognized=True
            ),
            False,
            FaceOutcome.UNFAMILIAR,
            id="a clear face matched nobody",
        ),
        pytest.param(
            _outcome_result(
                FaceTally(seen=3, matched=1, unclear=1),
                other=("Carl",),
                unrecognized=True,
            ),
            False,
            FaceOutcome.UNFAMILIAR,
            id="a clear stranger outranks an unapproved person",
        ),
        pytest.param(
            _outcome_result(FaceTally(seen=1, matched=1), other=("Carl",)),
            False,
            FaceOutcome.NOT_APPROVED,
            id="someone recognized is not approved",
        ),
        pytest.param(
            _outcome_result(
                FaceTally(seen=2, matched=2, unexamined=1),
                approved=("Amy",),
                unrecognized=True,
            ),
            False,
            FaceOutcome.UNEXAMINED,
            id="a frame could not be examined",
        ),
        pytest.param(
            _outcome_result(
                FaceTally(seen=3, matched=2, unclear=1, would_clear=True),
                approved=("Amy", "Brian"),
                unrecognized=True,
            ),
            False,
            FaceOutcome.UNCLEAR_IGNORABLE,
            id="unclear faces the setting would set aside",
        ),
        pytest.param(
            _outcome_result(
                FaceTally(seen=3, matched=2, unclear=1),
                approved=("Amy", "Brian"),
                unrecognized=True,
            ),
            False,
            FaceOutcome.UNCLEAR,
            id="unclear faces nothing would set aside",
        ),
        pytest.param(
            _outcome_result(FaceTally(seen=2, matched=2), approved=("Amy",)),
            False,
            FaceOutcome.EXTRA_PEOPLE,
            id="every face accounted for, more people than faces",
        ),
    ],
)
def test_classify_face_outcome(
    result: FaceRecognitionResult, recognized: bool, expected: str
) -> None:
    assert classify_face_outcome(result, recognized=recognized) == expected


def test_every_clip_held_back_has_a_reason_the_panel_can_list() -> None:
    assert set(HELD_BACK_OUTCOMES) == {
        FaceOutcome.UNFAMILIAR,
        FaceOutcome.UNCLEAR_IGNORABLE,
        FaceOutcome.UNCLEAR,
        FaceOutcome.NOT_APPROVED,
        FaceOutcome.EXTRA_PEOPLE,
        FaceOutcome.UNEXAMINED,
    }


def _outcome_row(
    outcome: str, clips: int, seen: int = 0, matched: int = 0, unclear: int = 0
):
    return {
        "outcome": outcome,
        "clips": clips,
        "faces_seen": seen,
        "faces_matched": matched,
        "faces_unclear": unclear,
    }


def test_summary_of_nothing_is_all_zeros_with_every_reason_present() -> None:
    summary = summarize_face_outcomes([])
    assert summary["clips_checked"] == 0
    assert summary["clips_with_faces"] == 0
    assert summary["held_back"] == {outcome.value: 0 for outcome in HELD_BACK_OUTCOMES}


def test_summary_adds_clips_and_faces_up() -> None:
    summary = summarize_face_outcomes(
        [
            _outcome_row(FaceOutcome.NO_FACES, 10),
            _outcome_row(FaceOutcome.RECOGNIZED, 6, seen=20, matched=20),
            _outcome_row(FaceOutcome.SET_ASIDE, 2, seen=9, matched=7, unclear=2),
            _outcome_row(FaceOutcome.UNFAMILIAR, 3, seen=8, matched=2, unclear=1),
            _outcome_row(
                FaceOutcome.UNCLEAR_IGNORABLE, 4, seen=12, matched=8, unclear=4
            ),
            _outcome_row("something-a-newer-version-wrote", 1, seen=1),
        ]
    )
    assert summary["clips_checked"] == 26
    assert summary["clips_recognized"] == 8
    assert summary["clips_set_aside"] == 2
    assert summary["held_back"][FaceOutcome.UNFAMILIAR.value] == 3
    assert summary["held_back"][FaceOutcome.UNCLEAR_IGNORABLE.value] == 4
    # With faces = recognized + held back, so the panel's figures add up
    # whatever else is in the table.
    assert summary["clips_with_faces"] == 8 + 7
    assert summary["faces_seen"] == 50
    assert summary["faces_matched"] == 37
    assert summary["faces_unclear"] == 7
