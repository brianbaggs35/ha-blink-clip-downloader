"""Evidence quality — how good the imagery behind a conclusion actually was.

Deliberately separate from both the AI model's confidence and the
deterministic detector's confidence. A model can be 95% sure it saw someone
try a car door; the detector can be 95% sure the boxes overlapped — and the
underlying evidence can still be three dark frames of a 40-pixel-tall
figure at the end of a driveway. Collapsing those into one number is how a
security system ends up sounding certain about something it barely saw.

The score produced here is used three ways: it is shown to the user beside
the AI's own confidence, it damps the deterministic risk score (see
:mod:`.scoring`) so weak evidence can't manufacture a critical alert on its
own, and it is stated plainly in the prompt so the AI model knows how much
weight the structured evidence deserves.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tracks import ObjectTrack, subject_tracks

#: Share of the frame's height a subject must span for its visual detail to
#: be fully trustworthy, and the share below which it gives essentially
#: nothing. Between them the sub-score scales linearly. Calibrated against
#: the ordinary surveillance rule of thumb that a figure needs to span a
#: third or so of frame height before fine detail (what someone is holding,
#: where their hands are) is legible, while under a tenth is barely more
#: than a moving blob.
_SUBJECT_HEIGHT_STRONG = 0.35
_SUBJECT_HEIGHT_WEAK = 0.08

#: Floor and span of the cap a small subject imposes on the overall score.
#: Without it, a confident detector, full frame coverage and unbroken
#: tracking can average a 25-pixel-tall figure up to "moderate" evidence —
#: but none of those things make a subject that small any more legible, and
#: the score exists precisely to stop that kind of laundering.
_SMALL_SUBJECT_CAP_FLOOR = 0.35

#: Continuity penalty applied to a pseudo-track assembled without real
#: tracking ids (see ``ObjectTrack.tracked``) — such a "track" cannot tell
#: two people apart, so any duration or trajectory read off it is worth
#: materially less than the same numbers from a genuine track.
_UNTRACKED_PENALTY = 0.5

#: Optional evidence sources the pipeline can contribute beyond object
#: detection itself: depth estimation, contact segmentation, pose
#: estimation, and face recognition. Used to scale the stage-coverage
#: sub-score.
TOTAL_OPTIONAL_SOURCES = 4

_WEIGHTS: dict[str, float] = {
    "frame_coverage": 0.20,
    "subject_size": 0.30,
    "detector_confidence": 0.20,
    "tracking_continuity": 0.20,
    "stage_coverage": 0.10,
}


@dataclass
class EvidenceQuality:
    """How much the visual evidence for this clip can bear.

    ``factors`` holds each 0.0-1.0 sub-score by name so the UI can show why
    the overall number came out where it did, and ``notes`` holds the
    plain-English version of the same thing for the prompt and the event
    detail panel.
    """

    score: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """A one-word band for the score, for prompts and UI badges."""
        if self.score < 0.4:
            return "weak"
        if self.score < 0.7:
            return "moderate"
        return "strong"

    def to_dict(self) -> dict[str, object]:
        """Serialize to the plain dict shape the API returns."""
        return {
            "score": round(self.score, 4),
            "label": self.label,
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "notes": list(self.notes),
            "unavailable": list(self.unavailable),
        }


def _scale(value: float, weak: float, strong: float) -> float:
    """Map *value* onto 0.0-1.0 across the *weak*-to-*strong* range."""
    if strong <= weak:
        return 0.0
    return max(0.0, min(1.0, (value - weak) / (strong - weak)))


def _subject_factors(
    subjects: list[ObjectTrack], frame_interval: float
) -> tuple[dict[str, float], list[str]]:
    """Score the factors that depend on the subjects actually in the clip.

    Returns the sub-scores plus the plain-English notes explaining any weak
    ones. A clip with no subject in it contributes no factors at all rather
    than zeroes, so the remaining weights renormalize — averaging in a zero
    would score an empty clip as bad evidence rather than as evidence about
    nothing.
    """
    if not subjects:
        return {}, ["no person or animal was detected in the analyzed frames"]

    factors: dict[str, float] = {}
    notes: list[str] = []

    peak_height = max(t.peak_height_fraction for t in subjects)
    factors["subject_size"] = _scale(
        peak_height, _SUBJECT_HEIGHT_WEAK, _SUBJECT_HEIGHT_STRONG
    )
    if factors["subject_size"] < 0.5:
        notes.append(
            "the largest subject spanned only "
            f"{peak_height * 100:.0f}% of the frame height"
        )

    factors["detector_confidence"] = sum(t.mean_confidence for t in subjects) / len(
        subjects
    )

    continuities = [
        t.continuity(frame_interval) * (1.0 if t.tracked else _UNTRACKED_PENALTY)
        for t in subjects
    ]
    factors["tracking_continuity"] = sum(continuities) / len(continuities)
    if any(not t.tracked for t in subjects):
        notes.append(
            "object tracking was unavailable, so subjects could not be told apart"
        )
    elif factors["tracking_continuity"] < 0.7:
        notes.append("subjects were repeatedly lost and reacquired between frames")

    return factors, notes


def assess_evidence(
    frames_analyzed: int,
    target_frames: int,
    tracks: list[ObjectTrack],
    frame_interval: float,
    unavailable_sources: list[str] | None = None,
    not_applicable_sources: list[str] | None = None,
) -> EvidenceQuality:
    """Score how trustworthy this clip's visual evidence is.

    *target_frames* is how many frames the configuration asked for, so a
    clip that yielded fewer (a very short event, an ffmpeg hiccup) scores
    lower on coverage without needing an absolute frame-count rule that
    would misjudge every non-default configuration.

    *unavailable_sources* names the optional stages that could not run for
    this clip — disabled, missing their dependency, or failed. They are
    reported verbatim so the final result can state which evidence was
    missing rather than silently concluding without it.

    *not_applicable_sources* names stages that had nothing in this clip to
    measure, which is not the same thing and must not be scored as though
    it were: depth, contact and pose all answer "how close did this subject
    get to that vehicle", so on a clip with no person-and-vehicle pair —
    most clips — there is no missing evidence, only a question that never
    arose. Counting those against coverage docked almost every ordinary
    clip for a structural reason. They are excluded from the numerator and
    the denominator alike, so coverage means "of the stages that had
    something to contribute, how many did".
    """
    unavailable = list(unavailable_sources or [])
    not_applicable = list(not_applicable_sources or [])
    notes: list[str] = []
    factors: dict[str, float] = {}

    factors["frame_coverage"] = (
        min(1.0, frames_analyzed / target_frames) if target_frames > 0 else 0.0
    )
    if factors["frame_coverage"] < 0.75:
        notes.append(
            f"only {frames_analyzed} of {target_frames} intended frames were analyzed"
        )

    applicable = TOTAL_OPTIONAL_SOURCES - len(not_applicable)
    # When nothing could have contributed there is no coverage to judge, so
    # the factor is left out entirely and the remaining weights renormalize
    # — the same treatment subject_size already gets on a clip with no
    # subject in it. Scoring it as zero would punish a clip for the shape of
    # its own contents.
    if applicable > 0:
        factors["stage_coverage"] = max(0.0, 1.0 - len(unavailable) / applicable)

    subject_factors, subject_notes = _subject_factors(
        subject_tracks(tracks), frame_interval
    )
    factors.update(subject_factors)
    notes.extend(subject_notes)

    weights = {k: w for k, w in _WEIGHTS.items() if k in factors}
    total_weight = sum(weights.values())
    score = (
        sum(factors[k] * w for k, w in weights.items()) / total_weight
        if total_weight > 0
        else 0.0
    )
    if "subject_size" in factors:
        cap = (
            _SMALL_SUBJECT_CAP_FLOOR
            + (1.0 - _SMALL_SUBJECT_CAP_FLOOR) * factors["subject_size"]
        )
        score = min(score, cap)

    return EvidenceQuality(
        score=score, factors=factors, notes=notes, unavailable=unavailable
    )
