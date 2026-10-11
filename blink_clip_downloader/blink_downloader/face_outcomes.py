"""What face recognition made of a clip, and why it did or did not count.

The numbers behind the Biometrics tab's Recognition results panel: a
:class:`FaceTally` of what recognition counted in one clip, the
:class:`FaceOutcome` that tally adds up to, and the totals the panel shows.
Numbers and a code only -- never who: nothing here carries a name, a photo or
an embedding.

Standard library only, and in its own module rather than in
``vision/faces.py``, for the reason ``ffmpeg_output.py`` and
``frame_motion.py`` are: the analyzer (which stores the outcome) and the
media server (which sums it) need it, and neither should import the optional
computer-vision package to get a few small functions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vision.faces import FaceRecognitionResult


@dataclass(frozen=True)
class FaceTally:
    """What face recognition counted in one clip -- numbers only, never who.

    Behind the Biometrics tab's Recognition results panel. A face is counted
    once per sampled frame it appears in, so these are detections rather
    than people. *unclear* is how many of the faces that matched nobody had
    an inconclusive look (see ``_could_be``) -- the ones
    ``ai_face_recognition_ignore_unclear_faces`` may set aside -- whether or
    not that option is on, and *would_clear* whether setting them aside
    would leave no face counting as a stranger, which is how the panel can
    say what turning the option on would change.
    """

    seen: int = 0
    matched: int = 0
    unclear: int = 0
    unexamined: int = 0
    would_clear: bool = False


class FaceOutcome(StrEnum):
    """Why one clip did or did not count as recognized.

    Stored with the analysis (``analysis_results.face_outcome``) and what the
    Recognition results panel groups clips by, so the values are a contract
    with rows already written: add to them, never rename one.
    """

    #: Recognition looked and found no face at all.
    NO_FACES = "no_faces"
    #: Everyone seen is an approved household member.
    RECOGNIZED = "recognized"
    #: Recognized, but only because unclear faces were set aside.
    SET_ASIDE = "set_aside"
    #: A clear look at a face that matched nobody: a visitor, or someone
    #: not enrolled yet.
    UNFAMILIAR = "unfamiliar"
    #: Someone enrolled but not approved is in the clip.
    NOT_APPROVED = "not_approved"
    #: A frame could not be examined.
    UNEXAMINED = "unexamined"
    #: Only unclear faces are in the way, and they could not be set aside
    #: (nobody recognized beside them) or the setting would not help.
    UNCLEAR = "unclear"
    #: Only unclear faces are in the way, and the Ignore Unclear Faces
    #: setting -- off when this was analyzed -- would set them aside.
    UNCLEAR_IGNORABLE = "unclear_ignorable"
    #: Every face is accounted for, but object detection saw more people at
    #: once than there were faces recognized.
    EXTRA_PEOPLE = "extra_people"


#: The reasons a clip with faces in it did not count as recognized, in the
#: order the panel lists them.
HELD_BACK_OUTCOMES = (
    FaceOutcome.UNFAMILIAR,
    FaceOutcome.UNCLEAR_IGNORABLE,
    FaceOutcome.UNCLEAR,
    FaceOutcome.NOT_APPROVED,
    FaceOutcome.EXTRA_PEOPLE,
    FaceOutcome.UNEXAMINED,
)


def classify_face_outcome(result: FaceRecognitionResult, *, recognized: bool) -> str:
    """The :class:`FaceOutcome` of a clip, or ``""`` if nothing was examined.

    *recognized* is whether the clip counted as recognized
    (``BaseAnalyzer._face_match_is_unambiguous``). When it did not, the
    reason is the first of these that applies, most actionable first -- and
    the last needs no test of its own: a clip that every face check passes
    and that still did not count can only have been held back by the people
    count from object detection.
    """
    tally = result.tally
    if tally is None:
        return ""
    if tally.seen == 0 and not tally.unexamined:
        return FaceOutcome.NO_FACES
    if recognized:
        return FaceOutcome.SET_ASIDE if result.unclear_faces else FaceOutcome.RECOGNIZED
    if tally.seen - tally.matched - tally.unclear:
        return FaceOutcome.UNFAMILIAR
    if result.other_names:
        return FaceOutcome.NOT_APPROVED
    if tally.unexamined:
        return FaceOutcome.UNEXAMINED
    if result.unrecognized_present:
        return (
            FaceOutcome.UNCLEAR_IGNORABLE if tally.would_clear else FaceOutcome.UNCLEAR
        )
    return FaceOutcome.EXTRA_PEOPLE


def summarize_face_outcomes(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The Recognition results panel's numbers from per-outcome totals.

    Each row is ``outcome``, ``clips`` and the faces ``faces_seen``,
    ``faces_matched`` and ``faces_unclear`` summed over those clips. A clip
    "with faces" is one recognized or held back, so the panel's own figures
    always add up.
    """
    held_back = {outcome.value: 0 for outcome in HELD_BACK_OUTCOMES}
    checked = recognized = set_aside = 0
    faces = {"seen": 0, "matched": 0, "unclear": 0}
    for row in rows:
        clips = int(row["clips"])
        checked += clips
        for key in faces:
            faces[key] += int(row[f"faces_{key}"])
        outcome = row["outcome"]
        if outcome in held_back:
            held_back[outcome] += clips
        elif outcome == FaceOutcome.RECOGNIZED:
            recognized += clips
        elif outcome == FaceOutcome.SET_ASIDE:
            recognized += clips
            set_aside += clips
    return {
        "clips_checked": checked,
        "clips_with_faces": recognized + sum(held_back.values()),
        "clips_recognized": recognized,
        "clips_set_aside": set_aside,
        "faces_seen": faces["seen"],
        "faces_matched": faces["matched"],
        "faces_unclear": faces["unclear"],
        "held_back": held_back,
    }
