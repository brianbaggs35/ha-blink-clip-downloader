"""Recording a person's verdict on the AI's verdict for one clip.

One implementation shared by the two places a person can give it: the
Library's thumbs up/down (``media_server/feedback.py``) and the "Not a
threat" button on a phone alert (``alert_actions.py``). Both have to write
the same row the same way, or the learning loop would be trained on two
slightly different meanings of "incorrect" depending on where someone
happened to react.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import ClipDatabase

# Stored as the correction note when a reviewer gave no words of their own,
# so the row still becomes usable prompt guidance (get_prompt_corrections
# only folds in rows with a non-empty note).
FALSE_POSITIVE_NOTE = (
    "Reviewer marked this as ordinary, routine activity that "
    "was incorrectly flagged suspicious."
)
FALSE_NEGATIVE_NOTE = (
    "Reviewer marked this as genuinely suspicious activity "
    "that was incorrectly cleared."
)


async def record_verdict_feedback(
    db: ClipDatabase,
    clip_id: str,
    *,
    correct: bool,
    correction_note: str = "",
    corrected_suspicious: bool | None = None,
) -> bool:
    """Store feedback on *clip_id*'s stored verdict. False if it has none.

    Feedback is a correction on an existing verdict, not a substitute for
    one, so a clip that was never analyzed records nothing.
    """
    result = await db.get_analysis_for_clip(clip_id)
    if not result:
        return False

    # correct=False always means the single is_suspicious boolean was
    # wrong — there is no third option, so the corrected value is fully
    # determined by the original one. Derived whenever the caller doesn't
    # override it, rather than left null: the Moondream fine-tuning
    # training-example builder falls back to original_suspicious for a null
    # corrected_suspicious, which silently trained toward the *wrong* label
    # for exactly the case this is meant to fix.
    if not correct and corrected_suspicious is None:
        corrected_suspicious = not result["is_suspicious"]

    # A bare thumbs-down with no typed note carries no reusable signal for
    # get_prompt_corrections, which only folds in rows with a non-empty
    # correction_note. Synthesize one from the direction of the correction
    # so every "incorrect" rating still becomes few-shot guidance.
    if not correct and not correction_note.strip():
        correction_note = (
            FALSE_POSITIVE_NOTE if result["is_suspicious"] else FALSE_NEGATIVE_NOTE
        )

    await db.add_feedback(
        clip_id=clip_id,
        camera=result["camera"],
        analysis_result_id=result.get("id"),
        original_suspicious=bool(result["is_suspicious"]),
        original_confidence=float(result["confidence"]),
        correct=correct,
        correction_note=correction_note,
        corrected_suspicious=corrected_suspicious,
    )
    return True


async def record_not_a_threat(db: ClipDatabase, clip_id: str) -> bool:
    """Record "this was not a threat" for *clip_id*. False if it has no verdict.

    Matches the Library's thumbs-down on a flagged clip. When the stored
    verdict is already a clear one (the clip was re-analyzed after the
    alert went out), "not a threat" *agrees* with it, so it is recorded as
    correct — a thumbs-down there would mean the opposite, "this should
    have been flagged".
    """
    result = await db.get_analysis_for_clip(clip_id)
    if not result:
        return False
    return await record_verdict_feedback(
        db, clip_id, correct=not result["is_suspicious"]
    )
